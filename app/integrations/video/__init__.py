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
    SceneVideoCompositionRequest,
    SceneVideoError,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoQualityRequest,
    SceneVideoQualityResult,
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

    def resolve_ordered(
        self,
        provider_ids: list[str],
        *,
        require_composition: bool = False,
        require_lipsync: bool = False,
        require_reference_image: bool = False,
    ) -> SceneVideoProvider:
        """Choose the first configured provider satisfying required capabilities.

        The caller owns the explicit priority order. Missing providers and
        capability mismatches are skipped; no implicit vendor is selected.
        """
        for provider_id in provider_ids:
            provider = self._providers.get(provider_id)
            if provider is None:
                continue
            if require_composition and not provider.supports_composition:
                continue
            if require_lipsync and not provider.supports_lipsync:
                continue
            if require_reference_image and not provider.supports_reference_image:
                continue
            return provider
        raise SceneVideoProviderRegistryError(
            "No configured scene-video provider satisfies the requested capabilities."
        )

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
    "SceneVideoCompositionRequest",
    "SceneVideoProviderRegistry",
    "SceneVideoProviderRegistryError",
    "SceneVideoQuotaExhaustedError",
    "SceneVideoRequest",
    "SceneVideoQualityRequest",
    "SceneVideoQualityResult",
    "SceneVideoTransientError",
    "SceneVideoValidation",
    "build_scene_video_registry",
]
