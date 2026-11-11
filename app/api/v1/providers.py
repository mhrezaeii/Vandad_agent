"""Provider registry endpoints (read-only metadata)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import company_scope, get_provider_registry
from app.integrations.providers import ProviderRegistry, UnknownProviderError
from app.schemas.provider import ProviderBudgetRead, ProviderBudgetUpsert, ProviderRead, ProviderUsageRead
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
