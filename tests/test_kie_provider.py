"""KIE.ai Grok Imagine adapter contract tests; no paid requests.

Covers both documented models:
- ``grok-imagine/image-to-video`` (default): one continuous 6..30s video from
  one reference image, string duration, mode field, no aspect_ratio.
- ``grok-imagine-video-1-5-preview`` (legacy alternative): 1..15s per-scene
  text-to-video, numeric duration, aspect_ratio, optional reference image.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.integrations.video import SceneJobState, SceneVideoRequest
from app.integrations.video.base import (
    SceneVideoPermanentError,
    SceneVideoQuotaExhaustedError,
    SceneVideoTransientError,
)
from app.integrations.video.kie import (
    I2V_MODEL_ID,
    T2V_MODEL_ID,
    KIEConfig,
    KIESceneVideoProvider,
)

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


def _request(
    duration: int | float = 15,
    reference_image_url: str | None = None,
    prompt: str = "A cinematic vertical product film with natural Persian dialogue.",
) -> SceneVideoRequest:
    return SceneVideoRequest(
        scene_id="project:0",
        prompt=prompt,
        duration_seconds=duration,
        aspect_ratio="9:16",
        reference_image_url=reference_image_url,
    )


def _provider(handler, model: str | None = None, **config_overrides):
    return KIESceneVideoProvider(
        KIEConfig(
            api_key="test-secret",
            poll_interval_seconds=1,
            model=model or I2V_MODEL_ID,
            **config_overrides,
        ),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )


def _credit_ok() -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": 60})


# ------------------------------------------------------------------ image-to-video
async def test_i2v_submits_documented_model_contract_with_reference_image():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/character.png") and request.method == "GET":
            return httpx.Response(
                200, content=_PNG_1PX, headers={"content-type": "image/png"}
            )
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["model"] == "grok-imagine/image-to-video"
            assert body["input"]["image_urls"] == ["https://assets.example.test/character.png"]
            assert body["input"]["duration"] == "20"  # documented string form
            assert body["input"]["resolution"] == "480p"
            assert body["input"]["mode"] == "normal"
            # Single-image mode derives geometry from the image itself.
            assert "aspect_ratio" not in body["input"]
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "i2v-task"}})
        return httpx.Response(404)

    provider = _provider(handler)
    health = await provider.check_health()
    job_id = await provider.generate_video(
        _request(20, reference_image_url="https://assets.example.test/character.png")
    )

    assert health.healthy is True
    assert job_id == "kie:i2v-task"
    assert ("POST", "/api/v1/jobs/createTask") in calls
    assert provider.model_id == "grok-imagine/image-to-video"
    assert provider.supports_reference_image is True
    assert provider.max_reference_images == 1


async def test_i2v_model_controlled_duration_omits_optional_payload_fields():
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/character.png") and request.method == "GET":
            return httpx.Response(200, content=_PNG_1PX, headers={"content-type": "image/png"})
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            payloads.append(body)
            assert set(body) == {"model", "input"}
            assert body["model"] == I2V_MODEL_ID
            assert set(body["input"]) == {"prompt", "image_urls"}
            assert body["input"]["image_urls"] == ["https://assets.example.test/character.png"]
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "i2v-free-task"}})
        return httpx.Response(404)

    provider = _provider(handler, send_duration=False)
    assert provider.duration_is_model_controlled is True

    job_id = await provider.generate_video(
        _request(20, reference_image_url="https://assets.example.test/character.png")
    )

    assert job_id == "kie:i2v-free-task"
    assert len(payloads) == 1


async def test_t2v_never_uses_model_controlled_duration():
    provider = _provider(lambda request: httpx.Response(404), model=T2V_MODEL_ID, send_duration=False)
    assert provider.duration_is_model_controlled is False


async def test_i2v_rejects_missing_reference_image_before_any_paid_call():
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            posts += 1
        return httpx.Response(404)

    provider = _provider(handler)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(_request(20))
    assert error.value.code == "provider_reference_image_missing"
    assert posts == 0


async def test_i2v_rejects_invalid_reference_image_urls():
    provider = _provider(lambda request: httpx.Response(404))
    for bad_url in (
        "http://assets.example.test/character.png",  # not HTTPS
        "https://assets.example.test/character.png?x=1",  # query string
        "https://assets.example.test/character.gif",  # unsupported format
        "https://user:pass@assets.example.test/character.png",  # userinfo
        "https://127.0.0.1/character.png",  # non-public literal host
        "https://assets.example.test:8080/character.png",  # non-standard port
    ):
        with pytest.raises(SceneVideoPermanentError) as error:
            await provider.generate_video(_request(20, reference_image_url=bad_url))
        assert error.value.code == "provider_reference_image_invalid"


async def test_i2v_enforces_documented_duration_window_and_whole_seconds():
    provider = _provider(lambda request: httpx.Response(404))
    for bad_duration in (5, 31, 20.5):
        with pytest.raises(SceneVideoPermanentError) as error:
            await provider.generate_video(
                _request(bad_duration, reference_image_url="https://assets.example.test/c.png")
            )
        assert error.value.code == "provider_invalid_duration"


async def test_i2v_rejects_prompts_over_documented_limit():
    provider = _provider(lambda request: httpx.Response(404))
    long_prompt = "x" * 5001
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/c.png", prompt=long_prompt)
        )
    assert error.value.code == "provider_invalid_prompt"


async def test_i2v_rejects_spicy_mode_configuration():
    with pytest.raises(ValueError):
        KIEConfig(api_key="test-secret", mode="spicy")


async def test_i2v_rejects_unknown_model_configuration():
    with pytest.raises(ValueError):
        KIEConfig(api_key="test-secret", model="grok-imagine/text-to-video")


async def test_i2v_uploads_local_reference_image_via_documented_base64_endpoint():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.path.endswith("/file-base64-upload"):
            body = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer test-secret"
            assert body["uploadPath"] == "vandad/character-references"
            assert body["fileName"] == "character.png"
            payload = body["base64Data"]
            assert payload.startswith("data:image/png;base64,")
            assert base64.b64decode(payload.split(",", 1)[1]) == _PNG_1PX
            return httpx.Response(200, json={
                "code": 200,
                "msg": "File uploaded successfully",
                "data": {
                    "fileName": "character.png",
                    "downloadUrl": "https://tempfile.example.test/vandad/character-references/character.png",
                },
            })
        return httpx.Response(404)

    provider = _provider(handler)
    hosted_url = await provider.upload_reference_image(_PNG_1PX, file_name="character.png")
    assert hosted_url == (
        "https://tempfile.example.test/vandad/character-references/character.png"
    )
    # Uploads live on the documented upload host, NOT the jobs API origin
    # (the jobs host answers that path with HTTP 404).
    assert ("POST", "kieai.redpandaai.co", "/api/file-base64-upload") in calls


async def test_i2v_upload_rejects_unsupported_image_content_and_oversize():
    provider = _provider(lambda request: httpx.Response(404))
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.upload_reference_image(b"GIF89a-not-an-image", file_name="a.png")
    assert error.value.code == "provider_reference_image_invalid"
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.upload_reference_image(b"", file_name="a.png")
    assert error.value.code == "provider_reference_image_invalid"
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.upload_reference_image(
            _PNG_1PX + b"\x00" * (10 * 1024 * 1024), file_name="a.png"
        )
    assert error.value.code == "provider_reference_image_too_large"


async def test_i2v_upload_failure_is_safe_and_transient_on_server_errors():
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path.endswith("/file-base64-upload"):
            posts += 1
            if posts == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"code": 401, "msg": "unauthorized"})
        return httpx.Response(404)

    provider = _provider(handler)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.upload_reference_image(_PNG_1PX, file_name="character.png")
    assert error.value.code == "provider_server_error"
    with pytest.raises(SceneVideoPermanentError):
        await provider.upload_reference_image(_PNG_1PX, file_name="character.png")


async def test_i2v_webp_reference_content_is_accepted():
    webp = b"RIFF\x24\x00\x00\x00WEBPVP8 \x00\x00\x00\x00" + b"\x00" * 32
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/file-base64-upload"):
            body = json.loads(request.content)
            captured["mime"] = body["base64Data"].split(";", 1)[0].removeprefix("data:")
            return httpx.Response(200, json={
                "code": 200,
                "data": {"downloadUrl": "https://tempfile.example.test/ref.webp"},
            })
        return httpx.Response(404)

    provider = _provider(handler)
    await provider.upload_reference_image(webp, file_name="ref.webp")
    assert captured["mime"] == "image/webp"


# ------------------------------------------------- legacy text-to-video model
async def test_t2v_health_and_generation_use_documented_contract():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["model"] == "grok-imagine-video-1-5-preview"
            assert body["input"]["duration"] == 15
            assert body["input"]["resolution"] == "480p"
            assert body["input"]["aspect_ratio"] == "9:16"
            assert "mode" not in body["input"]
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-123"}})
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    health = await provider.check_health()
    job_id = await provider.generate_video(_request())

    assert health.healthy is True
    assert job_id == "kie:task-123"
    assert ("POST", "/api/v1/jobs/createTask") in calls


async def test_t2v_accepts_fractional_duration_as_documented_number():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["input"]["duration"] == 7.5
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "task-7-5"}})
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    job_id = await provider.generate_video(_request(7.5))
    assert job_id == "kie:task-7-5"


async def test_t2v_image_to_video_omits_aspect_ratio_for_single_reference_image():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reference.png") and request.method == "GET":
            return httpx.Response(
                200, content=_PNG_1PX, headers={"content-type": "image/png"}
            )
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            body = json.loads(request.content)
            assert body["input"]["image_urls"] == ["https://assets.example.test/reference.png"]
            assert "aspect_ratio" not in body["input"]
            return httpx.Response(200, json={"code": 200, "data": {"taskId": "image-task"}})
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    job_id = await provider.generate_video(
        _request(reference_image_url="https://assets.example.test/reference.png")
    )
    assert job_id == "kie:image-task"


async def test_kie_blocks_project_scene_when_credits_are_insufficient():
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "data": 20})
        if request.method == "POST":
            post_calls += 1
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
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
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            calls += 1
            raise httpx.ReadTimeout("ambiguous")
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_submission_unknown"
    assert calls == 1


async def test_kie_marks_malformed_billable_submit_response_as_unknown():
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            post_calls += 1
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_submission_unknown"
    assert post_calls == 1


async def test_kie_rejects_durations_outside_model_contract():
    from app.integrations.video.base import SceneVideoPermanentError

    # Legacy text-to-video: 1..15s.
    provider = _provider(lambda request: httpx.Response(404), model=T2V_MODEL_ID)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(_request(16))
    assert error.value.code == "provider_invalid_duration"
    # Image-to-video: 6..30s.
    provider = _provider(lambda request: httpx.Response(404))
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(31, reference_image_url="https://assets.example.test/c.png")
        )
    assert error.value.code == "provider_invalid_duration"


# ------------------------------------------ reference image accessibility
async def test_unreachable_reference_image_fails_before_credits_or_paid_submit():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/character.png") and request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": 100})

    provider = _provider(handler)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/character.png")
        )
    assert error.value.code == "provider_reference_image_unreachable"
    paths = [path for _method, path in calls]
    assert "/api/v1/chat/credit" not in paths
    assert "/api/v1/jobs/createTask" not in paths


async def test_reference_image_must_return_image_content_and_respect_size_limit():
    def wrong_type(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, content=b"<html>not an image</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, json={"code": 200, "data": 100})

    provider = _provider(wrong_type)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/character.png")
        )
    assert error.value.code == "provider_reference_image_invalid"

    def oversized(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=_PNG_1PX,
                headers={
                    "content-type": "image/png",
                    "content-length": str(11 * 1024 * 1024),
                },
            )
        return httpx.Response(200, json={"code": 200, "data": 100})

    provider = _provider(oversized)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/character.png")
        )
    assert error.value.code == "provider_reference_image_too_large"


async def test_reference_image_host_failures_are_safe_preflight_outcomes():
    def temporarily_down(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503)
        return httpx.Response(200, json={"code": 200, "data": 100})

    provider = _provider(temporarily_down)
    with pytest.raises(SceneVideoTransientError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/character.png")
        )
    assert error.value.code == "provider_preflight_transient"

    def private_redirect(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/character.png"})
        return httpx.Response(200, json={"code": 200, "data": 100})

    provider = _provider(private_redirect)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(
            _request(20, reference_image_url="https://assets.example.test/character.png")
        )
    assert error.value.code == "provider_reference_image_invalid"


# ------------------------------------------------------- documented error codes
async def test_record_info_accepts_legacy_505_only_with_a_structured_task_payload():
    payload = {
        "taskId": "task-505",
        "state": "success",
        "resultJson": json.dumps({"resultUrls": ["https://cdn.example.test/video.mp4"]}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/recordInfo"):
            return httpx.Response(200, json={"code": 505, "msg": "success", "data": payload})
        return httpx.Response(404)

    provider = _provider(handler)
    status = await provider.get_generation_status("kie:task-505")
    assert status.state == SceneJobState.COMPLETED.value
    assert status.video_url == "https://cdn.example.test/video.mp4"


async def test_create_task_treats_505_as_feature_disabled_rejection():
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path.endswith("/chat/credit"):
            return _credit_ok()
        if request.url.path.endswith("/jobs/createTask"):
            posts += 1
            return httpx.Response(200, json={"code": 505, "msg": "Feature Disabled"})
        return httpx.Response(404)

    provider = _provider(handler, model=T2V_MODEL_ID)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.generate_video(_request())
    assert error.value.code == "provider_request_failed"
    assert posts == 1


async def test_record_info_505_without_a_task_payload_is_a_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/recordInfo"):
            return httpx.Response(200, json={"code": 505, "msg": "Feature Disabled", "data": {}})
        return httpx.Response(404)

    provider = _provider(handler)
    with pytest.raises(SceneVideoPermanentError) as error:
        await provider.get_generation_status("kie:task-505")
    assert error.value.code == "provider_request_failed"
