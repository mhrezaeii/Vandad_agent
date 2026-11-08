"""WorkflowExecution model: record of one run of an automated workflow.

Introduced in Phase 1 (name/type/status/metadata); Phase 3 extends it with
tenant, version, trigger, idempotency, scheduling and recovery columns. The
status check constraint retains 'succeeded' for Phase 1 rows while new
executions use the Phase 3 state set.
"""

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
    Integer,
    String,
    Text,
    Uuid,
    text,
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
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"  # paused on a delay node; resumable
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    # Legacy Phase 1 value; new executions never use it.
    SUCCEEDED = "succeeded"


class WorkflowTriggerType(enum.StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    API = "api"
    WEBHOOK = "webhook"


class WorkflowExecution(Base, TimestampMixin):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        CheckConstraint(
            "workflow_type IN ('marketing_content', 'video_production', "
            "'social_publishing', 'monitoring', 'custom')",
            name="workflow_type_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'waiting', 'completed', "
            "'failed', 'cancelled', 'timed_out', 'succeeded')",
            name="status_valid",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_at_after_started_at",
        ),
        CheckConstraint(
            "trigger_type IN ('manual', 'schedule', 'api', 'webhook')",
            name="trigger_type_valid",
        ),
        Index("ix_workflow_executions_type_status", "workflow_type", "status"),
        Index("ix_workflow_executions_created_at", "created_at"),
        Index("ix_workflow_executions_company_status", "company_id", "status"),
        # Idempotency: one execution per (company, key) when a key is provided.
        Index(
            "uq_workflow_executions_company_idempotency",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Recovery poller: executions believed running with a stale heartbeat.
        Index("ix_workflow_executions_heartbeat", "status", "heartbeat_at"),
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

    # --- Phase 3: workflow engine ------------------------------------------------
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # SET NULL: workflow/version deletion preserves execution history.
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Tenant owner; NULL for legacy Phase 1 rows.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Client-supplied idempotency key (unique per company when present).
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Context snapshot persisted when the execution pauses on a delay node.
    context_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Recovery bookkeeping: queue attempt count and worker liveness.
    queue_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowExecution id={self.id} name={self.workflow_name!r} status={self.status!r}>"
