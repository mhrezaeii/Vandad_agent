"""Persian TTS: SSML construction, sanitization, and backend selection."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.tts import (
    TextToSpeechError,
    TtsSettings,
    build_ssml,
    synthesize,
    tts_settings_from,
)


def test_ssml_escapes_and_sanitizes_text():
    ssml = build_ssml("سلام <تیم> *وندیار*", voice="fa-IR-DilaraNeural", rate_percent="-5%")
    assert ssml.startswith('<speak version="1.0"')
    assert "&lt;تیم&gt;" in ssml
    assert "*" not in ssml
    assert 'rate="-5%"' in ssml
    # A hostile voice name cannot be injected.
    hardened = build_ssml("سلام", voice='evil"onload="x', rate_percent="bogus")
    assert 'evil"' not in hardened
    assert 'rate="+0%"' in hardened


def test_settings_from_config_defaults():
    from types import SimpleNamespace

    settings = tts_settings_from(SimpleNamespace(
        tts_provider="azure_speech", tts_voice=None, tts_rate_percent=None,
        tts_azure_speech_key=None, tts_azure_speech_region=None,
    ))
    assert settings.voice == "fa-IR-DilaraNeural"
    assert settings.rate_percent == "+0%"


async def test_azure_backend_posts_ssml_and_returns_audio():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ssml"] = request.content.decode("utf-8")
        captured["format"] = request.headers.get("x-microsoft-outputformat")
        return httpx.Response(200, content=b"RIFF" + b"\x00" * 512)

    settings = TtsSettings(
        provider="azure_speech", voice="fa-IR-FaridNeural", azure_speech_key="test-key",
        azure_speech_region="germanywestcentral",
    )
    audio = await synthesize(
        "روایت تستی", settings, client_factory=lambda t: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=t
        )
    )
    assert audio.startswith(b"RIFF")
    assert captured["format"] == "audio-16khz-32kbitrate-mono-mp3"
    assert "روایت تستی" in captured["ssml"]
    assert "test-key" not in captured["ssml"]


async def test_azure_unauthorized_maps_to_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"{}")

    settings = TtsSettings(provider="azure_speech", azure_speech_key="bad", azure_speech_region="x1")
    with pytest.raises(TextToSpeechError, match="subscription key"):
        await synthesize("متن", settings, client_factory=lambda t: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=t
        ))


async def test_unconfigured_provider_fails_closed():
    with pytest.raises(TextToSpeechError, match="not configured"):
        await synthesize("متن", TtsSettings(provider=""))


def test_edge_rate_normalization():
    from app.integrations.tts.persian_tts import _normalize_rate

    assert _normalize_rate("+0%") == "+0%"
    assert _normalize_rate("-5%") == "-5%"
    assert _normalize_rate("+0") == "+0%"  # missing percent sign
    assert _normalize_rate("bogus") == "+0%"
