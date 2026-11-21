"""Validated input contracts for dispatch-ready agent capabilities."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.scenario import (
    MAX_SCENES,
    MAX_TARGET_DURATION_SECONDS,
    MIN_TARGET_DURATION_SECONDS,
)


class _CapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlatformDiscoverInput(_CapabilityInput):
    platform_id: uuid.UUID
    max_pages: int = Field(default=10, ge=1, le=25)


class PlatformKnowledgeInput(_CapabilityInput):
    platform_id: uuid.UUID
    connection_id: uuid.UUID | None = None
    max_documents: int = Field(default=20, ge=1, le=20)


class PlatformMonitorInput(PlatformDiscoverInput):
    connection_id: uuid.UUID | None = None
    max_documents: int = Field(default=20, ge=1, le=20)


class ContentScenarioInput(_CapabilityInput):
    project_id: uuid.UUID
    connection_id: uuid.UUID
    timeout_seconds: float = Field(default=180, ge=1, le=180)


class ContentAnalysisInput(_CapabilityInput):
    brand_id: uuid.UUID
    limit: int = Field(default=10, ge=1, le=50)


class ContentProductionInput(_CapabilityInput):
    """One-click production: monitoring → analysis → scenario → video → QC."""

    brand_id: uuid.UUID
    source_url: str | None = Field(default=None, max_length=2048)
    target_duration_seconds: int = Field(
        default=30, ge=MIN_TARGET_DURATION_SECONDS, le=MAX_TARGET_DURATION_SECONDS
    )
    scene_count: int | None = Field(default=None, ge=1, le=MAX_SCENES)
    provider_id: str | None = Field(default=None, min_length=1, max_length=80)
    provider_fallbacks: list[str] = Field(default_factory=list, max_length=5)
    character_key: str | None = Field(default=None, max_length=255)
    character_image_url: str | None = Field(default=None, max_length=2048)
    character_description: str | None = Field(default=None, max_length=1000)
    connection_id: uuid.UUID | None = None
    video_wait_seconds: float = Field(default=900, ge=0, le=3600)
    video_poll_seconds: float = Field(default=3, ge=1, le=60)


class VideoSceneInput(_CapabilityInput):
    project_id: uuid.UUID
    provider_id: str | None = Field(default=None, min_length=1, max_length=80)
    provider_fallbacks: list[str] = Field(default_factory=list, max_length=5)


class VideoAvatarInput(_CapabilityInput):
    project_id: uuid.UUID
    connection_id: uuid.UUID


CAPABILITY_INPUT_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("monitoring", "platform.monitor"): PlatformMonitorInput,
    ("monitoring", "platform.discover"): PlatformDiscoverInput,
    ("monitoring", "platform.change.detect"): PlatformDiscoverInput,
    ("monitoring", "platform.knowledge.extract"): PlatformKnowledgeInput,
    ("content", "content.scenario.generate"): ContentScenarioInput,
    ("content", "content.analysis.select"): ContentAnalysisInput,
    ("content", "content.production.run"): ContentProductionInput,
    ("video", "video.scene.generate"): VideoSceneInput,
    ("video", "video.avatar.generate"): VideoAvatarInput,
}


def has_capability_contract(agent_key: str, capability: str) -> bool:
    return (agent_key, capability) in CAPABILITY_INPUT_MODELS


def validate_capability_payload(
    agent_key: str, capability: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate and normalize a task payload against its code-owned contract."""
    model = CAPABILITY_INPUT_MODELS.get((agent_key, capability))
    if model is None:
        return payload
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError("Input payload does not match the registered capability contract.") from exc
