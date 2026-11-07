"""AuditEvent model: who did what to which resource, when."""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class ActorType(enum.StrEnum):
    SYSTEM = "system"
    AGENT = "agent"
    USER = "user"
    SERVICE = "service"


class AuditEvent(Base, TimestampMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('system', 'agent', 'user', 'service')", name="actor_type_valid"
        ),
        Index("ix_audit_events_company_created_at", "company_id", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Nullable: system-level events may not belong to a specific company.
    # FKs use SET NULL so deleting a company/platform never destroys audit history.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("platforms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ActorType.SYSTEM.value, server_default=ActorType.SYSTEM.value
    )
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Free-form structured context; use app.security.redaction.redact_mapping
    # before storing anything that could contain sensitive values.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditEvent id={self.id} event_type={self.event_type!r} action={self.action!r}>"
