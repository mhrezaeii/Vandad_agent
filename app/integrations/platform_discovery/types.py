"""Typed results shared by discovery adapters."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    title: str | None
    text: str
    source_type: str
    status_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class DiscoveryResult:
    pages: list[DiscoveredPage]
    warnings: list[str] = field(default_factory=list)
