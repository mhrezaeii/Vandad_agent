"""Platform connection lifecycle service.

Owns connection CRUD and lifecycle transitions. Transition rules come from
``connection_lifecycle``; every accepted transition (and every denied one, via
an exception at the API boundary) leaves an auditable trace — accepted ones add
a ``ConnectionEvent`` plus an ``AuditEvent`` in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.correlation import get_correlation_id
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.integrations.providers import ProviderCapability, ProviderRegistry, UnknownProviderError
from app.models import (
    Company,
    ConnectionStatus,
    Platform,
    PlatformConnection,
)
from app.repositories import ConnectionRepository
from app.security.redaction import redact_text
from app.services.audit_service import AuditService
from app.services.connection_lifecycle import can_transition

_CONNECTION_RESOURCE = "connection"


class ConnectionService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        *,
        master_key_box=None,  # MasterKeyBox; optional for connections without credentials
    ) -> None:
        self.session = session
        self.registry = registry
        self.connections = ConnectionRepository(session)
        self.audit = AuditService(session)
        self.master_key_box = master_key_box

    # --- creation -------------------------------------------------------------
    async def create_connection(
        self,
        *,
        company_id: uuid.UUID,
        platform_id: uuid.UUID,
        name: str,
        provider: str,
        configuration: dict[str, Any] | None = None,
        credential_reference_id: uuid.UUID | None = None,
    ) -> PlatformConnection:
        self._require_provider(provider)
        platform = await self._require_platform(platform_id)
        if platform.company_id != company_id:
            # Tenant mismatch: do not reveal the platform's existence.
            raise NotFoundError(
                "Platform not found.", details=[{"field": "platform_id", "value": str(platform_id)}]
            )

        if credential_reference_id is not None:
            await self._require_credential_in_tenant(credential_reference_id, platform.company_id)

        if await self.connections.exists_name(platform_id, name):
            raise ConflictError(
                f"A connection named '{name}' already exists for this platform.",
                details=[{"field": "name", "value": name}],
            )

        initial_status = (
            ConnectionStatus.CONFIGURED.value
            if configuration
            else ConnectionStatus.DRAFT.value
        )
        connection = PlatformConnection(
            company_id=platform.company_id,
            platform_id=platform_id,
            name=name,
            provider=provider,
            status=initial_status,
            configuration=configuration or {},
            credential_reference_id=credential_reference_id,
        )
        connection = await self.connections.add(connection)
        self.connections.add_event(
            connection_id=connection.id,
            event_type="created",
            to_status=initial_status,
            success=True,
            message="Connection created.",
            event_metadata={"provider": provider},
            correlation_id=get_correlation_id(),
        )
        self.audit.record(
            event_type="connection.created",
            action="create",
            company_id=connection.company_id,
            platform_id=connection.platform_id,
            resource_type=_CONNECTION_RESOURCE,
            resource_id=connection.id,
            metadata={"name": name, "provider": provider, "status": initial_status},
        )
        await self.session.flush()
        return connection

    # --- lifecycle ------------------------------------------------------------
    async def update_configuration(
        self, connection_id: uuid.UUID, *, company_id: uuid.UUID, configuration: dict[str, Any]
    ) -> PlatformConnection:
        connection = await self.get_connection(connection_id, company_id=company_id)
        connection.configuration = configuration
        await self.session.flush()
        await self.session.refresh(connection)

        # Configuration changes require re-validation before the connection can
        # be considered healthy again.
        if connection.status in (ConnectionStatus.CONNECTED.value, ConnectionStatus.DEGRADED.value):
            await self._transition(
                connection,
                ConnectionStatus.CONFIGURED,
                event_type="state_changed",
                message="Configuration updated; re-validation required.",
            )

        self.connections.add_event(
            connection_id=connection.id,
            event_type="config_updated",
            success=True,
            message="Connection configuration updated.",
            event_metadata={"changed_keys": sorted(configuration.keys())},
            correlation_id=get_correlation_id(),
        )
        self.audit.record(
            event_type="connection.updated",
            action="update",
            company_id=connection.company_id,
            platform_id=connection.platform_id,
            resource_type=_CONNECTION_RESOURCE,
            resource_id=connection.id,
            metadata={"changed_keys": sorted(configuration.keys())},
        )
        await self.session.flush()
        return connection

    async def mark_validating(self, connection: PlatformConnection) -> PlatformConnection:
        return await self._transition(
            connection,
            ConnectionStatus.VALIDATING,
            event_type="state_changed",
            message="Connection validation started.",
        )

    async def apply_validation_outcome(
        self,
        connection: PlatformConnection,
        *,
        success: bool,
        degraded: bool = False,
        message: str,
        error_code: str | None = None,
        attempts: int = 1,
    ) -> PlatformConnection:
        """Record a validation outcome and move the connection to its result state.

        A failed test never marks a connection healthy; a successful one clears
        failure counters and stamps ``last_connected_at``.
        """
        now = datetime.now(UTC)
        connection.last_tested_at = now
        if success and not degraded:
            target = ConnectionStatus.CONNECTED
            connection.last_connected_at = now
            connection.consecutive_failures = 0
            connection.last_error_code = None
            connection.last_error_message = None
        elif success and degraded:
            target = ConnectionStatus.DEGRADED
            connection.consecutive_failures = 0
        else:
            target = ConnectionStatus.ERROR
            connection.last_failure_at = now
            connection.last_error_code = error_code
            connection.last_error_message = redact_text(message)[:500]
            connection.consecutive_failures = connection.consecutive_failures + max(1, attempts)

        updated = await self._transition(
            connection,
            target,
            event_type="test_result",
            success=success and not degraded,
            message=message,
            metadata={"attempts": attempts, "error_code": error_code},
        )
        self.audit.record(
            event_type="connection.validated",
            action="test",
            company_id=connection.company_id,
            platform_id=connection.platform_id,
            resource_type=_CONNECTION_RESOURCE,
            resource_id=connection.id,
            metadata={
                "success": success and not degraded,
                "outcome_status": target.value,
                "error_code": error_code,
                "attempts": attempts,
            },
        )
        await self.session.flush()
        return updated

    async def deactivate(self, connection_id: uuid.UUID, *, company_id: uuid.UUID) -> PlatformConnection:
        connection = await self.get_connection(connection_id, company_id=company_id)
        return await self._transition(
            connection,
            ConnectionStatus.DISCONNECTED,
            event_type="state_changed",
            message="Connection deactivated.",
        )

    async def delete_connection(self, connection_id: uuid.UUID, *, company_id: uuid.UUID) -> None:
        connection = await self.get_connection(connection_id, company_id=company_id)
        snapshot = {
            "name": connection.name,
            "provider": connection.provider,
            "status": connection.status,
        }
        await self.connections.delete(connection)
        self.audit.record(
            event_type="connection.deleted",
            action="delete",
            company_id=company_id,
            platform_id=connection.platform_id,
            resource_type=_CONNECTION_RESOURCE,
            resource_id=connection.id,
            metadata=snapshot,
        )

    # --- queries ---------------------------------------------------------------
    async def get_connection(self, connection_id: uuid.UUID, *, company_id: uuid.UUID) -> PlatformConnection:
        """Tenant-aware fetch: a connection of another company reads as missing."""
        connection = await self.connections.get(connection_id)
        if connection is None or connection.company_id != company_id:
            raise NotFoundError(
                "Connection not found.", details=[{"field": "connection_id", "value": str(connection_id)}]
            )
        return connection

    async def list_connections(
        self,
        *,
        company_id: uuid.UUID,
        platform_id: uuid.UUID | None = None,
        status: str | None = None,
        provider: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[PlatformConnection], int]:
        return await self.connections.list(
            company_id=company_id,
            platform_id=platform_id,
            status=status,
            provider=provider,
            limit=limit,
            offset=offset,
        )

    # --- internals ---------------------------------------------------------------
    async def _transition(
        self,
        connection: PlatformConnection,
        target: ConnectionStatus,
        *,
        event_type: str,
        success: bool | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlatformConnection:
        if not can_transition(connection.status, target.value):
            raise ConflictError(
                f"Cannot transition connection from '{connection.status}' to '{target.value}'.",
                details=[{"from": connection.status, "to": target.value}],
            )
        from_status = connection.status
        connection.status = target.value
        if target is ConnectionStatus.CONNECTED and connection.activated_at is None:
            connection.activated_at = datetime.now(UTC)
        self.connections.add_event(
            connection_id=connection.id,
            event_type=event_type,
            from_status=from_status,
            to_status=target.value,
            success=success,
            message=message,
            event_metadata=metadata,
            correlation_id=get_correlation_id(),
        )
        await self.session.flush()
        # Reload server-managed columns (updated_at) before serialization.
        await self.session.refresh(connection)
        return connection

    def _require_provider(self, provider: str):
        try:
            return self.registry.get(provider)
        except UnknownProviderError:
            raise NotFoundError(
                "Provider is not registered.", details=[{"field": "provider", "value": provider}]
            ) from None

    async def _require_platform(self, platform_id: uuid.UUID) -> Platform:
        platform = await self.session.get(Platform, platform_id)
        if platform is None:
            raise NotFoundError(
                "Platform not found.", details=[{"field": "platform_id", "value": str(platform_id)}]
            )
        return platform

    async def _require_credential_in_tenant(
        self, credential_reference_id: uuid.UUID, company_id: uuid.UUID
    ) -> None:
        from app.models import CredentialReference

        credential = await self.session.get(CredentialReference, credential_reference_id)
        if credential is None:
            raise NotFoundError(
                "Credential not found.",
                details=[{"field": "credential_reference_id", "value": str(credential_reference_id)}],
            )
        platform = await self.session.get(Platform, credential.platform_id)
        if platform is None or platform.company_id != company_id:
            raise ForbiddenError(
                "The credential does not belong to this company.",
                details=[{"field": "credential_reference_id", "value": str(credential_reference_id)}],
            )

    @staticmethod
    async def require_company(company_id: uuid.UUID, session: AsyncSession) -> Company:
        company = await session.get(Company, company_id)
        if company is None:
            raise NotFoundError("Company not found.", details=[{"field": "company_id", "value": str(company_id)}])
        return company


def provider_supports_validation(registry: ProviderRegistry, provider: str) -> bool:
    adapter = registry.get(provider)
    return ProviderCapability.CONNECTION_VALIDATION in adapter.capabilities
