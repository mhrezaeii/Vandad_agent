"""Scene-video provider contract tests: mock, generic HTTP, registry."""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.video import (
    APIMasterConfig,
    APIMasterSceneVideoProvider,
    GenericHttpConfig,
    GenericHttpSceneVideoProvider,
    MockSceneVideoProvider,
    SceneJobState,
    SceneVideoCompositionRequest,
    SceneVideoHealthResult,
    SceneVideoPermanentError,
    SceneVideoProviderRegistry,
    SceneVideoProviderRegistryError,
    SceneVideoQualityRequest,
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
async def test_mock_provider_quality_review_is_explicitly_fixture_only():
    provider = MockSceneVideoProvider()
    assert provider.supports_quality_review is True
    result = await provider.review_video(
        SceneVideoQualityRequest(
            project_id="project-1",
            video_url="https://example.test/synthetic.mp4",
            storyboard={},
        )
    )
    assert result.accepted is True
    assert "no visual/audio quality is assessed" in result.summary


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
def _http_provider(
    handler, *, compose_path: str | None = None, quality_path: str | None = None,
    health_path: str | None = None
) -> GenericHttpSceneVideoProvider:
    config = GenericHttpConfig(
        base_url="https://video.example.test",
        generate_path="/v1/videos",
        status_path="/v1/videos/{job_id}",
        compose_path=compose_path,
        quality_path=quality_path,
        health_path=health_path,
        api_key="test-secret",
        download_allowed_hosts=("cdn.example.test",),
    )
    return GenericHttpSceneVideoProvider(
        config,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )



async def test_generic_http_composes_final_video_when_capability_is_configured():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received
        received = json.loads(request.content)
        return httpx.Response(202, json={"id": "compose-1"})

    provider = _http_provider(handler, compose_path="/v1/compose")
    assert provider.supports_composition is True
    request = SceneVideoCompositionRequest(
        project_id="project-1",
        storyboard={"title": "Campaign"},
        scene_video_urls=("https://cdn.example.test/1.mp4", "https://cdn.example.test/2.mp4"),
        width=1080,
        height=1920,
        idempotency_key="compose-idem",
    )
    job_id = await provider.compose_video(request)

    assert job_id == "compose-1"
    assert received["output"]["aspect_ratio"] == "9:16"
    assert received["scene_video_urls"] == list(request.scene_video_urls)
    assert received["idempotency_key"] == "compose-idem"


async def test_generic_http_without_compose_path_fails_closed():
    provider = _http_provider(lambda _request: httpx.Response(404), compose_path=None)
    assert provider.supports_composition is False
    request = SceneVideoCompositionRequest(
        project_id="project-1",
        storyboard={},
        scene_video_urls=("https://cdn.example.test/1.mp4",),
        width=1080,
        height=1920,
    )
    with pytest.raises(SceneVideoPermanentError, match="no composition endpoint"):
        await provider.compose_video(request)




async def test_generic_http_quality_review_validates_and_returns_report():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received
        received = json.loads(request.content)
        return httpx.Response(200, json={
            "accepted": True,
            "score": 0.92,
            "summary": "Clear narration and coherent scenes.",
            "issues": [],
            "usage": {"cost_usd": "0.12"},
        })

    provider = _http_provider(handler, quality_path="/v1/quality")
    assert provider.supports_quality_review is True
    result = await provider.review_video(
        SceneVideoQualityRequest(
            project_id="project-1",
            video_url="https://cdn.example.test/final.mp4",
            storyboard={"title": "Campaign"},
            expected_duration_seconds=30,
            aspect_ratio="9:16",
            idempotency_key="quality-idem",
        )
    )
    assert result.accepted is True
    assert result.score == pytest.approx(0.92)
    assert result.usage["cost_usd"] == "0.12"
    assert received["idempotency_key"] == "quality-idem"


async def test_generic_http_quality_review_fails_closed_without_endpoint():
    provider = _http_provider(lambda _request: httpx.Response(404))
    assert provider.supports_quality_review is False
    with pytest.raises(SceneVideoPermanentError, match="no quality-review endpoint"):
        await provider.review_video(
            SceneVideoQualityRequest(
                project_id="project-1",
                video_url="https://cdn.example.test/final.mp4",
                storyboard={},
            )
        )


async def test_generic_http_quality_review_rejects_malformed_report():
    provider = _http_provider(
        lambda _request: httpx.Response(200, json={"accepted": "yes"}),
        quality_path="/v1/quality",
    )
    with pytest.raises(SceneVideoPermanentError, match="invalid quality report"):
        await provider.review_video(
            SceneVideoQualityRequest(
                project_id="project-1",
                video_url="https://cdn.example.test/final.mp4",
                storyboard={},
            )
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
def test_ordered_registry_selection_skips_models_missing_required_capabilities():
    registry = build_scene_video_registry(
        apimaster_config=APIMasterConfig(api_key="test-key"),
        http_config=GenericHttpConfig(
            base_url="https://video.example.test",
            status_path="/v1/videos/{job_id}",
            compose_path="/v1/compose",
            request_extra={"model": "studio-compose-v1"},
        ),
    )
    selected = registry.resolve_ordered(
        ["apimaster", "generic_http"],
        require_composition=True,
    )
    assert selected.provider_id == "generic_http"
    assert selected.model_id == "studio-compose-v1"

    with pytest.raises(SceneVideoProviderRegistryError, match="satisfies the requested capabilities"):
        registry.resolve_ordered(["apimaster"], require_composition=True)


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
    payload = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128
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


async def test_generic_http_capability_contract_controls_seed_and_resolution():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"id": "generic-job"})

    provider = GenericHttpSceneVideoProvider(
        GenericHttpConfig(
            base_url="https://video.example.test",
            supported_resolutions=("1080x1920",),
            supports_seed=True,
            max_prompt_characters=100,
        ),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    assert provider.input_modalities == ("text",)
    assert provider.supported_resolutions == ("1080x1920",)
    assert provider.supports_seed is True
    assert provider.max_prompt_characters == 100
    assert provider.max_reference_images == 0

    await provider.generate_video(
        SceneVideoRequest(
            scene_id="p:0",
            prompt="A short test prompt.",
            duration_seconds=5,
            width=1080,
            height=1920,
            aspect_ratio="9:16",
            seed=7,
        )
    )
    assert captured["body"]["seed"] == 7


async def test_generic_http_rejects_seed_when_contract_does_not_declare_support():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(201, json={"id": "unexpected"})

    provider = GenericHttpSceneVideoProvider(
        GenericHttpConfig(base_url="https://video.example.test"),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    with pytest.raises(SceneVideoPermanentError, match="does not declare seed support"):
        await provider.generate_video(
            SceneVideoRequest(
                scene_id="p:0",
                prompt="A short test prompt.",
                duration_seconds=5,
                seed=7,
            )
        )
    assert requested == []


async def test_generic_http_rejects_undeclared_resolution_before_request():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(201, json={"id": "unexpected"})

    provider = GenericHttpSceneVideoProvider(
        GenericHttpConfig(
            base_url="https://video.example.test",
            supported_resolutions=("720x1280",),
        ),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    with pytest.raises(SceneVideoPermanentError, match="dimensions are not supported"):
        await provider.generate_video(
            SceneVideoRequest(
                scene_id="p:0",
                prompt="A short test prompt.",
                duration_seconds=5,
                width=1080,
                height=1920,
            )
        )
    assert requested == []


# --------------------------------------------------------------- APIMaster
async def test_apimaster_live_health_probe_uses_authenticated_read_only_models_endpoint():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "wan3.0-video-prime"}]},
        )

    provider = _apimaster_provider(handler)
    assert provider.supports_health_check is True
    result = await provider.check_health()

    assert result.healthy is True
    assert result.provider_id == "apimaster"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.apimaster.test/v1/models"
    assert captured["authorization"] == "Bearer apimaster-test-secret"


