"""Application settings loaded from environment variables and an optional .env file.

This module must stay import-safe without any side effects (no engine/client
creation) so that Alembic, tests and the API can all share one source of truth.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Platform configuration. Every value can be provided via environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "Vandad Agent"
    app_version: str = "0.1.0"
    environment: Literal["local", "development", "staging", "production"] = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # --- Logging ---
    log_level: str = Field(default="INFO")
    log_format: Literal["console", "json"] = "console"
    # Timeout (seconds) applied to readiness dependency checks.
    readiness_timeout_seconds: float = 3.0

    # --- Database ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/vandad_agent"
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1)
    db_echo: bool = False

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout: float = Field(default=5.0, gt=0)
    redis_socket_connect_timeout: float = Field(default=5.0, gt=0)
    redis_max_connections: int = Field(default=10, ge=1)

    # --- n8n integration (configuration prepared for a later phase) ---
    n8n_base_url: str | None = None
    n8n_api_key: str | None = None

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid:
            raise ValueError(f"log_level must be one of {sorted(valid)}")
        return level

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// driver scheme")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
