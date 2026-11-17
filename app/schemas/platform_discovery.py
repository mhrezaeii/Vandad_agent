"""Platform discovery API schemas."""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class PlatformDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["public", "browser"] = "public"
    max_pages: int = Field(default=25, ge=1, le=50)
    connection_id: uuid.UUID | None = None

class PlatformSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    platform_id: uuid.UUID
    url: str
    source_type: str
    title: str | None
    content_text: str
    content_hash: str
    http_status: int | None
    metadata_json: dict[str, Any]
    last_seen_at: datetime
    changed_at: datetime | None

class PlatformSourceListResponse(BaseModel):
    items: list[PlatformSourceRead]
    total: int
    limit: int
    offset: int
