"""AI-native content production endpoints: brands, scenarios, projects, jobs and videos."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import company_scope
from app.core.exceptions import ConflictError, NotFoundError
from app.models import ContentBrand
from app.schemas.content import (
    BrandCreate,
    BrandPlatformLinksUpdate,
    BrandUpdate,
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


@router.get("/projects", response_model=list[ContentProjectRead])
async def list_projects(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[ContentProjectRead]:
    projects = await service.list_projects(
        company_id=scope.company_id, limit=limit, offset=offset
    )
    return [ContentProjectRead.model_validate(project) for project in projects]


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
        screenshots_platform=payload.screenshots_platform,
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


# ---------------------------------------------------------------- clip assembly
@router.post("/scene-projects/{project_id}/assemble-clips", response_model=ContentProjectRead)
async def assemble_external_clips(
    project_id: uuid.UUID,
    request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[ContentService, Depends(get_content_service)],
    background_tasks: BackgroundTasks,
) -> ContentProjectRead:
    """Assemble the final video from operator-provided clips (no paid provider).

    Clips live at ``assets/clips/<project_id>/01.mp4, 02.mp4, ...`` in playback
    order; each clip covers one scenario scene. The worker adds Persian TTS
    narration, burned subtitles, screenshot overlays, and the branded end card.
    """
    from app.services.clip_assembly_service import ClipAssemblyError, ClipAssemblyService  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.integrations.tts import tts_settings_from  # noqa: PLC0415
    from app.models import ProjectStatus, WorkflowExecution, WorkflowStatus  # noqa: PLC0415

    project = await service.get_project(project_id, company_id=scope.company_id)
    if project.video_mode != "scene":
        raise ConflictError("This project does not use the scene pipeline.")
    allowed = {
        ProjectStatus.APPROVED.value,
        ProjectStatus.VIDEO_GENERATION_PENDING.value,
        ProjectStatus.SCENES_PREPARED.value,
        ProjectStatus.VIDEO_GENERATING.value,
        ProjectStatus.VIDEO_GENERATED.value,
        ProjectStatus.FAILED.value,
    }
    if project.status not in allowed:
        raise ConflictError(
            f"Clip assembly is not allowed from status '{project.status}'.",
            details=[{"status": project.status}],
        )
    if not project.scenario:
        raise ConflictError("The project has no generated scenario to narrate the clips.")

    execution = WorkflowExecution(
        workflow_name="vandad.clip.assembly.v1",
        company_id=scope.company_id,
        trigger_type="api",
        trigger_reference=str(project.id),
        input_metadata={"project_id": str(project.id), "mode": "external_clips"},
        status=WorkflowStatus.RUNNING.value,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    service.session.add(execution)
    await service.session.flush()
    project.status = ProjectStatus.VIDEO_GENERATION_PENDING.value
    project.error_message = None
    await service.session.flush()
    execution_id = execution.id
    brand_width, brand_height = 1080, 1920
    brand = await service.session.get(ContentBrand, project.brand_id)
    if brand is not None:
        brand_width, brand_height = int(brand.video_width), int(brand.video_height)

    settings = get_settings()
    app_state = request.app.state

    async def _run() -> None:
        from app.models import ContentBrand as _Brand  # noqa: PLC0415

        async with app_state.session_factory() as task_session:
            try:
                task_project = await task_session.get(ContentProject, project_id)
                if task_project is None:
                    return
                clip_service = ClipAssemblyService(
                    task_session,
                    app_state.scene_video_registry,
                    app_state.artifact_storage,
                    tts_settings=tts_settings_from(settings),
                    screenshots_root=settings.platform_screenshots_root,
                    clips_root=settings.platform_clips_root,
                    end_card_seconds=settings.end_card_seconds,
                )
                await clip_service.assemble(
                    task_project, execution_id=execution_id,
                    width=brand_width, height=brand_height,
                )
                await task_session.commit()
            except ClipAssemblyError as exc:
                await task_session.rollback()
                async with app_state.session_factory() as err_session:
                    failed_project = await err_session.get(ContentProject, project_id)
                    failed_execution = await err_session.get(WorkflowExecution, execution_id)
                    if failed_project is not None:
                        failed_project.status = ProjectStatus.FAILED.value
                        failed_project.error_message = exc.message[:500]
                    if failed_execution is not None:
                        failed_execution.status = WorkflowStatus.FAILED.value
                        failed_execution.error_message = exc.message[:500]
                    await err_session.commit()
            except Exception:  # noqa: BLE001 - background task must not crash silently
                import logging  # noqa: PLC0415

                logging.getLogger("app.clip_assembly").exception("clip assembly failed")
                await task_session.rollback()
                async with app_state.session_factory() as err_session:
                    failed_project = await err_session.get(ContentProject, project_id)
                    if failed_project is not None:
                        failed_project.status = ProjectStatus.FAILED.value
                        failed_project.error_message = "Clip assembly failed unexpectedly."
                    await err_session.commit()

    background_tasks.add_task(_run)
    await service.session.refresh(project)
    return ContentProjectRead.model_validate(project)
