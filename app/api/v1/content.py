"""Content production endpoints: brands, characters, projects, jobs, videos."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import company_scope
from app.core.exceptions import NotFoundError
from app.schemas.content import (
    BrandCharacterRead,
    BrandCreate,
    BrandPlatformLinksUpdate,
    BrandUpdate,
    CharacterCreateRequest,
    CharacterStatusRequest,
    ContentBrandRead,
    ContentConnectionRequest,
    ContentJobRead,
    ContentProjectCreate,
    ContentProjectRead,
    ContentVideoRead,
    ProjectScriptApproval,
    SceneProjectCreate,
    SceneVideoJobRead,
    SceneVideoStartRequest,
)
from app.security.authorization import CompanyScope
from app.services.content_service import ContentService


async def get_content_service(request: Request) -> AsyncIterator[ContentService]:
    """Request-scoped unit of work for content operations.

    ``ContentService`` mutates rows and relies on the surrounding transaction;
    the session is committed on success and rolled back on any error.
    """
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield ContentService(
                session,
                request.app.state.provider_registry,
                request.app.state.provider_executor,
                request.app.state.master_key_box,
                workflow_queue=getattr(request.app.state, "workflow_queue", None),
                scene_video_registry=getattr(request.app.state, "scene_video_registry", None),
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


router = APIRouter(prefix="/content", tags=["content"])


# ---------------------------------------------------------------------- brands
@router.get("/brands", response_model=list[ContentBrandRead])
async def list_brands(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
    include_archived: bool = False,
) -> list[ContentBrandRead]:
    # Idempotently provision the canonical brand set before listing.
    await service.ensure_default_brands(company_id=scope.company_id)
    brands = await service.list_brands(
        company_id=scope.company_id, include_archived=include_archived
    )
    return [ContentBrandRead.model_validate(brand) for brand in brands]


@router.post("/brands", response_model=ContentBrandRead, status_code=201)
async def create_brand(
    payload: BrandCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentBrandRead:
    brand = await service.create_brand(
        company_id=scope.company_id, data=payload.model_dump()
    )
    return ContentBrandRead.model_validate(brand)


@router.get("/brands/{brand_id}", response_model=ContentBrandRead)
async def get_brand(
    brand_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentBrandRead:
    brand = await service.get_brand(brand_id, company_id=scope.company_id)
    return ContentBrandRead.model_validate(brand)


@router.patch("/brands/{brand_id}", response_model=ContentBrandRead)
async def update_brand(
    brand_id: uuid.UUID,
    payload: BrandUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentBrandRead:
    brand = await service.update_brand(
        brand_id,
        company_id=scope.company_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return ContentBrandRead.model_validate(brand)


@router.put("/brands/{brand_id}/platforms")
async def set_brand_platforms(
    brand_id: uuid.UUID,
    payload: BrandPlatformLinksUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> dict:
    """Replace the verified platform sources linked to a content brand."""
    platform_ids = await service.set_brand_platforms(
        brand_id, company_id=scope.company_id, platform_ids=payload.platform_ids
    )
    return {"brand_id": str(brand_id), "platform_ids": [str(value) for value in platform_ids]}


@router.get("/brands/{brand_id}/platforms")
async def list_brand_platforms(
    brand_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> list[dict]:
    """List the tenant-owned platforms linked to a content brand."""
    platforms = await service.list_brand_platforms(
        brand_id, company_id=scope.company_id
    )
    return [
        {
            "id": str(platform.id),
            "name": platform.name,
            "base_url": platform.base_url,
            "platform_type": platform.platform_type,
        }
        for platform in platforms
    ]


@router.get("/brands/{brand_id}/character", response_model=BrandCharacterRead)
async def get_character(
    brand_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> BrandCharacterRead:
    character = await service.get_character(brand_id, company_id=scope.company_id)
    return BrandCharacterRead.model_validate(character)


@router.post("/brands/{brand_id}/character", response_model=BrandCharacterRead, status_code=201)
async def create_character(
    brand_id: uuid.UUID,
    payload: CharacterCreateRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> BrandCharacterRead:
    character = await service.create_character(
        brand_id,
        company_id=scope.company_id,
        connection_id=payload.connection_id,
        source_image_url=str(payload.source_image_url) if payload.source_image_url else None,
        voice_id=payload.voice_id,
    )
    return BrandCharacterRead.model_validate(character)


@router.post("/brands/{brand_id}/character/refresh", response_model=BrandCharacterRead)
async def refresh_character(
    brand_id: uuid.UUID,
    payload: CharacterCreateRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> BrandCharacterRead:
    """Poll HeyGen photo-avatar training; persist the avatar id when ready."""
    character = await service.refresh_character(
        brand_id,
        company_id=scope.company_id,
        connection_id=payload.connection_id,
    )
    return BrandCharacterRead.model_validate(character)


@router.post("/brands/{brand_id}/character/status", response_model=BrandCharacterRead)
async def set_character_status(
    brand_id: uuid.UUID,
    payload: CharacterStatusRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> BrandCharacterRead:
    character = await service.set_character_status(
        brand_id, company_id=scope.company_id, active=payload.active
    )
    return BrandCharacterRead.model_validate(character)


# -------------------------------------------------------------------- projects
@router.post("/projects", response_model=ContentProjectRead, status_code=201)
async def create_project(
    payload: ContentProjectCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.create_project(
        scope=scope, brand_id=payload.brand_id, topic=payload.topic
    )
    return ContentProjectRead.model_validate(project)


@router.get("/projects/{project_id}", response_model=ContentProjectRead)
async def get_project(
    project_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.get_project(project_id, company_id=scope.company_id)
    return ContentProjectRead.model_validate(project)


@router.post("/projects/{project_id}/generate-script", response_model=ContentProjectRead)
async def generate_script(
    project_id: uuid.UUID,
    payload: ContentConnectionRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.generate_script(
        project_id, scope=scope, connection_id=payload.connection_id
    )
    return ContentProjectRead.model_validate(project)


@router.post("/projects/{project_id}/approve-script", response_model=ContentProjectRead)
async def approve_script(
    project_id: uuid.UUID,
    payload: ProjectScriptApproval,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.approve_script(project_id, scope=scope)
    return ContentProjectRead.model_validate(project)


@router.post("/projects/{project_id}/generate-video", response_model=ContentProjectRead)
async def generate_video(
    project_id: uuid.UUID,
    payload: ContentConnectionRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.generate_video(
        project_id, scope=scope, connection_id=payload.connection_id
    )
    return ContentProjectRead.model_validate(project)


# -------------------------------------------------------------- scene pipeline
@router.get("/scene-providers")
async def list_scene_providers(
    request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
) -> list[dict[str, object]]:
    """List registered scene-video providers (never exposes credentials)."""
    registry = getattr(request.app.state, "scene_video_registry", None)
    if registry is None:
        return []
    return [
        {
            "provider_id": provider.provider_id,
            "display_name": provider.display_name,
            "supports_cancellation": provider.supports_cancellation,
            "requires_network": provider.requires_network,
            "daily_quota": provider.quota_limit_per_day(),
        }
        for provider in registry.list_providers()
    ]


@router.post("/scene-projects", response_model=ContentProjectRead, status_code=201)
async def create_scene_project(
    payload: SceneProjectCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.create_scene_project(
        scope=scope,
        brand_id=payload.brand_id,
        topic=payload.topic,
        target_duration_seconds=payload.target_duration_seconds,
        scene_count=payload.scene_count,
        provider_id=payload.provider_id,
        narration_artifact_id=payload.narration_artifact_id,
    )
    return ContentProjectRead.model_validate(project)


@router.post(
    "/scene-projects/{project_id}/generate-scenario",
    response_model=ContentProjectRead,
)
async def generate_scene_scenario(
    project_id: uuid.UUID,
    payload: ContentConnectionRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.generate_scene_scenario(
        project_id, scope=scope, connection_id=payload.connection_id
    )
    return ContentProjectRead.model_validate(project)


@router.post(
    "/scene-projects/{project_id}/approve-scenario",
    response_model=ContentProjectRead,
)
async def approve_scene_scenario(
    project_id: uuid.UUID,
    payload: ProjectScriptApproval,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.approve_scene_scenario(project_id, scope=scope)
    return ContentProjectRead.model_validate(project)


@router.post(
    "/scene-projects/{project_id}/generate-video",
    response_model=ContentProjectRead,
)
async def generate_scene_video(
    project_id: uuid.UUID,
    payload: SceneVideoStartRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.generate_scene_video(
        project_id, scope=scope, provider_id=payload.provider_id
    )
    return ContentProjectRead.model_validate(project)


@router.post(
    "/scene-projects/{project_id}/cancel",
    response_model=ContentProjectRead,
)
async def cancel_scene_video(
    project_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentProjectRead:
    project = await service.cancel_scene_video(project_id, scope=scope)
    return ContentProjectRead.model_validate(project)


@router.get(
    "/scene-projects/{project_id}/scenes",
    response_model=list[SceneVideoJobRead],
)
async def list_scene_jobs(
    project_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> list[SceneVideoJobRead]:
    jobs = await service.list_scene_jobs(project_id, scope=scope)
    return [SceneVideoJobRead.model_validate(job) for job in jobs]


# ------------------------------------------------------------------------ jobs
@router.get("/jobs/{job_id}", response_model=ContentJobRead)
async def get_job(
    job_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentJobRead:
    status = await service.get_job_status(job_id, company_id=scope.company_id)
    return ContentJobRead(**status)


# ---------------------------------------------------------------------- videos
@router.get("/videos", response_model=list[ContentVideoRead])
async def list_videos(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> list[ContentVideoRead]:
    projects = await service.list_videos(company_id=scope.company_id)
    return [ContentVideoRead.model_validate(project) for project in projects]


@router.get("/videos/{video_id}", response_model=ContentVideoRead)
async def get_video(
    video_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
) -> ContentVideoRead:
    project = await service.get_project(video_id, company_id=scope.company_id)
    if project.status != "completed":
        raise NotFoundError("Video not found.", details=[{"video_id": str(video_id)}])
    return ContentVideoRead.model_validate(project)


@router.get("/videos/{video_id}/content")
async def download_video(
    video_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
    request: Request,
) -> StreamingResponse:
    """Stream the final MP4 after tenant validation (no paths exposed)."""
    project = await service.get_project(video_id, company_id=scope.company_id)
    if project.status != "completed" or project.final_artifact_id is None:
        raise NotFoundError("Video not found.", details=[{"video_id": str(video_id)}])
    from app.services.artifact_service import ArtifactService  # noqa: PLC0415

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        artifact_service = ArtifactService(
            session,
            request.app.state.artifact_storage,
        )
        artifact, stream = await artifact_service.open_artifact_stream(
            project.final_artifact_id, company_id=scope.company_id
        )
    return StreamingResponse(
        stream,
        media_type=artifact.content_type,
        headers={"Content-Length": str(artifact.size_bytes)},
    )
