"""LLM provider adapter tests (httpx.MockTransport — no live calls)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.integrations.providers.base import (
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderTransientError,
)
from app.integrations.providers.llm_openai import LlmOpenAiProvider


def _context(
    operation: str, configuration: dict, *, credential_text: str = "secret-key-123"
) -> ProviderExecutionContext:
    from app.security.secret_value import SecretValue

    return ProviderExecutionContext(
        provider_id="test",
        connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        platform_id=uuid.uuid4(),
        configuration=configuration,
        credential=SecretValue(credential_text),
        credential_type="api_key",
        secret_reference="vault://test/key",
        timeout_seconds=10.0,
        operation=operation,
    )


def _mock_factory(handler) -> httpx.AsyncClient:
    return lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=timeout
    )


# --------------------------------------------------------------------- LLM
def _llm_handler(response_body: dict, expect_auth: str = "secret-key-123"):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode("utf-8")) if request.content else None
        return httpx.Response(200, json=response_body)

    handler.captured = captured  # type: ignore[attr-defined]
    return handler


async def test_llm_complete_returns_content_and_usage():
    response_body = {
        "choices": [{"message": {"content": "{\"title\": \"پایش امنیتی\"}"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        "model": "gpt-test",
    }
    handler = _llm_handler(response_body)
    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    context = _context("execute", {
        "url": "https://llm.example.com/v1",
        "model": "gpt-test",
        "params": {"user": "یک اسکریپت بنویس"},
    })
    result = await adapter.execute(context)
    assert result.success
    assert "پایش امنیتی" in result.metadata["content"]
    assert result.metadata["usage"]["total_tokens"] == 150
    assert handler.captured["auth"] == "Bearer secret-key-123"
    assert handler.captured["body"]["model"] == "gpt-test"


async def test_llm_credential_never_in_result():
    handler = _llm_handler({"choices": [{"message": {"content": "ok"}}]})
    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    result = await adapter.execute(_context("execute", {
        "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
    }))
    assert "secret-key-123" not in json.dumps(result.metadata)


async def test_llm_rate_limit_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError):
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))


async def test_llm_auth_failure_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderPermanentError) as exc_info:
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))
    assert exc_info.value.code == "llm_http_401"


async def test_llm_missing_model_is_permanent():
    adapter = LlmOpenAiProvider()
    with pytest.raises(ProviderPermanentError):
        await adapter.execute(_context("execute", {"url": "https://llm.example.com/v1"}))





async def test_llm_connect_error_is_safe_to_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError) as exc_info:
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))
    assert exc_info.value.code == "provider_unreachable"


async def test_llm_read_timeout_is_ambiguous_and_not_safe_to_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out", request=request)

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError) as exc_info:
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))
    assert exc_info.value.code == "provider_timeout"


async def test_llm_server_error_is_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError) as exc_info:
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))
    assert exc_info.value.code == "llm_server_error"


async def test_llm_model_not_found_is_a_definitive_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderPermanentError) as exc_info:
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "missing", "params": {"user": "x"},
        }))
    assert exc_info.value.code == "llm_http_404"


def test_llm_provider_declares_connection_validation_capability():
    """validate_connection delegates to the live health probe, so the adapter
    must advertise CONNECTION_VALIDATION; otherwise LLM connections can never
    be activated through the validation service (regression: activation
    rejected with provider_unsupported despite a working validate_connection).
    """
    from app.integrations.providers.base import ProviderCapability

    adapter = LlmOpenAiProvider()
    assert ProviderCapability.CONNECTION_VALIDATION in adapter.capabilities


async def test_llm_validate_connection_uses_health_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/models")
        assert request.headers.get("authorization") == "Bearer secret-key-123"
        return httpx.Response(200, json={"data": []})

    adapter = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    result = await adapter.validate_connection(_context("validate", {
        "url": "https://llm.example.com/v1",
    }))
    assert result.success is True


async def test_llm_retries_with_max_completion_tokens_on_unsupported_parameter():
    """Newer OpenAI models reject `max_tokens`; the adapter falls back once.

    Regression: knowledge/scenario extraction 400-failed against models that
    require `max_completion_tokens`, so no LLM operation could succeed.
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={
                "error": {
                    "message": "Unsupported parameter: 'max_tokens' is not supported with this model.",
                    "type": "invalid_request_error",
                    "code": "unsupported_parameter",
                }
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "پاسخ"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "gpt-6-luna",
        })

    provider = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    result = await provider.execute(_context(
        "execute",
        {
            "url": "https://api.openai.com/v1",
            "model": "gpt-6-luna",
            "params": {"user": "سلام"},
            "max_tokens": 2000,
        },
    ))

    assert result.success is True
    assert len(calls) == 2
    assert "max_tokens" in calls[0] and "max_completion_tokens" not in calls[0]
    assert calls[1].get("max_completion_tokens") == 2000
    assert "max_tokens" not in calls[1]


async def test_llm_drops_temperature_on_unsupported_value_and_retries():
    """Models that only support the default temperature get a corrected retry."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={
                "error": {
                    "message": "Unsupported value: 'temperature' does not support 0.1 with this model.",
                    "type": "invalid_request_error",
                    "code": "unsupported_value",
                }
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "پاسخ"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "gpt-6-luna",
        })

    provider = LlmOpenAiProvider(client_factory=_mock_factory(handler))
    result = await provider.execute(_context(
        "execute",
        {
            "url": "https://api.openai.com/v1",
            "model": "gpt-6-luna",
            "params": {"user": "سلام"},
            "temperature": 0.1,
            "max_tokens": 2000,
        },
    ))

    assert result.success is True
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]
    # The max_tokens correction never fired: this model accepted the parameter.
    assert "max_tokens" in calls[1]
