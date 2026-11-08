"""Test-only provider adapters (registered into app registries inside tests).

These are deliberately NOT part of the production package: they simulate
transient/permanent failures, timeouts, and degraded results to exercise the
execution, lifecycle, and reliability machinery.
"""

from __future__ import annotations

import asyncio

from app.integrations.providers.adapter import ProviderAdapter
from app.integrations.providers.base import (
    ProviderCapability,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)


class ScriptedProvider(ProviderAdapter):
    """Runs a scripted list of behaviors per operation call."""

    provider_id = "scripted"
    display_name = "Scripted Test Provider"
    capabilities = frozenset(
        {ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.HEALTH_CHECK, ProviderCapability.EXECUTION}
    )

    def __init__(self, script: list | None = None) -> None:
        self.script: list = list(script or [])
        self.calls = 0

    def queue(self, *behaviors) -> None:
        self.script.extend(behaviors)

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        self.calls += 1
        return await self._run_script()

    async def execute(self, context: ProviderExecutionContext) -> ProviderResult:
        self.calls += 1
        return await self._run_script()

    async def _run_script(self) -> ProviderResult:
        if not self.script:
            return ProviderResult(provider_id=self.provider_id, success=True, message="scripted ok")
        behavior = self.script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        if isinstance(behavior, float):
            await asyncio.sleep(behavior)
            return ProviderResult(provider_id=self.provider_id, success=True, message="scripted ok")
        if isinstance(behavior, dict):
            return ProviderResult(
                provider_id=self.provider_id,
                success=behavior.get("success", True),
                message=behavior.get("message", "scripted result"),
                metadata=behavior.get("metadata", {}),
            )
        return ProviderResult(provider_id=self.provider_id, success=True, message="scripted ok")

    async def check_health(self, context: ProviderExecutionContext) -> ProviderResult:
        return ProviderResult(provider_id=self.provider_id, success=True, message="healthy", latency_ms=1.0)


class AlwaysTransientProvider(ScriptedProvider):
    provider_id = "always_transient"

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        self.calls += 1
        raise ProviderTransientError("temporary network glitch", code="transient_glitch")


class AlwaysPermanentProvider(ScriptedProvider):
    provider_id = "always_permanent"

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        self.calls += 1
        raise ProviderPermanentError("invalid configuration supplied", code="bad_config")


class SecretEchoProvider(ScriptedProvider):
    """Deliberately broken provider: echoes the credential into its message.

    Used to verify the platform's defense-in-depth redaction when a provider
    misbehaves and leaks secret material into its result message.
    """

    provider_id = "secret_echo"

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        self.calls += 1
        leaked = context.credential.reveal() if context.credential else "no-credential"
        return ProviderResult(
            provider_id=self.provider_id,
            success=False,
            message=f"authentication failed with api_key={leaked}",
            metadata={},
        )
