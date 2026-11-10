"""Workflow, execution and schedule request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.workflows.nodes import (
    MAX_NODE_RETRIES,
    MAX_NODE_TIMEOUT_SECONDS,
    VALID_NODE_TYPES,
)


class WorkflowNodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    node_type: str
    name: str | None = Field(default=None, max_length=255)
    configuration: dict[str, Any] = Field(default_factory=dict)
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    output_mapping: dict[str, Any] = Field(default_factory=dict)
    execution_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in VALID_NODE_TYPES:
            raise ValueError(f"node_type must be one of {sorted(VALID_NODE_TYPES)}")
        return value

    @field_validator("execution_settings")
    @classmethod
    def _validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        timeout = value.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= MAX_NODE_TIMEOUT_SECONDS
        ):
            raise ValueError(f"timeout_seconds must be in (0, {MAX_NODE_TIMEOUT_SECONDS}]")
        retries = value.get("max_retries")
        if retries is not None and (not isinstance(retries, int) or not 0 <= retries <= MAX_NODE_RETRIES):
            raise ValueError(f"max_retries must be in [0, {MAX_NODE_RETRIES}]")
        delay = value.get("continue_on_error")
        if delay is not None and not isinstance(delay, bool):
            raise ValueError("continue_on_error must be a boolean")
        idem = value.get("idempotent")
        if idem is not None and not isinstance(idem, bool):
            raise ValueError("idempotent must be a boolean")
        return value


class WorkflowEdgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_key: str = Field(min_length=1, max_length=64)
    to_key: str = Field(min_length=1, max_length=64)
    condition_expression: str | None = Field(default=None, max_length=500)
    edge_order: int = Field(default=0, ge=0)


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    nodes: list[WorkflowNodeIn] = Field(min_length=1, max_length=100)
    edges: list[WorkflowEdgeIn] = Field(default_factory=list, max_length=200)
    default_execution_settings: dict[str, Any] = Field(default_factory=dict)


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    nodes: list[WorkflowNodeIn] | None = Field(default=None, max_length=100)
    edges: list[WorkflowEdgeIn] | None = Field(default=None, max_length=200)


class WorkflowNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_key: str
    node_type: str
    name: str
    configuration: dict[str, Any]
    input_mapping: dict[str, Any]
    output_mapping: dict[str, Any]
    execution_settings: dict[str, Any]


class WorkflowEdgeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_key: str
    to_key: str
    condition_expression: str | None
    edge_order: int


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    description: str | None
    status: str
    current_version_id: uuid.UUID | None
    default_execution_settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class WorkflowDetailRead(WorkflowRead):
    nodes: list[WorkflowNodeRead] = []
    edges: list[WorkflowEdgeRead] = []


class WorkflowListResponse(BaseModel):
    items: list[WorkflowRead]
    total: int
    limit: int
    offset: int


class ValidationIssueModel(BaseModel):
    code: str
    message: str
    node_key: str | None = None


class WorkflowValidationResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssueModel]


class WorkflowExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class WorkflowExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID | None
    workflow_version_id: uuid.UUID | None
    company_id: uuid.UUID | None
    workflow_name: str
    status: str
    trigger_type: str | None
    trigger_reference: str | None
    idempotency_key: str | None
    input_metadata: dict[str, Any] | None
    output_metadata: dict[str, Any] | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class WorkflowExecutionListResponse(BaseModel):
    items: list[WorkflowExecutionRead]
    total: int
    limit: int
    offset: int


class WorkflowStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_key: str
    node_type: str
    status: str
    attempt: int
    output: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None


class WorkflowLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_key: str | None
    level: str
    message: str
    correlation_id: str | None
    created_at: datetime


class WorkflowStepListResponse(BaseModel):
    items: list[WorkflowStepRead]
    total: int


class WorkflowLogListResponse(BaseModel):
    items: list[WorkflowLogRead]
    total: int


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    schedule_type: Literal["once", "interval", "cron"]
    schedule_config: dict[str, Any]
    timezone: str = Field(default="UTC", max_length=64)
    missed_policy: Literal["skip", "run_once"] = "skip"
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    schedule_config: dict[str, Any] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    company_id: uuid.UUID
    name: str
    schedule_type: str
    schedule_config: dict[str, Any]
    timezone: str
    enabled: bool
    missed_policy: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_execution_id: uuid.UUID | None
    last_status: str | None


class ScheduleListResponse(BaseModel):
    items: list[ScheduleRead]
    total: int


class WorkflowArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    execution_id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    original_filename: str | None
    created_at: datetime


class WorkflowArtifactListResponse(BaseModel):
    items: list[WorkflowArtifactRead]
    total: int
