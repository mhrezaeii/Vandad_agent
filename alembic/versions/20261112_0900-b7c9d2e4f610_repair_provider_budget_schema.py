"""Repair provider budget columns omitted by an already-applied migration.

Revision ID: b7c9d2e4f610
Revises: 4e5f6a7b8c9d

The original provider-budget revision was applied before task_id and
spend_ceiling_usd were added to its source file. This forward-only repair
reconciles databases created from either version of that historical file.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c9d2e4f610"
down_revision: str | None = "4e5f6a7b8c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    inspector = _inspector()

    # Agent task spend ceiling: add it only when the historical schema lacks it.
    agent_task_columns = {
        column["name"] for column in inspector.get_columns("agent_tasks")
    }
    if "spend_ceiling_usd" not in agent_task_columns:
        op.add_column(
            "agent_tasks",
            sa.Column("spend_ceiling_usd", sa.Numeric(14, 6), nullable=True),
        )

    agent_task_checks = inspector.get_check_constraints("agent_tasks")
    has_spend_check = any(
        constraint.get("name") == "spend_ceiling_positive"
        or "spend_ceiling_usd" in (constraint.get("sqltext") or "")
        for constraint in agent_task_checks
    )
    if not has_spend_check:
        op.create_check_constraint(
            "spend_ceiling_positive",
            "agent_tasks",
            "spend_ceiling_usd IS NULL OR spend_ceiling_usd > 0",
        )

    # Usage-ledger task association is nullable for non-task provider calls.
    usage_columns = {
        column["name"] for column in inspector.get_columns("provider_usage_records")
    }
    if "task_id" not in usage_columns:
        op.add_column(
            "provider_usage_records",
            sa.Column("task_id", sa.Uuid(), nullable=True),
        )

    usage_foreign_keys = inspector.get_foreign_keys("provider_usage_records")
    has_task_company_fk = any(
        fk.get("constrained_columns") == ["task_id", "company_id"]
        and fk.get("referred_table") == "agent_tasks"
        and fk.get("referred_columns") == ["id", "company_id"]
        for fk in usage_foreign_keys
    )
    if not has_task_company_fk:
        op.create_foreign_key(
            "fk_provider_usage_task_company",
            "provider_usage_records",
            "agent_tasks",
            ["task_id", "company_id"],
            ["id", "company_id"],
            ondelete="CASCADE",
        )

    usage_indexes = inspector.get_indexes("provider_usage_records")
    has_task_index = any(
        index.get("column_names") == ["task_id"] for index in usage_indexes
    )
    if not has_task_index:
        op.create_index(
            "ix_provider_usage_records_task_id",
            "provider_usage_records",
            ["task_id"],
        )


def downgrade() -> None:
    # Intentionally forward-only: the preceding historical revision exists in
    # two deployed shapes (with and without these fields). Dropping the fields
    # here would break databases whose preceding revision already had them.
    pass
