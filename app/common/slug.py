"""Small shared utilities with no external dependencies."""

from __future__ import annotations

import re
import unicodedata
import uuid

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Convert free text into a URL-safe slug; empty results fall back to a UUID stub."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_INVALID_CHARS.sub("-", normalized.lower()).strip("-")
    return slug or f"item-{uuid.uuid4().hex[:8]}"
