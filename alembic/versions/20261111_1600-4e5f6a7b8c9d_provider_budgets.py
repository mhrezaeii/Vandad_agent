"""Add company provider budgets and usage ledger.

Revision ID: 4e5f6a7b8c9d
Revises: 9a8b7c6d5e4f
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4e5f6a7b8c9d"
down_revision: str | None = "9a8b7c6d5e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_provider_budgets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.String(length=80), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="USD", nullable=False),
        sa.Column("monthly_limit_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("per_operation_limit_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_provider_budgets")),
        sa.UniqueConstraint("company_id", "provider_id", name="uq_company_provider_budgets_provider"),
        sa.CheckConstraint("monthly_limit_usd > 0 AND per_operation_limit_usd > 0", name="ck_company_provider_budget_limits_positive"),
    )
    op.create_index("ix_company_provider_budgets_company_enabled", "company_provider_budgets", ["company_id", "enabled"])
    op.create_index("ix_company_provider_budgets_company_id", "company_provider_budgets", ["company_id"])

    op.add_column("agent_tasks", sa.Column("spend_ceiling_usd", sa.Numeric(14, 6), nullable=True))
    op.create_check_constraint("spend_ceiling_positive", "agent_tasks", "spend_ceiling_usd IS NULL OR spend_ceiling_usd > 0")
    op.create_table(
        "provider_usage_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("provider_id", sa.String(length=80), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="reserved", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("reserved_amount_usd", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("usage_payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "company_id"], ["agent_tasks.id", "agent_tasks.company_id"], ondelete="CASCADE", name="fk_provider_usage_task_company"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_usage_records")),
        sa.UniqueConstraint("company_id", "idempotency_key", name="uq_provider_usage_company_idempotency"),
        sa.CheckConstraint("status IN ('reserved', 'settled', 'unknown', 'failed')", name="ck_provider_usage_status_valid"),
        sa.CheckConstraint("reserved_amount_usd >= 0 AND (actual_cost_usd IS NULL OR actual_cost_usd >= 0)", name="ck_provider_usage_amounts_non_negative"),
    )
    op.create_index("ix_provider_usage_company_provider_created", "provider_usage_records", ["company_id", "provider_id", "created_at"])
    op.create_index("ix_provider_usage_company_status", "provider_usage_records", ["company_id", "status"])
    op.create_index("ix_provider_usage_records_company_id", "provider_usage_records", ["company_id"])
    op.create_index("ix_provider_usage_records_task_id", "provider_usage_records", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_provider_usage_records_task_id", table_name="provider_usage_records")
    op.drop_index("ix_provider_usage_records_company_id", table_name="provider_usage_records")
    op.drop_index("ix_provider_usage_company_status", table_name="provider_usage_records")
    op.drop_index("ix_provider_usage_company_provider_created", table_name="provider_usage_records")
    op.drop_table("provider_usage_records")
    op.drop_constraint("spend_ceiling_positive", "agent_tasks", type_="check")
    op.drop_column("agent_tasks", "spend_ceiling_usd")
    op.drop_index("ix_company_provider_budgets_company_id", table_name="company_provider_budgets")
    op.drop_index("ix_company_provider_budgets_company_enabled", table_name="company_provider_budgets")
    op.drop_table("company_provider_budgets")
