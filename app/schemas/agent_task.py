"""API contracts for agent task requests and execution runs."""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security.redaction import redact_text

TaskStatus = Literal[
    "pending", "queued", "awaiting_approval", "running", "completed", "failed", "cancelled"
]


class AgentTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(min_length=1, max_length=80)
    capability: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=255)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int = Field(default=50, ge=0, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("input_payload")
    @classmethod
    def validate_input_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("input_payload must not exceed 64 KiB.")

        sensitive_hints = (
            "password", "passwd", "secret", "token", "apikey", "authorization",
            "credential", "privatekey", "encryptionkey",
        )

        def inspect(item: Any) -> None:
            if isinstance(item, Mapping):
                for key, nested in item.items():
                    normalized = str(key).lower().replace("_", "").replace("-", "")
                    if any(hint in normalized for hint in sensitive_hints):
                        raise ValueError("input_payload must contain secret references, not credentials.")
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)
            elif isinstance(item, str) and redact_text(item) != item:
                raise ValueError("input_payload must not embed credential-like values in text.")

        inspect(value)
        return value


class AgentTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    agent_key: str
    capability: str
    title: str
    status: TaskStatus
    priority: int
    input_payload: dict[str, Any]
    result_payload: dict[str, Any] | None
    idempotency_key: str | None
    requested_by: str | None
    cancel_requested: bool
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    agent_dispatch_ready: bool


class AgentTaskListRead(BaseModel):
    items: list[AgentTaskRead]
    total: int
    limit: int
    offset: int


class AgentTaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    company_id: uuid.UUID
    attempt_number: int
    status: Literal["running", "completed", "failed", "cancelled"]
    input_snapshot: dict[str, Any]
    result_payload: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    worker_id: str | None
    heartbeat_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentTaskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    company_id: uuid.UUID
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
