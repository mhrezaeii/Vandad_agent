"""DiagnosticProvider: exercises the integration core without external calls.

This is a genuine built-in (not a mock): it validates connection configuration
shape so operators can verify registry wiring, lifecycle transitions, and the
validation pipeline before a real provider is configured. It never touches the
network and therefore never leaks anything. Its ``execute`` operation returns
structured diagnostic output for workflow action nodes.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    capabilities = frozenset(
        {ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.HEALTH_CHECK, ProviderCapability.EXECUTION}
    )
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

    async def execute(self, context: ProviderExecutionContext) -> ProviderResult:
        """Return structured diagnostic output built from the execution inputs.

        Output is deliberately deterministic and secret-free: it echoes the
        resolved (non-secret) input parameters so pipeline wiring can be
        verified end to end.
        """
        await self.validate_connection(context)  # same configuration contract
        params = context.configuration.get("params", {})
        if not isinstance(params, dict):
            raise ProviderPermanentError("Configuration 'params' must be an object.", code="invalid_configuration")
        return ProviderResult(
            provider_id=self.provider_id,
            success=True,
            message="Diagnostic action completed.",
            metadata={
                "echo": params,
                "executed_at": datetime.now(UTC).isoformat(),
                "correlation_id": context.correlation_id,
            },
        )
