"""Content production domain: brands, persistent characters, video projects."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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
    VIDEO_GENERATING = "video_generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentProject(Base, TimestampMixin):
    """One video production request for a brand."""

    __tablename__ = "content_projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'script_generating', 'script_ready', 'script_failed', "
            "'approved', 'video_generating', 'completed', 'failed', 'cancelled')",
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
