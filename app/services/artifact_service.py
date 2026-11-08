"""ArtifactService: the tenant boundary for artifact storage.

Responsibilities:
- generate storage keys (never trust client filenames)
- stream uploads into the storage backend with size limits and checksums
- enforce company ownership on every read/write/delete
- audit security-relevant operations
- never expose filesystem paths: callers receive metadata and byte streams
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models import ArtifactKind, WorkflowArtifact, WorkflowExecution
from app.repositories import ArtifactRepository
from app.security.redaction import redact_text
from app.services.audit_service import AuditService
from app.storage.base import ArtifactStorage, StoredArtifact

_ARTIFACT_RESOURCE = "artifact"

# Defensible extension allowlist; the storage key uses it only as a suffix.
_SAFE_EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9]{1,10}$")


@dataclass(frozen=True)
class ArtifactMeta:
    id: uuid.UUID
    company_id: uuid.UUID
    execution_id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    original_filename: str | None
    created_at: datetime


class ArtifactService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ArtifactStorage,
        max_size_bytes: int = 2 * 1024**3,
    ) -> None:
        self.session = session
        self.storage = storage
        self.artifacts = ArtifactRepository(session)
        self.audit = AuditService(session)
        self._max_size_bytes = max_size_bytes

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def generate_storage_key(execution_id: uuid.UUID, kind: str, extension: str | None) -> str:
        """Build a server-generated storage key; client filenames are never used."""
        suffix = ""
        if extension:
            extension = extension.lstrip(".").lower()
            if not _SAFE_EXTENSION_PATTERN.fullmatch(extension):
                extension = "bin"
            suffix = f".{extension}"
        return f"executions/{execution_id}/{kind}/{uuid.uuid4().hex}{suffix}"

    async def _require_execution(
        self, execution_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> WorkflowExecution:
        execution = await self.session.get(WorkflowExecution, execution_id)
        if execution is None or execution.company_id != company_id:
            # Existence of foreign executions is never revealed.
            raise NotFoundError(
                "Execution not found.", details=[{"field": "execution_id", "value": str(execution_id)}]
            )
        return execution

    # ------------------------------------------------------------------- write
    async def store_artifact(
        self,
        execution_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        kind: str,
        content_type: str,
        chunks: AsyncIterator[bytes],
        filename: str | None = None,
    ) -> WorkflowArtifact:
        """Stream an upload into storage and persist its metadata atomically."""
        await self._require_execution(execution_id, company_id=company_id)
        artifact_kind = ArtifactKind(kind)
        extension = filename.rsplit(".", 1)[-1] if filename and "." in filename else None
        storage_key = self.generate_storage_key(execution_id, artifact_kind.value, extension)

        stored: StoredArtifact = await self.storage.save(storage_key, chunks)
        artifact = WorkflowArtifact(
            company_id=company_id,
            execution_id=execution_id,
            kind=artifact_kind.value,
            storage_key=stored.key,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            # Kept for provenance only; never used to address storage.
            original_filename=(filename or None)[:255] if filename else None,
        )
        artifact = await self.artifacts.add(artifact)
        self.audit.record(
            event_type="artifact.stored",
            action="create",
            company_id=company_id,
            resource_type=_ARTIFACT_RESOURCE,
            resource_id=artifact.id,
            metadata={
                "kind": artifact.kind,
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
                "content_type": content_type,
            },
        )
        await self.session.flush()
        return artifact

    # -------------------------------------------------------------------- read
    async def get_artifact(
        self, artifact_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> WorkflowArtifact:
        artifact = await self.artifacts.get_for_company(artifact_id, company_id=company_id)
        if artifact is None:
            raise NotFoundError(
                "Artifact not found.", details=[{"field": "artifact_id", "value": str(artifact_id)}]
            )
        return artifact

    async def list_execution_artifacts(
        self, execution_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> list[WorkflowArtifact]:
        await self._require_execution(execution_id, company_id=company_id)
        return await self.artifacts.list_for_execution(execution_id, company_id=company_id)

    async def open_artifact_stream(
        self, artifact_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> tuple[WorkflowArtifact, AsyncIterator[bytes]]:
        """Stream artifact bytes after tenant validation (no paths exposed)."""
        artifact = await self.get_artifact(artifact_id, company_id=company_id)
        stream = self.storage.open(artifact.storage_key)
        return artifact, stream

    # ------------------------------------------------------------------ delete
    async def delete_artifact(self, artifact_id: uuid.UUID, *, company_id: uuid.UUID) -> None:
        artifact = await self.get_artifact(artifact_id, company_id=company_id)
        storage_key = artifact.storage_key
        snapshot = {"kind": artifact.kind, "size_bytes": artifact.size_bytes}
        await self.artifacts.delete(artifact)
        await self.storage.delete(storage_key)
        self.audit.record(
            event_type="artifact.deleted",
            action="delete",
            company_id=company_id,
            resource_type=_ARTIFACT_RESOURCE,
            resource_id=artifact_id,
            metadata=snapshot,
        )
        await self.session.flush()

    # -------------------------------------------------------------- validation
    @staticmethod
    def redact_for_log(message: str) -> str:
        return redact_text(message)[:1000]

    @property
    def max_size_bytes(self) -> int:
        return self._max_size_bytes

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(UTC)
