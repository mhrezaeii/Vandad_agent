"""HeyGen provider adapter — official v3 API surface.

Verified against the current HeyGen documentation (developers.heygen.com,
November 2026):

- Auth header: ``X-Api-Key``.
- ``submit_video``          — POST /v3/videos
  body: {type: "avatar", avatar_id, voice_id, script, voice_settings?,
  motion_prompt?, expressiveness?, callback_id?}  ->  {video_id}
  Supports the documented ``Idempotency-Key`` header so a retried request
  cannot start (and bill) a second generation.
- ``video_status``          — GET /v3/videos/{video_id}
  -> {data: {status: pending|processing|completed|failed|cancelled,
  video_url, duration, thumbnail_url, subtitle_url, failure_code,
  failure_message, ...}}
- ``create_photo_avatar``   — POST /v3/avatars
  body: {type: "photo", name, file: {type: "url", url}}
  -> {avatar_item: {id}}  (the look/avatar id used for video generation)
- ``photo_avatar_status``   — GET /v3/avatars/looks/{look_id}
  -> {status: processing|completed|failed, preview_image_url,
  supported_api_engines, error}
- ``list_voices``           — GET /v3/voices

No undocumented endpoints or request fields are used. Older v1/v2 payload
shapes are rejected so a misconfigured workflow fails loudly instead of
silently sending an unsupported body.
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
HEYGEN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
HEYGEN_STATUSES = frozenset({"pending", "processing", "completed", "failed", "cancelled"})
MAX_IDEMPOTENCY_KEY_LENGTH = 255


class HeyGenProvider(ProviderAdapter):
    def __init__(self, client_factory=None) -> None:  # noqa: ANN001 - (timeout)->AsyncClient
        self._client_factory = client_factory

    provider_id = "heygen"
    display_name = "HeyGen"
    description = "Talking-avatar video generation via the official HeyGen v3 API."
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
        params = self._params(context.configuration)
        avatar_id = params.get("avatar_id")
        voice_id = params.get("voice_id")
        script = params.get("script")
        if not avatar_id or not voice_id or not script:
            raise ProviderPermanentError(
                "params.avatar_id, params.voice_id and params.script are required "
                "for submit_video (POST /v3/videos).",
                code="invalid_configuration",
            )
        payload: dict[str, Any] = {
            "type": "avatar",
            "avatar_id": str(avatar_id),
            "voice_id": str(voice_id),
            "script": str(script),
        }
        voice_settings = params.get("voice_settings")
        if isinstance(voice_settings, dict):
            payload["voice_settings"] = voice_settings
        for optional_key in ("motion_prompt", "expressiveness", "callback_id"):
            value = params.get(optional_key)
            if isinstance(value, str) and value.strip():
                payload[optional_key] = value

        idempotency_key = params.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key)
            if not (1 <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH):
                raise ProviderPermanentError(
                    "idempotency_key must be 1-255 characters.", code="invalid_configuration"
                )

        started = time.perf_counter()
        status, payload_json = await self._request(
            context,
            "POST",
            "/v3/videos",
            body=json.dumps(payload).encode("utf-8"),
            idempotency_key=idempotency_key,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if status not in (200, 201, 202):
            self._raise_for_status(status, payload_json, submit=True)
        data = self._data(payload_json)
        video_id = data.get("video_id") or data.get("id") or payload_json.get("video_id")
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
            metadata={
                "provider_job_id": str(video_id),
                "status": str(data.get("status", "pending")),
                "idempotency_key": idempotency_key,
            },
        )

    async def _video_status(self, context: ProviderExecutionContext) -> ProviderResult:
        params = self._params(context.configuration)
        video_id = params.get("provider_job_id")
        if not video_id:
            raise ProviderPermanentError(
                "params.provider_job_id is required for video_status.", code="invalid_configuration"
            )
        status, payload_json = await self._request(
            context, "GET", f"/v3/videos/{video_id}"
        )
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        data = self._data(payload_json)
        heygen_status = str(data.get("status", "")).lower()
        if heygen_status not in HEYGEN_STATUSES:
            raise ProviderPermanentError(
                f"HeyGen returned unknown status {heygen_status!r}.", code="heygen_bad_response"
            )
        if heygen_status in {"failed", "cancelled"}:
            message = data.get("failure_message") or data.get("error")
            if isinstance(message, dict):
                message = message.get("message")
            raise ProviderPermanentError(
                f"HeyGen generation {heygen_status}: {message or 'unknown error'}.",
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
        metadata = {
            "status": "completed",
            "video_url": video_url,
            "duration": data.get("duration"),
            "provider_job_id": str(video_id),
            "thumbnail_url": data.get("thumbnail_url"),
            "subtitle_url": data.get("subtitle_url"),
            "captioned_video_url": data.get("captioned_video_url"),
            "width": data.get("width"),
            "height": data.get("height"),
            "completed_at": data.get("completed_at"),
        }
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Generation completed.",
            metadata={k: v for k, v in metadata.items() if v is not None},
        )

    # ----------------------------------------------------------- photo avatar
    async def _create_photo_avatar(self, context: ProviderExecutionContext) -> ProviderResult:
        """Create a photo avatar via POST /v3/avatars (async look training)."""
        params = self._params(context.configuration)
        name = params.get("name")
        image_url = params.get("image_url")
        if not name or not image_url:
            raise ProviderPermanentError(
                "params.name and params.image_url are required to create a photo avatar.",
                code="invalid_configuration",
            )
        payload = {
            "type": "photo",
            "name": str(name),
            "file": {"type": "url", "url": str(image_url)},
        }
        idempotency_key = params.get("idempotency_key")
        status, payload_json = await self._request(
            context,
            "POST",
            "/v3/avatars",
            body=json.dumps(payload).encode("utf-8"),
            idempotency_key=str(idempotency_key) if idempotency_key else None,
        )
        if status not in (200, 201, 202):
            self._raise_for_status(status, payload_json, submit=True)
        data = self._data(payload_json)
        avatar_item = data.get("avatar_item") or payload_json.get("avatar_item") or {}
        avatar_id = (
            avatar_item.get("id")
            or data.get("id")
            or data.get("avatar_id")
            or payload_json.get("avatar_id")
        )
        if not avatar_id:
            raise ProviderPermanentError(
                "HeyGen returned no avatar/look id for the photo avatar.",
                code="heygen_bad_response",
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Photo avatar created; look training is asynchronous.",
            metadata={
                "provider_avatar_id": str(avatar_id),
                "provider_group_id": data.get("avatar_group_id") or data.get("group_id"),
                "status": str(avatar_item.get("status") or data.get("status") or "processing"),
            },
        )

    async def _photo_avatar_status(self, context: ProviderExecutionContext) -> ProviderResult:
        params = self._params(context.configuration)
        look_id = params.get("provider_avatar_id") or params.get("look_id")
        if not look_id:
            raise ProviderPermanentError(
                "params.provider_avatar_id (look id) is required.", code="invalid_configuration"
            )
        status, payload_json = await self._request(
            context, "GET", f"/v3/avatars/looks/{look_id}"
        )
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        data = self._data(payload_json)
        training_status = str(data.get("status", "")).lower()
        if training_status in {"processing", "training", "pending"}:
            raise ProviderTransientError(
                "Photo avatar training is still in progress.", code="still_training"
            )
        if training_status in {"failed", "error"}:
            error = data.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderPermanentError(
                f"HeyGen photo avatar training failed: {message or 'unknown error'}.",
                code="heygen_training_failed",
            )
        if training_status != "completed":
            raise ProviderPermanentError(
                f"HeyGen returned unknown avatar status {training_status!r}.",
                code="heygen_bad_response",
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Photo avatar ready.",
            metadata={
                "provider_avatar_id": str(look_id),
                "status": "completed",
                "preview_image_url": data.get("preview_image_url"),
                "supported_api_engines": data.get("supported_api_engines"),
            },
        )

    async def _list_voices(
        self, context: ProviderExecutionContext, *, limit: int | None = None
    ) -> ProviderResult:
        status, payload_json = await self._request(context, "GET", "/v3/voices")
        if status != 200:
            self._raise_for_status(status, payload_json, submit=False)
        data = self._data(payload_json)
        voices = data.get("voices") or []
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
            raise ProviderPermanentError(
                "Configuration 'params' must be an object.", code="invalid_configuration"
            )
        return params

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

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
        idempotency_key: str | None = None,
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
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        if status == 409:
            # Documented Idempotency-Key behavior: the original request is still
            # in flight. Wait and retry with the same key; never resubmit anew.
            raise ProviderTransientError(
                "HeyGen request is still in progress for this idempotency key.",
                code="request_in_progress",
            )
        if status in (408, 504):
            raise ProviderTransientError(
                f"HeyGen gateway timeout (status {status}).", code="heygen_gateway_timeout"
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
