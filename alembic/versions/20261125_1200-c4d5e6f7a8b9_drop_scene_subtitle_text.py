"""Drop the unused subtitle_text column from scene_video_jobs.

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("scene_video_jobs", "subtitle_text")


def downgrade() -> None:
    op.add_column("scene_video_jobs", sa.Column("subtitle_text", sa.Text(), nullable=True))
