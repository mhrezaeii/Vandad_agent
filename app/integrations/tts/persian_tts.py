"""Persian text-to-speech for video narration.

Two backends behind one small client:

- ``azure_speech`` — the official Azure Speech REST API (SSML), using the
  neural ``fa-IR-*`` voices. The free tier grants 500k characters per month,
  which covers hundreds of 30-second narrations.
- ``edge_tts`` — the same Microsoft neural voices through the Edge read-aloud
  endpoint. Keyless and free; intended for local development until the Azure
  Speech resource is configured. Unofficial endpoint, may change without
  notice.

The synthesized audio is a plain MP3/opus byte stream; nothing here touches
billing paths of the video provider.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("app.tts")

# Speech synthesis marks (LLM punctuation) must never reach the TTS input.
_TEXT_SANITIZE = re.compile(r"[\*_#`]+")

# Interval between Azure output formats: 16kHz mono MP3 is enough for voice.
_AZURE_OUTPUT_FORMAT = "audio-16khz-32kbitrate-mono-mp3"
_AZURE_VOICE_PATTERN = re.compile(r"^fa-IR-[A-Za-z]+Neural$")


class TextToSpeechError(RuntimeError):
    """Sanitized TTS error; never contains credentials."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TtsSettings:
    provider: str  # "" | "azure_speech" | "edge_tts"
    voice: str = "fa-IR-DilaraNeural"
    rate_percent: str = "+0%"
    azure_speech_key: str = ""
    azure_speech_region: str = ""


def build_ssml(text: str, *, voice: str, rate_percent: str) -> str:
    """Build a minimal, escaped SSML document for one Persian narration."""
    safe_voice = voice if _AZURE_VOICE_PATTERN.fullmatch(voice) else "fa-IR-DilaraNeural"
    safe_rate = rate_percent if re.fullmatch(r"[+-]\d{1,3}%", rate_percent or "") else "+0%"
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="fa-IR">'
        f'<voice name="{html.escape(safe_voice)}">'
        f'<prosody rate="{html.escape(safe_rate)}">{html.escape(_sanitize(text))}</prosody>'
        "</voice></speak>"
    )


def _sanitize(text: str) -> str:
    return _TEXT_SANITIZE.sub(" ", text).strip()


async def synthesize(
    text: str,
    settings: TtsSettings,
    *,
    timeout_seconds: float = 60.0,
    client_factory=None,
) -> bytes:
    """Synthesize one narration chunk; returns encoded audio bytes (MP3)."""
    stripped = _sanitize(text)
    if not stripped:
        raise TextToSpeechError("Narration text is empty.", code="tts_empty_text")
    if settings.provider == "azure_speech":
        return await _synthesize_azure(stripped, settings, timeout_seconds, client_factory)
    if settings.provider == "edge_tts":
        return await _synthesize_edge(stripped, settings)
    raise TextToSpeechError(
        "Text-to-speech is not configured; set TTS_PROVIDER=azure_speech or edge_tts.",
        code="tts_unconfigured",
    )


async def _synthesize_azure(
    text: str, settings: TtsSettings, timeout_seconds: float, client_factory
) -> bytes:
    key = settings.azure_speech_key.strip()
    region = settings.azure_speech_region.strip()
    if not key or not region:
        raise TextToSpeechError(
            "Azure Speech credentials are missing; set TTS_AZURE_SPEECH_KEY and TTS_AZURE_SPEECH_REGION.",
            code="tts_missing_credentials",
        )
    if not re.fullmatch(r"[a-z0-9]+", region):
        raise TextToSpeechError("Azure Speech region looks invalid.", code="tts_invalid_region")
    endpoint = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": _AZURE_OUTPUT_FORMAT,
        "User-Agent": "vandad-agent",
    }
    body = build_ssml(text, voice=settings.voice, rate_percent=settings.rate_percent).encode("utf-8")
    factory = client_factory or (lambda t: httpx.AsyncClient(timeout=t))
    try:
        async with factory(timeout_seconds) as client:
            response = await client.post(endpoint, headers=headers, content=body)
    except (httpx.HTTPError, OSError) as exc:
        raise TextToSpeechError("Azure Speech could not be reached.", code="tts_unreachable") from exc
    if response.status_code == 401 or response.status_code == 403:
        raise TextToSpeechError("Azure Speech rejected the subscription key.", code="tts_unauthorized")
    if response.status_code == 429:
        raise TextToSpeechError("Azure Speech free quota is exhausted for now.", code="tts_quota_exhausted")
    if response.status_code != 200:
        raise TextToSpeechError(
            f"Azure Speech synthesis failed (HTTP {response.status_code}).", code="tts_request_failed"
        )
    audio = response.content
    if len(audio) < 128:
        raise TextToSpeechError("Azure Speech returned an empty audio payload.", code="tts_empty_audio")
    return audio


def _normalize_rate(rate_percent: str) -> str:
    """Return a valid SSML-style rate ('+0%'); invalid input falls back."""
    return rate_percent if re.fullmatch(r"[+-]\d{1,3}%", rate_percent or "") else "+0%"


async def _synthesize_edge(text: str, settings: TtsSettings) -> bytes:
    try:
        import edge_tts  # noqa: PLC0415 - optional free backend
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise TextToSpeechError(
            "The edge-tts package is not installed.", code="tts_backend_missing"
        ) from exc
    rate = _normalize_rate(settings.rate_percent)
    chunks: list[bytes] = []
    try:
        communicate = edge_tts.Communicate(text, settings.voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
    except Exception as exc:  # noqa: BLE001 - the endpoint is unofficial; fail soft
        raise TextToSpeechError(
            "Edge text-to-speech could not synthesize the narration.", code="tts_edge_failed"
        ) from exc
    audio = b"".join(chunks)
    if len(audio) < 128:
        raise TextToSpeechError("Edge text-to-speech returned an empty audio payload.", code="tts_empty_audio")
    return audio


def tts_settings_from(config: object) -> TtsSettings:  # noqa: ANN001 - Settings
    """Extract TTS settings from the application Settings object."""
    def _text(name: str) -> str:
        value = getattr(config, name, "")
        if value is None:
            return ""
        secret = getattr(value, "get_secret_value", None)
        return str(secret() if callable(secret) else value).strip()

    return TtsSettings(
        provider=_text("tts_provider"),
        voice=_text("tts_voice") or "fa-IR-DilaraNeural",
        rate_percent=_text("tts_rate_percent") or "+0%",
        azure_speech_key=_text("tts_azure_speech_key"),
        azure_speech_region=_text("tts_azure_speech_region"),
    )
