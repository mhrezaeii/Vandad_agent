"""WorkflowSchedule model: one-time, interval and cron triggers per workflow."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class WorkflowScheduleType(enum.StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class WorkflowScheduleMissedPolicy(enum.StrEnum):
    SKIP = "skip"
    RUN_ONCE = "run_once"


class WorkflowSchedule(Base, TimestampMixin):
    __tablename__ = "workflow_schedules"
    __table_args__ = (
        UniqueConstraint("workflow_id", "name", name="uq_workflow_schedules_workflow_name"),
        CheckConstraint("schedule_type IN ('once', 'interval', 'cron')", name="schedule_type_valid"),
        CheckConstraint("missed_policy IN ('skip', 'run_once')", name="missed_policy_valid"),
        Index("ix_workflow_schedules_due", "enabled", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # {"at": iso8601} | {"interval_seconds": int} | {"cron": "5 4 * * *"}
    schedule_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # IANA timezone applied to cron/once interpretation (company timezone).
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 'skip' jumps past missed runs; 'run_once' executes one catch-up run.
    missed_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=WorkflowScheduleMissedPolicy.SKIP.value,
        server_default=WorkflowScheduleMissedPolicy.SKIP.value,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowSchedule id={self.id} type={self.schedule_type!r} enabled={self.enabled}>"
