"""Generic HTTP scene-video adapter with fail-closed media URL handling."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

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
    download_allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("GenericHttpConfig.base_url must be an HTTPS URL without userinfo.")
        if parsed.query or parsed.fragment:
            raise ValueError("GenericHttpConfig.base_url must not contain query or fragment.")
        if "{job_id}" not in self.status_path:
            raise ValueError("GenericHttpConfig.status_path must contain '{job_id}'.")
        if self.retrieve_path is not None and "{job_id}" not in self.retrieve_path:
            raise ValueError("GenericHttpConfig.retrieve_path must contain '{job_id}'.")
        for host in self.download_allowed_hosts:
            if not host or host != host.strip() or "/" in host or ":" in host:
                raise ValueError("download_allowed_hosts must contain plain hostnames only.")


class GenericHttpSceneVideoProvider(SceneVideoProvider):
    """Configurable HTTP adapter for a provider with a documented API."""

    provider_id = "generic_http"
    display_name = "Generic HTTP scene-video provider"
    supports_cancellation = False
    requires_network = True
    _MAX_REDIRECTS = 3

    def __init__(self, config: GenericHttpConfig, *, client_factory=None) -> None:  # noqa: ANN001
        self.config = config
        self._client_factory = client_factory

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

        # Provider-supplied media URLs are untrusted. Never attach API credentials
        # to this request, even when the URL happens to share the API hostname.
        current_url = self._validate_media_url(url)
        client = self._client()
        try:
            async with client:
                for redirect_count in range(self._MAX_REDIRECTS + 1):
                    async with client.stream(
                        "GET",
                        current_url,
                        headers={"Accept": "video/*,application/octet-stream"},
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or redirect_count >= self._MAX_REDIRECTS:
                                raise SceneVideoPermanentError(
                                    "The media server returned an invalid redirect.",
                                    code="provider_download_redirect",
                                )
                            current_url = self._validate_media_url(urljoin(current_url, location))
                            continue
                        if response.status_code != 200:
                            raise self._error_for_status(response.status_code)
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type and not (
                            content_type.startswith("video/")
                            or content_type.startswith("application/octet-stream")
                        ):
                            raise SceneVideoPermanentError(
                                "The media server did not return a video content type.",
                                code="provider_download_content_type",
                            )
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if chunk:
                                yield chunk
                        return
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "The provider download timed out.", code="provider_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError(
                "The provider could not be reached for download.", code="provider_unreachable"
            ) from exc

    def _validate_media_url(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise SceneVideoPermanentError(
                "The provider returned an unsafe media URL.", code="provider_unsafe_media_url"
            )

        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and not literal_ip.is_global:
            raise SceneVideoPermanentError(
                "The provider returned a non-public media address.", code="provider_private_media_ip"
            )

        api_host = (urlsplit(self._base()).hostname or "").lower().rstrip(".")
        allowed = {api_host, *(item.lower().rstrip(".") for item in self.config.download_allowed_hosts)}
        if host not in allowed:
            raise SceneVideoPermanentError(
                "The provider media host is not allowlisted.", code="provider_media_host_denied"
            )

        # Resolve at download time and reject private, loopback, link-local,
        # reserved and otherwise non-global destinations. A configured host
        # allowlist is still mandatory for every non-API CDN host.
        if self._client_factory is None and literal_ip is None:
            try:
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise SceneVideoTransientError(
                    "The provider media hostname could not be resolved.",
                    code="provider_media_dns_failed",
                ) from exc
            if not addresses or any(
                not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global
                for item in addresses
            ):
                raise SceneVideoPermanentError(
                    "The provider media hostname resolves to a non-public address.",
                    code="provider_private_media_ip",
                )
        return value

    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:  # noqa: ANN201
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False)

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
        if response.status_code in (200, 201):
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
