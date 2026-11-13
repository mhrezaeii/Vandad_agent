"""Build the scene-video registry from application settings."""

from __future__ import annotations

from typing import Any

from app.integrations.video import (
    APIMasterConfig,
    GenericHttpConfig,
    SceneVideoProviderRegistry,
    WaveSpeedConfig,
    build_scene_video_registry,
)


def build_video_registry_from_settings(settings: Any) -> SceneVideoProviderRegistry:
    """Build configured scene-video providers and fail closed on bad selection."""
    http_config: GenericHttpConfig | None = None
    base_url = getattr(settings, "scene_video_http_base_url", None)
    if base_url:
        http_config = GenericHttpConfig(
            base_url=base_url,
            generate_path=getattr(settings, "scene_video_http_generate_path", "/v1/videos"),
            status_path=getattr(settings, "scene_video_http_status_path", "/v1/videos/{job_id}"),
            retrieve_path=getattr(settings, "scene_video_http_retrieve_path", None),
            compose_path=getattr(settings, "scene_video_http_compose_path", None),
            quality_path=getattr(settings, "scene_video_http_quality_path", None),
            health_path=getattr(settings, "scene_video_http_health_path", None),
            api_key=getattr(settings, "scene_video_http_api_key", None),
            auth_header=getattr(settings, "scene_video_http_auth_header", "Authorization"),
            auth_scheme=getattr(settings, "scene_video_http_auth_scheme", "Bearer"),
            download_allowed_hosts=tuple(
                host.strip()
                for host in getattr(settings, "scene_video_http_download_allowed_hosts", "").split(",")
                if host.strip()
            ),
            supported_modalities=tuple(
                value.strip().lower()
                for value in getattr(settings, "scene_video_http_supported_modalities", "video").split(",")
                if value.strip()
            ),
            supported_languages=tuple(
                value.strip()
                for value in getattr(settings, "scene_video_http_supported_languages", "").split(",")
                if value.strip()
            ) or None,
            supported_aspect_ratios=tuple(
                value.strip()
                for value in getattr(settings, "scene_video_http_supported_aspect_ratios", "").split(",")
                if value.strip()
            ) or None,
            supported_resolutions=tuple(
                value.strip()
                for value in getattr(settings, "scene_video_http_supported_resolutions", "").split(",")
                if value.strip()
            ) or None,
            supports_seed=bool(getattr(settings, "scene_video_http_supports_seed", False)),
            max_prompt_characters=getattr(settings, "scene_video_http_max_prompt_characters", None),
            min_duration_seconds=getattr(settings, "scene_video_http_min_duration_seconds", None),
            max_duration_seconds=getattr(settings, "scene_video_http_max_duration_seconds", None),
        )

    apimaster_config: APIMasterConfig | None = None
    apimaster_key = getattr(settings, "scene_video_apimaster_api_key", None)
    if apimaster_key and apimaster_key.strip():
        apimaster_config = APIMasterConfig(
            api_key=apimaster_key.strip(),
            base_url=getattr(settings, "scene_video_apimaster_base_url", "https://apimaster.ai"),
            model=getattr(settings, "scene_video_apimaster_model", "wan3.0-video-prime"),
            route=getattr(settings, "scene_video_apimaster_route", "gmi"),
            resolution=getattr(settings, "scene_video_apimaster_resolution", "720P"),
            timeout_seconds=getattr(settings, "scene_video_apimaster_timeout_seconds", 60.0),
            download_allowed_hosts=tuple(
                host.strip()
                for host in getattr(settings, "scene_video_apimaster_download_allowed_hosts", "").split(",")
                if host.strip()
            ),
        )

    wavespeed_config: WaveSpeedConfig | None = None
    wavespeed_key = getattr(settings, "scene_video_wavespeed_api_key", None)
    if wavespeed_key and wavespeed_key.strip():
        wavespeed_config = WaveSpeedConfig(
            api_key=wavespeed_key.strip(),
            base_url=getattr(settings, "scene_video_wavespeed_base_url", "https://api.wavespeed.ai/api/v3"),
            model=getattr(settings, "scene_video_wavespeed_model", "wavespeed-ai/ltx-2.3/text-to-video"),
            resolution=getattr(settings, "scene_video_wavespeed_resolution", "480p"),
            aspect_ratio=getattr(settings, "scene_video_wavespeed_aspect_ratio", "9:16"),
            timeout_seconds=getattr(settings, "scene_video_wavespeed_timeout_seconds", 60.0),
            poll_interval_seconds=getattr(settings, "scene_video_poll_interval_seconds", 2.0),
            max_poll_attempts=getattr(settings, "scene_video_max_poll_attempts", 600),
            download_allowed_hosts=tuple(
                host.strip() for host in getattr(settings, "scene_video_wavespeed_download_allowed_hosts", "").split(",") if host.strip()
            ),
        )

    composition_provider = None
    if http_config is not None and http_config.compose_path and http_config.quality_path:
        from app.integrations.video.generic_http import GenericHttpSceneVideoProvider
        composition_provider = GenericHttpSceneVideoProvider(http_config)

    registry = build_scene_video_registry(
        include_mock=not getattr(settings, "is_production", False),
        http_config=http_config,
        apimaster_config=apimaster_config,
        wavespeed_config=wavespeed_config,
        wavespeed_composition_provider=composition_provider,
    )
    selected = getattr(settings, "scene_video_provider", "mock")
    if getattr(settings, "is_production", False) and selected == "mock":
        raise ValueError("The mock scene-video provider is forbidden in production.")
    if not registry.has(selected):
        raise ValueError(
            f"scene_video_provider={selected!r} is not available; "
            "configure its connection or choose 'mock'."
        )
    return registry


__all__ = ["build_video_registry_from_settings"]
