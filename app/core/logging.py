"""Structured logging: JSON or console output, correlation IDs, secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.correlation import get_correlation_id
from app.security.redaction import redact_text

# Attributes every LogRecord carries; everything else in record.__dict__ is an
# application "extra" and is emitted as a structured field.
_STD_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_name", "exc_info", "exc_text", "stack_info", "lineno",
        "funcName", "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime", "correlation_id",
    }
)


class CorrelationIdFilter(logging.Filter):
    """Attach the current correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class RedactionFilter(logging.Filter):
    """Strip `secret=value`-style fragments from rendered log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg and isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line with stable field names."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STD_RECORD_ATTRS
        }
        base = (
            f"{datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec='milliseconds')} "
            f"{record.levelname:<8} [{getattr(record, 'correlation_id', '-')}] "
            f"{record.name}: {record.getMessage()}"
        )
        if extras:
            base += f" | {extras}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(settings: Settings) -> None:
    """Configure the root logger. Safe to call multiple times (idempotent)."""
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(RedactionFilter())
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())

    root.handlers.clear()
    root.addHandler(handler)

    # uvicorn configures its own handlers on import; route them through the root
    # logger so uvicorn lines also carry correlation IDs and our format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
