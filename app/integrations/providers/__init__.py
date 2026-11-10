"""Provider architecture: adapters, registry, execution.

``build_default_registry`` assembles the built-in adapters; real third-party
providers register themselves here in later phases.
"""

from app.integrations.providers.adapter import ProviderAdapter, ProviderCredentialRequirement
from app.integrations.providers.base import (
    InvalidCredentialError,
    ProviderCapability,
    ProviderError,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.integrations.providers.diagnostic import DiagnosticProvider
from app.integrations.providers.execution import ExecutionOutcome, ProviderExecutor, RetryPolicy
from app.integrations.providers.heygen import HeyGenProvider
from app.integrations.providers.http_endpoint import HttpEndpointProvider
from app.integrations.providers.llm_openai import LlmOpenAiProvider
from app.integrations.providers.registry import (
    DuplicateProviderError,
    ProviderRegistry,
    ProviderRegistryError,
    UnknownProviderError,
)


def build_default_registry() -> ProviderRegistry:
    """Registry with the built-in adapters."""
    registry = ProviderRegistry()
    registry.register(DiagnosticProvider())
    registry.register(HttpEndpointProvider())
    registry.register(LlmOpenAiProvider())
    registry.register(HeyGenProvider())
    return registry


__all__ = [
    "DuplicateProviderError",
    "ExecutionOutcome",
    "HeyGenProvider",
    "HttpEndpointProvider",
    "LlmOpenAiProvider",
    "InvalidCredentialError",
    "DiagnosticProvider",
    "ProviderAdapter",
    "ProviderCapability",
    "ProviderCredentialRequirement",
    "ProviderError",
    "ProviderExecutionContext",
    "ProviderExecutor",
    "ProviderPermanentError",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderResult",
    "ProviderTimeoutError",
    "ProviderTransientError",
    "RetryPolicy",
    "UnknownProviderError",
    "build_default_registry",
]
