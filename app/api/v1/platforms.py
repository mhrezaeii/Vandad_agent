"""Platform CRUD endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import company_scope, get_platform_service, pagination_params
from app.security.authorization import CompanyScope
from app.schemas.platform import PlatformCreate, PlatformListResponse, PlatformRead, PlatformUpdate
from app.services.platform_service import PlatformService

router = APIRouter(prefix="/platforms", tags=["platforms"])


@router.post("", response_model=PlatformRead, status_code=status.HTTP_201_CREATED)
async def create_platform(
    payload: PlatformCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> PlatformRead:
    platform = await service.create_platform(payload, company_id=scope.company_id)
    return PlatformRead.model_validate(platform)


@router.get("", response_model=PlatformListResponse)
async def list_platforms(
    service: Annotated[PlatformService, Depends(get_platform_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
    scope: Annotated[CompanyScope, Depends(company_scope)],
    status_filter: str | None = None,
    platform_type: str | None = None,
) -> PlatformListResponse:
    limit, offset = pagination
    items, total = await service.list_platforms(
        company_id=scope.company_id,
        status=status_filter,
        platform_type=platform_type,
        limit=limit,
        offset=offset,
    )
    return PlatformListResponse(
        items=[PlatformRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{platform_id}", response_model=PlatformRead)
async def get_platform(
    platform_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> PlatformRead:
    return PlatformRead.model_validate(await service.get_platform(platform_id, company_id=scope.company_id))


@router.patch("/{platform_id}", response_model=PlatformRead)
async def update_platform(
    platform_id: uuid.UUID,
    payload: PlatformUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> PlatformRead:
    return PlatformRead.model_validate(await service.update_platform(platform_id, payload, company_id=scope.company_id))


@router.delete("/{platform_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform(
    platform_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> Response:
    await service.delete_platform(platform_id, company_id=scope.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
