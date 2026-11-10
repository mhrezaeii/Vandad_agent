"""ProviderAdapter: the abstract contract every provider must implement.

Providers are registered independently in the ``ProviderRegistry`` and never
imported by FastAPI routes or database models. Adapters receive a sanitized
``ProviderExecutionContext`` and must return ``ProviderResult`` objects whose
messages are safe for storage and display.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.integrations.providers.base import (
    ProviderCapability,
    ProviderExecutionContext,
    ProviderResult,
)


@dataclass(frozen=True)
class ProviderCredentialRequirement:
    """What kind of credential this provider needs, and whether it is optional."""

    credential_type: str  # one of CredentialType values
    required: bool = True
    description: str = ""


class ProviderAdapter(ABC):
    """Abstract provider contract."""

    #: Unique registry key, e.g. "http_endpoint".
    provider_id: str = ""
    display_name: str = ""
    description: str = ""
    capabilities: frozenset[ProviderCapability] = frozenset()
    #: Credential types this provider can use.
    credential_requirements: tuple[ProviderCredentialRequirement, ...] = ()
    #: PlatformType values this provider can bind to (empty = any).
    supported_platform_types: frozenset[str] = frozenset()
    #: Whether the provider's default operation is safe to retry.
    default_operation_idempotent: bool = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "provider_id", ""):
            raise TypeError(f"{cls.__name__} must define a non-empty provider_id")

    @abstractmethod
    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        """Verify the connection can reach its target using its credential."""

    async def check_health(self, context: ProviderExecutionContext) -> ProviderResult:
        """Lightweight liveness check; defaults to validation."""
        return await self.validate_connection(context)

    async def execute(self, context: ProviderExecutionContext) -> ProviderResult:
        """Run a provider operation; only providers declaring EXECUTION support it."""
        raise NotImplementedError(f"provider {self.provider_id!r} does not support execution")
