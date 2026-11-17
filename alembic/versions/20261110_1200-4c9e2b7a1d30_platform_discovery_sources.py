"""Persist platform discovery snapshots.
Revision ID: 4c9e2b7a1d30
Revises: a7c41f2d9b10
"""
from __future__ import annotations
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
revision: str = "4c9e2b7a1d30"
down_revision: str | None = "a7c41f2d9b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table("platform_source_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("platform_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("source_type", sa.String(32), server_default="website", nullable=False),
        sa.Column("title", sa.String(1000), nullable=True),
        sa.Column("content_text", sa.Text(), server_default="", nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform_id", "url", name="uq_platform_source_documents_platform_url"))
    op.create_index("ix_platform_source_documents_company_id", "platform_source_documents", ["company_id"])
    op.create_index("ix_platform_source_documents_platform_id", "platform_source_documents", ["platform_id"])
    op.create_index("ix_platform_source_documents_content_hash", "platform_source_documents", ["content_hash"])

def downgrade() -> None:
    op.drop_index("ix_platform_source_documents_content_hash", table_name="platform_source_documents")
    op.drop_index("ix_platform_source_documents_platform_id", table_name="platform_source_documents")
    op.drop_index("ix_platform_source_documents_company_id", table_name="platform_source_documents")
    op.drop_table("platform_source_documents")
