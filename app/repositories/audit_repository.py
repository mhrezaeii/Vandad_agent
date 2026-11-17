from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActorType, AuditEvent


class AuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(
        self,
        *,
        event_type: str,
        action: str,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_id: str | None = None,
        company_id: uuid.UUID | None = None,
        platform_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        event_metadata: dict | None = None,
    ) -> AuditEvent:
        """Record an audit event inside the current transaction."""
        event = AuditEvent(
            event_type=event_type,
            action=action,
            actor_type=actor_type.value,
            actor_id=actor_id,
            company_id=company_id,
            platform_id=platform_id,
            resource_type=resource_type,
            resource_id=resource_id,
            event_metadata=event_metadata or {},
        )
        self.session.add(event)
        return event