async def test_apimaster_health_probe_fails_closed_on_unauthorized_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    result = await _apimaster_provider(handler).check_health()

    assert result.healthy is False
    assert result.provider_id == "apimaster"
    assert "apimaster-test-secret" not in result.message


async def test_apimaster_health_probe_rejects_unexpected_catalogue_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    result = await _apimaster_provider(handler).check_health()

    assert result.healthy is False
    assert "unexpected" in result.message.lower()


def _apimaster_provider(handler) -> APIMasterSceneVideoProvider:
    config = APIMasterConfig(
        api_key="apimaster-test-secret",
        base_url="https://api.apimaster.test",
        model="wan3.0-video-prime",
        resolution="720P",
        route="gmi",
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
        "reference_image": "https://assets.example.test/vandiar.png",
    }


async def test_apimaster_native_route_submits_reference_image_media():
    import json

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(201, json={"id": "native-task", "status": "queued"})

    provider = APIMasterSceneVideoProvider(
        APIMasterConfig(
            api_key="apimaster-test-secret",
            base_url="https://api.apimaster.test",
            route="native",
        ),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    await provider.generate_video(
        SceneVideoRequest(
            scene_id="p:0",
            prompt="Animate the character shown in image 1.",
            duration_seconds=5,
            reference_image_url="https://assets.example.test/vandiar.png",
            aspect_ratio="9:16",
        )
    )
    body = json.loads(captured["body"])
    assert body["metadata"]["input"]["media"] == [
        {"type": "reference_image", "url": "https://assets.example.test/vandiar.png"}
    ]
    assert "input_reference" not in body


async def test_apimaster_polls_status_and_downloads_with_api_auth():
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128
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
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128
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
    with pytest.raises(SceneVideoPermanentError, match="duration from 2 to 30 seconds"):
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



def test_ordered_registry_enforces_quality_review_and_declared_duration_limits():
    registry = build_scene_video_registry(
        apimaster_config=APIMasterConfig(api_key="test-key"),
        http_config=GenericHttpConfig(
            base_url="https://video.example.test",
            status_path="/v1/videos/{job_id}",
            compose_path="/v1/compose",
            quality_path="/v1/quality",
            request_extra={"model": "studio-compose-v1"},
            min_duration_seconds=3,
            max_duration_seconds=12,
            supported_languages=("fa-IR",),
        ),
    )
    selected = registry.resolve_ordered(
        ["apimaster", "generic_http"],
        require_composition=True,
        require_quality_review=True,
        required_durations=(5.0, 10.0),
        required_language="fa-IR",
    )
    assert selected.provider_id == "generic_http"
    with pytest.raises(SceneVideoProviderRegistryError):
        registry.resolve_ordered(
            ["generic_http"],
            require_composition=True,
            require_quality_review=True,
            required_durations=(13.0,),
        )

    with pytest.raises(SceneVideoProviderRegistryError):
        registry.resolve_ordered(
            ["generic_http"],
            require_composition=True,
            require_quality_review=True,
            required_language="en-US",
        )



async def test_generic_http_live_health_probe_requires_explicit_endpoint():
    def healthy_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"healthy": True})

    provider = _http_provider(healthy_handler, health_path="/health")
    assert provider.supports_health_check is True
    result = await provider.check_health()
    assert result.provider_id == "generic_http"
    assert result.healthy is True
    assert result.latency_ms is not None

    unconfigured = _http_provider(lambda _request: httpx.Response(404))
    assert unconfigured.supports_health_check is False
    with pytest.raises(SceneVideoPermanentError, match="no configured live health probe"):
        await unconfigured.check_health()



