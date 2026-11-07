"""PlatformConnection model: one provider binding of a platform.

Tenant consistency is enforced at the database level: the composite foreign
key ``(platform_id, company_id) -> platforms(id, company_id)`` makes it
impossible to attach a connection to a platform owned by another company.
Lifecycle transitions are validated in ``app.services.connection_service``
and every transition writes an immutable ``ConnectionEvent``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.connection_event import ConnectionEvent
    from app.models.credential_reference import CredentialReference
    from app.models.platform import Platform


class ConnectionStatus(enum.StrEnum):
    DRAFT = "draft"
    CONFIGURED = "configured"
    VALIDATING = "validating"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class PlatformConnection(Base, TimestampMixin):
    __tablename__ = "platform_connections"
    __table_args__ = (
        UniqueConstraint("platform_id", "name", name="uq_platform_connections_platform_name"),
        CheckConstraint(
            "status IN ('draft', 'configured', 'validating', 'connected', 'degraded', 'disconnected', 'error')",
            name="status_valid",
        ),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_non_negative"),
        Index("ix_platform_connections_company_status", "company_id", "status"),
        # Composite FK: makes it impossible to attach a connection to a platform
        # owned by a different company, enforced by the database itself.
        ForeignKeyConstraint(
            ["platform_id", "company_id"],
            ["platforms.id", "platforms.company_id"],
            ondelete="CASCADE",
            name="fk_platform_connections_platform_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Referenced together with company_id via the composite tenant FK above.
    platform_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Registry key of the provider adapter (e.g. 'http_endpoint').
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ConnectionStatus.DRAFT.value, server_default=ConnectionStatus.DRAFT.value
    )
    # Non-secret integration settings (URLs, feature flags, timeout overrides).
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    credential_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credential_references.id", ondelete="SET NULL"), nullable=True
    )

    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Sanitized, classified failure information (never raw exceptions).
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    company: Mapped[Company] = relationship()
    platform: Mapped[Platform] = relationship()
    credential_reference: Mapped[CredentialReference | None] = relationship()
    events: Mapped[list[ConnectionEvent]] = relationship(
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ConnectionEvent.created_at",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<PlatformConnection id={self.id} name={self.name!r} "
            f"provider={self.provider!r} status={self.status!r}>"
        )
