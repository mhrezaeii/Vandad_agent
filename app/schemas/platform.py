"""Platform request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.platform import (
    IntegrationType,
    PlatformStatus,
    PlatformType,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PlatformBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="URL-safe unique identifier within the company; derived from name when omitted.",
    )
    platform_type: PlatformType = Field(default=PlatformType.OTHER)
    base_url: str | None = Field(
        default=None, max_length=2048, description="HTTP(S) base URL of the platform."
    )
    description: str | None = Field(default=None, max_length=2000)
    status: PlatformStatus = Field(default=PlatformStatus.ACTIVE)
    integration_type: IntegrationType = Field(default=IntegrationType.API)
    configuration: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip().lower().startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must be lowercase letters, digits and hyphens (e.g. 'my-platform')")
        return value


class PlatformCreate(PlatformBase):
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Owning company; when omitted the platform is attached to the default company.",
    )


class PlatformUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    platform_type: PlatformType | None = None
    base_url: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    status: PlatformStatus | None = None
    integration_type: IntegrationType | None = None
    configuration: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip().lower().startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must be lowercase letters, digits and hyphens (e.g. 'my-platform')")
        return value


class PlatformRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    slug: str
    platform_type: str
    base_url: str | None
    description: str | None
    status: str
    integration_type: str
    configuration: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PlatformListResponse(BaseModel):
    items: list[PlatformRead]
    total: int
    limit: int
    offset: int
