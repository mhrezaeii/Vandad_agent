"""Credential reference and redaction helpers.

Phase 1 stores only *references* to secrets (vault keys, secret names), never
the secrets themselves. These helpers validate reference format and make sure
sensitive values never survive into logs or error payloads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Expected shapes: "vault://platform-x/api-key", "env:PLATFORM_X_TOKEN",
# "secret-store/production/github-oauth" etc. Anything that looks like prose or
# contains whitespace is rejected.
SECRET_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$")

REDACTED_VALUE = "***REDACTED***"

# Substrings that mark a key as sensitive (checked case-insensitively).
SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "encryption_key",
)

# Matches `password=hunter2`, `"api_key": "abc123"`, `token: xyz`, etc.
# Group 1 = key, group 2 = separator — the value is replaced by the marker.
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(SENSITIVE_KEY_HINTS) + r")\b(\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}\]]+"
)


def is_valid_secret_reference(value: str) -> bool:
    """Return True when *value* is a well-formed secret reference."""
    return bool(SECRET_REFERENCE_PATTERN.fullmatch(value))


def validate_secret_reference(value: str) -> str:
    """Validate a secret reference, raising ValueError with a safe message."""
    if not isinstance(value, str) or not is_valid_secret_reference(value):
        raise ValueError(
            "secret_reference must be a locator such as 'vault://<path>/<name>', "
            "not a secret value; it must match " + SECRET_REFERENCE_PATTERN.pattern
        )
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def redact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of *value* with sensitive keys replaced by a redaction marker."""
    if not value:
        return {}
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if _is_sensitive_key(str(key)):
            redacted[key] = REDACTED_VALUE
        elif isinstance(item, Mapping):
            redacted[key] = redact_mapping(item)
        elif isinstance(item, (list, tuple)):
            redacted[key] = [redact_mapping(entry) if isinstance(entry, Mapping) else entry for entry in item]
        else:
            redacted[key] = item
    return redacted


def redact_text(text: str) -> str:
    """Mask `key=value` style secret occurrences inside free-form text (e.g. errors)."""
    if not text:
        return text
    return _SENSITIVE_TEXT_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED_VALUE}", text)
