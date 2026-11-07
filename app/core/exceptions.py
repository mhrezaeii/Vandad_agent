"""Application exception hierarchy.

API error handlers translate these into consistent JSON error responses;
nothing here should leak implementation details to clients.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, client-facing application errors."""

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.code = code or self.code
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with the current state of the resource."


class UnprocessableEntityError(AppError):
    status_code = 422
    code = "validation_error"
    default_message = "The request payload failed validation."


class DependencyUnavailableError(AppError):
    status_code = 503
    code = "dependency_unavailable"
    default_message = "A required dependency is currently unavailable."
