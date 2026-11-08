"""ConnectionEvent model: immutable lifecycle/audit history for connections.

Every status transition, configuration change, validation result and credential
change appends one event here. Rows are never updated. ``correlation_id`` ties
each event to the originating API request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.platform_connection import PlatformConnection


class ConnectionEvent(Base):
    __tablename__ = "connection_events"
    __table_args__ = (
        Index("ix_connection_events_connection_created_at", "connection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Sanitized, human-readable outcome (never exception payloads or secrets).
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # clock_timestamp() (not now()): rows inserted within one transaction must
    # get strictly increasing timestamps so history ordering is stable.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    connection: Mapped[PlatformConnection] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ConnectionEvent id={self.id} connection={self.connection_id} "
            f"type={self.event_type!r} to={self.to_status!r}>"
        )
