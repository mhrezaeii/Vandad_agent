"""Persist AI-extracted platform knowledge snapshots.
Revision ID: c6d4a8f21b90
Revises: 4c9e2b7a1d30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6d4a8f21b90"
down_revision: str | None = "4c9e2b7a1d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_knowledge_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("platform_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("knowledge_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_platform_knowledge_snapshots_company_id", "platform_knowledge_snapshots", ["company_id"])
    op.create_index("ix_platform_knowledge_snapshots_platform_id", "platform_knowledge_snapshots", ["platform_id"])
    op.create_index("ix_platform_knowledge_snapshots_source_fingerprint", "platform_knowledge_snapshots", ["source_fingerprint"])
    op.create_index("ix_platform_knowledge_snapshots_company_platform", "platform_knowledge_snapshots", ["company_id", "platform_id"])
    op.create_index("ix_platform_knowledge_snapshots_platform_created", "platform_knowledge_snapshots", ["platform_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_platform_knowledge_snapshots_platform_created", table_name="platform_knowledge_snapshots")
    op.drop_index("ix_platform_knowledge_snapshots_company_platform", table_name="platform_knowledge_snapshots")
    op.drop_index("ix_platform_knowledge_snapshots_source_fingerprint", table_name="platform_knowledge_snapshots")
    op.drop_index("ix_platform_knowledge_snapshots_platform_id", table_name="platform_knowledge_snapshots")
    op.drop_index("ix_platform_knowledge_snapshots_company_id", table_name="platform_knowledge_snapshots")
    op.drop_table("platform_knowledge_snapshots")
