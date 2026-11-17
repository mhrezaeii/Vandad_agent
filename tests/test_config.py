"""Settings validation behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.video.factory import build_video_registry_from_settings


def test_settings_defaults_are_valid(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("SCENE_VIDEO_PROVIDER", raising=False)
    monkeypatch.delenv("SCENE_VIDEO_GENERATION_MODE", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
        _env_file=None,
    )
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.db_pool_size == 5
    assert settings.scene_video_provider == "kie"
    assert settings.scene_video_generation_mode == "single_shot"
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-6-luna"


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



def test_video_registry_is_kie_only_and_requires_a_key():
    settings = Settings(
        scene_video_provider="kie",
        scene_video_kie_api_key="test-key",
        _env_file=None,
    )
    registry = build_video_registry_from_settings(settings)
    assert [provider.provider_id for provider in registry.list_providers()] == ["kie"]


def test_video_registry_rejects_unselected_future_providers():
    settings = Settings(scene_video_provider="future_provider", _env_file=None)
    with pytest.raises(ValueError, match="supports only 'kie'"):
        build_video_registry_from_settings(settings)


def test_kie_key_is_required_for_the_selected_provider():
    settings = Settings(scene_video_provider="kie", scene_video_kie_api_key="", _env_file=None)
    with pytest.raises(ValueError, match="SCENE_VIDEO_KIE_API_KEY is required"):
        build_video_registry_from_settings(settings)


def test_kie_settings_documented_in_env_example_load_into_settings():
    """Every documented SCENE_VIDEO_KIE_* / mode / character variable is real.

    Guards .env.example drift: parsing the example file with the Settings
    model must succeed and the documented defaults must match the contract.
    """
    from pathlib import Path

    from app.core.config import Settings

    example_path = Path(__file__).resolve().parents[1] / ".env.example"
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    settings = Settings(
        _env_file=example_path,
        _secrets_dir=None,
        scene_video_provider="kie",
        scene_video_generation_mode="single_shot",
    )

    assert settings.scene_video_kie_base_url == "https://api.kie.ai"
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-6-luna"
    # The documented default model is the single-shot image-to-video contract.
    assert settings.scene_video_kie_model == "grok-imagine/image-to-video"
    assert settings.scene_video_kie_resolution == "480p"
    assert settings.scene_video_kie_mode == "normal"
    assert settings.scene_video_generation_mode == "single_shot"
    assert settings.platform_characters_root == "assets/Paltform_charactar"
    assert settings.scene_video_daily_quota is None
    assert settings.scene_video_kie_upload_path == "vandad/character-references"
    # Poll settings documented for KIE exist and are consumed by the factory.
    assert settings.scene_video_poll_interval_seconds > 0
    assert settings.scene_video_max_poll_attempts > 0


def test_kie_single_shot_settings_build_the_documented_provider():
    from app.core.config import Settings
    from app.integrations.video.factory import build_video_registry_from_settings

    settings = Settings(
        scene_video_provider="kie",
        scene_video_kie_api_key="test-key",
        _env_file=None,
    )
    provider = build_video_registry_from_settings(settings).get("kie")
    assert provider.model_id == "grok-imagine/image-to-video"
    assert provider.is_image_to_video is True
    assert provider.supports_reference_image is True
    assert provider.min_duration_seconds == 6
    assert provider.max_duration_seconds == 30

    legacy = Settings(
        scene_video_provider="kie",
        scene_video_kie_api_key="test-key",
        scene_video_kie_model="grok-imagine-video-1-5-preview",
        _env_file=None,
    )
    legacy_provider = build_video_registry_from_settings(legacy).get("kie")
    assert legacy_provider.model_id == "grok-imagine-video-1-5-preview"
    assert legacy_provider.is_image_to_video is False
    assert legacy_provider.min_duration_seconds == 1
    assert legacy_provider.max_duration_seconds == 15


def test_kie_blank_upload_base_url_falls_back_to_the_documented_upload_host():
    """A documented `VAR=` line must behave like an unset optional host."""
    from app.core.config import Settings
    from app.integrations.video.factory import build_video_registry_from_settings
    from app.integrations.video.kie import DEFAULT_UPLOAD_BASE_URL, KIEConfig

    settings = Settings(
        scene_video_provider="kie",
        scene_video_kie_api_key="test-key",
        scene_video_kie_upload_base_url="",
        _env_file=None,
    )
    provider = build_video_registry_from_settings(settings).get("kie")
    assert provider.config.upload_base_url == DEFAULT_UPLOAD_BASE_URL

    # The adapter tolerates a blank override even when constructed directly and
    # still targets the separate documented upload service.
    assert KIEConfig(api_key="test-key", upload_base_url="   ").upload_base_url == (
        DEFAULT_UPLOAD_BASE_URL
    )
    assert KIEConfig(api_key="test-key").upload_base_url == DEFAULT_UPLOAD_BASE_URL
    assert (
        KIEConfig(api_key="test-key", upload_base_url="https://upload.example.test")
        .upload_base_url
        == "https://upload.example.test"
    )
