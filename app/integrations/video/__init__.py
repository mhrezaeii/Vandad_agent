"""Scene-video provider registry.

Providers are resolved by their stable ``provider_id`` (persisted on every
``SceneVideoJob``). The local mock provider is always available; the generic
HTTP adapter is registered only when an operator supplies an explicit,
authorised HTTP contract.
"""

from __future__ import annotations

from app.integrations.video.apimaster import APIMasterConfig, APIMasterSceneVideoProvider
from app.integrations.video.base import (
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoError,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
    SceneVideoValidation,
)
from app.integrations.video.generic_http import GenericHttpConfig, GenericHttpSceneVideoProvider
from app.integrations.video.mock import MockSceneVideoProvider


class SceneVideoProviderRegistryError(Exception):
    pass


class SceneVideoProviderRegistry:
    """Holds scene-video providers keyed by ``provider_id``."""

    def __init__(self) -> None:
        self._providers: dict[str, SceneVideoProvider] = {}

    def register(self, provider: SceneVideoProvider) -> None:
        if not provider.provider_id:
            raise SceneVideoProviderRegistryError("provider must define provider_id")
        if provider.provider_id in self._providers:
            raise SceneVideoProviderRegistryError(
                f"provider {provider.provider_id!r} is already registered"
            )
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> SceneVideoProvider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise SceneVideoProviderRegistryError(
                f"provider {provider_id!r} is not registered"
            ) from None

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def list_providers(self) -> list[SceneVideoProvider]:
        return sorted(self._providers.values(), key=lambda provider: provider.provider_id)

    def __len__(self) -> int:
        return len(self._providers)


DEFAULT_SCENE_VIDEO_PROVIDER = "mock"


def build_scene_video_registry(
    *,
    include_mock: bool = True,
    http_config: GenericHttpConfig | None = None,
    apimaster_config: APIMasterConfig | None = None,
    mock_provider: MockSceneVideoProvider | None = None,
) -> SceneVideoProviderRegistry:
    """Build the scene-video registry.

    ``mock`` produces synthetic clips and never touches the network. The
    ``generic_http`` provider is registered only when a configuration is
    supplied; there is no silent fallback to any vendor.
    """
    registry = SceneVideoProviderRegistry()
    if include_mock:
        registry.register(mock_provider or MockSceneVideoProvider())
    if http_config is not None:
        registry.register(GenericHttpSceneVideoProvider(http_config))
    if apimaster_config is not None:
        registry.register(APIMasterSceneVideoProvider(apimaster_config))
    return registry


__all__ = [
    "APIMasterConfig",
    "APIMasterSceneVideoProvider",
    "DEFAULT_SCENE_VIDEO_PROVIDER",
    "GenericHttpConfig",
    "GenericHttpSceneVideoProvider",
    "MockSceneVideoProvider",
    "SceneGenerationStatus",
    "SceneJobState",
    "SceneVideoError",
    "SceneVideoPermanentError",
    "SceneVideoProvider",
    "SceneVideoProviderRegistry",
    "SceneVideoProviderRegistryError",
    "SceneVideoQuotaExhaustedError",
    "SceneVideoRequest",
    "SceneVideoTransientError",
    "SceneVideoValidation",
    "build_scene_video_registry",
]
