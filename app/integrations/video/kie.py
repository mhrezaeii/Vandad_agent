"""KIE.ai Grok Imagine Video 1.5 Preview scene-video adapter.

KIE uses a createTask/recordInfo API and returns an MP4 with native audio.
The adapter deliberately never retries a billable POST after an ambiguous
transport/server failure. A durable submission fence is enforced by
SceneVideoService before calling this adapter.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from app.integrations.video.base import (
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoHealthResult,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
)
from app.security.redaction import redact_text

_CREDITS_PER_SECOND = {
    "480p": Decimal("2.4"),
    "720p": Decimal("4.5"),
    "1080p": Decimal("8"),
}
_STATUS_MAP = {
    "waiting": SceneJobState.QUEUED.value,
    "queuing": SceneJobState.QUEUED.value,
    "generating": SceneJobState.GENERATING.value,
    "success": SceneJobState.COMPLETED.value,
    "fail": SceneJobState.FAILED.value,
}


@dataclass(frozen=True)
class KIEConfig:
    api_key: str
    base_url: str = "https://api.kie.ai"
    model: str = "grok-imagine-video-1-5-preview"
    resolution: str = "480p"
    aspect_ratio: str = "9:16"
    timeout_seconds: float = 60.0
    poll_interval_seconds: float = 3.0
    max_poll_attempts: int = 300
    price_per_credit_usd: float = 0.005
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
            or parsed.path not in ("", "/")
        ):
            raise ValueError("KIE base_url must be an HTTPS origin without userinfo, path, query or fragment.")
        if not self.api_key.strip():
            raise ValueError("KIE api_key must not be blank.")
        if self.model != "grok-imagine-video-1-5-preview":
            raise ValueError("KIE model must be grok-imagine-video-1-5-preview.")
        if self.resolution not in _CREDITS_PER_SECOND:
            raise ValueError("KIE resolution must be 480p, 720p or 1080p.")
        if self.aspect_ratio not in {"1:1", "16:9", "9:16", "3:2", "2:3", "auto"}:
            raise ValueError("KIE aspect_ratio is not supported.")
        if self.timeout_seconds <= 0 or self.poll_interval_seconds < 1 or self.max_poll_attempts < 1:
            raise ValueError("KIE timeout and polling settings are invalid.")
        if not math.isfinite(self.price_per_credit_usd) or not 0 < self.price_per_credit_usd <= 1:
            raise ValueError("KIE price_per_credit_usd must be a positive, finite amount.")
        for host in self.download_allowed_hosts:
            if not host or host != host.strip() or "/" in host or ":" in host:
                raise ValueError("KIE download allowlist must contain exact hostnames.")


class KIESceneVideoProvider(SceneVideoProvider):
    provider_id = "kie"
    display_name = "KIE.ai Grok Imagine Video 1.5 Preview"
    requires_network = True
    supports_health_check = True
    supports_reference_image = True
    supports_native_audio = True
    input_modalities = ("text", "image")
    supported_modalities = ("video", "audio")
    supported_resolutions = ("480p", "720p", "1080p")
    supported_aspect_ratios = ("1:1", "16:9", "9:16", "3:2", "2:3", "auto")
    min_duration_seconds = 1
    max_duration_seconds = 15
    max_prompt_characters = 4096
    max_reference_images = 1

    def __init__(self, config: KIEConfig, *, client_factory=None) -> None:
        self.config = config
        self.model_id = config.model
        self._client_factory = client_factory
        self._status_cache: dict[str, tuple[float, SceneGenerationStatus]] = {}
        self.supported_resolutions = (config.resolution,)
        self.supported_aspect_ratios = (config.aspect_ratio,)

    async def check_health(self) -> SceneVideoHealthResult:
        started = time.perf_counter()
        try:
            balance = await self.get_credit_balance()
            return SceneVideoHealthResult(
                self.provider_id,
                balance is not None and balance >= 0,
                "KIE authenticated credit endpoint is reachable."
                if balance is not None and balance >= 0
                else "KIE returned an invalid credit balance.",
                round((time.perf_counter() - started) * 1000, 2),
            )
        except (SceneVideoTransientError, SceneVideoPermanentError) as exc:
            return SceneVideoHealthResult(
                self.provider_id,
                False,
                str(exc),
                round((time.perf_counter() - started) * 1000, 2),
            )

    async def get_credit_balance(self) -> Decimal:
        payload = await self._request_json("GET", "/api/v1/chat/credit")
        value = payload
        if isinstance(payload, dict):
            value = payload.get("credits", payload.get("balance", payload.get("credit")))
        try:
            balance = Decimal(str(value))
        except Exception as exc:
            raise SceneVideoPermanentError(
                "KIE returned an invalid account-credit response.", code="provider_bad_response"
            ) from exc
        if not balance.is_finite() or balance < 0:
            raise SceneVideoPermanentError(
                "KIE returned an invalid account-credit response.", code="provider_bad_response"
            )
        return balance

    def estimate_credits(self, duration_seconds: float) -> Decimal:
        duration = Decimal(str(duration_seconds))
        return _CREDITS_PER_SECOND[self.config.resolution] * duration

    def estimate_cost_usd(self, duration_seconds: float) -> str:
        amount = self.estimate_credits(duration_seconds) * Decimal(str(self.config.price_per_credit_usd))
        return f"{amount:.6f}"

    async def generate_video(self, request: SceneVideoRequest) -> str:
        duration = request.duration_seconds
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or int(duration) != duration
            or not 1 <= int(duration) <= 15
        ):
            raise SceneVideoPermanentError(
                "KIE Grok Imagine Video 1.5 requires an integer duration from 1 through 15 seconds.",
                code="provider_invalid_duration",
            )
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > self.max_prompt_characters:
            raise SceneVideoPermanentError(
                "KIE requires a prompt containing 1 to 4096 characters.",
                code="provider_invalid_prompt",
            )
        if request.aspect_ratio not in self.supported_aspect_ratios:
            raise SceneVideoPermanentError(
                "The requested aspect ratio does not match the configured KIE aspect ratio.",
                code="provider_invalid_aspect_ratio",
            )

        required_credits = self.estimate_credits(float(duration))
        try:
            balance = await self.get_credit_balance()
        except SceneVideoTransientError as exc:
            raise SceneVideoTransientError(
                "KIE credit preflight failed before task submission.",
                code="provider_preflight_transient",
            ) from exc
        if balance < required_credits:
            raise SceneVideoQuotaExhaustedError(
                f"KIE has {balance} credits but this scene requires {required_credits}.",
                code="provider_insufficient_credits",
            )

        input_data = {
            "prompt": prompt,
            "aspect_ratio": self.config.aspect_ratio,
            "resolution": self.config.resolution,
            "duration": int(duration),
        }
        if request.reference_image_url:
            input_data["image_urls"] = [request.reference_image_url]
        body = {"model": self.config.model, "input": input_data}
        try:
            payload = await self._request_json("POST", "/api/v1/jobs/createTask", json=body)
        except SceneVideoTransientError as exc:
            if exc.code == "provider_rate_limited":
                raise
            raise SceneVideoTransientError(
                "KIE task submission outcome is unknown. Check KIE task history before retrying.",
                code="provider_submission_unknown",
            ) from exc

        task_id = payload.get("taskId") if isinstance(payload, dict) else None
        if not isinstance(task_id, (str, int)) or not str(task_id).strip():
            raise SceneVideoTransientError(
                "KIE accepted a response without a usable task ID; reconcile task history before retrying.",
                code="provider_submission_unknown",
            )
        return f"kie:{task_id}"

    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        task_id = self._task_id(job_id)
        payload = await self._request_json(
            "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id}
        )
        if not isinstance(payload, dict):
            raise SceneVideoPermanentError(
                "KIE returned an unexpected task-status response.", code="provider_bad_response"
            )
        raw_state = str(payload.get("state") or "").strip().lower()
        state = _STATUS_MAP.get(raw_state, SceneJobState.GENERATING.value)
        result = payload.get("resultJson")
        result_data: dict = {}
        if isinstance(result, str) and result.strip():
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    result_data = parsed
            except ValueError:
                raise SceneVideoPermanentError(
                    "KIE returned invalid result metadata.", code="provider_bad_response"
                ) from None
        elif isinstance(result, dict):
            result_data = result

        video_url = self._output_url(result_data) if state == SceneJobState.COMPLETED.value else None
        if state == SceneJobState.COMPLETED.value and not video_url:
            state = SceneJobState.GENERATING.value

        raw = dict(payload)
        raw["model"] = payload.get("model") or self.config.model
        credits = self._number(payload.get("creditsConsumed"))
        if credits is not None:
            raw["credits_consumed"] = str(credits)
            raw["cost_usd"] = str(
                (credits * Decimal(str(self.config.price_per_credit_usd))).quantize(Decimal("0.000001"))
            )
            raw["currency"] = "USD"
        progress = self._number(payload.get("progress"))
        if progress is not None:
            progress = max(0.0, min(1.0, progress / 100.0 if progress > 1 else progress))
        duration_value = self._number(payload.get("duration"))
        fail_code = str(payload.get("failCode") or "").strip() or None
        fail_message = str(payload.get("failMsg") or "").strip()
        status = SceneGenerationStatus(
            job_id=job_id,
            state=state,
            progress=(
                1.0 if state == SceneJobState.COMPLETED.value
                else (float(progress) if progress is not None else None)
            ),
            video_url=video_url,
            duration_seconds=float(duration_value) if duration_value is not None else None,
            error_code=(f"kie_{fail_code}" if fail_code else "kie_generation_failed")
            if state == SceneJobState.FAILED.value else None,
            error_message=redact_text(fail_message)[:500] if fail_message else None,
            raw=raw,
        )
        self._status_cache[self._task_id(job_id)] = (time.monotonic(), status)
        return status

    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        cached = self._status_cache.get(self._task_id(job_id))
        if cached and time.monotonic() - cached[0] <= 10:
            status = cached[1]
        else:
            status = await self.get_generation_status(job_id)
        if not status.is_completed or not status.video_url:
            raise SceneVideoPermanentError(
                "KIE task is not completed or has no output URL.",
                code="provider_job_not_completed",
            )
        url = self._validate_download_url(status.video_url)
        client = self._client()
        try:
            async with client:
                async with client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise SceneVideoPermanentError(
                            "KIE output redirected to an unverified host.",
                            code="provider_download_redirect",
                        )
                    if response.status_code != 200:
                        raise self._error_for_status(response.status_code)
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not (
                        content_type.startswith("video/") or "application/octet-stream" in content_type
                    ):
                        raise SceneVideoPermanentError(
                            "KIE output endpoint did not return video bytes.",
                            code="provider_download_content_type",
                        )
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            yield chunk
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError("KIE video download timed out.", code="provider_timeout") from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError("KIE video download failed.", code="provider_unreachable") from exc

    async def _request_json(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None):
        method = method.upper()
        attempts = 3 if method == "GET" else 1
        delay = self.config.poll_interval_seconds
        response = None
        for attempt in range(attempts):
            client = self._client()
            try:
                async with client:
                    response = await client.request(
                        method,
                        f"{self.config.base_url.rstrip('/')}{path}",
                        json=json,
                        params=params,
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}",
                            "Accept": "application/json",
                            **({"Content-Type": "application/json"} if json is not None else {}),
                        },
                    )
            except httpx.TimeoutException as exc:
                if method == "GET" and attempt + 1 < attempts:
                    await asyncio.sleep(delay)
                    delay = min(15.0, delay * 2)
                    continue
                raise SceneVideoTransientError("KIE request timed out.", code="provider_timeout") from exc
            except httpx.TransportError as exc:
                if method == "GET" and attempt + 1 < attempts:
                    await asyncio.sleep(delay)
                    delay = min(15.0, delay * 2)
                    continue
                raise SceneVideoTransientError("KIE could not be reached.", code="provider_unreachable") from exc
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                await asyncio.sleep(delay)
                delay = min(15.0, delay * 2)
                continue
            break

        assert response is not None
        if response.status_code == 402:
            raise SceneVideoQuotaExhaustedError(
                "KIE reports insufficient credits.", code="provider_insufficient_credits"
            )
        if response.status_code == 429:
            raise SceneVideoTransientError("KIE rate limit was reached.", code="provider_rate_limited")
        if response.status_code >= 500:
            raise SceneVideoTransientError(
                f"KIE returned HTTP {response.status_code}.", code="provider_server_error"
            )
        if response.status_code in (401, 403):
            raise SceneVideoPermanentError("KIE rejected the API credential.", code="provider_unauthorized")
        if not 200 <= response.status_code < 300:
            raise SceneVideoPermanentError(
                f"KIE request failed with HTTP {response.status_code}.", code="provider_request_failed"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SceneVideoPermanentError("KIE returned invalid JSON.", code="provider_bad_response") from exc
        if not isinstance(body, dict):
            raise SceneVideoPermanentError("KIE returned an unexpected response.", code="provider_bad_response")
        code = body.get("code")
        data = body.get("data")
        # recordInfo's published example uses code 505 with msg=success; accept
        # that response only when it contains a structured task payload.
        if code == 402:
            raise SceneVideoQuotaExhaustedError(
                "KIE reports insufficient credits.", code="provider_insufficient_credits"
            )
        if code == 429:
            raise SceneVideoTransientError("KIE rate limit was reached.", code="provider_rate_limited")
        if code not in (None, 200, 505):
            message = redact_text(str(body.get("msg") or ""))[:200]
            raise SceneVideoPermanentError(
                f"KIE API rejected the request{(': ' + message) if message else '.'}",
                code="provider_request_failed",
            )
        if data is None:
            raise SceneVideoPermanentError("KIE response omitted data.", code="provider_bad_response")
        return data

    def _output_url(self, result: dict) -> str | None:
        urls = result.get("resultUrls") or result.get("result_urls")
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            return None
        value = urls[0]
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SceneVideoPermanentError("KIE returned an unsafe output URL.", code="provider_unsafe_media_url")
        return value

    def _validate_download_url(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise SceneVideoPermanentError(
                "KIE media URL contains an invalid port.", code="provider_unsafe_media_url"
            ) from exc
        allowed = {item.lower().rstrip(".") for item in self.config.download_allowed_hosts}
        if (
            parsed.scheme != "https" or not host or parsed.username or parsed.password
            or port not in (None, 443) or parsed.fragment
            or (allowed and host not in allowed)
        ):
            raise SceneVideoPermanentError("KIE media URL is not allowed.", code="provider_media_host_denied")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise SceneVideoPermanentError("KIE media host is not public.", code="provider_private_media_ip")
        if self._client_factory is None and literal is None:
            try:
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise SceneVideoTransientError(
                    "KIE media hostname could not be resolved.", code="provider_media_dns_failed"
                ) from exc
            if not addresses or any(
                not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses
            ):
                raise SceneVideoPermanentError(
                    "KIE media hostname resolves to a non-public address.",
                    code="provider_private_media_ip",
                )
        return value

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False)

    def _task_id(self, job_id: str) -> str:
        value = job_id.split(":", 1)[1] if job_id.startswith("kie:") else job_id
        if not value or len(value) > 256:
            raise SceneVideoPermanentError("Empty or invalid KIE task identifier.", code="provider_job_not_found")
        return value

    @staticmethod
    def _number(value) -> Decimal | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        return result if result.is_finite() else None

    @staticmethod
    def _error_for_status(status_code: int) -> SceneVideoPermanentError | SceneVideoTransientError:
        if status_code == 429 or status_code >= 500:
            return SceneVideoTransientError(
                f"KIE media download failed with HTTP {status_code}.",
                code="provider_rate_limited" if status_code == 429 else "provider_server_error",
            )
        return SceneVideoPermanentError(
            f"KIE media download failed with HTTP {status_code}.",
            code="provider_download_failed",
        )
