"""KIE.ai Grok Imagine Video 1.5 adapter contract tests; no paid requests."""
from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.video import SceneJobState, SceneVideoRequest
from app.integrations.video.kie import KIEConfig, KIESceneVideoProvider


def _request(duration: int = 15) -> SceneVideoRequest:
    return SceneVideoRequest(
        scene_id="project:0",
        prompt="A cinematic vertical product film with natural Persian dialogue.",
        duration_seconds=duration,
        aspect_ratio="9:16",
    )


def _provider(handler):
    return KIESceneVideoProvider(
        KIEConfig(api_key="test-secret", poll_interval_seconds=1),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )


async def test_kie_health_and_generation_use_documented_contract():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "data": 60})
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["model"] == "grok-imagine-video-1-5-preview"
            assert body["input"]["duration"] == 15
            assert body["input"]["resolution"] == "480p"
            assert body["input"]["aspect_ratio"] == "9:16"
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-123"}})
        return httpx.Response(404)

    provider = _provider(handler)
    health = await provider.check_health()
    job_id = await provider.generate_video(_request())

    assert health.healthy is True
    assert job_id == "kie:task-123"
    assert ("POST", "/api/v1/jobs/createTask") in calls


async def test_kie_accepts_fractional_duration_as_documented_number():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "data": 60})
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["input"]["duration"] == 7.5
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-7-5"}})
        return httpx.Response(404)

    provider = _provider(handler)
    job_id = await provider.generate_video(_request(7.5))
    assert job_id == "kie:task-7-5"


async def test_kie_blocks_project_scene_when_credits_are_insufficient():
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "data": 20})
        if request.method == "POST":
            post_calls += 1
        return httpx.Response(404)

    provider = _provider(handler)
    from app.integrations.video.base import SceneVideoQuotaExhaustedError

    with pytest.raises(SceneVideoQuotaExhaustedError):
        await provider.generate_video(_request(15))
    assert post_calls == 0


async def test_kie_status_parses_native_audio_video_url_and_consumed_credits():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/recordInfo"):
            return httpx.Response(200, json={
                "code": 200,
                "data": {
                    "taskId": "task-123",
                    "state": "success",
                    "progress": 100,
                    "resultJson": json.dumps({"resultUrls": ["https://cdn.example.test/video.mp4"]}),
                    "creditsConsumed": 36,
                },
            })
        return httpx.Response(404)

    provider = _provider(handler)
    status = await provider.get_generation_status("kie:task-123")

    assert status.state == SceneJobState.COMPLETED.value
    assert status.video_url == "https://cdn.example.test/video.mp4"
    assert status.raw["credits_consumed"] == "36"
    assert status.raw["cost_usd"] == "0.180000"
    assert provider.supports_native_audio is True


async def test_kie_does_not_retry_ambiguous_billable_submit():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "data": 60})
        if request.url.path.endswith("/jobs/createTask"):
            calls += 1
            raise httpx.ReadTimeout("ambiguous")
        return httpx.Response(404)

    provider = _provider(handler)
    from app.integrations.video.base import SceneVideoTransientError

    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_submission_unknown"
    assert calls == 1


async def test_kie_rejects_durations_outside_model_contract():
    provider = _provider(lambda request: httpx.Response(404))
    from app.integrations.video.base import SceneVideoPermanentError

    with pytest.raises(SceneVideoPermanentError):
        await provider.generate_video(_request(16))
