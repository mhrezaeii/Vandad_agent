"""Health and readiness response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness: the process is up. Says nothing about dependencies."""

    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    timestamp: datetime


class DependencyCheck(BaseModel):
    status: Literal["ok", "unavailable"]
    latency_ms: float | None = None
    error: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness: dependencies (database, redis) verified reachable."""

    status: Literal["ready", "degraded"]
    checks: dict[str, DependencyCheck]
    timestamp: datetime
