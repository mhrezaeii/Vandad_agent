"""Schemas for source-grounded platform knowledge extraction."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlatformKnowledgeExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: uuid.UUID
    max_documents: int = Field(default=20, ge=1, le=50)


class PlatformKnowledgeFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=1000)
    source_url: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0, le=1)


class PlatformKnowledgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=5000)
    products_and_services: list[PlatformKnowledgeFact] = Field(default_factory=list, max_length=50)
    capabilities: list[PlatformKnowledgeFact] = Field(default_factory=list, max_length=100)
    workflows: list[PlatformKnowledgeFact] = Field(default_factory=list, max_length=50)
    target_audiences: list[PlatformKnowledgeFact] = Field(default_factory=list, max_length=30)
    differentiators: list[PlatformKnowledgeFact] = Field(default_factory=list, max_length=30)
    content_opportunities: list[str] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=50)


class PlatformKnowledgeExtractResponse(BaseModel):
    id: uuid.UUID
    platform_id: uuid.UUID
    source_document_count: int
    source_fingerprint: str
    model: str | None
    extracted_at: datetime
    knowledge: PlatformKnowledgePayload


class PlatformKnowledgeSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    platform_id: uuid.UUID
    source_document_ids: list[str]
    source_fingerprint: str
    knowledge_json: dict[str, Any]
    model: str | None
    extracted_at: datetime
