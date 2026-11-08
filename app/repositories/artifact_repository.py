"""Data access for workflow artifacts (tenant isolation lives here and in the
service boundary; storage paths never leave this layer)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkflowArtifact


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, artifact: WorkflowArtifact) -> WorkflowArtifact:
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def get(self, artifact_id: uuid.UUID) -> WorkflowArtifact | None:
        return await self.session.get(WorkflowArtifact, artifact_id)

    async def get_for_company(
        self, artifact_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> WorkflowArtifact | None:
        """Tenant-safe fetch: artifacts of other companies read as missing."""
        artifact = await self.session.get(WorkflowArtifact, artifact_id)
        if artifact is None or artifact.company_id != company_id:
            return None
        return artifact

    async def list_for_execution(
        self, execution_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> list[WorkflowArtifact]:
        result = await self.session.execute(
            select(WorkflowArtifact)
            .where(
                WorkflowArtifact.execution_id == execution_id,
                WorkflowArtifact.company_id == company_id,
            )
            .order_by(WorkflowArtifact.created_at, WorkflowArtifact.id)
        )
        return list(result.scalars().all())

    async def delete(self, artifact: WorkflowArtifact) -> None:
        await self.session.delete(artifact)
        await self.session.flush()
