"""Connection validation service: the safe path from "test this connection" to
a recorded, sanitized outcome.

Flow: tenant check → provider lookup → credential resolution (trusted
boundary) → provider execution under timeout/retry rules → lifecycle update →
audit. The API never sees exceptions from providers; only sanitized results.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.providers import (
    ExecutionOutcome,
    ProviderCapability,
    ProviderError,
    ProviderExecutor,
    ProviderRegistry,
    UnknownProviderError,
)
from app.models import (
    ConnectionStatus,
)
from app.security.crypto import MasterKeyBox
from app.security.redaction import redact_text
from app.services.connection_service import ConnectionService
from app.services.credential_service import CredentialService

logger = logging.getLogger("app.connection_validation")


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    status_after: str
    provider_id: str
    message: str
    error_code: str | None
    latency_ms: float | None
    attempts: int

    @property
    def degraded(self) -> bool:
        return self.status_after == ConnectionStatus.DEGRADED.value


class ConnectionValidationService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box
        self.connections_service = ConnectionService(session, registry)

    async def test_connection(
        self, connection_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> ValidationResult:
        """Run a full validation cycle for one connection. Never raises provider errors."""
        connection = await self.connections_service.get_connection(connection_id, company_id=company_id)

        # Move into VALIDATING before doing anything else: every outcome path
        # (provider missing, credential broken, provider failure) records a
        # proper result from the validating state.
        connection = await self.connections_service.mark_validating(connection)

        try:
            adapter = self.registry.get(connection.provider)
        except UnknownProviderError:
            return await self._fail_without_provider(
                connection, message="Provider is not registered.", code="provider_unregistered"
            )
        if ProviderCapability.CONNECTION_VALIDATION not in adapter.capabilities:
            return await self._fail_without_provider(
                connection, message="Provider does not support connection validation.", code="provider_unsupported"
            )

        # Resolve credential inside the trusted boundary (tenant-checked).
        credential = None
        credential_type: str | None = None
        secret_reference: str | None = None
        if connection.credential_reference_id is not None:
            try:
                credential_service = CredentialService(self.session, self._keys)
                resolved = await credential_service.resolve_credential(
                    connection.credential_reference_id, company_id=company_id
                )
            except Exception:  # noqa: BLE001 - resolution failure is a validation failure
                logger.warning(
                    "credential resolution failed for connection %s", connection.id, exc_info=True
                )
                return await self._record_failure(
                    connection,
                    message="Credential could not be resolved.",
                    code="credential_resolution_failed",
                    attempts=1,
                )
            credential = resolved.payload
            credential_type = resolved.credential_type
            secret_reference = resolved.secret_reference

        context = self.executor.build_context(
            adapter=adapter,
            connection_id=connection.id,
            company_id=connection.company_id,
            platform_id=connection.platform_id,
            configuration=connection.configuration,
            credential=credential,
            credential_type=credential_type,
            secret_reference=secret_reference,
            operation="validate_connection",
            timeout_seconds=connection.configuration.get("timeout_seconds"),
        )
        outcome = await self.executor.run(
            adapter, context, adapter.validate_connection, idempotent=True
        )

        # A provider that *returns* a failure result (instead of raising) reports
        # its own sanitized diagnosis — treat it as a permanent failure with the
        # provider-authored message (redaction still applies downstream).
        if outcome.error is None and outcome.result is not None and not outcome.result.success:
            from app.integrations.providers.base import ProviderPermanentError

            outcome = ExecutionOutcome(
                result=None,
                error=ProviderPermanentError(
                    outcome.result.message, code="provider_reported_failure"
                ),
                attempts=outcome.attempts,
            )

        if outcome.success:
            result = outcome.result
            degraded = bool(result.metadata.get("degraded"))
            updated = await self.connections_service.apply_validation_outcome(
                connection,
                success=True,
                degraded=degraded,
                message=result.message,
                attempts=outcome.attempts,
            )
            return ValidationResult(
                success=True,
                status_after=updated.status,
                provider_id=adapter.provider_id,
                message=redact_text(result.message),
                error_code=None,
                latency_ms=result.latency_ms,
                attempts=outcome.attempts,
            )

        error = outcome.error or ProviderError("The provider operation failed.")
        updated = await self.connections_service.apply_validation_outcome(
            connection,
            success=False,
            message=error.message,
            error_code=error.code,
            attempts=outcome.attempts,
        )
        logger.warning(
            "connection validation failed: connection=%s provider=%s code=%s attempts=%d",
            connection.id,
            adapter.provider_id,
            error.code,
            outcome.attempts,
        )
        return ValidationResult(
            success=False,
            status_after=updated.status,
            provider_id=adapter.provider_id,
            message=redact_text(error.message),
            error_code=error.code,
            latency_ms=None,
            attempts=outcome.attempts,
        )

    async def check_health(
        self, connection_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> ValidationResult:
        """Live provider health check; falls back to validation semantics."""
        connection = await self.connections_service.get_connection(connection_id, company_id=company_id)
        adapter = self.registry.get(connection.provider)
        credential = None
        credential_type = None
        secret_reference = None
        if connection.credential_reference_id is not None:
            resolved = await CredentialService(self.session, self._keys).resolve_credential(
                connection.credential_reference_id, company_id=company_id
            )
            credential, credential_type, secret_reference = (
                resolved.payload,
                resolved.credential_type,
                resolved.secret_reference,
            )
        context = self.executor.build_context(
            adapter=adapter,
            connection_id=connection.id,
            company_id=connection.company_id,
            platform_id=connection.platform_id,
            configuration=connection.configuration,
            credential=credential,
            credential_type=credential_type,
            secret_reference=secret_reference,
            operation="check_health",
        )
        outcome = await self.executor.run(adapter, context, adapter.check_health, idempotent=True)
        if outcome.success:
            return ValidationResult(
                success=True,
                status_after=connection.status,
                provider_id=adapter.provider_id,
                message=redact_text(outcome.result.message),
                error_code=None,
                latency_ms=outcome.result.latency_ms,
                attempts=outcome.attempts,
            )
        error = outcome.error or ProviderError("The provider operation failed.")
        return ValidationResult(
            success=False,
            status_after=connection.status,
            provider_id=adapter.provider_id,
            message=error.message,
            error_code=error.code,
            latency_ms=None,
            attempts=outcome.attempts,
        )

    async def _record_failure(
        self, connection, *, message: str, code: str, attempts: int
    ) -> ValidationResult:
        """Record a failed outcome from the current (validating) state."""
        updated = await self.connections_service.apply_validation_outcome(
            connection, success=False, message=message, error_code=code, attempts=attempts
        )
        return ValidationResult(
            success=False,
            status_after=updated.status,
            provider_id=connection.provider,
            message=message,
            error_code=code,
            latency_ms=None,
            attempts=attempts,
        )

    async def _fail_without_provider(
        self, connection, *, message: str, code: str
    ) -> ValidationResult:
        """Handle missing/unsupported providers (connection already VALIDATING)."""
        return await self._record_failure(connection, message=message, code=code, attempts=1)
