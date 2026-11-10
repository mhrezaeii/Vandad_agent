"""Build the scene-video registry from application settings."""

from __future__ import annotations

from typing import Any

from app.integrations.video import (
    GenericHttpConfig,
    SceneVideoProviderRegistry,
    build_scene_video_registry,
)


def build_video_registry_from_settings(settings: Any) -> SceneVideoProviderRegistry:
    """Return the configured registry, failing fast on an unusable selection.

    ``mock`` is always available. ``generic_http`` is registered only when a
    base URL is configured; selecting it without one is a configuration error,
    never a silent fallback to another provider.
    """
    http_config: GenericHttpConfig | None = None
    base_url = getattr(settings, "scene_video_http_base_url", None)
    if base_url:
        http_config = GenericHttpConfig(
            base_url=base_url,
            generate_path=getattr(settings, "scene_video_http_generate_path", "/v1/videos"),
            status_path=getattr(settings, "scene_video_http_status_path", "/v1/videos/{job_id}"),
            retrieve_path=getattr(settings, "scene_video_http_retrieve_path", None),
            api_key=getattr(settings, "scene_video_http_api_key", None),
            auth_header=getattr(settings, "scene_video_http_auth_header", "Authorization"),
            auth_scheme=getattr(settings, "scene_video_http_auth_scheme", "Bearer"),
            download_allowed_hosts=tuple(
                host.strip()
                for host in getattr(settings, "scene_video_http_download_allowed_hosts", "").split(",")
                if host.strip()
            ),
        )

    registry = build_scene_video_registry(http_config=http_config)
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
