"""Final-video probe validation tests; no media generation or paid calls.

The paid KIE single-shot deliverable is a provider MP4 with native audio. The
service must fail closed when ffprobe is missing, when the audio stream is
absent, or when the returned duration does not match the paid contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.video.base import SceneVideoPermanentError
from app.services import scene_video_service, video_assembly
from app.services.scene_video_service import SceneVideoService


def _install_probe(
    monkeypatch, payload: dict | None, *, returncode: int = 0
) -> None:
    class Result:
        def __init__(self, code: int, stdout: bytes) -> None:
            self.returncode = code
            self.stdout = stdout

    stdout = json.dumps(payload or {}).encode()
    monkeypatch.setattr(video_assembly, "ffprobe_binary", lambda: "ffprobe")
    monkeypatch.setattr(
        video_assembly.subprocess,
        "run",
        lambda *args, **kwargs: Result(returncode, stdout),
    )


def _media(*, audio: bool = True, duration: str = "20.0") -> dict:
    streams = [
        {"codec_type": "video", "codec_name": "h264", "width": 608, "height": 1080},
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": duration}}


def test_probe_video_streams_reports_media_facts(monkeypatch):
    _install_probe(monkeypatch, _media())
    facts = video_assembly.probe_video_streams(Path("final.mp4"))
    assert facts == {
        "has_video": True,
        "has_audio": True,
        "width": 608,
        "height": 1080,
        "video_codec": "h264",
        "audio_codec": "aac",
        "duration_seconds": 20.0,
        "audio_stream_count": 1,
    }


def test_probe_video_streams_returns_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr(video_assembly, "ffprobe_binary", lambda: None)
    assert video_assembly.probe_video_streams(Path("final.mp4")) is None


def test_probe_video_streams_fails_closed_on_unreadable_media(monkeypatch):
    _install_probe(monkeypatch, {}, returncode=1)
    with pytest.raises(SceneVideoPermanentError) as error:
        video_assembly.probe_video_streams(Path("final.mp4"))
    assert error.value.code == "invalid_video"


def _service_with_file(tmp_path) -> SceneVideoService:
    storage = SimpleNamespace(local_path=lambda key: tmp_path / key)
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 16)
    return SimpleNamespace(storage=storage)  # type: ignore[return-value]


def _artifact() -> SimpleNamespace:
    return SimpleNamespace(storage_key="clip.mp4")


def _job(duration: float = 20.0) -> SimpleNamespace:
    return SimpleNamespace(duration_seconds=duration)


def test_kie_delivery_accepts_native_audio_and_matching_duration(tmp_path, monkeypatch):
    service = _service_with_file(tmp_path)
    monkeypatch.setattr(
        scene_video_service, "probe_video_streams", lambda path: video_assembly.probe_video_streams(path)
    )
    _install_probe(monkeypatch, _media())
    facts = SceneVideoService._verify_kie_delivery(service, _job(), _artifact())
    assert facts["has_audio"] is True
    assert facts["duration_seconds"] == 20.0


def test_kie_delivery_rejects_missing_native_audio(tmp_path, monkeypatch):
    service = _service_with_file(tmp_path)
    _install_probe(monkeypatch, _media(audio=False))
    with pytest.raises(SceneVideoPermanentError) as error:
        SceneVideoService._verify_kie_delivery(service, _job(), _artifact())
    assert error.value.code == "native_audio_missing"


def test_kie_delivery_rejects_wrong_duration(tmp_path, monkeypatch):
    service = _service_with_file(tmp_path)
    _install_probe(monkeypatch, _media(duration="14.0"))
    with pytest.raises(SceneVideoPermanentError) as error:
        SceneVideoService._verify_kie_delivery(service, _job(20.0), _artifact())
    assert error.value.code == "invalid_video"


def test_kie_delivery_requires_ffprobe(tmp_path, monkeypatch):
    service = _service_with_file(tmp_path)
    monkeypatch.setattr(video_assembly, "ffprobe_binary", lambda: None)
    with pytest.raises(SceneVideoPermanentError) as error:
        SceneVideoService._verify_kie_delivery(service, _job(), _artifact())
    assert error.value.code == "ffprobe_unavailable"
