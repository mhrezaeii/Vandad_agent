"""Structured logging: JSON formatter, correlation filter, redaction."""

from __future__ import annotations

import json
import logging

from app.core.correlation import set_correlation_id
from app.core.logging import CorrelationIdFilter, JsonFormatter, RedactionFilter


def _make_record(message: str, **extras) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )
    for key, value in extras.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_valid_json_with_extras():
    from app.core.correlation import reset_correlation_id

    token = set_correlation_id("corr-12345678")
    try:
        formatter = JsonFormatter()
        record = _make_record("hello world")
        record.http_method = "GET"
        record.http_path = "/api/v1/health"
        # In production the handler's filter stamps the correlation id before
        # the formatter runs; mirror that here.
        CorrelationIdFilter().filter(record)
        line = formatter.format(record)
    finally:
        reset_correlation_id(token)

    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["correlation_id"] == "corr-12345678"
    assert payload["http_method"] == "GET"
    assert "timestamp" in payload


def test_json_formatter_includes_exception_info():
    formatter = JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _make_record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(formatter.format(record))
    assert "RuntimeError: boom" in payload["exception"]


def test_correlation_filter_defaults_to_dash():
    record = _make_record("no correlation id set")
    CorrelationIdFilter().filter(record)
    assert record.correlation_id == "-"


def test_redaction_filter_masks_sensitive_message():
    record = _make_record("login failed with api_key=abc123secret")
    RedactionFilter().filter(record)
    assert "abc123secret" not in record.msg
