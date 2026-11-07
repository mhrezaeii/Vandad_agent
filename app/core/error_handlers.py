"""Centralized exception handlers producing a consistent error envelope:

    {"error": {"code": ..., "message": ..., "details": [...], "correlation_id": ...}}

Handlers never echo exception internals that could contain credentials or
connection strings; details are redacted before logging or responding.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.correlation import get_correlation_id
from app.core.exceptions import AppError
from app.security.redaction import redact_text

logger = logging.getLogger("app.errors")

_FALLBACK_MESSAGE = "An unexpected error occurred."


def _error_payload(
    *,
    code: str,
    message: str,
    details: list[dict] | None = None,
) -> dict:
    error: dict = {
        "code": code,
        "message": redact_text(message),
        "correlation_id": get_correlation_id() or "-",
    }
    if details:
        error["details"] = details
    return {"error": error}


def register_exception_handlers(app: FastAPI) -> None:
    """Install all application exception handlers on *app*."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "application error: %s (%s)", exc.code, exc.message,
            extra={"error_code": exc.code, "http_status": exc.status_code},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(code=exc.code, message=exc.message, details=exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        logger.info("request validation failed", extra={"error_count": len(details)})
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                code="validation_error",
                message="The request payload failed validation.",
                details=details,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                code=f"http_{exc.status_code}",
                message=str(exc.detail) if exc.detail else _FALLBACK_MESSAGE,
            ),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Detail stays in the log; clients get a generic conflict.
        logger.error(
            "database integrity error on %s %s",
            request.method, request.url.path, exc_info=exc,
        )
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                code="conflict",
                message="The request violates a database constraint.",
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error(
            "database error on %s %s",
            request.method, request.url.path, exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(code="database_error", message="A database error occurred."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.critical(
            "unhandled error on %s %s",
            request.method, request.url.path, exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(code="internal_error", message=_FALLBACK_MESSAGE),
        )
