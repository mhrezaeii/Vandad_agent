"""Pure-ASGI middleware: request correlation IDs and structured access logs."""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.correlation import (
    CORRELATION_ID_HEADER,
    new_correlation_id,
    reset_correlation_id,
    sanitize_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger("app.request")


class CorrelationIdMiddleware:
    """Assign/propagate X-Request-ID and emit one structured log per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_header = next(
            (value.decode("latin-1") for key, value in scope.get("headers", []) if key == b"x-request-id"),
            None,
        )
        correlation_id = sanitize_correlation_id(raw_header) or new_correlation_id()

        token = set_correlation_id(correlation_id)
        started_at = time.perf_counter()
        status_code = 500  # overwritten by the first response start; safe default on crash

        async def send_with_correlation_id(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((CORRELATION_ID_HEADER.lower().encode("latin-1"), correlation_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation_id)
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "request.completed",
                extra={
                    "http_method": scope.get("method"),
                    "http_path": scope.get("path"),
                    "http_status": status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            reset_correlation_id(token)
