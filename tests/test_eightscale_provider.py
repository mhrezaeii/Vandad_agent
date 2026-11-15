"""8Scale provider: tolerant response parsing and error mapping."""

from __future__ import annotations

import httpx
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
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "POST":
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


async def test_async_flow_polls_until_completed():
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"task_id": "job-77"}})
        state["polls"] += 1
        if state["polls"] < 2:
            return httpx.Response(200, json={"data": {"task_id": "job-77", "status": "processing"}})
        return httpx.Response(200, json={"data": {
            "task_id": "job-77", "status": "completed",
            "video_url": "https://static.8scale.com/out/done.mp4"}})

    provider = _provider(handler)
    job = await provider.generate_video(_request())
    assert job == "8scale:job-77"
    await provider.get_generation_status(job)   # first poll: still processing
    status = await provider.get_generation_status(job)  # second poll: completed
    assert status.is_completed and state["polls"] == 2


async def test_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(402, json={"message": "no credits"})
        return httpx.Response(500)

    provider = _provider(handler)
    with pytest.raises(SceneVideoQuotaExhaustedError):
        await provider.generate_video(_request())
