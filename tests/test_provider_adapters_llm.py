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
