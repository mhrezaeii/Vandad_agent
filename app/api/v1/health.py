"""Health endpoints: liveness vs. dependency readiness."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_readiness_service
from app.core.config import get_settings
from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.readiness_service import ReadinessService

router = APIRouter(tags=["health"])


@router.get("", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — the application process is running."""
    settings = get_settings()
    return HealthResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness(
    readiness_service: ReadinessService = Depends(get_readiness_service),
) -> ReadinessResponse:
    """Readiness probe — verifies database and Redis connectivity.

    Returns 200 when every dependency is reachable, 503 otherwise.
    """
    settings = get_settings()
    report = await readiness_service.check(timeout=settings.readiness_timeout_seconds)
    payload = ReadinessResponse(
        status=report.status,
        checks={
            name: {"status": check.status, "latency_ms": check.latency_ms, "error": check.error}
            for name, check in report.checks.items()
        },
        timestamp=datetime.now(UTC),
    )
    if not report.is_ready:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump(mode="json"))
    return payload
