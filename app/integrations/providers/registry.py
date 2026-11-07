"""Provider registry: independent registration and lookup of adapters."""

from __future__ import annotations

from app.integrations.providers.adapter import ProviderAdapter


class ProviderRegistryError(Exception):
    pass


class UnknownProviderError(ProviderRegistryError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(f"provider {provider_id!r} is not registered")
        self.provider_id = provider_id


class DuplicateProviderError(ProviderRegistryError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(f"provider {provider_id!r} is already registered")
        self.provider_id = provider_id


class ProviderRegistry:
    """Holds provider adapter instances keyed by ``provider_id``.

    One registry instance is built by the application lifespan and shared via
    ``app.state``; tests build their own and register test adapters freely.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        if not adapter.provider_id:
            raise ProviderRegistryError("adapter must define provider_id")
        if adapter.provider_id in self._adapters:
            raise DuplicateProviderError(adapter.provider_id)
        self._adapters[adapter.provider_id] = adapter

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._adapters[provider_id]
        except KeyError:
            raise UnknownProviderError(provider_id) from None

    def has(self, provider_id: str) -> bool:
        return provider_id in self._adapters

    def list_adapters(self) -> list[ProviderAdapter]:
        return sorted(self._adapters.values(), key=lambda a: a.provider_id)

    def __len__(self) -> int:
        return len(self._adapters)
