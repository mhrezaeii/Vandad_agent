"""Add tenant-scoped agent tasks and execution attempts.

Revision ID: e1f2a3b4c5d6
Revises: b6a3e1d9f204
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "b6a3e1d9f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(length=80), nullable=False),
        sa.Column("capability", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default="50", nullable=False),
        sa.Column("input_payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'awaiting_approval', 'running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_agent_tasks_status_valid"),
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name=op.f("ck_agent_tasks_priority_valid")),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name=op.f("ck_agent_tasks_attempt_limits_valid"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_agent_tasks_completed_at_after_started_at"),
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE",
                                name=op.f("fk_agent_tasks_company_id_companies")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tasks")),
        sa.UniqueConstraint("id", "company_id", name="uq_agent_tasks_id_company"),
    )
    op.create_index("ix_agent_tasks_company_id", "agent_tasks", ["company_id"])
    op.create_index(
        "ix_agent_tasks_company_status_created", "agent_tasks",
        ["company_id", "status", "created_at"],
    )
    op.create_index(
        "uq_agent_tasks_company_idempotency", "agent_tasks",
        ["company_id", "idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "agent_task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="running", nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_agent_task_runs_status_valid"),
        ),
        sa.CheckConstraint("attempt_number >= 1", name=op.f("ck_agent_task_runs_attempt_number_valid")),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_agent_task_runs_completed_at_after_started_at"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "company_id"], ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE", name="fk_agent_task_runs_task_company",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_task_runs")),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_runs_task_attempt"),
    )
    op.create_index("ix_agent_task_runs_company_id", "agent_task_runs", ["company_id"])
    op.create_index("ix_agent_task_runs_company_status", "agent_task_runs", ["company_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_task_runs_company_status", table_name="agent_task_runs")
    op.drop_index("ix_agent_task_runs_company_id", table_name="agent_task_runs")
    op.drop_table("agent_task_runs")
    op.drop_index("uq_agent_tasks_company_idempotency", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_company_status_created", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_company_id", table_name="agent_tasks")
    op.drop_table("agent_tasks")
