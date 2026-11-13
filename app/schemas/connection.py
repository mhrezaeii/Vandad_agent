"""Connection request/response schemas.

Credential material is accepted only as write-only input fields and is never
present in any response schema. ``SecretStr`` prevents accidental logging of
the plaintext value inside application code.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.models import ConnectionStatus

StorageTypeLiteral = Literal["reference", "encrypted"]


def _validate_connection_configuration(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return value
    fallbacks = value.get("llm_fallbacks")
    if fallbacks is None:
        return value
    if not isinstance(fallbacks, list) or len(fallbacks) > 5:
        raise ValueError("llm_fallbacks must be a list with at most 5 entries.")
    for item in fallbacks:
        if not isinstance(item, dict) or set(item) - {"connection_id", "model"}:
            raise ValueError("Each LLM fallback must contain only connection_id and model.")
        connection_id = item.get("connection_id")
        model = item.get("model")
        if connection_id is None and model is None:
            raise ValueError("Each LLM fallback must specify a connection_id or model.")
        if connection_id is not None:
            try:
                uuid.UUID(str(connection_id))
            except (TypeError, ValueError) as exc:
                raise ValueError("LLM fallback connection_id must be a UUID.") from exc
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 200
        ):
            raise ValueError("LLM fallback model must contain 1 to 200 characters.")
    return value


class ConnectionCredentialInput(BaseModel):
    """Inline credential supplied while creating a connection (write-only)."""

    model_config = ConfigDict(extra="forbid")

    credential_type: str = Field(description="One of the supported CredentialType values.")
    storage_type: StorageTypeLiteral = "encrypted"
    secret_reference: str = Field(min_length=3, max_length=512)
    # Write-only plaintext; accepted over TLS, immediately encrypted, never stored
    # in plaintext, never returned, never logged.
    value: SecretStr | None = Field(default=None, max_length=65536)

    @field_validator("credential_type")
    @classmethod
    def _validate_credential_type(cls, value: str) -> str:
        from app.models import CredentialType

        valid = {item.value for item in CredentialType}
        if value not in valid:
            raise ValueError(f"credential_type must be one of {sorted(valid)}")
        return value


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_id: uuid.UUID
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Owning company; resolved to the default company when omitted.",
    )
    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=100)
    configuration: dict[str, Any] = Field(default_factory=dict)
    credential_reference_id: uuid.UUID | None = None
    credential: ConnectionCredentialInput | None = None

    @field_validator("configuration")
    @classmethod
    def _validate_configuration(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_connection_configuration(value) or {}


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    configuration: dict[str, Any] | None = None

    @field_validator("configuration")
    @classmethod
    def _validate_configuration(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_connection_configuration(value)


class ConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    platform_id: uuid.UUID
    name: str
    provider: str
    status: str
    configuration: dict[str, Any]
    credential_reference_id: uuid.UUID | None
    activated_at: datetime | None
    last_tested_at: datetime | None
    last_connected_at: datetime | None
    last_failure_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime


class ConnectionListResponse(BaseModel):
    items: list[ConnectionRead]
    total: int
    limit: int
    offset: int


class ConnectionTestResult(BaseModel):
    success: bool
    status_after: str
    provider_id: str
    message: str
    error_code: str | None
    latency_ms: float | None
    attempts: int
    degraded: bool


class ConnectionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    from_status: str | None
    to_status: str | None
    success: bool | None
    message: str | None
    correlation_id: str | None
    created_at: datetime


class ConnectionEventListResponse(BaseModel):
    items: list[ConnectionEventRead]
    total: int


def status_is_known(value: str) -> bool:
    return value in {item.value for item in ConnectionStatus}