async def test_ordered_healthy_registry_skips_unhealthy_provider():
    class UnhealthyMockProvider(MockSceneVideoProvider):
        provider_id = "unhealthy_mock"

        async def check_health(self):
            return SceneVideoHealthResult(
                provider_id=self.provider_id,
                healthy=False,
                message="Provider is unhealthy.",
            )

    registry = SceneVideoProviderRegistry()
    registry.register(UnhealthyMockProvider())
    registry.register(MockSceneVideoProvider())
    selected = await registry.resolve_ordered_healthy(
        ["unhealthy_mock", "mock"],
        require_composition=True,
        require_quality_review=True,
    )
    assert selected.provider_id == "mock"



async def test_ordered_registry_selects_apimaster_only_after_live_health_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "wan3.0-video-prime"}]},
        )

    provider = _apimaster_provider(handler)
    registry = SceneVideoProviderRegistry()
    registry.register(provider)

    selected = await registry.resolve_ordered_healthy(
        ["apimaster"], require_reference_image=True
    )

    assert selected is provider


async def test_ordered_registry_rejects_apimaster_when_live_probe_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    registry = SceneVideoProviderRegistry()
    registry.register(_apimaster_provider(handler))

    with pytest.raises(SceneVideoProviderRegistryError, match="passes a live health probe"):
        await registry.resolve_ordered_healthy(["apimaster"])
