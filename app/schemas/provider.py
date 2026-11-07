"""Provider metadata response schemas (sourced from the provider registry)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProviderCredentialRequirementModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    credential_type: str
    required: bool
    description: str


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_id: str = Field(validation_alias="provider_id")
    display_name: str
    description: str
    capabilities: list[str]
    credential_requirements: list[ProviderCredentialRequirementModel]
    supported_platform_types: list[str]
