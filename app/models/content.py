"""Content production domain: brands, persistent characters, video projects."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin


class ContentBrandStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ContentBrand(Base, TimestampMixin):
    """A brand the platform produces content for (tenant-owned)."""

    __tablename__ = "content_brands"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_content_brands_company_name"),
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Operator-facing name (may contain spaces/diacritics; ``name`` stays the
    # stable unique key used for idempotent seeding and duplicate checks).
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Call-to-action template: text/label/url/hashtags (see BrandCtaConfig).
    cta_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="fa-IR")
    # Vertical 9:16 default for social-first videos.
    video_width: Mapped[int] = mapped_column(Integer, nullable=False, default=1080)
    video_height: Mapped[int] = mapped_column(Integer, nullable=False, default=1920)
    tone: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Brand visual template: colors, logo artifact id, intro/outro preferences.
    visual_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ContentBrandStatus.ACTIVE.value,
        server_default=ContentBrandStatus.ACTIVE.value,
    )

    characters: Mapped[list[BrandCharacter]] = relationship(
        back_populates="brand", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ContentBrand id={self.id} name={self.name!r}>"


class CharacterStatus(enum.StrEnum):
    NOT_CREATED = "not_created"
    CREATING = "creating"
    TRAINING = "training"
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"


class BrandCharacter(Base, TimestampMixin):
    """One persistent provider character per brand (never recreated per video)."""

    __tablename__ = "brand_characters"
    __table_args__ = (
        # One persistent character per (brand, provider).
        UniqueConstraint("brand_id", "provider", name="uq_brand_characters_brand_provider"),
        CheckConstraint(
            "status IN ('not_created', 'creating', 'training', 'active', 'inactive', 'failed')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Provider identifiers: NULL until a successful provider response exists.
    provider_avatar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="fa-IR")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CharacterStatus.NOT_CREATED.value,
        server_default=CharacterStatus.NOT_CREATED.value,
    )
    # Explicit operator-supplied asset for photo-avatar creation (public URL the
    # provider can fetch). Never a local filesystem path.
    source_image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Creation/training metadata + provider-reported costs when available.
    creation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    brand: Mapped[ContentBrand] = relationship(back_populates="characters")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BrandCharacter id={self.id} provider={self.provider!r} status={self.status!r}>"


class ProjectStatus(enum.StrEnum):
    DRAFT = "draft"
    SCRIPT_GENERATING = "script_generating"
    SCRIPT_READY = "script_ready"
    SCRIPT_FAILED = "script_failed"
    APPROVED = "approved"
    SCENES_PREPARED = "scenes_prepared"
    VIDEO_GENERATION_PENDING = "video_generation_pending"
    VIDEO_GENERATING = "video_generating"
    VIDEO_GENERATED = "video_generated"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentProject(Base, TimestampMixin):
    """One video production request for a brand."""

    __tablename__ = "content_projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'script_generating', 'script_ready', 'script_failed', "
            "'approved', 'scenes_prepared', 'video_generation_pending', "
            "'video_generating', 'video_generated', 'assembling', 'completed', "
            "'failed', 'cancelled')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProjectStatus.DRAFT.value, server_default=ProjectStatus.DRAFT.value
    )
    # Script (LLM output) — Persian.
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_segments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Non-avatar scene pipeline: "avatar" keeps the legacy HeyGen path;
    # "scene" uses per-scene provider clips assembled with FFmpeg.
    video_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="scene", server_default="scene"
    )
    # Structured scenario (title, scenes, narration, CTA, caption, hashtags).
    scenario: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Scene pipeline options captured at creation: target duration, scene count,
    # provider preference, narration artifact. Persisted so a retried workflow
    # reproduces the same plan without trusting request state.
    scene_options: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    scene_video_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    assembly_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    video_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    call_to_action: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # LLM usage/cost metadata (tokens; cost only when provider reports it).
    llm_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    script_workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )
    video_workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True
    )
    # Paid generation protection: one persisted provider submission per project.
    # NULL until HeyGen accepted a generation request.
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_video_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    submission_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Actual provider usage/costs when reported; never fabricated prices.
    cost_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    final_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    brand: Mapped[ContentBrand] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ContentProject id={self.id} status={self.status!r}>"


class SceneVideoJobStatus(enum.StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    GENERATING = "generating"
    COMPLETED = "completed"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SceneVideoJob(Base, TimestampMixin):
    """One provider submission for one scene of a project.

    ``(project_id, scene_index)`` is unique and ``(provider, provider_job_id)``
    is unique, so a worker restart can never submit (or bill) the same scene
    twice.
    """

    __tablename__ = "scene_video_jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "scene_index", name="uq_scene_video_jobs_project_scene"),
        UniqueConstraint("provider", "provider_job_id", name="uq_scene_video_jobs_provider_job"),
        CheckConstraint(
            "status IN ('pending', 'submitted', 'generating', 'completed', "
            "'downloaded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("scene_index >= 0", name="scene_index_non_negative"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scene_index: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_key: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SceneVideoJobStatus.PENDING.value,
        server_default=SceneVideoJobStatus.PENDING.value,
    )
    # Provider id is NULL until the provider accepted the submission.
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    narration: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_screen_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitle_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cost_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<SceneVideoJob id={self.id} scene={self.scene_index} "
            f"status={self.status!r} provider={self.provider!r}>"
        )


class ProviderQuotaUsage(Base, TimestampMixin):
    """Per-company, per-provider, per-day usage counter.

    The unique key makes quota accounting idempotent under concurrency: the
    service increments the row inside a savepoint and treats an exhausted
    counter as a permanent failure.
    """

    __tablename__ = "provider_quota_usage"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "provider", "period_date", name="uq_provider_quota_usage_period"
        ),
        CheckConstraint("units_used >= 0", name="units_used_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    period_date: Mapped[date] = mapped_column(Date, nullable=False)
    units_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    units_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ProviderQuotaUsage provider={self.provider!r} "
            f"period={self.period_date} used={self.units_used}>"
        )
