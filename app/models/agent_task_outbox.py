"""Transactional outbox rows for reliable agent-task dispatch."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class AgentTaskOutbox(Base, TimestampMixin):
    __tablename__ = "agent_task_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'delivered')", name="status_valid"),
        CheckConstraint("attempt_number >= 1 AND delivery_attempts >= 0", name="attempts_valid"),
        ForeignKeyConstraint(
            ["task_id", "company_id"],
            ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE",
            name="fk_agent_task_outbox_task_company",
        ),
        UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_outbox_task_attempt"),
        Index("ix_agent_task_outbox_dispatch", "status", "available_at", "created_at"),
        Index("ix_agent_task_outbox_company", "company_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
