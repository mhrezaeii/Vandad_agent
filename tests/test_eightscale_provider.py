"""8Scale provider: tolerant response parsing and error mapping."""

from __future__ import annotations

import httpx
import json
import pytest

from app.integrations.video.base import (
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
)
from app.integrations.video.eightscale import (
    EightScaleConfig,
    EightScaleSceneVideoProvider,
    extract_job_id,
    extract_video_url,
)


def _provider(handler) -> EightScaleSceneVideoProvider:
    return EightScaleSceneVideoProvider(
        EightScaleConfig(api_key="test-key", download_allowed_hosts=("static.8scale.com",)),
        client_factory=lambda t: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=t),
    )


def _request() -> SceneVideoRequest:
    return SceneVideoRequest(scene_id="p:0", prompt="Pixar-style scene", duration_seconds=15,
                             idempotency_key="k-1")


def test_extract_helpers_tolerate_common_shapes():
    assert extract_video_url({"data": {"output": [{"url": "https://static.8scale.com/x.mp4"}]}}) == \
        "https://static.8scale.com/x.mp4"
    assert extract_job_id({"data": {"task_id": "abc123"}}) == "abc123"
    assert extract_video_url({"status": "running"}) is None
    assert extract_job_id({"prompt": "hello world"}) is None


async def test_sync_flow_returns_url_job_and_retrieves():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content.decode())
            assert len(body["prompt"]) <= 2000
            return httpx.Response(200, json={
                "video_url": "https://static.8scale.com/out/clip-01.mp4"})
        return httpx.Response(200, content=b"VIDEOBYTES")

    provider = _provider(handler)
    job = await provider.generate_video(_request())
    assert job.startswith("8scale:https://static.8scale.com/out/clip-01.mp4")

    status = await provider.get_generation_status(job)
    assert status.is_completed and status.video_url.endswith("clip-01.mp4")

    chunks = [chunk async for chunk in provider.retrieve_video(job)]
    assert b"".join(chunks) == b"VIDEOBYTES"


async def test_async_flow_polls_status_then_out_json():
    """Submit -> /status/{id} (presigned out.json) -> completed with video URL."""
    polls = {"n": 0}
    manifest = {"video_url": "https://m.8scale.com/job-77/out.mp4"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "job-77"})
        if "/status/" in url:
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"status": "PROCESSING", "executionTimeMs": 1})
            return httpx.Response(200, json={
                "status": "COMPLETED",
                "output": "https://m.8scale.com/job-77/out.mp4?sig=1"})
        if "out.mp4" in url:
            return httpx.Response(200, content=b"VIDEOBYTES")
        return httpx.Response(404, json={"message": "Not Found"})

    provider = _provider(handler)
    job = await provider.generate_video(_request())
    assert job == "8scale:job-77"
    await provider.get_generation_status(job)      # first poll: not ready
    status = await provider.get_generation_status(job)  # second poll: completed
    assert status.is_completed and status.video_url.endswith("out.mp4?sig=1")
    assert polls["n"] == 2


async def test_validation_error_in_manifest_maps_to_failed():
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "job-88"})
        if "/status/" in url:
            return httpx.Response(200, json={
                "status": "FAILED",
                "output": "https://m.8scale.com/job-88/out.json?sig=1"})
        if "out.json" in url:
            return httpx.Response(200, json={"detail": [
                {"type": "string_too_long", "msg": "String should have at most 2000 characters"}]})
        return httpx.Response(404)

    provider = _provider(handler)
    job = await provider.generate_video(_request())
    status = await provider.get_generation_status(job)
    assert not status.is_completed
    assert status.error_code == "provider_generation_failed"
    assert "2000" in (status.error_message or "")


async def test_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(402, json={"message": "no credits"})
        return httpx.Response(500)

    provider = _provider(handler)
    with pytest.raises(SceneVideoQuotaExhaustedError):
        await provider.generate_video(_request())
