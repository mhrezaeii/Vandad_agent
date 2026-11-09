"""LLM + HeyGen provider adapter tests (httpx.MockTransport — no live calls,
no paid requests). Mocks validate adapter logic, not live integration."""

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
from app.integrations.providers.heygen import HeyGenProvider
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
    with pytest.raises(ProviderPermanentError):
        await adapter.execute(_context("execute", {
            "url": "https://llm.example.com/v1", "model": "m", "params": {"user": "x"},
        }))


async def test_llm_missing_model_is_permanent():
    adapter = LlmOpenAiProvider()
    with pytest.raises(ProviderPermanentError):
        await adapter.execute(_context("execute", {"url": "https://llm.example.com/v1"}))


# ------------------------------------------------------------------- HeyGen
def _heygen_handler(routes: list[tuple[str, str, dict, int]]):
    """routes: (method, path-fragment, response_body, status)"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url),
                      "body": json.loads(request.content.decode()) if request.content else None,
                      "api_key": request.headers.get("x-api-key"),
                      "idempotency_key": request.headers.get("idempotency-key")})
        for method, fragment, body, status_code in routes:
            if request.method == method and fragment in str(request.url):
                return httpx.Response(status_code, json=body)
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


async def test_heygen_submit_video_returns_job_id():
    routes = [("POST", "/v3/videos", {"video_id": "hg-123"}, 200)]
    handler = _heygen_handler(routes)
    adapter = HeyGenProvider(client_factory=_mock_factory(handler))
    result = await adapter.execute(_context("submit_video", {
        "url": "https://api.heygen.com",
        "params": {
            "avatar_id": "look-1",
            "voice_id": "v-1",
            "script": "سلام دنیا",
            "idempotency_key": "vandad-video-1",
            "motion_prompt": "subtle confident gestures",
        },
    }))
    assert result.metadata["provider_job_id"] == "hg-123"
    body = handler.calls[0]["body"]
    assert body["type"] == "avatar"
    assert body["avatar_id"] == "look-1"
    assert body["voice_id"] == "v-1"
    assert body["script"] == "سلام دنیا"
    assert body["motion_prompt"] == "subtle confident gestures"
    assert handler.calls[0]["api_key"] == "secret-key-123"
    assert handler.calls[0]["idempotency_key"] == "vandad-video-1"


async def test_heygen_submit_video_requires_v3_fields():
    adapter = HeyGenProvider()
    with pytest.raises(ProviderPermanentError):
        await adapter.execute(_context("submit_video", {
            "url": "https://api.heygen.com",
            # legacy v2 shape must be rejected, not silently accepted
            "params": {"video_input": {"character": {"avatar_id": "av-1"}}},
        }))


async def test_heygen_idempotency_conflict_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "request_in_progress"}})

    adapter = HeyGenProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError) as excinfo:
        await adapter.execute(_context("submit_video", {
            "url": "https://api.heygen.com",
            "params": {"avatar_id": "look-1", "voice_id": "v-1", "script": "x",
                       "idempotency_key": "vandad-video-1"},
        }))
    assert excinfo.value.code == "request_in_progress"


async def test_heygen_status_processing_is_transient_completed_has_url():
    HeyGenProvider()
    processing = _context("video_status", {
        "params": {"provider_job_id": "hg-1"},
        "url": "https://api.heygen.com",
    })

    def processing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100, "data": {"status": "processing"}})

    adapter2 = HeyGenProvider(client_factory=_mock_factory(processing_handler))
    with pytest.raises(ProviderTransientError) as excinfo:
        await adapter2.execute(processing)
    assert excinfo.value.code == "still_processing"

    completed_handler = _heygen_handler([
        ("GET", "/v3/videos/hg-1",
         {"data": {"status": "completed",
                   "video_url": "https://cdn.heygen.video/x.mp4",
                   "duration": 31.2,
                   "width": 1080, "height": 1920}}, 200),
    ])
    adapter3 = HeyGenProvider(client_factory=_mock_factory(completed_handler))
    result = await adapter3.execute(processing)
    assert result.metadata["status"] == "completed"
    assert result.metadata["video_url"] == "https://cdn.heygen.video/x.mp4"
    assert result.metadata["width"] == 1080


async def test_heygen_failed_generation_is_permanent():
    handler = _heygen_handler([
        ("GET", "/v3/videos/hg-1",
         {"data": {"status": "failed",
                   "failure_code": "generation_failed",
                   "failure_message": "render exploded"}}, 200),
    ])
    adapter = HeyGenProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderPermanentError) as excinfo:
        await adapter.execute(_context("video_status", {
            "params": {"provider_job_id": "hg-1"}, "url": "https://api.heygen.com",
        }))
    assert excinfo.value.code == "heygen_generation_failed"


async def test_heygen_photo_avatar_creation_and_training():
    creation_handler = _heygen_handler([
        ("POST", "/v3/avatars",
         {"avatar_item": {"id": "look-persian-77"}}, 200),
    ])
    adapter = HeyGenProvider(client_factory=_mock_factory(creation_handler))
    result = await adapter.execute(_context("create_photo_avatar", {
        "url": "https://api.heygen.com",
        "params": {"name": "Vandefence character",
                   "image_url": "https://vandefence.ir/portrait.jpg",
                   "idempotency_key": "vandad-avatar-1"},
    }))
    assert result.metadata["provider_avatar_id"] == "look-persian-77"
    body = creation_handler.calls[0]["body"]
    assert body["type"] == "photo"
    assert body["file"] == {"type": "url", "url": "https://vandefence.ir/portrait.jpg"}
    assert creation_handler.calls[0]["idempotency_key"] == "vandad-avatar-1"

    status_handler = _heygen_handler([
        ("GET", "/v3/avatars/looks/look-persian-77",
         {"status": "completed",
          "preview_image_url": "https://cdn.heygen.ai/look.png",
          "supported_api_engines": ["avatar_iv"]}, 200),
    ])
    adapter2 = HeyGenProvider(client_factory=_mock_factory(status_handler))
    result2 = await adapter2.execute(_context("photo_avatar_status", {
        "url": "https://api.heygen.com",
        "params": {"provider_avatar_id": "look-persian-77"},
    }))
    assert result2.metadata["provider_avatar_id"] == "look-persian-77"
    assert result2.metadata["status"] == "completed"


async def test_heygen_photo_avatar_training_in_progress_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "processing"})

    adapter = HeyGenProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderTransientError) as excinfo:
        await adapter.execute(_context("photo_avatar_status", {
            "url": "https://api.heygen.com",
            "params": {"provider_avatar_id": "look-1"},
        }))
    assert excinfo.value.code == "still_training"


async def test_heygen_auth_failure_is_permanent():
    handler = _heygen_handler([
        ("GET", "/v3/voices", {"code": 401, "message": "unauthorized"}, 401),
    ])
    adapter = HeyGenProvider(client_factory=_mock_factory(handler))
    with pytest.raises(ProviderPermanentError) as excinfo:
        await adapter.validate_connection(
            _context("list_voices", {"url": "https://api.heygen.com"})
        )
    assert excinfo.value.code == "heygen_auth_failed"
