"""8Scale Wan 2.2 14B text-to-video provider.

Cheap hosted Wan 2.2 (verified ~$0.015 per 5s 480p clip, silent output).
The provider's docs sit behind its dashboard, so the response parsing is
deliberately tolerant: it accepts the documented field names plus the common
async job-wrapper shapes ({id}/{task_id}, {data:{...}}, {output(s)}), and
supports both synchronous (video URL in the submit response) and async
(job id + polling) flows. Once a real API key is configured, any shape
mismatch is fixed in one place: :func:`_extract_video_url` /
:func:`_extract_job_id`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
class EightScaleConfig:
    api_key: str
    base_url: str = "https://8scale.run"
    model_path: str = "wan-2.2/14b/text-to-video"
    resolution: str = "480p"
    aspect_ratio: str = "9:16"
    timeout_seconds: float = 60.0
    poll_interval_seconds: float = 5.0
    max_poll_attempts: int = 240
    download_allowed_hosts: tuple[str, ...] = ()
    price_per_second_usd: float | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("8Scale base_url must be HTTPS.")
        if not self.api_key.strip():
            raise ValueError("8Scale api_key must not be blank.")
        if not re.fullmatch(r"[a-z0-9./-]+", self.model_path):
            raise ValueError("8Scale model_path looks invalid.")
        if self.price_per_second_usd is not None and not (0 <= self.price_per_second_usd <= 10):
            raise ValueError("8Scale price_per_second_usd must be a sane rate.")


def _deep_find_first(payload: Any, keys: tuple[str, ...]) -> Any:
    """Depth-first search for the first non-empty value under any of *keys*."""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                nested = _deep_find_first(value[0], keys)
                if nested:
                    return nested
        for value in payload.values():
            nested = _deep_find_first(value, keys)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _deep_find_first(item, keys)
            if nested:
                return nested
    return None


_URL_KEYS = ("video_url", "videoUrl", "output_url", "outputUrl", "url", "output", "outputs", "result_url", "file_url")
_JOB_KEYS = ("id", "job_id", "jobId", "task_id", "taskId", "request_id", "requestId")


def extract_video_url(payload: Any) -> str | None:
    value = _deep_find_first(payload, _URL_KEYS)
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")) and ".mp4" in value.lower():
        return value
    return None


def extract_job_id(payload: Any) -> str | None:
    value = _deep_find_first(payload, _JOB_KEYS)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{4,120}", value):
        return value
    return None


def extract_status(payload: Any) -> str | None:
    value = _deep_find_first(payload, ("status", "state"))
    return str(value).strip().lower() if isinstance(value, str) else None


class EightScaleSceneVideoProvider(SceneVideoProvider):
    provider_id = "8scale"
    display_name = "8Scale Wan 2.2 14B"
    model_id = "wan-2.2/14b/text-to-video"
    requires_network = True
    supports_health_check = True
    input_modalities = ("text",)
    supported_modalities = ("video",)
    supported_resolutions = ("480p", "720p")
    supported_aspect_ratios = ("16:9", "9:16")
    min_duration_seconds = 3
    max_duration_seconds = 30

    def __init__(self, config: EightScaleConfig, *, client_factory=None) -> None:
        self.config = config
        self.model_id = config.model_path
        self._client_factory = client_factory

    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> Any:
        try:
            async with self._client() as client:
                response = await client.request(
                    method, f"{self._base()}{path}", headers=self._headers(), json=json_body
                )
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError(
                "8Scale request timed out.", code="provider_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise SceneVideoTransientError(
                "8Scale could not be reached.", code="provider_unreachable"
            ) from exc
        if response.status_code == 401 or response.status_code == 403:
            raise SceneVideoPermanentError(
                "8Scale rejected the API key.", code="provider_unauthorized"
            )
        if response.status_code == 402:
            raise SceneVideoQuotaExhaustedError(
                "8Scale credits are exhausted; top up or claim free generations.",
                code="provider_insufficient_balance",
            )
        if response.status_code == 429:
            raise SceneVideoTransientError(
                "8Scale rate limit reached.", code="provider_rate_limited"
            )
        if 500 <= response.status_code <= 599:
            raise SceneVideoTransientError(
                f"8Scale server error (HTTP {response.status_code}).", code="provider_server_error"
            )
        if not 200 <= response.status_code < 300:
            detail = ""
            try:
                detail = str(response.json().get("message") or response.json().get("error") or "")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise SceneVideoPermanentError(
                f"8Scale request failed (HTTP {response.status_code}){(': ' + detail) if detail else '.'}",
                code="provider_request_failed",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SceneVideoPermanentError(
                "8Scale returned non-JSON content.", code="provider_bad_response"
            ) from exc

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(timeout=self.config.timeout_seconds)

    # ------------------------------------------------------------------ ABC
    PROMPT_LIMIT = 2000

    @staticmethod
    def _cap_prompt(prompt: str, limit: int = 2000) -> str:
        """Fit the composed prompt under the provider's 2000-char limit.

        The composed prompt puts the scene type/style first and the concrete
        visual prompt last; keep both ends and drop the middle when over.
        """
        text = prompt.strip()
        if len(text) <= limit:
            return text
        keep = limit - 20
        head = keep * 30 // 100
        tail = keep - head
        return text[:head] + chr(10) + "..." + chr(10) + text[-tail:]

    async def generate_video(self, request: SceneVideoRequest) -> str:
        body: dict[str, Any] = {
            "prompt": self._cap_prompt(request.prompt),
            "resolution": self.config.resolution,
            "aspect_ratio": self.config.aspect_ratio,
            "duration": int(request.duration_seconds),
        }
        payload = await self._request(
            "POST", f"/{self.config.model_path}", json_body=body
        )
        # Synchronous flow: the video URL may come straight back.
        video_url = extract_video_url(payload)
        if video_url:
            return f"8scale:{video_url}"
        job_id = extract_job_id(payload)
        if job_id:
            return f"8scale:{job_id}"
        raise SceneVideoPermanentError(
            "8Scale response contained neither a video URL nor a job id.",
            code="provider_bad_response",
        )

    async def check_health(self):
        """Liveness probe: any HTTP answer (even 4xx) proves reachability."""
        from app.integrations.video.base import SceneVideoHealthResult

        try:
            await self._request("GET", f"/{self.config.model_path}")
        except SceneVideoTransientError:
            raise
        except SceneVideoPermanentError:
            pass  # a 4xx still proves the endpoint answered with our auth
        return SceneVideoHealthResult(
            provider_id=self.provider_id, healthy=True,
            message="8Scale API reachable.", latency_ms=None,
        )

    async def _fetch_result_manifest(self, out_url: str) -> dict[str, Any] | None:
        """Fetch a small out.json document (failure details / nested outputs)."""
        for attempt in range(3):
            try:
                async with self._client() as client:
                    response = await client.get(out_url, timeout=120.0)
                if response.status_code != 200:
                    return None
                data = response.json()
                return data if isinstance(data, dict) else None
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    return None
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        raw = job_id.removeprefix("8scale:")
        # Synchronous submissions embed the video URL directly.
        if raw.lower().startswith(("http://", "https://")):
            return SceneGenerationStatus(
                job_id=job_id, state=SceneJobState.COMPLETED.value, video_url=raw,
            )
        payload = await self._request("GET", f"/status/{raw}")
        status = str(payload.get("status") or "").strip().upper()
        output = payload.get("output") if isinstance(payload.get("output"), str) else None
        if status == "COMPLETED" and output:
            # Completed jobs put the presigned MP4 URL straight in `output`;
            # failed jobs point at a small out.json error document.
            if "out.json" not in output.lower():
                return SceneGenerationStatus(
                    job_id=job_id, state=SceneJobState.COMPLETED.value, video_url=output,
                )
            manifest = await self._fetch_result_manifest(output)
            if manifest and "detail" not in manifest:
                video_url = manifest.get("_video_url") or extract_video_url(manifest)
                if video_url:
                    return SceneGenerationStatus(
                        job_id=job_id, state=SceneJobState.COMPLETED.value, video_url=video_url,
                    )
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            message = "8Scale reported generation failure."
            if output and "out.json" in output.lower():
                manifest = await self._fetch_result_manifest(output)
                if manifest and "detail" in manifest:
                    detail = manifest["detail"]
                    message = (json.dumps(detail, ensure_ascii=False)[:300]
                               if not isinstance(detail, str) else detail[:300])
            return SceneGenerationStatus(
                job_id=job_id, state=SceneJobState.FAILED.value,
                error_code="provider_generation_failed", error_message=message,
            )
        return SceneGenerationStatus(job_id=job_id, state=SceneJobState.GENERATING.value)

    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        url = job_id.removeprefix("8scale:")
        if not url.lower().startswith(("http://", "https://")):
            payload = await self._request("GET", f"/status/{url}")
            url = payload.get("output") if isinstance(payload.get("output"), str) else ""
        if not url:
            raise SceneVideoPermanentError(
                "No video URL is available for this 8Scale job.",
                code="provider_video_url_missing",
            )
        if "out.json" in url.lower():
            manifest = await self._fetch_result_manifest(url)
            url = (extract_video_url(manifest) if manifest else "") or ""
        if not url:
            raise SceneVideoPermanentError(
                "No video URL is available for this 8Scale job.",
                code="provider_video_url_missing",
            )
        self._validate_download_host(url)
        allowed_hosts = self.config.download_allowed_hosts
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if allowed_hosts and host not in {h.lower().rstrip(".") for h in allowed_hosts}:
            raise SceneVideoPermanentError(
                "The 8Scale video host is not in the configured download allowlist.",
                code="provider_download_host_blocked",
            )
        # Resumable download: the VPN relay drops long transfers mid-body,
        # so each break resumes from the byte offset already yielded. Retry even
        # when the first connection fails before yielding any bytes.
        received = 0
        for attempt in range(5):
            try:
                headers = {"Range": f"bytes={received}-"} if received else {}
                async with self._client() as client:
                    async with client.stream(
                        "GET", url, headers=headers, timeout=300.0, follow_redirects=False
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            raise SceneVideoPermanentError(
                                "The 8Scale video URL redirected; refusing an unverified host.",
                                code="provider_download_redirect",
                            )
                        if response.status_code in (408, 425, 429) or response.status_code >= 500:
                            response.raise_for_status()
                        if received and response.status_code != 206:
                            raise SceneVideoPermanentError(
                                "The 8Scale host did not honor the resume Range request.",
                                code="provider_download_range_unsupported",
                            )
                        if response.status_code not in (200, 206):
                            raise SceneVideoPermanentError(
                                f"Video download failed (HTTP {response.status_code}).",
                                code="provider_download_failed",
                            )
                        if received:
                            content_range = response.headers.get("content-range", "")
                            if not content_range.startswith(f"bytes {received}-"):
                                raise SceneVideoPermanentError(
                                    "The 8Scale host returned an invalid resumed byte range.",
                                    code="provider_download_range_mismatch",
                                )
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if chunk:
                                received += len(chunk)
                                yield chunk
                        return
            except httpx.HTTPError as exc:
                if attempt == 4:
                    raise SceneVideoTransientError(
                        f"Video download failed after {received} bytes ({type(exc).__name__}).",
                        code="provider_download_failed",
                    ) from exc
                await asyncio.sleep(2 * (attempt + 1))

    def _validate_download_host(self, url: str) -> None:  # pragma: no cover - trivial guard
        if not url.lower().startswith("https://"):
            raise SceneVideoPermanentError(
                "The 8Scale video URL is not HTTPS.", code="provider_download_insecure"
            )

    def estimate_cost_usd(self, duration_seconds: float) -> str:
        if self.config.price_per_second_usd is not None:
            return f"{float(self.config.price_per_second_usd) * float(duration_seconds):.6f}"
        # Published rate: $0.015 per 5s 480p clip.
        return f"{0.003 * float(duration_seconds):.6f}"
