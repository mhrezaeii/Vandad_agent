"""Provider-neutral scene-video generation contract.

The scene pipeline never talks to a vendor directly. It submits scene-generation
and final-composition requests, polls persisted provider jobs, retrieves media,
and validates the final provider-produced video.

Nothing in this module is vendor-specific: concrete adapters implement the
provider contract, including ``generate_video``, ``compose_video``,
``get_generation_status``, ``retrieve_video`` and ``validate_output``.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SceneJobState(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {SceneJobState.COMPLETED, SceneJobState.GENERATING, SceneJobState.FAILED, SceneJobState.CANCELLED}
) - {SceneJobState.GENERATING}


class SceneVideoError(Exception):
    """Base error for scene-video providers (message is client-safe)."""

    def __init__(self, message: str, *, code: str = "scene_video_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SceneVideoTransientError(SceneVideoError):
    """A retryable provider failure (network, 5xx, rate limit)."""


class SceneVideoPermanentError(SceneVideoError):
    """A non-retryable provider failure (bad request, invalid credential)."""


class SceneVideoQuotaExhaustedError(SceneVideoPermanentError):
    """The provider (or the local daily budget) refuses further generations."""


@dataclass(frozen=True)
class SceneVideoRequest:
    """One scene submission."""

    scene_id: str
    prompt: str
    duration_seconds: float
    aspect_ratio: str = "9:16"
    width: int = 1080
    height: int = 1920
    reference_image_url: str | None = None
    seed: int | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneGenerationStatus:
    """Normalised provider status for one scene job."""

    job_id: str
    state: str
    progress: float | None = None
    video_url: str | None = None
    duration_seconds: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            SceneJobState.COMPLETED.value,
            SceneJobState.FAILED.value,
            SceneJobState.CANCELLED.value,
        )

    @property
    def is_completed(self) -> bool:
        return self.state == SceneJobState.COMPLETED.value


@dataclass(frozen=True)
class SceneVideoValidation:
    """Result of validating provider-generated media before artifact finalization."""

    ok: bool
    size_bytes: int
    sha256: str
    content_type: str | None = None
    duration_seconds: float | None = None
    issues: tuple[str, ...] = ()


_MP4_BRANDS = (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash")
_WEBM_SIGNATURE = b"\x1a\x45\xdf\xa3"


def _looks_like_video(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[4:8] == b"ftyp":
        return True
    if data.startswith(_WEBM_SIGNATURE):
        return True
    if data.startswith(b"OggS"):
        return True
    return data[0:1] == b"\x00" and b"mdat" in data[:4096]


@dataclass(frozen=True)
class SceneVideoCompositionRequest:
    """AI-native composition request for a complete storyboard video."""

    project_id: str
    storyboard: dict[str, Any]
    scene_video_urls: tuple[str, ...]
    width: int
    height: int
    brand_context: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


class SceneVideoProvider(ABC):
    """Abstract scene-video provider.

    Implementations must be safe to call concurrently and must never log
    credentials. ``provider_id`` is the stable registry key persisted on jobs.
    """

    provider_id: str = ""
    display_name: str = ""
    supports_cancellation: bool = False
    supports_lipsync: bool = False
    requires_network: bool = False
    supports_reference_image: bool = False

    @abstractmethod
    async def generate_video(self, request: SceneVideoRequest) -> str:
        """Submit one scene and return the provider job id."""

    @abstractmethod
    async def compose_video(self, request: SceneVideoCompositionRequest) -> str:
        """Submit AI-provider editing/composition and return its persisted job id.

        Providers that only generate isolated clips must fail closed rather than
        silently falling back to local FFmpeg composition.
        """
        raise SceneVideoPermanentError(
            "This AI provider does not support storyboard video composition.",
            code="provider_composition_unsupported",
        )

    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        """Return the normalised status for a previously submitted job."""

    @abstractmethod
    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        """Yield the finished clip in bounded chunks."""

    async def cancel_generation(self, job_id: str) -> bool:
        """Cancel a job when the provider supports it. Default: unsupported."""
        return False

    def quota_limit_per_day(self) -> int | None:
        """Provider-reported daily generation limit, when known."""
        return None

    async def validate_output(
        self, data: bytes, *, content_type: str | None = None
    ) -> SceneVideoValidation:
        """Validate downloaded bytes. The default checks size + container magic."""
        issues: list[str] = []
        if not data:
            issues.append("The downloaded clip is empty.")
        if content_type and not content_type.lower().startswith("video/"):
            issues.append(f"Unexpected content type {content_type!r}.")
        if data and not _looks_like_video(data):
            issues.append("The downloaded file does not look like a video container.")
        digest = hashlib.sha256(data).hexdigest() if data else ""
        return SceneVideoValidation(
            ok=not issues,
            size_bytes=len(data),
            sha256=digest,
            content_type=content_type,
            issues=tuple(issues),
        )


class InMemorySceneVideoProvider(SceneVideoProvider):
    """Shared bookkeeping for providers that keep job state in memory.

    The mock provider uses it directly; network adapters can reuse the quota
    helpers without inheriting any fake generation behaviour.
    """

    def __init__(self, *, quota_limit_per_day: int | None = None) -> None:
        self._quota_limit_per_day = quota_limit_per_day
        self._daily_usage = 0

    def quota_limit_per_day(self) -> int | None:  # noqa: D401 - property-style
        return self._quota_limit_per_day

    def daily_usage(self) -> int:
        return self._daily_usage

    def _consume_quota(self, *, units: int = 1) -> None:
        if self._quota_limit_per_day is not None and self._daily_usage + units > self._quota_limit_per_day:
            raise SceneVideoQuotaExhaustedError(
                "The daily scene-video quota is exhausted.",
                code="quota_exhausted",
            )
        self._daily_usage += units
