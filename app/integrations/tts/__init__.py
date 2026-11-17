"""Persian text-to-speech backends for narration audio."""
from app.integrations.tts.persian_tts import (
    TextToSpeechError,
    TtsSettings,
    build_ssml,
    synthesize,
    tts_settings_from,
)

__all__ = [
    "TextToSpeechError",
    "TtsSettings",
    "build_ssml",
    "synthesize",
    "tts_settings_from",
]
