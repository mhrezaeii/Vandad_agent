"""Request correlation ID propagation through contextvars."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

CORRELATION_ID_HEADER = "X-Request-ID"

# Allow typical trace-id shapes (UUIDs, hex, W3C traceparent ids) but nothing exotic.
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(value: str | None) -> str | None:
    """Return the incoming header value if it is safe to use, otherwise None."""
    if value is None:
        return None
    value = value.strip()
    if _CORRELATION_ID_PATTERN.fullmatch(value):
        return value
    return None


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str | None]:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)
