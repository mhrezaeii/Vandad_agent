"""Generic HTTP scene-video adapter.

This adapter is intentionally vendor-neutral. It is **not** a claim that any
particular provider offers a free API; it is the integration seam used once a
legitimate, operator-approved provider with a documented HTTP contract exists.

It stays disabled unless ``SCENE_VIDEO_HTTP_BASE_URL`` is configured, and it
never invents endpoints or credentials. Operators supply the exact paths and
response field names for the provider they are authorised to use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.integrations.video.base import (
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoRequest,
    SceneVideoTransientError,
)

DEFAULT_STATE_MAP: dict[str, str] = {
    "queued": SceneJobState.QUEUED.value,
    "pending": SceneJobState.QUEUED.value,
    "submitted": SceneJobState.QUEUED.value,
    "starting": SceneJobState.QUEUED.value,
    "processing": SceneJobState.GENERATING.value,
    "generating": SceneJobState.GENERATING.value,
    "running": SceneJobState.GENERATING.value,
    "in_progress": SceneJobState.GENERATING.value,
    "completed": SceneJobState.COMPLETED.value,
    "succeeded": SceneJobState.COMPLETED.value,
    "success": SceneJobState.COMPLETED.value,
    "done": SceneJobState.COMPLETED.value,
    "ready": SceneJobState.COMPLETED.value,
    "failed": SceneJobState.FAILED.value,
    "error": SceneJobState.FAILED.value,
    "cancelled": SceneJobState.CANCELLED.value,
    "canceled": SceneJobState.CANCELLED.value,
}


def _dig(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


@dataclass(frozen=True)
class GenericHttpConfig:
    """Operator-supplied HTTP contract for one authorised provider."""

    base_url: str
    generate_path: str = "/v1/videos"
    status_path: str = "/v1/videos/{job_id}"
    retrieve_path: str | None = None
    api_key: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    job_id_field: str = "id"
    state_field: str = "status"
    video_url_field: str = "video_url"
    duration_field: str = "duration"
    progress_field: str = "progress"
    error_message_field: str = "error"
    state_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATE_MAP))
    request_extra: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("GenericHttpConfig.base_url must be an http(s) URL.")
        if "{job_id}" not in self.status_path:
            raise ValueError("GenericHttpConfig.status_path must contain '{job_id}'.")
        if self.retrieve_path is not None and "{job_id}" not in self.retrieve_path:
            raise ValueError("GenericHttpConfig.retrieve_path must contain '{job_id}'.")


class GenericHttpSceneVideoProvider(SceneVideoProvider):
    """Configurable HTTP adapter for a provider with a documented API."""

    provider_id = "generic_http"
    display_name = "Generic HTTP scene-video provider"
    supports_cancellation = False
    requires_network = True

    def __init__(self, config: GenericHttpConfig, *, client_factory=None) -> None:  # noqa: ANN001
        self.config = config
        self._client_factory = client_factory

    # ------------------------------------------------------------- submission
    async def generate_video(self, request: SceneVideoRequest) -> str:
        body: dict[str, Any] = {
            "prompt": request.prompt,
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "width": request.width,
            "height": request.height,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.idempotency_key:
            body["idempotency_key"] = request.idempotency_key
        body.update(self.config.request_extra)

        payload = await self._request_json(
            "POST", f"{self._base()}{self.config.generate_path}", json=body
        )
        job_id = _dig(payload, self.config.job_id_field)
        if job_id is None or str(job_id).strip() == "":
            raise SceneVideoPermanentError(
                "The provider response did not contain a job id.", code="provider_bad_response"
            )
        return str(job_id)

    # ----------------------------------------------------------------- status
    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        path = self.config.status_path.format(job_id=job_id)
        payload = await self._request_json("GET", f"{self._base()}{path}")
        raw_state = str(_dig(payload, self.config.state_field) or "").strip().lower()
        state = self.config.state_map.get(raw_state, SceneJobState.GENERATING.value)
        progress = _dig(payload, self.config.progress_field)
        duration = _dig(payload, self.config.duration_field)
        error_message = _dig(payload, self.config.error_message_field)
        return SceneGenerationStatus(
            job_id=job_id,
            state=state,
            progress=float(progress) if isinstance(progress, (int, float)) else None,
            video_url=_as_optional_str(_dig(payload, self.config.video_url_field)),
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
            error_code=raw_state or None if state == SceneJobState.FAILED.value else None,
            error_message=_as_optional_str(error_message),
            raw=payload if isinstance(payload, dict) else {},
        )

    # --------------------------------------------------------------- retrieval
    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        status = await self.get_generation_status(job_id)
        if not status.is_completed:
            raise SceneVideoPermanentError(
                "The provider job is not completed yet.", code="provider_job_not_completed"
            )
        url = status.video_url
        if url is None:
            if self.config.retrieve_path is None:
                raise SceneVideoPermanentError(
                    "The provider reported no video URL and no retrieve_path is configured.",
                    code="provider_video_url_missing",
                )
            url = f"{self._base()}{self.config.retrieve_path.format(job_id=job_id)}"

        client = self._client()
        try:
            async with client:
                async with client.stream("GET", url, headers=self._headers()) as response:
                    if response.status_code != 200:
                        raise self._error_for_status(response.status_code)
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            yield chunk
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "The provider download timed out.", code="provider_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError(
                "The provider could not be reached for download.", code="provider_unreachable"
            ) from exc

    # ---------------------------------------------------------------- helpers
    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:  # noqa: ANN201
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            value = self.config.api_key
            if self.config.auth_scheme:
                value = f"{self.config.auth_scheme} {value}"
            headers[self.config.auth_header] = value
        return headers

    async def _request_json(self, method: str, url: str, *, json: Any = None) -> Any:
        client = self._client()
        try:
            async with client:
                response = await client.request(method, url, json=json, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "The provider request timed out.", code="provider_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError(
                "The provider could not be reached.", code="provider_unreachable"
            ) from exc
        if response.status_code == 200 or response.status_code == 201:
            try:
                return response.json()
            except ValueError as exc:
                raise SceneVideoPermanentError(
                    "The provider returned non-JSON content.", code="provider_bad_response"
                ) from exc
        raise self._error_for_status(response.status_code)

    @staticmethod
    def _error_for_status(status_code: int) -> Exception:
        if status_code == 429:
            return SceneVideoTransientError(
                "The provider rate limit was reached.", code="provider_rate_limited"
            )
        if 500 <= status_code <= 599:
            return SceneVideoTransientError(
                f"The provider responded with status {status_code}.", code="provider_server_error"
            )
        if status_code in (401, 403):
            return SceneVideoPermanentError(
                "The provider rejected the credential.", code="provider_unauthorized"
            )
        return SceneVideoPermanentError(
            f"The provider responded with status {status_code}.", code="provider_client_error"
        )


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
