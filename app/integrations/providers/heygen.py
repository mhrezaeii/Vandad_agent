"""HeyGen provider adapter (v2 video generation + v1 status polling + v2 photo
avatar creation), implementing the official API surface:

- Auth header: ``X-Api-Key``.
- ``heygen.submit_video``   — POST /v2/video/generate
  body: {video_inputs: [{character: {type, avatar_id|talking_photo_id,
  voice: {type: "text", voice_id, input_text}}}], dimension: {width, height},
  test: bool}  ->  {data: {video_id}}
- ``heygen.video_status``   — GET /v1/video_status.get?video_id=...
  -> {data: {status: pending|processing|completed|failed, video_url, duration,
  error}}
- ``heygen.create_photo_avatar`` — POST /v2/photo_avatar/avatar_group/create
  ({name, image_url}) -> {data: {group_id, avatar_id...}} then POST
  /v2/photo_avatar/train — training is polled via
  GET /v2/photo_avatar/avatar_group/{group_id}.

Endpoint/base configuration: ``url`` (default https://api.heygen.com).
Verification note: HeyGen's docs site is Cloudflare-gated from this network;
the surface above was verified against multiple mirrors of the official
reference (developers.heygen.com/reference/create-video, /reference/get-video,
and the photo-avatar reference) and must be re-verified live with a real key
before first production use.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.integrations.providers.adapter import ProviderAdapter, ProviderCredentialRequirement
from app.integrations.providers.base import (
    ProviderCapability,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)

DEFAULT_HEYGEN_BASE = "https://api.heygen.com"
OPERATIONS = frozenset(
    {"submit_video", "video_status", "create_photo_avatar", "photo_avatar_status", "list_voices"}
)
HEYGEN_TERMINAL_STATUSES = frozenset({"completed", "failed"})


class HeyGenProvider(ProviderAdapter):
    def __init__(self, client_factory=None) -> None:  # noqa: ANN001 - (timeout)->AsyncClient
        self._client_factory = client_factory

    provider_id = "heygen"
    display_name = "HeyGen"
    description = "Talking-avatar video generation via the official HeyGen API."
    capabilities = frozenset({ProviderCapability.EXECUTION, ProviderCapability.HEALTH_CHECK})
    credential_requirements = (
        ProviderCredentialRequirement("api_key", required=True, description="HeyGen X-Api-Key."),
    )
    default_operation_idempotent = False  # submit costs money; callers gate it

    async def execute(self, context: ProviderExecutionContext) -> ProviderResult:
        operation = context.operation
        if operation == "submit_video":
            return await self._submit_video(context)
        if operation == "video_status":
            return await self._video_status(context)
        if operation == "create_photo_avatar":
            return await self._create_photo_avatar(context)
        if operation == "photo_avatar_status":
            return await self._photo_avatar_status(context)
        if operation == "list_voices":
            return await self._list_voices(context)
        raise ProviderPermanentError(
            f"Unknown HeyGen operation {operation!r}.", code="invalid_operation"
        )

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        return await self._list_voices(context, limit=1)

    # ------------------------------------------------------------------ submit
    async def _submit_video(self, context: ProviderExecutionContext) -> ProviderResult:
        config = context.configuration
        params = self._params(config)
        video_input = params.get("video_input")
        if not isinstance(video_input, dict):
            raise ProviderPermanentError(
                "params.video_input is required for submit_video.", code="invalid_configuration"
            )
        dimension = params.get("dimension")
        if not isinstance(dimension, dict):
            raise ProviderPermanentError(
                "params.dimension {width, height} is required.", code="invalid_configuration"
            )
        payload: dict[str, Any] = {
            "video_inputs": [video_input],
            "dimension": {"width": int(dimension["width"]), "height": int(dimension["height"])},
            "test": bool(params.get("test", False)),
        }
        body = json.dumps(payload).encode("utf-8")
        started = time.perf_counter()
        status, payload_json = await self._request(
            context, "POST", "/v2/video/generate", body=body
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if status != 200:
            self._raise_for_status(status, payload_json, submit=True)
        data = payload_json.get("data") or {}
        video_id = data.get("video_id")
        if not video_id:
            raise ProviderPermanentError(
                "HeyGen accepted the request but returned no video_id.",
                code="heygen_bad_response",
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Generation submitted.",
            latency_ms=latency_ms,
            metadata={"provider_job_id": video_id},
        )

    async def _video_status(self, context: ProviderExecutionContext) -> ProviderResult:
        params = self._params(context.configuration)
        video_id = params.get("provider_job_id")
        if not video_id:
            raise ProviderPermanentError(
                "params.provider_job_id is required for video_status.", code="invalid_configuration"
            )
        status, payload_json = await self._request(
            context, "GET", "/v1/video_status.get", query={"video_id": str(video_id)}
        )
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        data = payload_json.get("data") or {}
        heygen_status = str(data.get("status", "")).lower()
        if heygen_status not in {"pending", "processing", "completed", "failed"}:
            raise ProviderPermanentError(
                f"HeyGen returned unknown status {heygen_status!r}.", code="heygen_bad_response"
            )
        if heygen_status == "failed":
            error = data.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderPermanentError(
                f"HeyGen generation failed: {message or 'unknown error'}.",
                code="heygen_generation_failed",
            )
        if heygen_status in {"pending", "processing"}:
            # Non-terminal: the engine retries transient failures with backoff,
            # re-polling from the persisted provider_job_id. Never resubmits.
            raise ProviderTransientError(
                f"HeyGen video is {heygen_status}.", code="still_processing"
            )
        video_url = data.get("video_url")
        if not video_url:
            # completed but CDN link not ready yet — documented HeyGen behavior.
            raise ProviderTransientError(
                "HeyGen completed but the video URL is not ready yet.",
                code="video_url_pending",
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Generation completed.",
            metadata={
                "status": "completed",
                "video_url": video_url,
                "duration": data.get("duration"),
                "provider_job_id": str(video_id),
            },
        )

    # ----------------------------------------------------------- photo avatar
    async def _create_photo_avatar(self, context: ProviderExecutionContext) -> ProviderResult:
        params = self._params(context.configuration)
        name = params.get("name")
        image_url = params.get("image_url")
        if not name or not image_url:
            raise ProviderPermanentError(
                "params.name and params.image_url are required to create a photo avatar.",
                code="invalid_configuration",
            )
        body = json.dumps({"name": str(name), "image_url": str(image_url)}).encode("utf-8")
        status, payload_json = await self._request(
            context, "POST", "/v2/photo_avatar/avatar_group/create", body=body
        )
        if status != 200:
            self._raise_for_status(status, payload_json, submit=True)
        data = payload_json.get("data") or {}
        group_id = data.get("group_id") or data.get("id")
        if not group_id:
            raise ProviderPermanentError(
                "HeyGen returned no avatar group id.", code="heygen_bad_response"
            )
        # Kick off training; completion is polled via photo_avatar_status.
        train_body = json.dumps({"group_id": str(group_id)}).encode("utf-8")
        train_status, _train_payload = await self._request(
            context, "POST", "/v2/photo_avatar/train", body=train_body
        )
        if train_status not in (200, 201):
            self._raise_for_status(train_status, _train_payload, submit=True)
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Photo avatar group created; training started.",
            metadata={"provider_group_id": str(group_id)},
        )

    async def _photo_avatar_status(self, context: ProviderExecutionContext) -> ProviderResult:
        params = self._params(context.configuration)
        group_id = params.get("provider_group_id")
        if not group_id:
            raise ProviderPermanentError(
                "params.provider_group_id is required.", code="invalid_configuration"
            )
        status, payload_json = await self._request(
            context, "GET", f"/v2/photo_avatar/avatar_group/{group_id}"
        )
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        data = payload_json.get("data") or {}
        training_status = str(data.get("status", "")).lower()
        # Look for the first avatar_id inside the group's looks.
        avatar_id = None
        looks = data.get("looks") or data.get("photo_avatars") or []
        if isinstance(looks, list):
            for look in looks:
                if isinstance(look, dict) and look.get("avatar_id"):
                    avatar_id = str(look["avatar_id"])
                    break
        if training_status in {"processing", "training", "pending"}:
            raise ProviderTransientError(
                "Photo avatar training is still in progress.", code="still_training"
            )
        if training_status in {"failed", "error"}:
            raise ProviderPermanentError(
                "HeyGen photo avatar training failed.", code="heygen_training_failed"
            )
        if not avatar_id:
            raise ProviderTransientError(
                "Training finished but no avatar_id is available yet.",
                code="avatar_id_pending",
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Photo avatar ready.",
            metadata={"provider_group_id": str(group_id), "provider_avatar_id": avatar_id},
        )

    async def _list_voices(
        self, context: ProviderExecutionContext, *, limit: int | None = None
    ) -> ProviderResult:
        status, payload_json = await self._request(context, "GET", "/v2/voices")
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        voices = (payload_json.get("data") or {}).get("voices") or []
        if limit is not None:
            voices = voices[:limit]
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message=f"{len(voices)} voices available.",
            metadata={"voices": voices},
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _params(config: dict[str, Any]) -> dict[str, Any]:
        params = config.get("params", {})
        if not isinstance(params, dict):
            raise ProviderPermanentError("Configuration 'params' must be an object.", code="invalid_configuration")
        return params

    @staticmethod
    def _base_url(config: dict[str, Any]) -> str:
        return str(config.get("url") or DEFAULT_HEYGEN_BASE).rstrip("/")

    async def _request(
        self,
        context: ProviderExecutionContext,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if context.credential is None or context.credential.is_empty():
            raise ProviderPermanentError(
                "HeyGen requires an api_key credential on the connection.",
                code="heygen_credential_missing",
            )
        base_url = self._base_url(context.configuration)
        headers = {
            "X-Api-Key": context.credential.reveal(),
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        client = (
            self._client_factory(context.timeout_seconds)
            if self._client_factory is not None
            else httpx.AsyncClient(timeout=context.timeout_seconds)
        )
        try:
            async with client as http_client:
                response = await http_client.request(
                    method, f"{base_url}{path}", content=body, headers=headers, params=query
                )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                f"HeyGen request timed out after {context.timeout_seconds:.0f}s.",
                code="provider_timeout",
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                "HeyGen could not be reached.", code="provider_unreachable"
            ) from exc
        try:
            payload_json = response.json()
        except json.JSONDecodeError:
            payload_json = {}
        return response.status_code, payload_json

    @staticmethod
    def _raise_for_status(status: int, payload: dict[str, Any], *, submit: bool) -> None:
        message = ""
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", ""))
        elif isinstance(error, str):
            message = error
        if not message:
            message = str(payload.get("message", ""))[:200]
        if status == 401:
            raise ProviderPermanentError(
                "HeyGen rejected the API key.", code="heygen_auth_failed"
            )
        if status == 429:
            raise ProviderTransientError("HeyGen rate limit reached.", code="heygen_rate_limited")
        if status >= 500:
            raise ProviderTransientError(
                f"HeyGen responded with status {status}.", code="heygen_server_error"
            )
        raise ProviderPermanentError(
            f"HeyGen rejected the request (status {status})." + (f" {message}" if message else ""),
            code="heygen_client_error",
        )
