"""Application settings loaded from environment variables and an optional .env file.

This module must stay import-safe without any side effects (no engine/client
creation) so that Alembic, tests and the API can all share one source of truth.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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

    # --- Credential encryption (AES-256-GCM, see app.security.crypto) ---------
    # JSON object mapping master-key version -> base64-encoded 32-byte key,
    # e.g. CREDENTIAL_MASTER_KEYS='{"1": "base64key...", "2": "base64new..."}'.
    # Generate a key with: openssl rand -base64 32
    credential_master_keys: str = Field(default="{}")
    # Key version used for new ciphertexts; must exist in credential_master_keys.
    credential_active_key_version: int = Field(default=1, ge=1)

    # --- Provider execution ----------------------------------------------------
    provider_default_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    provider_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)

    # --- Authorization boundary (see app.security.authorization) ---------------
    # Comma-separated company UUIDs the bootstrap principal may act for.
    # "*" grants all companies and is ONLY valid outside production; production
    # requires an explicit list (fail-secure default deny).
    bootstrap_principal_company_ids: str = "*"
    # Production identity is supplied by a trusted OIDC/auth proxy. The proxy
    # must strip client-supplied X-Auth-* headers and inject these claims itself.
    auth_proxy_shared_secret: SecretStr | None = None
    auth_proxy_user_header: str = "X-Auth-Request-User"
    auth_proxy_company_ids_header: str = "X-Auth-Request-Company-IDs"
    auth_proxy_roles_header: str = "X-Auth-Request-Roles"

    # --- Artifact storage (see app.storage) --------------------------------------
    # Root directory for generated workflow artifacts (mounted volume in Docker).
    artifact_storage_root: str = "data/artifacts"
    # Hard cap per artifact; streaming writes abort beyond it.
    artifact_max_size_bytes: int = Field(default=2 * 1024**3, ge=1)

    # --- Video rendering (see app.services.video_render_service) ----------------
    # Directory containing Persian-capable fonts (the Docker image installs
    # fonts-vazirmatn under /usr/share/fonts). If it does not exist, libass
    # falls back to the system fontconfig.
    video_fonts_dir: str = "/usr/share/fonts"
    video_font_name: str = "Vazirmatn"
    video_ffmpeg_bin: str = "ffmpeg"
    video_ffprobe_bin: str = "ffprobe"

    # --- Scene-based (non-avatar) video pipeline ------------------------------
    # ``mock`` produces synthetic local clips and never calls a network or a
    # paid provider. Set to ``generic_http`` only after an authorised provider
    # contract is configured below.
    scene_video_provider: str = "mock"
    scene_video_daily_quota: int | None = Field(default=None, ge=0)
    scene_video_max_scene_attempts: int = Field(default=3, ge=1, le=10)
    scene_video_poll_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    scene_video_max_poll_attempts: int = Field(default=60, ge=1, le=600)
    scene_video_output_width: int = Field(default=1080, ge=1, le=8192)
    scene_video_output_height: int = Field(default=1920, ge=1, le=8192)
    scene_video_output_fps: int = Field(default=30, ge=1, le=120)
    scene_assembly_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)

    # APIMaster is opt-in; no paid request is made unless selected explicitly.
    scene_video_apimaster_api_key: str | None = None
    scene_video_apimaster_base_url: str = "https://apimaster.ai"
    scene_video_apimaster_model: str = "wan3.0-video-prime"
    scene_video_apimaster_route: Literal["gmi", "native"] = "gmi"
    scene_video_apimaster_resolution: Literal["480P", "720P", "1080P"] = "720P"
    scene_video_apimaster_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    scene_video_apimaster_download_allowed_hosts: str = ""

    # Optional generic HTTP contract for a provider the operator is authorised
    # to use. Disabled unless ``SCENE_VIDEO_HTTP_BASE_URL`` is set. The key is
    # never logged; it is only sent as the configured auth header.
    scene_video_http_base_url: str | None = None
    scene_video_http_generate_path: str = "/v1/videos"
    scene_video_http_status_path: str = "/v1/videos/{job_id}"
    scene_video_http_retrieve_path: str | None = None
    scene_video_http_api_key: str | None = None
    scene_video_http_auth_header: str = "Authorization"
    scene_video_http_auth_scheme: str = "Bearer"
    # Comma-separated exact HTTPS hostnames allowed for provider media downloads.
    scene_video_http_download_allowed_hosts: str = ""

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

    @field_validator("credential_master_keys")
    @classmethod
    def _validate_master_keys_json(cls, value: str) -> str:
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("credential_master_keys must be valid JSON") from exc
        if not isinstance(parsed, dict) or not all(isinstance(k, str) for k in parsed):
            raise ValueError("credential_master_keys must be a JSON object of version -> key")
        return value

    @field_validator("bootstrap_principal_company_ids")
    @classmethod
    def _validate_bootstrap_companies(cls, value: str) -> str:
        value = value.strip()
        if value == "*":
            return value
        import uuid as _uuid

        for part in value.split(","):
            part = part.strip()
            if part:
                _uuid.UUID(part)  # raises ValueError on malformed ids
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
