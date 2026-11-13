"""Settings validation behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.video.factory import build_video_registry_from_settings


def test_settings_defaults_are_valid(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
        _env_file=None,
    )
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.db_pool_size == 5


def test_settings_rejects_non_asyncpg_database_url():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://u:p@localhost:5432/db", _env_file=None)


def test_settings_rejects_unknown_log_level():
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD", _env_file=None)


def test_settings_rejects_unknown_environment():
    with pytest.raises(ValidationError):
        Settings(environment="chaos", _env_file=None)


def test_settings_normalizes_log_level_case():
    settings = Settings(log_level="debug", _env_file=None)
    assert settings.log_level == "DEBUG"


def test_settings_ignores_compose_only_variables(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "irrelevant-in-app-settings")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "postgres_password")



def test_generic_scene_video_capability_settings_reach_provider_registry():
    settings = Settings(
        environment="local",
        scene_video_provider="generic_http",
        scene_video_http_base_url="https://video.example.test",
        scene_video_http_supported_resolutions="720x1280,1080x1920",
        scene_video_http_supports_seed=True,
        scene_video_http_max_prompt_characters=3000,
        _env_file=None,
    )
    provider = build_video_registry_from_settings(settings).get("generic_http")

    assert provider.supported_resolutions == ("720x1280", "1080x1920")
    assert provider.supports_seed is True
    assert provider.max_prompt_characters == 3000
    assert provider.input_modalities == ("text",)
    assert provider.max_reference_images == 0



def test_wavespeed_environment_settings_reach_registry():
    settings = Settings(
        environment="local",
        scene_video_provider="wavespeed",
        scene_video_wavespeed_api_key="test-key",
        scene_video_wavespeed_resolution="480p",
        scene_video_wavespeed_aspect_ratio="9:16",
        scene_video_wavespeed_duration_seconds=10,
        scene_video_wavespeed_download_allowed_hosts="cdn.example.test",
        _env_file=None,
    )
    provider = build_video_registry_from_settings(settings).get("wavespeed")
    assert provider.model_id == "wavespeed-ai/ltx-2.3/text-to-video"
    assert provider.config.resolution == "480p"
    assert provider.config.aspect_ratio == "9:16"
    assert provider.config.duration_seconds == 10
    assert provider.config.download_allowed_hosts == ("cdn.example.test",)
    assert provider.supports_composition is False
    assert provider.supports_quality_review is False


def test_wavespeed_uses_explicit_generic_http_finalizer_when_both_paths_configured():
    settings = Settings(
        environment="local",
        scene_video_provider="wavespeed",
        scene_video_wavespeed_api_key="test-key",
        scene_video_http_base_url="https://video.example.test",
        scene_video_http_compose_path="/v1/compose",
        scene_video_http_quality_path="/v1/quality",
        scene_video_http_health_path="/health",
        scene_video_http_api_key="secondary-test-key",
        _env_file=None,
    )
    provider = build_video_registry_from_settings(settings).get("wavespeed")
    assert provider.supports_composition is True
    assert provider.supports_quality_review is True
