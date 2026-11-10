"""Data access for platform connections and their event history."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConnectionEvent, PlatformConnection
from app.security.redaction import redact_text


class ConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, connection: PlatformConnection) -> PlatformConnection:
        self.session.add(connection)
        await self.session.flush()
        return connection

    async def get(self, connection_id: uuid.UUID) -> PlatformConnection | None:
        return await self.session.get(PlatformConnection, connection_id)

    async def exists_name(self, platform_id: uuid.UUID, name: str) -> bool:
        result = await self.session.execute(
            select(PlatformConnection.id).where(
                PlatformConnection.platform_id == platform_id, PlatformConnection.name == name
            )
        )
        return result.scalar_one_or_none() is not None

    async def list(
        self,
        *,
        company_id: uuid.UUID,
        platform_id: uuid.UUID | None = None,
        status: str | None = None,
        provider: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[PlatformConnection], int]:
        conditions = [PlatformConnection.company_id == company_id]
        if platform_id is not None:
            conditions.append(PlatformConnection.platform_id == platform_id)
        if status is not None:
            conditions.append(PlatformConnection.status == status)
        if provider is not None:
            conditions.append(PlatformConnection.provider == provider)

        total_query = select(func.count()).select_from(PlatformConnection).where(*conditions)
        total = int((await self.session.execute(total_query)).scalar_one())

        items_query = (
            select(PlatformConnection)
            .where(*conditions)
            .order_by(PlatformConnection.created_at.desc(), PlatformConnection.id)
            .limit(limit)
            .offset(offset)
        )
        items = list((await self.session.execute(items_query)).scalars().all())
        return items, total

    async def delete(self, connection: PlatformConnection) -> None:
        await self.session.delete(connection)
        await self.session.flush()

    # --- events -------------------------------------------------------------
    def add_event(
        self,
        *,
        connection_id: uuid.UUID,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        success: bool | None = None,
        message: str | None = None,
        event_metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> ConnectionEvent:
        event = ConnectionEvent(
            connection_id=connection_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            success=success,
            message=redact_text(message)[:500] if message else None,
            event_metadata=event_metadata or {},
            correlation_id=correlation_id,
        )
        self.session.add(event)
        return event

    async def list_events(
        self, connection_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> list[ConnectionEvent]:
        result = await self.session.execute(
            select(ConnectionEvent)
            .where(ConnectionEvent.connection_id == connection_id)
            .order_by(ConnectionEvent.created_at.desc(), ConnectionEvent.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
