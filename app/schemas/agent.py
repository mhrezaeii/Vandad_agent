"""API schemas for the approved agent catalog and company agent instances."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentDefinitionRead(BaseModel):
    key: str
    name: str
    description: str
    capabilities: list[str]
    maturity: Literal["partial", "planned"]
    dispatch_ready: bool


class AgentCatalogRead(BaseModel):
    items: list[AgentDefinitionRead]


class CompanyAgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    agent_key: str
    status: Literal["active", "disabled"]
    created_at: datetime
    updated_at: datetime
    definition: AgentDefinitionRead


class AgentStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"]
