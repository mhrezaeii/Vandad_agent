"""phase 5: scene-video pipeline (scenario, scene jobs, provider quota)

Revision ID: a7c41f2d9b10
Revises: f3a91c72b0d4
Create Date: 2026-11-10 09:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a7c41f2d9b10'
down_revision: str | None = 'f3a91c72b0d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = (
    "'draft', 'script_generating', 'script_ready', 'script_failed', "
    "'approved', 'video_generating', 'completed', 'failed', 'cancelled'"
)
_NEW_STATUSES = (
    "'draft', 'script_generating', 'script_ready', 'script_failed', "
    "'approved', 'scenes_prepared', 'video_generation_pending', "
    "'video_generating', 'video_generated', 'assembling', 'completed', "
    "'failed', 'cancelled'"
)


def upgrade() -> None:
    # --- content_projects: scene pipeline columns ---------------------------
    op.add_column(
        'content_projects',
        sa.Column('video_mode', sa.String(length=32), server_default='scene', nullable=False),
    )
    op.add_column(
        'content_projects',
        sa.Column('scenario', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'content_projects',
        sa.Column('scene_options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'content_projects',
        sa.Column('scene_video_provider', sa.String(length=50), nullable=True),
    )
    op.add_column(
        'content_projects',
        sa.Column('assembly_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.drop_constraint(op.f('ck_content_projects_status_valid'), 'content_projects', type_='check')
    op.create_check_constraint(
        'status_valid',
        'content_projects',
        f"status IN ({_NEW_STATUSES})",
    )

    # --- scene_video_jobs ----------------------------------------------------
    op.create_table(
        'scene_video_jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('scene_index', sa.Integer(), nullable=False),
        sa.Column('scene_key', sa.String(length=100), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='pending', nullable=False),
        sa.Column('provider_job_id', sa.String(length=255), nullable=True),
        sa.Column('provider_status', sa.String(length=32), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('narration', sa.Text(), nullable=True),
        sa.Column('on_screen_text', sa.Text(), nullable=True),
        sa.Column('subtitle_text', sa.Text(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
        sa.Column('progress', sa.Float(), nullable=True),
        sa.Column('video_url', sa.String(length=2048), nullable=True),
        sa.Column('artifact_id', sa.Uuid(), nullable=True),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('submission_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cost_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_poll_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'generating', 'completed', "
            "'downloaded', 'failed', 'cancelled')",
            name=op.f('ck_scene_video_jobs_status_valid'),
        ),
        sa.CheckConstraint('scene_index >= 0', name=op.f('ck_scene_video_jobs_scene_index_non_negative')),
        sa.CheckConstraint('attempts >= 0', name=op.f('ck_scene_video_jobs_attempts_non_negative')),
        sa.ForeignKeyConstraint(
            ['company_id'],
            ['companies.id'],
            name=op.f('fk_scene_video_jobs_company_id_companies'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['project_id'],
            ['content_projects.id'],
            name=op.f('fk_scene_video_jobs_project_id_content_projects'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_scene_video_jobs')),
        sa.UniqueConstraint(
            'project_id', 'scene_index', name='uq_scene_video_jobs_project_scene'
        ),
        sa.UniqueConstraint(
            'provider', 'provider_job_id', name='uq_scene_video_jobs_provider_job'
        ),
    )
    op.create_index(op.f('ix_scene_video_jobs_company_id'), 'scene_video_jobs', ['company_id'], unique=False)
    op.create_index(op.f('ix_scene_video_jobs_project_id'), 'scene_video_jobs', ['project_id'], unique=False)
    op.create_index(op.f('ix_scene_video_jobs_provider_job_id'), 'scene_video_jobs', ['provider_job_id'], unique=False)
    op.create_index(op.f('ix_scene_video_jobs_artifact_id'), 'scene_video_jobs', ['artifact_id'], unique=False)

    # --- provider_quota_usage ------------------------------------------------
    op.create_table(
        'provider_quota_usage',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('period_date', sa.Date(), nullable=False),
        sa.Column('units_used', sa.Integer(), server_default='0', nullable=False),
        sa.Column('units_limit', sa.Integer(), nullable=True),
        sa.Column('usage_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('units_used >= 0', name=op.f('ck_provider_quota_usage_units_used_non_negative')),
        sa.ForeignKeyConstraint(
            ['company_id'],
            ['companies.id'],
            name=op.f('fk_provider_quota_usage_company_id_companies'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_quota_usage')),
        sa.UniqueConstraint(
            'company_id', 'provider', 'period_date', name='uq_provider_quota_usage_period'
        ),
    )
    op.create_index(op.f('ix_provider_quota_usage_company_id'), 'provider_quota_usage', ['company_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_provider_quota_usage_company_id'), table_name='provider_quota_usage')
    op.drop_table('provider_quota_usage')

    op.drop_index(op.f('ix_scene_video_jobs_artifact_id'), table_name='scene_video_jobs')
    op.drop_index(op.f('ix_scene_video_jobs_provider_job_id'), table_name='scene_video_jobs')
    op.drop_index(op.f('ix_scene_video_jobs_project_id'), table_name='scene_video_jobs')
    op.drop_index(op.f('ix_scene_video_jobs_company_id'), table_name='scene_video_jobs')
    op.drop_table('scene_video_jobs')

    op.drop_constraint(op.f('ck_content_projects_status_valid'), 'content_projects', type_='check')
    op.create_check_constraint(
        'status_valid',
        'content_projects',
        f"status IN ({_OLD_STATUSES})",
    )
    op.drop_column('content_projects', 'assembly_metadata')
    op.drop_column('content_projects', 'scene_video_provider')
    op.drop_column('content_projects', 'scene_options')
    op.drop_column('content_projects', 'scenario')
    op.drop_column('content_projects', 'video_mode')
