from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.video import (
    build_scene_video_registry,
    GenericHttpConfig,
    GenericHttpSceneVideoProvider,
    SceneJobState,
    SceneVideoCompositionRequest,
    SceneVideoPermanentError,
    SceneVideoQualityRequest,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
    WaveSpeedConfig,
    WaveSpeedSceneVideoProvider,
)


MODEL = "wavespeed-ai/ltx-2.3/text-to-video"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128


def _request(*, duration: float = 5.0, aspect_ratio: str = "9:16") -> SceneVideoRequest:
    return SceneVideoRequest(
        scene_id="project:0",
        prompt="A cinematic vertical product shot with a slow camera move.",
        duration_seconds=duration,
        aspect_ratio=aspect_ratio,
        idempotency_key="stable-scene-key",
    )


def _provider(handler, *, hosts=("cdn.example.test",), composition_provider=None):
    config = WaveSpeedConfig(
        api_key="test-key",
        model=MODEL,
        resolution="480p",
        aspect_ratio="9:16",
        download_allowed_hosts=hosts,
    )
    return WaveSpeedSceneVideoProvider(
        config,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
        composition_provider=composition_provider,
    )


async def test_wavespeed_health_submit_poll_and_download_contract():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET" and request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"data": {"balance": 5.0}})
        if request.method == "POST" and request.url.path.endswith("/" + MODEL):
            body = json.loads(request.content)
            assert body == {
                "prompt": "A cinematic vertical product shot with a slow camera move.",
                "resolution": "480p",
                "aspect_ratio": "9:16",
                "duration": 5,
            }
            return httpx.Response(200, json={"data": {"id": "pred-123", "status": "created"}})
        if request.method == "GET" and request.url.path.endswith("/predictions/pred-123/result"):
            return httpx.Response(200, json={"data": {
                "id": "pred-123", "status": "completed",
                "outputs": ["https://cdn.example.test/video.mp4"],
                "duration": 5, "cost_usd": "0.10",
            }})
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/video.mp4":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    provider = _provider(handler)
    health = await provider.check_health()
    assert health.healthy is True

    prediction_id = await provider.generate_video(_request())
    assert prediction_id == "wavespeed:pred-123"

    status = await provider.get_generation_status(prediction_id)
    assert status.state == SceneJobState.COMPLETED.value
    assert status.video_url == "https://cdn.example.test/video.mp4"
    assert status.raw["cost_usd"] == "0.10"

    chunks = [chunk async for chunk in provider.retrieve_video(prediction_id)]
    assert b"".join(chunks) == MP4
    assert ("POST", f"https://api.wavespeed.ai/api/v3/{MODEL}") in calls


@pytest.mark.parametrize("duration", [4, 21, 5.5, 0])
async def test_wavespeed_rejects_unsupported_durations_before_submit(duration):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"id": "should-not-exist"}})

    provider = _provider(handler)
    with pytest.raises(SceneVideoPermanentError, match="duration"):
        await provider.generate_video(_request(duration=duration))
    assert calls == []


async def test_wavespeed_ambiguous_submit_is_never_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            calls += 1
            raise httpx.ConnectError("connection dropped after request write", request=request)
        return httpx.Response(200, json={"data": {"balance": 5.0}})

    provider = _provider(handler)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_submission_unknown"
    assert calls == 1


async def test_wavespeed_explicit_rate_limit_is_retryable_not_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"data": [{"model_id": MODEL}]})

    provider = _provider(handler)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_rate_limited"


async def test_wavespeed_download_requires_exact_https_host_allowlist():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/predictions/pred-123/result"):
            return httpx.Response(200, json={"data": {
                "status": "completed", "outputs": ["https://evil.example/video.mp4"]
            }})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    provider = _provider(handler)
    with pytest.raises(SceneVideoPermanentError, match="allowlist"):
        _ = [chunk async for chunk in provider.retrieve_video("wavespeed:pred-123")]


