"""Shared API schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    loc: list[str] = []
    message: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | dict[str, Any] | None = None
    correlation_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
