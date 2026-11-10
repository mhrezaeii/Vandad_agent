"""Content production request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class BrandCtaConfig(BaseModel):
    """Call-to-action template attached to a brand."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=2048)
    hashtags: list[str] = Field(default_factory=list)


class BrandCreate(BaseModel):
    """Create a tenant-owned brand."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, description="Stable, company-unique brand key.")
    display_name: str | None = Field(default=None, max_length=150)
    tone: str | None = Field(default=None, max_length=500)
    language: str = Field(default="fa-IR", min_length=2, max_length=20)
    description: str | None = Field(default=None, max_length=5000)
    cta_config: BrandCtaConfig = Field(default_factory=BrandCtaConfig)
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=100)
    video_width: int = Field(default=1080, ge=1, le=8192)
    video_height: int = Field(default=1920, ge=1, le=8192)
    visual_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Brand name must not be blank.")
        return stripped


class BrandUpdate(BaseModel):
    """Partial brand update.

    ``name`` is intentionally not patchable: it is the stable unique key used
    by idempotent seeding, so renaming would make the seeder recreate a brand.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=150)
    tone: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, min_length=2, max_length=20)
    description: str | None = Field(default=None, max_length=5000)
    cta_config: BrandCtaConfig | None = None
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=100)
    status: Literal["active", "archived"] | None = None


class ContentBrandRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    display_name: str | None
    description: str | None
    cta_config: dict[str, Any]
    domain: str | None
    industry: str | None
    language: str
    video_width: int
    video_height: int
    tone: str | None
    visual_config: dict[str, Any]
    status: str


class BrandCharacterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_id: uuid.UUID
    provider: str
    provider_avatar_id: str | None
    provider_group_id: str | None
    avatar_kind: str | None
    voice_id: str | None
    language: str
    status: str
    creation_metadata: dict[str, Any]
    last_checked_at: datetime | None


class CharacterCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID = Field(description="HeyGen platform connection carrying the API key.")
    source_image_url: HttpUrl | None = Field(
        default=None,
        description="Explicit portrait asset the provider fetches (required on creation).",
    )
    voice_id: str | None = Field(default=None, max_length=255)


class CharacterStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = Field(description="Activate or deactivate the persistent character.")


class ContentConnectionRequest(BaseModel):
    """Provider connection selected for one pipeline step."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID


class ContentProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: uuid.UUID
    topic: str = Field(min_length=3, max_length=2000)


class SceneProjectCreate(BaseModel):
    """Create a non-avatar project built from provider-generated scene clips."""

    model_config = ConfigDict(extra="forbid")

    brand_id: uuid.UUID
    topic: str = Field(min_length=3, max_length=2000)
    target_duration_seconds: int = Field(default=30, ge=5, le=120)
    scene_count: int | None = Field(default=None, ge=3, le=12)
    provider_id: str | None = Field(
        default=None,
        max_length=50,
        description="Scene-video provider id; defaults to the platform setting.",
    )
    narration_artifact_id: uuid.UUID | None = Field(
        default=None,
        description="Optional pre-generated Persian narration audio artifact.",
    )


class SceneVideoStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = Field(default=None, max_length=50)


class ContentProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    brand_id: uuid.UUID
    topic: str
    status: str
    video_mode: str = "scene"
    script_text: str | None
    script_segments: list[dict[str, Any]] | None
    scenario: dict[str, Any] | None = None
    scene_options: dict[str, Any] | None = None
    scene_video_provider: str | None = None
    assembly_metadata: dict[str, Any] | None = None
    video_title: str | None
    call_to_action: str | None
    llm_usage: dict[str, Any] | None
    script_workflow_execution_id: uuid.UUID | None
    video_workflow_execution_id: uuid.UUID | None
    provider_job_id: str | None
    provider_status: str | None
    raw_artifact_id: uuid.UUID | None
    final_artifact_id: uuid.UUID | None
    cost_metadata: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ProjectScriptApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = True


class ContentJobRead(BaseModel):
    project_id: str
    status: str
    video_mode: str = "scene"
    provider_job_id: str | None
    provider_status: str | None
    scene_video_provider: str | None = None
    scene_jobs: list[dict[str, Any]] = Field(default_factory=list)
    assembly_metadata: dict[str, Any] | None = None
    raw_artifact_id: str | None
    final_artifact_id: str | None
    execution_status: str | None
    cost_metadata: dict[str, Any] | None
    llm_usage: dict[str, Any] | None


class SceneVideoJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    scene_index: int
    scene_key: str
    provider: str
    status: str
    provider_job_id: str | None
    provider_status: str | None
    progress: float | None
    duration_seconds: float
    attempts: int
    max_attempts: int
    artifact_id: uuid.UUID | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ContentVideoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_id: uuid.UUID
    topic: str
    status: str
    video_title: str | None
    final_artifact_id: uuid.UUID | None
    cost_metadata: dict[str, Any] | None
    updated_at: datetime
