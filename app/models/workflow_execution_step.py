"""WorkflowExecutionStep and WorkflowExecutionLog models.

Steps record per-node outcomes (including retry attempts); logs are the
redacted, append-only execution trail. Both cascade with their execution.
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

if TYPE_CHECKING:
    from app.models.workflow_execution import WorkflowExecution


class WorkflowStepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowExecutionStep(Base):
    __tablename__ = "workflow_execution_steps"
    __table_args__ = (
        UniqueConstraint("execution_id", "node_key", "attempt", name="uq_workflow_steps_execution_node_attempt"),
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'skipped')",
            name="status_valid",
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=WorkflowStepStatus.PENDING.value,
        server_default=WorkflowStepStatus.PENDING.value,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Sanitized node output (never contains credential material).
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    execution: Mapped[WorkflowExecution] = relationship(foreign_keys=[execution_id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowExecutionStep node={self.node_key!r} status={self.status!r}>"


class WorkflowLogLevel(enum.StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class WorkflowExecutionLog(Base):
    __tablename__ = "workflow_execution_logs"
    __table_args__ = (
        Index("ix_workflow_logs_execution_created", "execution_id", "created_at"),
        CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name="level_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_execution_steps.id", ondelete="SET NULL"), nullable=True
    )
    node_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    # Redacted human-readable line; never exception payloads or secrets.
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # clock_timestamp(): strictly increasing ordering within one transaction.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowExecutionLog execution={self.execution_id} level={self.level!r}>"
