"""Add tenant-scoped company agent registry.
Revision ID: b6a3e1d9f204
Revises: d7a91c4e2b60
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6a3e1d9f204"
down_revision: str | None = "d7a91c4e2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="disabled", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_company_agents_status_valid")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_agents_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_agents")),
        sa.UniqueConstraint("company_id", "agent_key", name="uq_company_agents_company_key"),
    )
    op.create_index("ix_company_agents_company_id", "company_agents", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_company_agents_company_id", table_name="company_agents")
    op.drop_table("company_agents")
