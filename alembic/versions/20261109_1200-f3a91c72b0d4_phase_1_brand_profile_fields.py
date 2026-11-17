"""phase 1: brand profile fields (display_name, description, cta_config)

Revision ID: f3a91c72b0d4
Revises: c9bf942e9178
Create Date: 2026-11-09 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f3a91c72b0d4'
down_revision: str | None = 'c9bf942e9178'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'content_brands',
        sa.Column('display_name', sa.String(length=150), nullable=True),
    )
    op.add_column(
        'content_brands',
        sa.Column('description', sa.Text(), nullable=True),
    )
    op.add_column(
        'content_brands',
        sa.Column(
            'cta_config',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # Backfill the operator-facing label for rows created before this migration.
    op.execute("UPDATE content_brands SET display_name = name WHERE display_name IS NULL")


def downgrade() -> None:
    op.drop_column('content_brands', 'cta_config')
    op.drop_column('content_brands', 'description')
    op.drop_column('content_brands', 'display_name')
