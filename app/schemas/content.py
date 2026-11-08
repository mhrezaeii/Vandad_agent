"""Content production request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ContentBrandRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
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


class CharacterCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID = Field(description="HeyGen platform connection carrying the API key.")
    source_image_url: HttpUrl | None = Field(
        default=None,
        description="Explicit portrait asset the provider fetches (required on creation).",
    )
    voice_id: str | None = Field(default=None, max_length=255)


class ContentProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: uuid.UUID
    topic: str = Field(min_length=3, max_length=2000)


class ContentProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    brand_id: uuid.UUID
    topic: str
    status: str
    script_text: str | None
    script_segments: list[dict[str, Any]] | None
    video_title: str | None
    call_to_action: str | None
    llm_usage: dict[str, Any] | None
    provider_job_id: str | None
    provider_status: str | None
    raw_artifact_id: uuid.UUID | None
    final_artifact_id: uuid.UUID | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ProjectScriptApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = True


class ContentJobRead(BaseModel):
    project_id: str
    status: str
    provider_job_id: str | None
    provider_status: str | None
    raw_artifact_id: str | None
    final_artifact_id: str | None
    execution_status: str | None
    cost_metadata: dict[str, Any] | None
    llm_usage: dict[str, Any] | None


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
