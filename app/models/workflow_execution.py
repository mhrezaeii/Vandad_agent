"""WorkflowExecution model: record of one run of an automated workflow.

Phase 1 only persists execution records; orchestration (n8n integration) is a
later phase and will write to this table.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class WorkflowType(enum.StrEnum):
    MARKETING_CONTENT = "marketing_content"
    VIDEO_PRODUCTION = "video_production"
    SOCIAL_PUBLISHING = "social_publishing"
    MONITORING = "monitoring"
    CUSTOM = "custom"


class WorkflowStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class WorkflowExecution(Base, TimestampMixin):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        CheckConstraint(
            "workflow_type IN ('marketing_content', 'video_production', "
            "'social_publishing', 'monitoring', 'custom')",
            name="workflow_type_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out')",
            name="status_valid",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_at_after_started_at",
        ),
        Index("ix_workflow_executions_type_status", "workflow_type", "status"),
        Index("ix_workflow_executions_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowType.CUSTOM.value, server_default=WorkflowType.CUSTOM.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowStatus.PENDING.value, server_default=WorkflowStatus.PENDING.value
    )
    # External execution identifier (e.g. an n8n execution id).
    execution_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    input_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowExecution id={self.id} name={self.workflow_name!r} status={self.status!r}>"
