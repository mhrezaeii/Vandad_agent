"""Build the only supported video provider (KIE) from application settings."""

from __future__ import annotations

from typing import Any

from app.integrations.video import (
    KIEConfig,
    SceneVideoProviderRegistry,
    build_scene_video_registry,
)


def build_video_registry_from_settings(settings: Any) -> SceneVideoProviderRegistry:
    """Build a KIE-only registry; mock is available only when explicitly selected in tests."""
    selected = str(getattr(settings, "scene_video_provider", "kie")).strip().lower()
    is_production = bool(getattr(settings, "is_production", False))

    if selected == "mock":
        if is_production:
            raise ValueError("The mock scene-video provider is forbidden in production.")
        return build_scene_video_registry(include_mock=True)

    if selected != "kie":
        raise ValueError(
            f"Unsupported scene_video_provider={selected!r}; this release supports only 'kie'."
        )

    api_key = str(getattr(settings, "scene_video_kie_api_key", "") or "").strip()
    if not api_key:
        raise ValueError(
            "SCENE_VIDEO_KIE_API_KEY is required. Add the KIE API key to .env before starting."
        )

    kie_config = KIEConfig(
        api_key=api_key,
        base_url=getattr(settings, "scene_video_kie_base_url", "https://api.kie.ai"),
        model=getattr(settings, "scene_video_kie_model", "grok-imagine/image-to-video"),
        resolution=getattr(settings, "scene_video_kie_resolution", "480p"),
        aspect_ratio=getattr(settings, "scene_video_kie_aspect_ratio", "9:16"),
        mode=getattr(settings, "scene_video_kie_mode", "normal"),
        timeout_seconds=getattr(settings, "scene_video_kie_timeout_seconds", 60.0),
        poll_interval_seconds=getattr(settings, "scene_video_poll_interval_seconds", 3.0),
        max_poll_attempts=getattr(settings, "scene_video_max_poll_attempts", 300),
        price_per_credit_usd=getattr(settings, "scene_video_kie_price_per_credit_usd", 0.005),
        download_allowed_hosts=tuple(
            host.strip()
            for host in getattr(settings, "scene_video_kie_download_allowed_hosts", "").split(",")
            if host.strip()
        ),
        upload_base_url=(getattr(settings, "scene_video_kie_upload_base_url", None) or None),
        upload_path=getattr(settings, "scene_video_kie_upload_path", "vandad/character-references"),
    )
    return build_scene_video_registry(include_mock=False, kie_config=kie_config)


__all__ = ["build_video_registry_from_settings"]