async def test_wavespeed_composition_and_quality_are_delegated_only_when_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/compose":
            return httpx.Response(202, json={"id": "composition-1"})
        if request.method == "POST" and request.url.path == "/quality":
            return httpx.Response(200, json={
                "accepted": True, "score": 0.95, "summary": "Passed",
                "issues": [], "usage": {"cost_usd": "0.01"},
            })
        return httpx.Response(200, json={"status": "processing"})

    generic_config = GenericHttpConfig(
        base_url="https://video.example.test",
        status_path="/v1/videos/{job_id}",
        compose_path="/compose",
        quality_path="/quality",
        health_path="/health",
        api_key="secondary-test-key",
        download_allowed_hosts=("cdn.example.test",),
    )
    generic = GenericHttpSceneVideoProvider(
        generic_config,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    provider = _provider(handler, composition_provider=generic)
    assert provider.supports_composition is True
    assert provider.supports_quality_review is True
    assert _provider(handler).supports_composition is False
    assert _provider(handler).supports_quality_review is False


def test_wavespeed_is_registered_only_when_configured():
    registry = build_scene_video_registry(
        include_mock=False,
        wavespeed_config=WaveSpeedConfig(api_key="test-key"),
    )
    assert registry.has("wavespeed")
    assert registry.get("wavespeed").model_id == MODEL



async def test_wavespeed_delegates_composition_and_quality_to_configured_http_provider():
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/compose":
            return httpx.Response(202, json={"id": "compose-42"})
        if request.method == "POST" and request.url.path == "/quality":
            return httpx.Response(200, json={
                "accepted": True, "score": 0.93, "summary": "Review passed",
                "issues": [], "usage": {"cost_usd": "0.02"},
            })
        return httpx.Response(404)

    generic = GenericHttpSceneVideoProvider(
        GenericHttpConfig(
            base_url="https://video.example.test",
            status_path="/v1/videos/{job_id}",
            compose_path="/compose",
            quality_path="/quality",
            api_key="secondary-key",
        ),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    provider = _provider(handler, composition_provider=generic)
    composition_id = await provider.compose_video(SceneVideoCompositionRequest(
        project_id="project-42",
        storyboard={"title": "Campaign"},
        scene_video_urls=("https://cdn.example.test/a.mp4",),
        width=1080,
        height=1920,
        idempotency_key="compose-key",
    ))
    assert composition_id == "compose:compose-42"

    quality = await provider.review_video(SceneVideoQualityRequest(
        project_id="project-42",
        video_url="https://cdn.example.test/final.mp4",
        storyboard={"title": "Campaign"},
        idempotency_key="quality-key",
    ))
    assert quality.accepted is True
    assert quality.usage["cost_usd"] == "0.02"
    assert ("POST", "/compose") in received
    assert ("POST", "/quality") in received



async def test_wavespeed_blocks_submit_when_balance_is_below_estimate():
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"balance": 0.05}})
        if request.method == "POST":
            post_calls += 1
            return httpx.Response(200, json={"data": {"id": "unexpected"}})
        return httpx.Response(404)

    provider = _provider(handler)
    with pytest.raises(SceneVideoQuotaExhaustedError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_insufficient_balance"
    assert post_calls == 0



async def test_wavespeed_retries_safe_get_with_retry_after(monkeypatch):
    calls = 0
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("app.integrations.video.wavespeed.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "slow down"})
        return httpx.Response(200, json={"data": {"balance": 5.0}})

    provider = _provider(handler)
    result = await provider._request_json("GET", "https://api.wavespeed.ai/api/v3/balance")
    assert result["balance"] == 5.0
    assert calls == 2
    assert delays == [3.0]



async def test_wavespeed_reconciles_actual_cost_from_prediction_billing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/predictions/pred-77/result"):
            return httpx.Response(200, json={"data": {
                "id": "pred-77", "status": "completed",
                "outputs": ["https://cdn.example.test/video.mp4"],
            }})
        if request.method == "POST" and request.url.path.endswith("/billings/search"):
            body = json.loads(request.content)
            assert body["prediction_uuids"] == ["pred-77"]
            return httpx.Response(200, json={"data": {"items": [{
                "billing_type": "deduct",
                "price": 0.11,
                "prediction": {"uuid": "pred-77"},
            }]}})
        return httpx.Response(404)

    provider = _provider(handler)
    status = await provider.get_generation_status("wavespeed:pred-77")
    assert status.state == SceneJobState.COMPLETED.value
    assert status.raw["cost_usd"] == "0.110000"
    assert status.raw["currency"] == "USD"


async def test_wavespeed_falls_back_to_estimate_when_billing_permission_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/predictions/pred-78/result"):
            return httpx.Response(200, json={"data": {
                "id": "pred-78", "status": "completed",
                "outputs": ["https://cdn.example.test/video.mp4"],
            }})
        if request.method == "POST" and request.url.path.endswith("/billings/search"):
            return httpx.Response(403, json={"message": "billing role required"})
        return httpx.Response(404)

    provider = _provider(handler)
    status = await provider.get_generation_status("wavespeed:pred-78")
    assert status.state == SceneJobState.COMPLETED.value
    assert "cost_usd" not in status.raw
