from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActorType
from app.repositories import AuditEventRepository
from app.security.redaction import redact_mapping


class AuditService:
    """Writes audit events into the current transaction.

    Audit rows share the caller's transaction, so a rolled-back business
    operation never leaves a dangling audit record (and vice versa).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._repo = AuditEventRepository(session)

    def record(
        self,
        *,
        event_type: str,
        action: str,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_id: str | None = None,
        company_id: uuid.UUID | None = None,
        platform_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._repo.add(
            event_type=event_type,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            company_id=company_id,
            platform_id=platform_id,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            event_metadata=redact_mapping(metadata),
        )
