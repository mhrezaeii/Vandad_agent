from __future__ import annotations

import pytest

from scripts.wavespeed_smoke_test import (
    WaveSpeedError,
    _data,
    _prediction_id,
    _video_url,
    _https_url,
    build_payload,
)


def test_unwraps_wave_speed_data_envelope() -> None:
    assert _data({"data": {"id": "prediction-1"}}) == {"id": "prediction-1"}


def test_accepts_unwrapped_wave_speed_payload() -> None:
    assert _data({"id": "prediction-1"}) == {"id": "prediction-1"}


def test_extracts_prediction_id() -> None:
    assert _prediction_id({"data": {"id": "abc-123"}}) == "abc-123"


def test_rejects_missing_prediction_id() -> None:
    with pytest.raises(WaveSpeedError, match="prediction ID"):
        _prediction_id({"data": {"status": "created"}})


def test_extracts_completed_output_url() -> None:
    assert _video_url({"data": {"outputs": ["https://cdn.example/video.mp4"]}}) == (
        "https://cdn.example/video.mp4"
    )


def test_extracts_object_output_url() -> None:
    assert _video_url({"data": {"outputs": [{"url": "https://cdn.example/video.mp4"}]}}) == (
        "https://cdn.example/video.mp4"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/video.mp4",
        "https://user:pass@example.com/video.mp4",
        "file:///etc/passwd",
        "",
        None,
    ],
)
def test_rejects_unsafe_urls(url: object) -> None:
    with pytest.raises(WaveSpeedError):
        _https_url(url, label="test")


def test_rejects_empty_output_list() -> None:
    with pytest.raises(WaveSpeedError, match="no output URL"):
        _video_url({"data": {"outputs": []}})


def test_builds_text_only_payload_without_image_fields() -> None:
    payload = build_payload(
        prompt="A cinematic product reveal",
        resolution="480p",
        aspect_ratio="9:16",
        duration=5,
    )
    assert payload == {
        "prompt": "A cinematic product reveal",
        "resolution": "480p",
        "aspect_ratio": "9:16",
        "duration": 5,
    }
    assert "image" not in payload
    assert "images" not in payload


def test_build_payload_includes_optional_seed() -> None:
    payload = build_payload(
        prompt="A cinematic product reveal",
        resolution="720p",
        aspect_ratio="16:9",
        duration=10,
        seed=42,
    )
    assert payload["seed"] == 42


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": " ", "resolution": "480p", "aspect_ratio": "9:16", "duration": 5},
        {"prompt": "scene", "resolution": "4k", "aspect_ratio": "9:16", "duration": 5},
        {"prompt": "scene", "resolution": "480p", "aspect_ratio": "1:1", "duration": 5},
        {"prompt": "scene", "resolution": "480p", "aspect_ratio": "9:16", "duration": 4},
    ],
)
def test_rejects_invalid_payload_options(kwargs: dict[str, object]) -> None:
    with pytest.raises(WaveSpeedError):
        build_payload(**kwargs)
