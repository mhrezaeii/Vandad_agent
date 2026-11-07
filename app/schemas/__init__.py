"""Pydantic request/response schemas."""

from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse
from app.schemas.health import DependencyCheck, HealthResponse, ReadinessResponse
from app.schemas.platform import (
    PlatformCreate,
    PlatformListResponse,
    PlatformRead,
    PlatformUpdate,
)

__all__ = [
    "DependencyCheck",
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "PlatformCreate",
    "PlatformListResponse",
    "PlatformRead",
    "PlatformUpdate",
    "ReadinessResponse",
]
