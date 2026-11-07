"""DiagnosticProvider: exercises the integration core without external calls.

This is a genuine built-in (not a mock): it validates connection configuration
shape so operators can verify registry wiring, lifecycle transitions, and the
validation pipeline before a real provider is configured. It never touches the
network and therefore never leaks anything.
"""

from __future__ import annotations

from app.integrations.providers.adapter import ProviderAdapter, ProviderCredentialRequirement
from app.integrations.providers.base import (
    ProviderCapability,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
)


class DiagnosticProvider(ProviderAdapter):
    provider_id = "diagnostic"
    display_name = "Diagnostic"
    description = "Validates integration-core wiring; performs no external calls."
    capabilities = frozenset({ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.HEALTH_CHECK})
    credential_requirements = (ProviderCredentialRequirement("token", required=False),)

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        config = context.configuration
        problems = [
            f"missing configuration key {key!r}"
            for key in ("expected_config_key",)
            if key not in config
        ]
        if problems:
            raise ProviderPermanentError(
                f"Configuration is invalid: {'; '.join(problems)}.", code="invalid_configuration"
            )
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Diagnostic configuration verified.",
            metadata={"mode": "diagnostic", "correlation_id": context.correlation_id},
        )
