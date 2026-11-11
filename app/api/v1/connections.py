"""Platform connection endpoints.

Every route resolves an authorized ``CompanyScope`` first (X-Company-ID
header, default company otherwise). Cross-company access returns 404 so the
existence of foreign resources is never revealed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    company_scope,
    get_connection_service,
    get_connection_validation_service,
    pagination_params,
    require_company_admin,
)
from app.core.exceptions import ConflictError, ForbiddenError, UnprocessableEntityError
from app.models import CredentialStorageType, CredentialType
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionEventListResponse,
    ConnectionEventRead,
    ConnectionListResponse,
    ConnectionRead,
    ConnectionTestResult,
    ConnectionUpdate,
    status_is_known,
)
from app.security.authorization import CompanyScope
from app.security.secret_value import SecretValue
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
from app.services.credential_service import CredentialService


def _validate_inline_credential(payload: ConnectionCreate) -> None:
    """Encrypted credentials need a value; reference credentials forbid one."""
    credential = payload.credential
    if credential is None:
        return
    if credential.storage_type == "encrypted" and credential.value is None:
        raise UnprocessableEntityError(
            "credential.value is required when storage_type is 'encrypted'.",
            details=[{"field": "credential.value"}],
        )
    if credential.storage_type == "reference" and credential.value is not None:
        raise UnprocessableEntityError(
            "credential.value must not be provided when storage_type is 'reference'.",
            details=[{"field": "credential.value"}],
        )

router = APIRouter(prefix="/connections", tags=["connections"])


async def _resolve_company(
    payload_company_id: uuid.UUID | None, scope: CompanyScope
) -> uuid.UUID:
    """Payload company must match the authorized scope when provided."""
    if payload_company_id is not None and payload_company_id != scope.company_id:
        raise ForbiddenError(
            "The company_id in the payload does not match the authorized company context.",
            details=[{"field": "company_id", "value": str(payload_company_id)}],
        )
    return scope.company_id


@router.post("", response_model=ConnectionRead, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: ConnectionCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> ConnectionRead:
    company_id = await _resolve_company(payload.company_id, scope)
    _validate_inline_credential(payload)

    credential_reference_id = payload.credential_reference_id
    if payload.credential is not None:
        credential_service = CredentialService(
            service.session, service.master_key_box
        )
        reference = await credential_service.create_credential(
            platform_id=payload.platform_id,
            credential_type=CredentialType(payload.credential.credential_type),
            secret_reference=payload.credential.secret_reference,
            storage_type=CredentialStorageType(payload.credential.storage_type),
            value=(
                SecretValue(payload.credential.value.get_secret_value())
                if payload.credential.value is not None
                else None
            ),
        )
        credential_reference_id = reference.id

    connection = await service.create_connection(
        company_id=company_id,
        platform_id=payload.platform_id,
        name=payload.name,
        provider=payload.provider,
        configuration=payload.configuration,
        credential_reference_id=credential_reference_id,
    )
    return ConnectionRead.model_validate(connection)


@router.get("", response_model=ConnectionListResponse)
async def list_connections(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
    platform_id: uuid.UUID | None = None,
    connection_status: str | None = Query(default=None, alias="status"),
    provider: str | None = None,
) -> ConnectionListResponse:
    if connection_status is not None and not status_is_known(connection_status):
        from app.core.exceptions import UnprocessableEntityError

        raise UnprocessableEntityError(
            "Unknown connection status filter.",
            details=[{"field": "status", "value": connection_status}],
        )
    limit, offset = pagination
    items, total = await service.list_connections(
        company_id=scope.company_id,
        platform_id=platform_id,
        status=connection_status,
        provider=provider,
        limit=limit,
        offset=offset,
    )
    return ConnectionListResponse(
        items=[ConnectionRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{connection_id}", response_model=ConnectionRead)
async def get_connection(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> ConnectionRead:
    connection = await service.get_connection(connection_id, company_id=scope.company_id)
    return ConnectionRead.model_validate(connection)


@router.patch("/{connection_id}", response_model=ConnectionRead)
async def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> ConnectionRead:
    updates = payload.model_dump(exclude_unset=True)
    if "configuration" in updates and updates["configuration"] is not None:
        connection = await service.update_configuration(
            connection_id, company_id=scope.company_id, configuration=updates["configuration"]
        )
    else:
        connection = await service.get_connection(connection_id, company_id=scope.company_id)
        if "name" in updates and updates["name"] is not None:
            if await service.connections.exists_name(connection.platform_id, updates["name"]):
                raise ConflictError(
                    f"A connection named '{updates['name']}' already exists for this platform.",
                    details=[{"field": "name", "value": updates["name"]}],
                )
            connection.name = updates["name"]
            await service.session.flush()
            await service.session.refresh(connection)
    return ConnectionRead.model_validate(connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> Response:
    await service.delete_connection(connection_id, company_id=scope.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{connection_id}/activate", response_model=ConnectionTestResult)
async def activate_connection(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
    validation: Annotated[ConnectionValidationService, Depends(get_connection_validation_service)],
) -> ConnectionTestResult:
    """Activate = validate + connect.

    Runs the full validation cycle in a single unit of work: the connection
    moves to VALIDATING and lands on CONNECTED/DEGRADED/ERROR from the result.
    (Two separate sessions here would deadlock on the connection row lock.)
    """
    result = await validation.test_connection(connection_id, company_id=scope.company_id)
    return ConnectionTestResult(
        success=result.success,
        status_after=result.status_after,
        provider_id=result.provider_id,
        message=result.message,
        error_code=result.error_code,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        degraded=result.degraded,
    )


@router.post("/{connection_id}/deactivate", response_model=ConnectionRead)
async def deactivate_connection(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> ConnectionRead:
    connection = await service.deactivate(connection_id, company_id=scope.company_id)
    return ConnectionRead.model_validate(connection)


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
async def test_connection(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    validation: Annotated[ConnectionValidationService, Depends(get_connection_validation_service)],
) -> ConnectionTestResult:
    result = await validation.test_connection(connection_id, company_id=scope.company_id)
    return ConnectionTestResult(
        success=result.success,
        status_after=result.status_after,
        provider_id=result.provider_id,
        message=result.message,
        error_code=result.error_code,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        degraded=result.degraded,
    )


@router.get("/{connection_id}/health", response_model=ConnectionRead)
async def connection_health(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
) -> ConnectionRead:
    """Stored health information (use POST /test for a live check)."""
    connection = await service.get_connection(connection_id, company_id=scope.company_id)
    return ConnectionRead.model_validate(connection)


@router.get("/{connection_id}/events", response_model=ConnectionEventListResponse)
async def connection_events(
    connection_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ConnectionService, Depends(get_connection_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
) -> ConnectionEventListResponse:
    await service.get_connection(connection_id, company_id=scope.company_id)
    limit, offset = pagination
    events = await service.connections.list_events(connection_id, limit=limit, offset=offset)
    return ConnectionEventListResponse(
        items=[ConnectionEventRead.model_validate(event) for event in events],
        total=len(events),
    )
