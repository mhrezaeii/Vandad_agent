"""Scene-video provider contract tests: mock, generic HTTP, registry."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.video import (
    APIMasterConfig,
    APIMasterSceneVideoProvider,
    GenericHttpConfig,
    GenericHttpSceneVideoProvider,
    MockSceneVideoProvider,
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
    build_scene_video_registry,
)
from app.integrations.video.base import SceneVideoValidation


def _request(scene_id: str = "p:0", key: str | None = "idem-1") -> SceneVideoRequest:
    return SceneVideoRequest(
        scene_id=scene_id,
        prompt="A slow vertical camera move over a city at dusk.",
        duration_seconds=5.0,
        idempotency_key=key,
    )


async def _drain(provider, job_id: str) -> bytes:
    chunks = []
    async for chunk in provider.retrieve_video(job_id):
        chunks.append(chunk)
    return b"".join(chunks)


# ------------------------------------------------------------------ mock
async def test_mock_provider_polls_to_completion_and_downloads():
    provider = MockSceneVideoProvider(poll_steps=2)
    job_id = await provider.generate_video(_request())

    first = await provider.get_generation_status(job_id)
    assert first.state == SceneJobState.GENERATING.value
    assert first.is_terminal is False

    second = await provider.get_generation_status(job_id)
    assert second.state == SceneJobState.COMPLETED.value
    assert second.is_completed is True
    assert second.progress == 1.0

    payload = await _drain(provider, job_id)
    assert len(payload) > 0
    assert payload[4:8] == b"ftyp"

    validation = await provider.validate_output(payload, content_type="video/mp4")
    assert validation.ok is True
    assert validation.size_bytes == len(payload)


async def test_mock_provider_is_idempotent_by_key():
    provider = MockSceneVideoProvider()
    first = await provider.generate_video(_request(key="stable-key"))
    second = await provider.generate_video(_request(key="stable-key"))
    assert first == second
    assert len(provider.jobs) == 1


async def test_mock_provider_enforces_daily_quota():
    provider = MockSceneVideoProvider(quota_limit_per_day=1)
    await provider.generate_video(_request("p:0", key="a"))
    with pytest.raises(SceneVideoQuotaExhaustedError):
        await provider.generate_video(_request("p:1", key="b"))


async def test_mock_provider_cancellation_is_terminal():
    provider = MockSceneVideoProvider(poll_steps=5)
    job_id = await provider.generate_video(_request())
    assert await provider.cancel_generation(job_id) is True
    status = await provider.get_generation_status(job_id)
    assert status.state == SceneJobState.CANCELLED.value
    with pytest.raises(SceneVideoPermanentError):
        await _drain(provider, job_id)


async def test_default_validation_rejects_non_video_bytes():
    provider = MockSceneVideoProvider()
    validation: SceneVideoValidation = await provider.validate_output(
        b"<html>not a video</html>", content_type="text/html"
    )
    assert validation.ok is False
    assert len(validation.issues) >= 2


# ---------------------------------------------------------- generic HTTP
def _http_provider(handler) -> GenericHttpSceneVideoProvider:
    config = GenericHttpConfig(
        base_url="https://video.example.test",
        generate_path="/v1/videos",
        status_path="/v1/videos/{job_id}",
        api_key="test-secret",
        download_allowed_hosts=("cdn.example.test",),
    )
    return GenericHttpSceneVideoProvider(
        config,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )


async def test_generic_http_submits_with_bearer_auth_and_polls():
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("Authorization")))
        if request.method == "POST" and str(request.url).endswith("/v1/videos"):
            return httpx.Response(201, json={"id": "job-42"})
        if str(request.url).endswith("/v1/videos/job-42"):
            return httpx.Response(
                200,
                json={"status": "processing", "progress": 0.4},
            )
        return httpx.Response(404, json={})

    provider = _http_provider(handler)
    job_id = await provider.generate_video(_request())
    assert job_id == "job-42"

    status = await provider.get_generation_status(job_id)
    assert status.state == SceneJobState.GENERATING.value
    assert status.progress == pytest.approx(0.4)
    # The secret is only ever sent in the configured header.
    assert all(auth == "Bearer test-secret" for _m, _u, auth in calls)


async def test_generic_http_maps_completed_and_downloads_video_url():
    payload = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/videos/job-9"):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "video_url": "https://cdn.example.test/job-9.mp4",
                    "duration": 5,
                },
            )
        if str(request.url) == "https://cdn.example.test/job-9.mp4":
            return httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={})

    provider = _http_provider(handler)
    status = await provider.get_generation_status("job-9")
    assert status.is_completed is True
    assert status.video_url == "https://cdn.example.test/job-9.mp4"

    downloaded = await _drain(provider, "job-9")
    assert downloaded == payload


async def test_generic_http_classifies_rate_limit_as_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    provider = _http_provider(handler)
    with pytest.raises(SceneVideoTransientError):
        await provider.generate_video(_request())


async def test_generic_http_classifies_auth_failure_as_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    provider = _http_provider(handler)
    with pytest.raises(SceneVideoPermanentError):
        await provider.generate_video(_request())


async def test_generic_http_rejects_missing_job_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "queued"})

    provider = _http_provider(handler)
    with pytest.raises(SceneVideoPermanentError):
        await provider.generate_video(_request())


def test_generic_http_config_requires_status_placeholder():
    with pytest.raises(ValueError):
        GenericHttpConfig(base_url="https://x.test", status_path="/v1/jobs")


# ------------------------------------------------------------- registry
def test_registry_includes_mock_and_only_configured_http():
    mock_only = build_scene_video_registry()
    assert mock_only.has("mock") is True
    assert mock_only.has("generic_http") is False

    configured = build_scene_video_registry(
        http_config=GenericHttpConfig(
            base_url="https://video.example.test",
            status_path="/v1/videos/{job_id}",
        )
    )
    assert configured.has("generic_http") is True
    assert len(configured) == 2


async def test_generic_http_never_sends_api_key_to_media_cdn():
    payload = b"\\x00\\x00\\x00\\x18ftypmp42" + b"\\x00" * 128
    media_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/videos/job-10"):
            return httpx.Response(200, json={
                "status": "completed",
                "video_url": "https://cdn.example.test/job-10.mp4",
            })
        media_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})

    provider = _http_provider(handler)
    assert await _drain(provider, "job-10") == payload
    assert media_auth == [None]


async def test_generic_http_rejects_untrusted_media_host_before_request():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={
            "status": "completed",
            "video_url": "https://attacker.example.test/steal",
        })

    provider = _http_provider(handler)
    with pytest.raises(SceneVideoPermanentError) as exc:
        await _drain(provider, "job-11")
    assert exc.value.code == "provider_media_host_denied"
    assert requested == ["https://video.example.test/v1/videos/job-11"]


async def test_generic_http_rejects_private_media_ip():
    provider = _http_provider(lambda _request: httpx.Response(200))
    with pytest.raises(SceneVideoPermanentError) as exc:
        provider._validate_media_url("https://127.0.0.1/private")
    assert exc.value.code == "provider_private_media_ip"


# --------------------------------------------------------------- APIMaster
def _apimaster_provider(handler) -> APIMasterSceneVideoProvider:
    config = APIMasterConfig(
        api_key="apimaster-test-secret",
        base_url="https://api.apimaster.test",
        model="wan3.0-video-prime",
        resolution="720P",
        download_allowed_hosts=("media.apimaster.test",),
    )
    return APIMasterSceneVideoProvider(
        config,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )


async def test_apimaster_submits_wan_request_with_image_reference():
    import json

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = request.read()
        return httpx.Response(201, json={"id": "task-123", "status": "queued"})

    provider = _apimaster_provider(handler)
    request = SceneVideoRequest(
        scene_id="p:0",
        prompt="A consistent presenter moves naturally in a studio.",
        duration_seconds=5.4,
        reference_image_url="https://assets.example.test/vandiar.png",
        aspect_ratio="9:16",
    )
    task_id = await provider.generate_video(request)

    body = json.loads(captured["body"])
    assert task_id == "task-123"
    assert captured["request"].headers["Authorization"] == "Bearer apimaster-test-secret"
    assert body == {
        "model": "wan3.0-video-prime",
        "prompt": request.prompt,
        "duration": 5,
        "resolution": "720P",
        "aspect_ratio": "9:16",
        "input_reference": "https://assets.example.test/vandiar.png",
    }


async def test_apimaster_polls_status_and_downloads_with_api_auth():
    video = b"\\x00\\x00\\x00\\x18ftypmp42" + b"\\x00" * 128
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("Authorization")))
        if request.method == "GET" and str(request.url).endswith("/v1/videos/task-9"):
            return httpx.Response(
                200,
                json={
                    "id": "task-9",
                    "status": "completed",
                    "progress": 100,
                    "metadata": {"url": "https://api.apimaster.test/v1/videos/task-9/content"},
                },
            )
        if str(request.url).endswith("/v1/videos/task-9/content"):
            return httpx.Response(200, content=video, headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={})

    provider = _apimaster_provider(handler)
    status = await provider.get_generation_status("task-9")
    assert status.is_completed is True
    assert status.progress == 1.0
    assert await _drain(provider, "task-9") == video
    assert all(auth == "Bearer apimaster-test-secret" for _method, _url, auth in calls)


async def test_apimaster_never_sends_api_key_to_redirected_media_host():
    video = b"\\x00\\x00\\x00\\x18ftypmp42" + b"\\x00" * 128
    media_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/videos/task-10"):
            return httpx.Response(200, json={"status": "completed"})
        if str(request.url).endswith("/v1/videos/task-10/content"):
            return httpx.Response(
                302,
                headers={"location": "https://media.apimaster.test/task-10.mp4"},
            )
        media_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, content=video, headers={"content-type": "video/mp4"})

    provider = _apimaster_provider(handler)
    assert await _drain(provider, "task-10") == video
    assert media_auth == [None]


async def test_apimaster_rejects_invalid_duration_and_untrusted_reference():
    provider = _apimaster_provider(lambda _request: httpx.Response(201, json={"id": "unused"}))
    with pytest.raises(SceneVideoPermanentError, match="integer duration"):
        await provider.generate_video(
            SceneVideoRequest(scene_id="p:1", prompt="test", duration_seconds=1.5)
        )
    with pytest.raises(SceneVideoPermanentError) as exc:
        await provider.generate_video(
            SceneVideoRequest(
                scene_id="p:2",
                prompt="test",
                duration_seconds=5,
                reference_image_url="http://127.0.0.1/private.png",
            )
        )
    assert exc.value.code == "provider_invalid_reference_image"


async def test_apimaster_maps_insufficient_balance_to_quota_error():
    provider = _apimaster_provider(lambda _request: httpx.Response(402, json={"error": "balance"}))
    with pytest.raises(SceneVideoQuotaExhaustedError):
        await provider.generate_video(_request())


def test_registry_registers_apimaster_only_when_configured():
    from app.integrations.video import build_scene_video_registry

    assert build_scene_video_registry().has("apimaster") is False
    configured = build_scene_video_registry(
        apimaster_config=APIMasterConfig(api_key="test-key")
    )
    assert configured.has("apimaster") is True
