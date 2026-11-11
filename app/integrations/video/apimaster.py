"""APIMaster asynchronous video-generation adapter.

Implements APIMaster's documented /v1/videos contract. API credentials are
sent only to the APIMaster API host; redirects to media hosts are allowlisted
and never receive the API key.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.integrations.video.base import (
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
)


@dataclass(frozen=True)
class APIMasterConfig:
    """Credentials and safe download policy for APIMaster."""

    api_key: str
    base_url: str = "https://apimaster.ai"
    model: str = "wan3.0-video-prime"
    resolution: str = "720P"
    timeout_seconds: float = 60.0
    download_allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APIMaster base_url must be an HTTPS URL without userinfo, query or fragment.")
        if not self.api_key.strip():
            raise ValueError("APIMaster api_key must not be blank.")
        if self.resolution.upper() not in {"480P", "720P", "1080P"}:
            raise ValueError("APIMaster resolution must be 480P, 720P or 1080P.")
        for host in self.download_allowed_hosts:
            if not host or host != host.strip() or "/" in host or ":" in host:
                raise ValueError("APIMaster download allowlist must contain plain hostnames.")


class APIMasterSceneVideoProvider(SceneVideoProvider):
    """Scene-video provider for APIMaster's asynchronous video API."""

    provider_id = "apimaster"
    display_name = "APIMaster"
    requires_network = True
    supports_reference_image = True
    supports_lipsync = False
    _MAX_REDIRECTS = 3

    def __init__(self, config: APIMasterConfig, *, client_factory=None) -> None:  # noqa: ANN001
        self.config = config
        self._client_factory = client_factory

    async def generate_video(self, request: SceneVideoRequest) -> str:
        raw_duration = request.duration_seconds
        if (
            isinstance(raw_duration, bool)
            or not isinstance(raw_duration, (int, float))
            or not float(raw_duration).is_integer()
            or not 2 <= raw_duration <= 30
        ):
            raise SceneVideoPermanentError(
                "APIMaster Wan 3.0 requires an integer duration from 2 to 30 seconds.",
                code="provider_invalid_duration",
            )
        duration = int(raw_duration)
        if not request.prompt.strip() or len(request.prompt) > 5000:
            raise SceneVideoPermanentError(
                "APIMaster requires a prompt containing 1 to 5000 characters.",
                code="provider_invalid_prompt",
            )

        body: dict[str, Any] = {
            "model": self.config.model,
            "prompt": request.prompt,
            "duration": duration,
            "resolution": self.config.resolution.upper(),
            "aspect_ratio": request.aspect_ratio,
        }
        if request.reference_image_url:
            body["input_reference"] = self._validate_reference_image_url(
                request.reference_image_url
            )
        if request.seed is not None:
            if not 1 <= request.seed <= 2_147_483_647:
                raise SceneVideoPermanentError(
                    "APIMaster seed must be between 1 and 2147483647.",
                    code="provider_invalid_seed",
                )
            body["seed"] = request.seed

        payload = await self._request_json("POST", f"{self._base()}/v1/videos", json=body)
        task_id = payload.get("id") or payload.get("task_id")
        if not isinstance(task_id, (str, int)) or not str(task_id).strip():
            raise SceneVideoPermanentError(
                "APIMaster response did not contain a task id.",
                code="provider_bad_response",
            )
        return str(task_id)

    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        task_id = quote(job_id, safe="")
        payload = await self._request_json("GET", f"{self._base()}/v1/videos/{task_id}")
        raw_state = str(payload.get("status") or "").strip().lower()
        state_map = {
            "queued": SceneJobState.QUEUED.value,
            "pending": SceneJobState.QUEUED.value,
            "submitted": SceneJobState.QUEUED.value,
            "in_progress": SceneJobState.GENERATING.value,
            "processing": SceneJobState.GENERATING.value,
            "generating": SceneJobState.GENERATING.value,
            "running": SceneJobState.GENERATING.value,
            "completed": SceneJobState.COMPLETED.value,
            "succeeded": SceneJobState.COMPLETED.value,
            "success": SceneJobState.COMPLETED.value,
            "failed": SceneJobState.FAILED.value,
            "error": SceneJobState.FAILED.value,
            "cancelled": SceneJobState.CANCELLED.value,
            "canceled": SceneJobState.CANCELLED.value,
        }
        state = state_map.get(raw_state, SceneJobState.GENERATING.value)
        progress = payload.get("progress")
        if isinstance(progress, (int, float)):
            progress = float(progress)
            if progress > 1:
                progress /= 100
            progress = min(1.0, max(0.0, progress))
        else:
            progress = None

        error = payload.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        error_message = error.get("message") if isinstance(error, dict) else error
        metadata = payload.get("metadata")
        metadata_url = metadata.get("url") if isinstance(metadata, dict) else None
        video_url = payload.get("url") or metadata_url or payload.get("video_url")
        return SceneGenerationStatus(
            job_id=job_id,
            state=state,
            progress=progress,
            video_url=str(video_url) if video_url else None,
            duration_seconds=self._number(payload.get("duration")),
            error_code=str(error_code or raw_state) if state == SceneJobState.FAILED.value else None,
            error_message=str(error_message) if error_message else None,
            raw=payload,
        )

    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        status = await self.get_generation_status(job_id)
        if not status.is_completed:
            raise SceneVideoPermanentError(
                "The APIMaster task is not completed yet.",
                code="provider_job_not_completed",
            )

        task_id = quote(job_id, safe="")
        url = status.video_url or f"{self._base()}/v1/videos/{task_id}/content"
        current_url = self._validate_download_url(url)
        client = self._client()
        try:
            async with client:
                for redirect_count in range(self._MAX_REDIRECTS + 1):
                    headers = {"Accept": "video/*,application/octet-stream"}
                    if self._is_api_host(current_url):
                        headers["Authorization"] = f"Bearer {self.config.api_key}"
                    async with client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or redirect_count >= self._MAX_REDIRECTS:
                                raise SceneVideoPermanentError(
                                    "APIMaster media download returned an invalid redirect.",
                                    code="provider_download_redirect",
                                )
                            current_url = self._validate_download_url(
                                urljoin(current_url, location)
                            )
                            continue
                        if response.status_code != 200:
                            raise self._error_for_status(response.status_code)
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type and not (
                            content_type.startswith("video/")
                            or content_type.startswith("application/octet-stream")
                        ):
                            raise SceneVideoPermanentError(
                                "APIMaster media endpoint did not return a video.",
                                code="provider_download_content_type",
                            )
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if chunk:
                                yield chunk
                        return
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "APIMaster media download timed out.",
                code="provider_timeout",
            ) from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError(
                "APIMaster media download failed.",
                code="provider_unreachable",
            ) from exc

    async def _request_json(self, method: str, url: str, *, json: Any = None) -> dict[str, Any]:
        client = self._client()
        try:
            async with client:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Accept": "application/json",
                        **({"Content-Type": "application/json"} if json is not None else {}),
                    },
                )
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "APIMaster request timed out.",
                code="provider_timeout",
            ) from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError(
                "APIMaster could not be reached.",
                code="provider_unreachable",
            ) from exc

        if response.status_code in (200, 201, 202):
            try:
                payload = response.json()
            except ValueError as exc:
                raise SceneVideoPermanentError(
                    "APIMaster returned a non-JSON response.",
                    code="provider_bad_response",
                ) from exc
            if not isinstance(payload, dict):
                raise SceneVideoPermanentError(
                    "APIMaster returned an unexpected response.",
                    code="provider_bad_response",
                )
            return payload
        raise self._error_for_status(response.status_code)

    def _validate_reference_image_url(self, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SceneVideoPermanentError(
                "The character reference image must be a public HTTPS URL.",
                code="provider_invalid_reference_image",
            )
        return value

    def _validate_download_url(self, value: str) -> str:
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
                "APIMaster returned an unsafe media URL.",
                code="provider_unsafe_media_url",
            )
        allowed = {
            (urlsplit(self._base()).hostname or "").lower().rstrip("."),
            *(item.lower().rstrip(".") for item in self.config.download_allowed_hosts),
        }
        if host not in allowed:
            raise SceneVideoPermanentError(
                "APIMaster media host is not allowlisted.",
                code="provider_media_host_denied",
            )
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and not literal_ip.is_global:
            raise SceneVideoPermanentError(
                "APIMaster returned a non-public media address.",
                code="provider_private_media_ip",
            )
        if self._client_factory is None and literal_ip is None:
            try:
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise SceneVideoTransientError(
                    "APIMaster media hostname could not be resolved.",
                    code="provider_media_dns_failed",
                ) from exc
            if not addresses or any(
                not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global
                for item in addresses
            ):
                raise SceneVideoPermanentError(
                    "APIMaster media hostname resolves to a non-public address.",
                    code="provider_private_media_ip",
                )
        return value

    def _is_api_host(self, value: str) -> bool:
        return (urlsplit(value).hostname or "").lower().rstrip(".") == (
            urlsplit(self._base()).hostname or ""
        ).lower().rstrip(".")

    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:  # noqa: ANN201
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        )

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _error_for_status(status_code: int) -> Exception:
        if status_code == 402:
            return SceneVideoQuotaExhaustedError(
                "APIMaster rejected the request because the account balance or route quota is insufficient.",
                code="provider_insufficient_balance",
            )
        if status_code == 429:
            return SceneVideoTransientError(
                "APIMaster rate limit was reached.",
                code="provider_rate_limited",
            )
        if 500 <= status_code <= 599:
            return SceneVideoTransientError(
                f"APIMaster responded with status {status_code}.",
                code="provider_server_error",
            )
        if status_code in (401, 403):
            return SceneVideoPermanentError(
                "APIMaster rejected the API credential.",
                code="provider_unauthorized",
            )
        return SceneVideoPermanentError(
            f"APIMaster responded with status {status_code}.",
            code="provider_client_error",
        )
