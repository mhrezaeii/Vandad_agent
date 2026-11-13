"""WaveSpeedAI LTX-2.3 asynchronous scene-video adapter.

WaveSpeed's documented contract supports text-to-video generation and prediction
polling. Final composition and quality review are delegated only when an
explicitly configured generic HTTP provider exposes both capabilities.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.integrations.video.base import (
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoCompositionRequest,
    SceneVideoHealthResult,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoQualityRequest,
    SceneVideoQualityResult,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
)

_TERMINAL_FAILURES = {"failed", "cancelled", "canceled", "timeout", "deleted"}
_STATUS_MAP = {
    "created": SceneJobState.QUEUED.value,
    "queued": SceneJobState.QUEUED.value,
    "pending": SceneJobState.QUEUED.value,
    "processing": SceneJobState.GENERATING.value,
    "running": SceneJobState.GENERATING.value,
    "in_progress": SceneJobState.GENERATING.value,
    "completed": SceneJobState.COMPLETED.value,
    "failed": SceneJobState.FAILED.value,
    "cancelled": SceneJobState.CANCELLED.value,
    "canceled": SceneJobState.CANCELLED.value,
    "timeout": SceneJobState.FAILED.value,
    "deleted": SceneJobState.FAILED.value,
}
_PRICE_PER_FIVE_SECONDS = {"480p": 0.10, "720p": 0.15, "1080p": 0.20}


@dataclass(frozen=True)
class WaveSpeedConfig:
    api_key: str
    base_url: str = "https://api.wavespeed.ai/api/v3"
    model: str = "wavespeed-ai/ltx-2.3/text-to-video"
    resolution: str = "480p"
    aspect_ratio: str = "9:16"
    duration_seconds: int | None = None
    timeout_seconds: float = 60.0
    poll_interval_seconds: float = 2.0
    max_poll_attempts: int = 600
    download_allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("WaveSpeed base_url must be HTTPS without userinfo, query or fragment.")
        if not self.api_key.strip():
            raise ValueError("WaveSpeed api_key must not be blank.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){1,2}", self.model):
            raise ValueError("WaveSpeed model must be a valid model identifier.")
        if self.resolution not in _PRICE_PER_FIVE_SECONDS:
            raise ValueError("WaveSpeed resolution must be 480p, 720p or 1080p.")
        if self.aspect_ratio not in {"16:9", "9:16"}:
            raise ValueError("WaveSpeed aspect_ratio must be 16:9 or 9:16.")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or not 5 <= self.duration_seconds <= 20
        ):
            raise ValueError("WaveSpeed duration_seconds must be an integer from 5 through 20.")
        if self.timeout_seconds <= 0 or self.poll_interval_seconds < 2 or self.max_poll_attempts < 1:
            raise ValueError("WaveSpeed timeout and polling settings are invalid.")
        for host in self.download_allowed_hosts:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                raise ValueError("WaveSpeed download allowlist must contain exact hostnames.")


class WaveSpeedSceneVideoProvider(SceneVideoProvider):
    provider_id = "wavespeed"
    display_name = "WaveSpeedAI LTX-2.3"
    requires_network = True
    supports_health_check = True
    supports_seed = True
    input_modalities = ("text",)
    supported_modalities = ("video",)
    supported_resolutions = ("480p", "720p", "1080p")
    supported_aspect_ratios = ("16:9", "9:16")
    min_duration_seconds = 5
    max_duration_seconds = 20
    max_reference_images = 0

    def __init__(self, config: WaveSpeedConfig, *, client_factory=None,
                 composition_provider: SceneVideoProvider | None = None) -> None:
        self.config = config
        self.model_id = config.model
        self._client_factory = client_factory
        self._composition_provider = composition_provider
        self._status_cache: dict[str, tuple[float, SceneGenerationStatus]] = {}
        self.supported_aspect_ratios = (config.aspect_ratio,)
        self.supported_durations = None
        if config.duration_seconds is not None:
            # A configured fixed duration overrides storyboard clip lengths.
            self.min_duration_seconds = None
            self.max_duration_seconds = None
        self.supports_composition = bool(
            composition_provider and composition_provider.supports_composition
        )
        self.supports_quality_review = bool(
            composition_provider and composition_provider.supports_quality_review
        )

    async def check_health(self) -> SceneVideoHealthResult:
        started = time.perf_counter()
        try:
            payload = await self._request_json("GET", f"{self._base()}/balance")
            balance = self._number(payload.get("balance"))
            if balance is None or balance < 0:
                raise SceneVideoPermanentError(
                    "WaveSpeed returned an invalid account-balance response.", code="provider_bad_response"
                )
            return SceneVideoHealthResult(
                self.provider_id, True, "WaveSpeed authenticated balance probe succeeded.",
                round((time.perf_counter() - started) * 1000, 2),
            )
        except (SceneVideoTransientError, SceneVideoPermanentError) as exc:
            return SceneVideoHealthResult(
                self.provider_id, False, str(exc),
                round((time.perf_counter() - started) * 1000, 2),
            )

    async def generate_video(self, request: SceneVideoRequest) -> str:
        duration = self.config.duration_seconds if self.config.duration_seconds is not None else request.duration_seconds
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration)) or int(duration) != duration
                or not 5 <= int(duration) <= 20):
            raise SceneVideoPermanentError(
                "WaveSpeed LTX-2.3 duration must be 5, 10, 15, or 20 seconds.",
                code="provider_invalid_duration",
            )
        if not request.prompt.strip():
            raise SceneVideoPermanentError(
                "WaveSpeed requires a non-empty prompt within the configured model limit.",
                code="provider_invalid_prompt",
            )
        if request.reference_image_url:
            raise SceneVideoPermanentError(
                "WaveSpeed LTX-2.3 text-to-video does not accept reference images.",
                code="provider_reference_image_unsupported",
            )
        # The adapter's aspect ratio is an operator setting; the scene service
        # currently supplies its contract default on every request.
        aspect_ratio = self.config.aspect_ratio
        if aspect_ratio not in self.supported_aspect_ratios:
            raise SceneVideoPermanentError(
                "WaveSpeed LTX-2.3 supports only 16:9 and 9:16 aspect ratios.",
                code="provider_invalid_aspect_ratio",
            )
        body: dict[str, Any] = {
            "prompt": request.prompt.strip(),
            "resolution": self.config.resolution,
            "aspect_ratio": aspect_ratio,
            "duration": int(duration),
        }
        if request.seed is not None:
            if isinstance(request.seed, bool) or not isinstance(request.seed, int):
                raise SceneVideoPermanentError("WaveSpeed seed must be an integer.", code="provider_invalid_seed")
            body["seed"] = request.seed

        try:
            balance_payload = await self._request_json("GET", f"{self._base()}/balance")
        except SceneVideoTransientError as exc:
            raise SceneVideoTransientError(
                "WaveSpeed balance preflight failed before any prediction was submitted.",
                code="provider_preflight_transient",
            ) from exc
        balance = self._number(balance_payload.get("balance"))
        estimated_cost = float(self.estimate_cost_usd(float(duration)))
        if balance is None or balance < estimated_cost:
            raise SceneVideoQuotaExhaustedError(
                "WaveSpeed account balance is below the estimated cost for this scene.",
                code="provider_insufficient_balance",
            )

        try:
            payload = await self._request_json("POST", f"{self._base()}/{self.config.model}", json=body)
        except SceneVideoTransientError as exc:
            # A documented 429 is an explicit rejection. A timeout, dropped
            # connection, or server error may have accepted a billable POST.
            if exc.code == "provider_rate_limited":
                raise
            raise SceneVideoTransientError(
                "WaveSpeed submission outcome is unknown. Prediction history must be checked before any retry.",
                code="provider_submission_unknown",
            ) from exc

        prediction_id = payload.get("id")
        if not isinstance(prediction_id, (str, int)) or not str(prediction_id).strip():
            raise SceneVideoTransientError(
                "WaveSpeed accepted a response without a usable prediction ID; reconcile prediction history before retrying.",
                code="provider_submission_unknown",
            )
        return f"wavespeed:{prediction_id}"

    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        if job_id.startswith("compose:"):
            return await self._composition().get_generation_status(job_id.split(":", 1)[1])
        prediction_id = self._prediction_id(job_id)
        payload = await self._request_json(
            "GET", f"{self._base()}/predictions/{quote(prediction_id, safe='')}/result"
        )
        raw_status = str(payload.get("status") or "").strip().lower()
        state = _STATUS_MAP.get(raw_status, SceneJobState.GENERATING.value)
        output_url = self._output_url(payload) if state == SceneJobState.COMPLETED.value else None
        raw = dict(payload)
        raw["model"] = payload.get("model") or self.config.model
        reported = self._reported_cost(payload)
        if reported is None and state == SceneJobState.COMPLETED.value:
            reported = await self._get_billings_cost(prediction_id)
        if reported is not None:
            raw["cost_usd"] = reported
            raw["currency"] = "USD"
        status = SceneGenerationStatus(
            job_id=job_id,
            state=state,
            progress=1.0 if state == SceneJobState.COMPLETED.value else None,
            video_url=output_url,
            duration_seconds=self._number(payload.get("duration")),
            error_code=f"wavespeed_{raw_status}" if state == SceneJobState.FAILED.value else None,
            error_message=str(payload.get("error") or "")[:500] or None,
            raw=raw,
        )
        self._status_cache[prediction_id] = (time.monotonic(), status)
        return status

    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        if job_id.startswith("compose:"):
            async for chunk in self._composition().retrieve_video(job_id.split(":", 1)[1]):
                yield chunk
            return
        prediction_id = self._prediction_id(job_id)
        cached = self._status_cache.get(prediction_id)
        if cached is not None and time.monotonic() - cached[0] <= 10.0:
            status = cached[1]
        else:
            status = await self.get_generation_status(job_id)
        if not status.is_completed or not status.video_url:
            raise SceneVideoPermanentError(
                "WaveSpeed prediction is not completed or has no output URL.",
                code="provider_job_not_completed",
            )
        url = self._validate_download_url(status.video_url)
        client = self._client()
        try:
            async with client:
                async with client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise SceneVideoPermanentError(
                            "WaveSpeed output redirected to an unverified host.",
                            code="provider_download_redirect",
                        )
                    if response.status_code != 200:
                        raise self._error_for_status(response.status_code)
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not (
                        content_type.startswith("video/") or "application/octet-stream" in content_type
                    ):
                        raise SceneVideoPermanentError(
                            "WaveSpeed output endpoint did not return video bytes.",
                            code="provider_download_content_type",
                        )
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            yield chunk
        except httpx.TimeoutException as exc:
            raise SceneVideoTransientError("WaveSpeed video download timed out.", code="provider_timeout") from exc
        except httpx.TransportError as exc:
            raise SceneVideoTransientError("WaveSpeed video download failed.", code="provider_unreachable") from exc

    async def compose_video(self, request: SceneVideoCompositionRequest) -> str:
        provider = self._composition()
        job_id = await provider.compose_video(request)
        return f"compose:{job_id}"

    async def review_video(self, request: SceneVideoQualityRequest) -> SceneVideoQualityResult:
        return await self._composition().review_video(request)

    async def _request_json(self, method: str, url: str, *, json: Any = None) -> Any:
        method = method.upper()
        max_get_attempts = 5 if method == "GET" else 1
        delay = self.config.poll_interval_seconds
        response: httpx.Response | None = None

        for attempt in range(max_get_attempts):
            client = self._client()
            try:
                async with client:
                    response = await client.request(
                        method, url, json=json,
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}",
                            "Accept": "application/json",
                            **({"Content-Type": "application/json"} if json is not None else {}),
                        },
                    )
            except httpx.TimeoutException as exc:
                if method == "GET" and attempt + 1 < max_get_attempts:
                    await asyncio.sleep(min(delay, 30.0))
                    delay = min(30.0, delay * 2)
                    continue
                raise SceneVideoTransientError(
                    "WaveSpeed request timed out.", code="provider_timeout"
                ) from exc
            except httpx.TransportError as exc:
                if method == "GET" and attempt + 1 < max_get_attempts:
                    await asyncio.sleep(min(delay, 30.0))
                    delay = min(30.0, delay * 2)
                    continue
                raise SceneVideoTransientError(
                    "WaveSpeed could not be reached.", code="provider_unreachable"
                ) from exc

            if method == "GET" and (
                response.status_code == 429 or response.status_code in (500, 502, 503, 504)
            ) and attempt + 1 < max_get_attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(30.0, max(2.0, float(retry_after))) if retry_after else min(30.0, delay * 2)
                except ValueError:
                    delay = min(30.0, delay * 2)
                await asyncio.sleep(delay)
                continue
            break

        assert response is not None
        if response.status_code == 402:
            raise SceneVideoQuotaExhaustedError(
                "WaveSpeed reports insufficient account balance.", code="provider_insufficient_balance"
            )
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise SceneVideoTransientError(
                f"WaveSpeed returned HTTP {response.status_code}.",
                code="provider_rate_limited" if response.status_code == 429 else "provider_server_error",
            )
        if response.status_code in (401, 403):
            raise SceneVideoPermanentError(
                "WaveSpeed rejected the API credential or IP allowlist.", code="provider_unauthorized"
            )
        if not 200 <= response.status_code < 300:
            raise SceneVideoPermanentError(
                f"WaveSpeed request failed (HTTP {response.status_code}).", code="provider_request_failed"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SceneVideoPermanentError(
                "WaveSpeed returned invalid JSON.", code="provider_bad_response"
            ) from exc
        if not isinstance(body, dict):
            raise SceneVideoPermanentError(
                "WaveSpeed returned an unexpected response.", code="provider_bad_response"
            )
        if body.get("code") not in (None, 200):
            raise SceneVideoPermanentError(
                "WaveSpeed returned an API-level error.", code="provider_request_failed"
            )
        data = body.get("data", body)
        if not isinstance(data, (dict, list)):
            raise SceneVideoPermanentError(
                "WaveSpeed response data is invalid.", code="provider_bad_response"
            )
        return data

    def _output_url(self, payload: dict[str, Any]) -> str | None:
        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return None
        first = outputs[0]
        if isinstance(first, dict):
            first = first.get("url") or first.get("video_url")
        if not isinstance(first, str):
            return None
        parsed = urlsplit(first)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SceneVideoPermanentError("WaveSpeed returned an unsafe output URL.", code="provider_unsafe_media_url")
        return first

    def _validate_download_url(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = {item.lower().rstrip(".") for item in self.config.download_allowed_hosts}
        try:
            port = parsed.port
        except ValueError as exc:
            raise SceneVideoPermanentError(
                "WaveSpeed media URL contains an invalid port.", code="provider_unsafe_media_url"
            ) from exc
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or port not in (None, 443) or parsed.fragment or host not in allowed):
            raise SceneVideoPermanentError(
                "WaveSpeed media host is not in the exact HTTPS download allowlist.",
                code="provider_media_host_denied",
            )
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and not literal_ip.is_global:
            raise SceneVideoPermanentError("WaveSpeed media host is not public.", code="provider_private_media_ip")
        if self._client_factory is None and literal_ip is None:
            try:
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise SceneVideoTransientError("WaveSpeed media hostname could not be resolved.", code="provider_media_dns_failed") from exc
            if not addresses or any(
                not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses
            ):
                raise SceneVideoPermanentError("WaveSpeed media hostname resolves to a non-public address.", code="provider_private_media_ip")
        return value

    def _composition(self) -> SceneVideoProvider:
        if self._composition_provider is None:
            raise SceneVideoPermanentError(
                "WaveSpeed generation is configured, but a separate composition and quality-review provider is not configured.",
                code="provider_composition_unsupported",
            )
        return self._composition_provider

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(self.config.timeout_seconds)
        return httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False)

    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    @staticmethod
    def _prediction_id(job_id: str) -> str:
        if not job_id.startswith("wavespeed:"):
            raise SceneVideoPermanentError("Unknown WaveSpeed job identifier.", code="provider_job_not_found")
        value = job_id.split(":", 1)[1]
        if not value:
            raise SceneVideoPermanentError("Empty WaveSpeed prediction identifier.", code="provider_job_not_found")
        return value

    def estimate_cost_usd(self, duration_seconds: float) -> str:
        """Return the published LTX-2.3 estimate; never treat it as actual spend."""
        configured_duration = self.config.duration_seconds or duration_seconds
        amount = _PRICE_PER_FIVE_SECONDS[self.config.resolution] * float(configured_duration) / 5.0
        return f"{amount:.6f}"

    async def _get_billings_cost(self, prediction_id: str) -> str | None:
        """Read actual itemized USD charges when the key has billing permission."""
        payload = {"prediction_uuids": [prediction_id], "page": 1, "page_size": 100}
        for attempt, delay in enumerate((0.0, 2.0, 5.0)):
            if delay:
                await asyncio.sleep(delay)
            try:
                data = await self._request_json(
                    "POST", f"{self._base()}/billings/search", json=payload
                )
            except SceneVideoPermanentError:
                # Billing access is optional; a Developer key may run predictions
                # but cannot read itemized charges. Never fail video generation
                # merely because billing reconciliation is unavailable.
                return None
            except SceneVideoTransientError:
                if attempt == 2:
                    return None
                continue
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return None
            total = Decimal("0")
            found = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                prediction = item.get("prediction")
                if not isinstance(prediction, dict) or str(prediction.get("uuid")) != prediction_id:
                    continue
                price = self._number(item.get("price"))
                if price is None:
                    continue
                billing_type = str(item.get("billing_type") or "").lower()
                if billing_type == "deduct":
                    total += Decimal(str(price))
                    found = True
                elif billing_type in {"refund", "credit"}:
                    total -= Decimal(str(price))
                    found = True
            if found:
                return f"{max(total, Decimal('0')):.6f}"
        return None

    @staticmethod
    def _reported_cost(payload: dict[str, Any]) -> str | None:
        for key in ("cost_usd", "total_cost_usd"):
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(amount) and amount >= 0:
                    return str(value)
        if str(payload.get("currency") or "").upper() == "USD":
            for key in ("cost", "total_cost"):
                value = payload.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    try:
                        amount = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(amount) and amount >= 0:
                        return str(value)
        billing = payload.get("billing")
        if isinstance(billing, dict):
            for key in ("cost_usd", "total_cost_usd"):
                value = billing.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    try:
                        amount = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(amount) and amount >= 0:
                        return str(value)
        return None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _error_for_status(status_code: int) -> Exception:
        if status_code == 402:
            return SceneVideoQuotaExhaustedError("WaveSpeed account balance is insufficient.", code="provider_insufficient_balance")
        if status_code == 429 or 500 <= status_code <= 599:
            return SceneVideoTransientError(f"WaveSpeed media returned HTTP {status_code}.", code="provider_download_transient")
        return SceneVideoPermanentError(f"WaveSpeed media download failed (HTTP {status_code}).", code="provider_download_failed")
