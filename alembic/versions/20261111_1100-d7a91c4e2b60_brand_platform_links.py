"""Link content brands to tenant-owned platforms.
Revision ID: d7a91c4e2b60
Revises: c6d4a8f21b90
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a91c4e2b60"
down_revision: str | None = "c6d4a8f21b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_content_brands_id_company", "content_brands", ["id", "company_id"]
    )
    op.create_table(
        "brand_platform_links",
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("platform_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id", "company_id"],
            ["content_brands.id", "content_brands.company_id"],
            ondelete="CASCADE",
            name="fk_brand_platform_links_brand_company",
        ),
        sa.ForeignKeyConstraint(
            ["platform_id", "company_id"],
            ["platforms.id", "platforms.company_id"],
            ondelete="CASCADE",
            name="fk_brand_platform_links_platform_company",
        ),
        sa.PrimaryKeyConstraint("brand_id", "platform_id"),
    )
    op.create_index("ix_brand_platform_links_company_id", "brand_platform_links", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_brand_platform_links_company_id", table_name="brand_platform_links")
    op.drop_table("brand_platform_links")
    op.drop_constraint("uq_content_brands_id_company", "content_brands", type_="unique")
