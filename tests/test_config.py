"""Settings validation behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
