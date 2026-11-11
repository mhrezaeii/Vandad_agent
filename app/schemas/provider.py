"""Provider metadata response schemas (sourced from the provider registry)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

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


class ProviderBudgetUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_limit_usd: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    per_operation_limit_usd: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    enabled: bool = True


class ProviderBudgetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    provider_id: str
    currency: str
    monthly_limit_usd: Decimal
    per_operation_limit_usd: Decimal
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ProviderUsageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    provider_id: str
    operation: str
    model: str | None
    status: str
    reserved_amount_usd: Decimal
    actual_cost_usd: Decimal | None
    usage_payload: dict[str, Any]
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class ProviderHealthRead(BaseModel):
    provider_id: str
    calls_30d: int
    errors_30d: int
    pending_30d: int
    success_rate: float | None
    average_latency_ms: float | None
    last_call_at: datetime


class SceneVideoProviderRead(BaseModel):
    provider_id: str
    display_name: str
    requires_network: bool
    supports_lipsync: bool
    supports_reference_image: bool
    supports_cancellation: bool
    supports_composition: bool
    daily_quota: int | None = None


class ProviderUsageReconcile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_cost_usd: Decimal = Field(ge=0, max_digits=14, decimal_places=6)
    note: str = Field(min_length=5, max_length=500)
