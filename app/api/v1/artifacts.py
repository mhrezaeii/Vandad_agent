"""Artifact endpoints: upload, list, metadata, streaming download, delete.

Public responses expose metadata and byte streams only — storage keys are
opaque generated identifiers and filesystem paths never leave the service.
Every access is tenant-checked against the artifact's owning company.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import company_scope  # noqa: F401 - company_scope used via Annotated
from app.core.config import get_settings
from app.core.exceptions import UnprocessableEntityError
from app.schemas.workflow import WorkflowArtifactListResponse, WorkflowArtifactRead
from app.security.authorization import CompanyScope
from app.services.artifact_service import ArtifactService

router = APIRouter(prefix="/executions/{execution_id}/artifacts", tags=["artifacts"])
artifact_router = APIRouter(prefix="/artifacts", tags=["artifacts"])

_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def get_artifact_service(request: Request) -> AsyncIterator[ArtifactService]:
    """Artifact service bound to the request's unit of work."""
    settings = get_settings()
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield ArtifactService(
                session,
                request.app.state.artifact_storage,
                max_size_bytes=settings.artifact_max_size_bytes,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _validate_kind(kind: str) -> str:
    from app.models import ArtifactKind  # noqa: PLC0415

    if kind not in {item.value for item in ArtifactKind}:
        raise UnprocessableEntityError(
            f"kind must be one of {sorted(item.value for item in ArtifactKind)}",
            details=[{"field": "kind", "value": kind}],
        )
    return kind


@router.post("", response_model=WorkflowArtifactRead, status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
    file: UploadFile,
    kind: str = "other",
) -> WorkflowArtifactRead:
    """Stream an artifact upload for an execution owned by the acting company."""
    _validate_kind(kind)
    content_type = file.content_type or "application/octet-stream"
    filename = file.filename  # provenance only; storage keys are generated

    async def _chunks() -> AsyncIterator[bytes]:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk

    artifact = await service.store_artifact(
        execution_id,
        company_id=scope.company_id,
        kind=kind,
        content_type=content_type,
        chunks=_chunks(),
        filename=filename,
    )
    return WorkflowArtifactRead.model_validate(artifact)


@router.get("", response_model=WorkflowArtifactListResponse)
async def list_execution_artifacts(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> WorkflowArtifactListResponse:
    artifacts = await service.list_execution_artifacts(execution_id, company_id=scope.company_id)
    return WorkflowArtifactListResponse(
        items=[WorkflowArtifactRead.model_validate(artifact) for artifact in artifacts],
        total=len(artifacts),
    )


@artifact_router.get("/{artifact_id}", response_model=WorkflowArtifactRead)
async def get_artifact(
    artifact_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> WorkflowArtifactRead:
    artifact = await service.get_artifact(artifact_id, company_id=scope.company_id)
    return WorkflowArtifactRead.model_validate(artifact)


@artifact_router.get("/{artifact_id}/content")
async def download_artifact(
    artifact_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> StreamingResponse:
    """Stream artifact bytes (bounded chunks; never materialized in memory)."""
    artifact, stream = await service.open_artifact_stream(artifact_id, company_id=scope.company_id)
    headers = {"Content-Length": str(artifact.size_bytes)}
    return StreamingResponse(
        stream,
        media_type=artifact.content_type,
        headers=headers,
    )


@artifact_router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    artifact_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> Response:
    await service.delete_artifact(artifact_id, company_id=scope.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@artifact_router.head("/{artifact_id}/content")
async def head_artifact(
    artifact_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> Response:
    artifact = await service.get_artifact(artifact_id, company_id=scope.company_id)
    return Response(
        status_code=status.HTTP_200_OK,
        media_type=artifact.content_type,
        headers={"Content-Length": str(artifact.size_bytes)},
    )
