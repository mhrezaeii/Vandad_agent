"""Tenant-scoped agent task requests and their execution attempts."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, SmallInteger, String, Text, UniqueConstraint, Uuid, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class AgentTaskStatus(enum.StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentTaskRunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentTask(Base, TimestampMixin):
    """A company-owned request assigned to one approved agent capability."""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'queued', 'awaiting_approval', 'running', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_valid"),
        CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="attempt_limits_valid"),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_at_after_started_at",
        ),
        UniqueConstraint("id", "company_id", name="uq_agent_tasks_id_company"),
        Index("ix_agent_tasks_company_status_created", "company_id", "status", "created_at"),
        Index(
            "uq_agent_tasks_company_idempotency",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    capability: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AgentTaskStatus.PENDING.value,
        server_default=AgentTaskStatus.PENDING.value,
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=50, server_default="50")
    input_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentTaskRun(Base, TimestampMixin):
    """One concrete attempt to execute an agent task."""

    __tablename__ = "agent_task_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_valid"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="completed_at_after_started_at",
        ),
        ForeignKeyConstraint(
            ["task_id", "company_id"],
            ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE",
            name="fk_agent_task_runs_task_company",
        ),
        UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_runs_task_attempt"),
        Index("ix_agent_task_runs_company_status", "company_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", server_default="running")
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
