"""Provider registry endpoints (read-only metadata)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    company_scope,
    get_principal,
    get_provider_registry,
    require_company_admin,
)
from app.integrations.providers import ProviderRegistry, UnknownProviderError
from app.schemas.provider import (
    ProviderBudgetRead,
    ProviderBudgetUpsert,
    ProviderHealthRead,
    ProviderRead,
    ProviderUsageRead,
    ProviderUsageReconcile,
    SceneVideoModelRead,
    SceneVideoProviderRead,
)
from app.services.provider_budget_service import ProviderBudgetService

router = APIRouter(prefix="/providers", tags=["providers"])


def _to_read(adapter) -> ProviderRead:  # noqa: ANN001 - ProviderAdapter
    return ProviderRead(
        provider_id=adapter.provider_id,
        display_name=adapter.display_name,
        description=adapter.description,
        capabilities=sorted(capability.value for capability in adapter.capabilities),
        credential_requirements=[
            {
                "credential_type": req.credential_type,
                "required": req.required,
                "description": req.description,
            }
            for req in adapter.credential_requirements
        ],
        supported_platform_types=sorted(adapter.supported_platform_types),
    )


@router.get("", response_model=list[ProviderRead])
async def list_providers(
    registry: Annotated[ProviderRegistry, Depends(get_provider_registry)],
    _scope: Annotated[object, Depends(company_scope)],
) -> list[ProviderRead]:
    return [_to_read(adapter) for adapter in registry.list_adapters()]



@router.get("/budgets", response_model=list[ProviderBudgetRead])
async def list_provider_budgets(
    request: Request,
    scope: Annotated[object, Depends(company_scope)],
) -> list[ProviderBudgetRead]:
    async with request.app.state.session_factory() as session:
        rows = await ProviderBudgetService(session).list_budgets(scope.company_id)
        return [ProviderBudgetRead.model_validate(row) for row in rows]


@router.put("/budgets/{provider_id}", response_model=ProviderBudgetRead)
async def upsert_provider_budget(
    provider_id: str,
    payload: ProviderBudgetUpsert,
    request: Request,
    scope: Annotated[object, Depends(company_scope)],
    registry: Annotated[ProviderRegistry, Depends(get_provider_registry)],
    _admin: Annotated[object, Depends(require_company_admin)],
) -> ProviderBudgetRead:
    scene_registry = getattr(request.app.state, "scene_video_registry", None)
    if not registry.has(provider_id) and not (
        scene_registry is not None and scene_registry.has(provider_id)
    ):
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Provider is not registered.")
    async with request.app.state.session_factory() as session:
        try:
            row = await ProviderBudgetService(session).set_budget(
                scope.company_id,
                provider_id,
                monthly_limit_usd=payload.monthly_limit_usd,
                per_operation_limit_usd=payload.per_operation_limit_usd,
                enabled=payload.enabled,
            )
            await session.commit()
            await session.refresh(row)
            return ProviderBudgetRead.model_validate(row)
        except Exception:
            await session.rollback()
            raise


@router.get("/telemetry", response_model=list[ProviderHealthRead])
async def provider_telemetry(
    request: Request,
    scope: Annotated[object, Depends(company_scope)],
) -> list[ProviderHealthRead]:
    async with request.app.state.session_factory() as session:
        rows = await ProviderBudgetService(session).telemetry(scope.company_id)
        return [ProviderHealthRead.model_validate(row) for row in rows]


@router.get("/usage", response_model=list[ProviderUsageRead])
async def list_provider_usage(
    request: Request,
    scope: Annotated[object, Depends(company_scope)],
    provider_id: str | None = None,
    limit: int = 100,
) -> list[ProviderUsageRead]:
    bounded_limit = max(1, min(limit, 500))
    async with request.app.state.session_factory() as session:
        rows = await ProviderBudgetService(session).list_usage(
            scope.company_id, provider_id=provider_id, limit=bounded_limit
        )
        return [ProviderUsageRead.model_validate(row) for row in rows]


@router.post("/usage/{record_id}/reconcile", response_model=ProviderUsageRead)
async def reconcile_provider_usage(
    record_id: uuid.UUID,
    payload: ProviderUsageReconcile,
    request: Request,
    scope: Annotated[object, Depends(company_scope)],
    _admin: Annotated[object, Depends(require_company_admin)],
) -> ProviderUsageRead:
    async with request.app.state.session_factory() as session:
        try:
            row = await ProviderBudgetService(session).reconcile_usage(
                scope.company_id,
                record_id,
                actual_cost_usd=payload.actual_cost_usd,
                note=payload.note,
                reconciled_by=scope.principal.subject,
            )
            await session.commit()
            await session.refresh(row)
            return ProviderUsageRead.model_validate(row)
        except Exception:
            await session.rollback()
            raise


@router.get("/scene-video", response_model=list[SceneVideoProviderRead])
async def list_scene_video_providers(
    request: Request,
    _principal: Annotated[object, Depends(get_principal)],
) -> list[SceneVideoProviderRead]:
    registry = getattr(request.app.state, "scene_video_registry", None)
    if registry is None:
        return []
    return [
        SceneVideoProviderRead(
            provider_id=provider.provider_id,
            display_name=provider.display_name,
            model_id=provider.model_id,
            requires_network=provider.requires_network,
            supports_lipsync=provider.supports_lipsync,
            supports_reference_image=provider.supports_reference_image,
            supports_cancellation=provider.supports_cancellation,
            supports_composition=provider.supports_composition,
            daily_quota=provider.quota_limit_per_day(),
        )
        for provider in registry.list_providers()
    ]


@router.get("/scene-video/models", response_model=list[SceneVideoModelRead])
async def list_scene_video_models(
    request: Request,
    _principal: Annotated[object, Depends(get_principal)],
) -> list[SceneVideoModelRead]:
    """Return configured model capabilities without inventing unknown constraints."""
    registry = getattr(request.app.state, "scene_video_registry", None)
    if registry is None:
        return []
    return [
        SceneVideoModelRead(
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            supports_lipsync=provider.supports_lipsync,
            supports_reference_image=provider.supports_reference_image,
            supports_cancellation=provider.supports_cancellation,
            supports_composition=provider.supports_composition,
        )
        for provider in registry.list_providers()
    ]


@router.get("/{provider_id}", response_model=ProviderRead)
async def get_provider(
    provider_id: str,
    registry: Annotated[ProviderRegistry, Depends(get_provider_registry)],
    _scope: Annotated[object, Depends(company_scope)],
) -> ProviderRead:
    try:
        adapter = registry.get(provider_id)
    except UnknownProviderError:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(
            "Provider is not registered.", details=[{"field": "provider_id", "value": provider_id}]
        ) from None
    return _to_read(adapter)
