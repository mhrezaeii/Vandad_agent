"""Add durable agent task dispatch outbox.

Revision ID: f7a1b2c3d4e5
Revises: e1f2a3b4c5d6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a1b2c3d4e5"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_task_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'delivered')",
            name=op.f("ck_agent_task_outbox_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_number >= 1 AND delivery_attempts >= 0",
            name=op.f("ck_agent_task_outbox_attempts_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "company_id"], ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE", name="fk_agent_task_outbox_task_company",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_task_outbox")),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_outbox_task_attempt"),
    )
    op.create_index(
        "ix_agent_task_outbox_dispatch", "agent_task_outbox",
        ["status", "available_at", "created_at"],
    )
    op.create_index("ix_agent_task_outbox_company", "agent_task_outbox", ["company_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_task_outbox_company", table_name="agent_task_outbox")
    op.drop_index("ix_agent_task_outbox_dispatch", table_name="agent_task_outbox")
    op.drop_table("agent_task_outbox")
