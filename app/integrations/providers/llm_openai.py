"""OpenAI-compatible LLM provider adapter.

Works against any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI,
Azure OpenAI gateways, vLLM, OpenRouter, ...). Configuration lives on the
platform connection; the API key is resolved through the credential boundary
and is never logged.

Operations:
- ``llm.complete``          — one structured chat completion (JSON mode optional)
- ``llm.health_check``      — cheap GET /models probe

Output metadata (never contains the key):
- ``content``  — the assistant message text
- ``usage``    — {prompt_tokens, completion_tokens, total_tokens} when reported
- ``model``    — the model that actually served the request
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

DEFAULT_LLM_TIMEOUT_SECONDS = 120
MAX_MESSAGES = 20
MAX_COMPLETION_TOKENS_LIMIT = 32768


class LlmOpenAiProvider(ProviderAdapter):
    def __init__(self, client_factory=None) -> None:  # noqa: ANN001 - (timeout)->AsyncClient
        self._client_factory = client_factory

    provider_id = "llm_openai"
    display_name = "OpenAI-compatible LLM"
    description = "Chat completions against any OpenAI-compatible endpoint."
    capabilities = frozenset({ProviderCapability.EXECUTION, ProviderCapability.HEALTH_CHECK})
    credential_requirements = (
        ProviderCredentialRequirement("api_key", required=True, description="Bearer API key."),
    )
    default_operation_idempotent = True  # completions are stateless GET-like calls

    async def execute(self, context: ProviderExecutionContext) -> ProviderResult:
        config = context.configuration
        base_url = self._base_url(config)
        model = config.get("model")
        if not model:
            raise ProviderPermanentError(
                "Configuration must contain a 'model'.", code="invalid_configuration"
            )

        messages = self._build_messages(context, config)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if config.get("response_format") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if config.get("temperature") is not None:
            payload["temperature"] = float(config["temperature"])
        max_tokens = config.get("max_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = min(int(max_tokens), MAX_COMPLETION_TOKENS_LIMIT)

        body = json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if context.credential is not None and not context.credential.is_empty():
            headers["Authorization"] = f"Bearer {context.credential.reveal()}"

        started = time.perf_counter()
        status, response_payload = await self._post_json(
            context, f"{base_url}/chat/completions", body, headers
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        if status == 429:
            raise ProviderTransientError("LLM rate limit reached.", code="llm_rate_limited")
        if 500 <= status <= 599:
            raise ProviderTransientError(
                f"LLM endpoint responded with status {status}.", code="llm_server_error"
            )
        if status != 200:
            raise ProviderPermanentError(
                f"LLM endpoint responded with status {status}.", code="llm_client_error"
            )

        try:
            choice = response_payload["choices"][0]["message"]["content"]
            usage = response_payload.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderPermanentError(
                "LLM response shape was unexpected.", code="llm_bad_response"
            ) from exc

        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Completion generated.",
            latency_ms=latency_ms,
            metadata={
                "content": choice,
                "model": response_payload.get("model", model),
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                },
            },
        )

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        return await self.check_health(context)

    async def check_health(self, context: ProviderExecutionContext) -> ProviderResult:
        base_url = self._base_url(context.configuration)
        headers: dict[str, str] = {}
        if context.credential is not None and not context.credential.is_empty():
            headers["Authorization"] = f"Bearer {context.credential.reveal()}"
        status, _payload = await self._post_json(
            context, f"{base_url}/models", b"", headers, method="GET"
        )
        if status == 200:
            return ProviderResult(
                provider_id=self.provider_id, success=True, message="LLM endpoint reachable."
            )
        raise ProviderTransientError(
            f"LLM endpoint responded with status {status}.", code="llm_server_error"
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _base_url(config: dict[str, Any]) -> str:
        url = str(config.get("url", "")).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ProviderPermanentError(
                "Configuration must contain an 'url' with an http(s) LLM endpoint.",
                code="invalid_configuration",
            )
        return url

    @staticmethod
    def _build_messages(context: ProviderExecutionContext, config: dict[str, Any]) -> list[dict[str, str]]:
        params = config.get("params", {})
        # Workflow templates naturally produce params as a JSON string; accept
        # both a pre-parsed object and a string form.
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError as exc:
                raise ProviderPermanentError(
                    "params must be a JSON object (or a JSON-encoded string).",
                    code="invalid_configuration",
                ) from exc
        if not isinstance(params, dict):
            raise ProviderPermanentError("Configuration 'params' must be an object.", code="invalid_configuration")
        system = params.get("system") or config.get("system")
        user = params.get("user")
        if not isinstance(user, str) or not user.strip():
            raise ProviderPermanentError("A 'user' prompt is required.", code="invalid_configuration")
        messages: list[dict[str, str]] = []
        if isinstance(system, str) and system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        if len(messages) > MAX_MESSAGES:
            raise ProviderPermanentError("Too many messages.", code="invalid_configuration")
        return messages

    async def _post_json(
        self,
        context: ProviderExecutionContext,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        method: str = "POST",
    ) -> tuple[int, dict[str, Any]]:
        client = (
            self._client_factory(context.timeout_seconds)
            if self._client_factory is not None
            else httpx.AsyncClient(timeout=context.timeout_seconds)
        )
        try:
            async with client as http_client:
                response = await http_client.request(method, url, content=body or None, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                f"LLM request timed out after {context.timeout_seconds:.0f}s.", code="provider_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                "LLM endpoint could not be reached.", code="provider_unreachable"
            ) from exc
        if response.status_code == 200 or method == "GET":
            try:
                return response.status_code, response.json()
            except json.JSONDecodeError as exc:
                if response.status_code == 200:
                    raise ProviderPermanentError(
                        "LLM returned non-JSON content.", code="llm_bad_response"
                    ) from exc
                return response.status_code, {}
        return response.status_code, {}
