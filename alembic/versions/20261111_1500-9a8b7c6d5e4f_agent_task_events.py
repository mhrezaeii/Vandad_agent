"""Add durable agent task progress events.

Revision ID: 9a8b7c6d5e4f
Revises: f7a1b2c3d4e5
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a8b7c6d5e4f"
down_revision: str | None = "f7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_task_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "company_id"],
            ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE",
            name="fk_agent_task_events_task_company",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_task_events")),
        sa.UniqueConstraint("task_id", "sequence", name="uq_agent_task_events_task_sequence"),
    )
    op.create_index(
        "ix_agent_task_events_company_task_sequence",
        "agent_task_events",
        ["company_id", "task_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_task_events_company_task_sequence", table_name="agent_task_events")
    op.drop_table("agent_task_events")
