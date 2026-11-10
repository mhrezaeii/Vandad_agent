"""Provider registry endpoints (read-only metadata)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import company_scope, get_provider_registry
from app.integrations.providers import ProviderRegistry, UnknownProviderError
from app.schemas.provider import ProviderRead

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
