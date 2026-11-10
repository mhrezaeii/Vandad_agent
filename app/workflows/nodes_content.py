"""Content-production node handlers for the workflow engine.

These nodes wire the generic engine to the video-production domain:

- ``content.update_project``  — persist LLM script output onto the project
- ``content.submit_video``    — idempotent paid HeyGen submission (persisted
  provider_job_id gates every resubmission)
- ``content.poll_video``      — non-terminal statuses raise transient errors so
  the engine's retry/backoff loop re-polls from the persisted job id
- ``content.download_video``  — stream the provider video into ArtifactStorage
- ``content.render_video``    — FFmpeg render (subtitles/branding) into storage

All handlers resolve the project inside the execution's tenant: a project of
another company reads as missing. Paid submission happens exactly once per
project because ``provider_job_id`` has a unique constraint and is checked
before any provider call.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.exceptions import NotFoundError
from app.integrations.providers.base import (
    ProviderError,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderTransientError,
)
from app.models import (
    ArtifactKind,
    BrandCharacter,
    CharacterStatus,
    ContentBrand,
    ContentProject,
    ProjectStatus,
    WorkflowExecution,
    WorkflowNode,
)
from app.services.artifact_service import ArtifactService
from app.services.audit_service import AuditService

logger = logging.getLogger("app.content_nodes")

# Content nodes reference their provider connection explicitly; this map is a
# safety default when a node omits the provider.
_PROVIDER_BY_OPERATION = {
    "submit_video": "heygen",
    "video_status": "heygen",
    "create_photo_avatar": "heygen",
    "photo_avatar_status": "heygen",
    "list_voices": "heygen",
}


async def dispatch_content_node(
    engine,  # noqa: ANN001 - WorkflowEngine (avoid circular import)
    execution: WorkflowExecution,
    node: WorkflowNode,
    params: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Route content.* node types to their handlers."""
    handlers = {
        "content.update_project": _update_project,
        "content.submit_video": _submit_video,
        "content.poll_video": _poll_video,
        "content.download_video": _download_video,
        "content.render_video": _render_video,
        "content.generate_scenario": _generate_scenario,
        "content.prepare_scenes": _prepare_scenes,
        "content.submit_scene_videos": _submit_scene_videos,
        "content.poll_scene_videos": _poll_scene_videos,
        "content.retrieve_scene_videos": _retrieve_scene_videos,
        "content.assemble_scenes": _assemble_scenes,
    }
    handler = handlers.get(node.node_type)
    if handler is None:
        raise ProviderPermanentError(
            f"Unknown content node type {node.node_type!r}.", code="invalid_node_type"
        )
    return await handler(engine, execution, node, params, timeout_seconds)


# ----------------------------------------------------------------- helpers
def _project_id(params: dict[str, Any], node: WorkflowNode) -> uuid.UUID:
    raw = params.get("project_id") or node.configuration.get("project_id")
    if not raw:
        raise ProviderPermanentError(
            "A project_id is required for content nodes.", code="invalid_configuration"
        )
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise ProviderPermanentError(
            "project_id must be a UUID.", code="invalid_configuration"
        ) from exc


async def _require_project(engine, execution: WorkflowExecution, project_id: uuid.UUID) -> ContentProject:
    project = await engine.session.get(ContentProject, project_id)
    if project is None or project.company_id != execution.company_id:
        raise NotFoundError(
            "Project not found.", details=[{"field": "project_id", "value": str(project_id)}]
        )
    return project


async def _run_provider_operation(
    engine,
    execution: WorkflowExecution,
    node: WorkflowNode,
    params: dict[str, Any],
    *,
    connection_id: uuid.UUID,
    operation: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one provider operation through the existing executor boundary."""
    from app.services.credential_service import CredentialService

    provider_id = node.configuration.get("provider") or _PROVIDER_BY_OPERATION.get(operation)
    if not provider_id:
        raise ProviderPermanentError(
            f"No provider configured for content node {node.node_key!r}.",
            code="invalid_configuration",
        )
    adapter = engine.registry.get(provider_id)
    connection = await engine.session.get(
        __import__("app").models.PlatformConnection, connection_id
    )
    if connection is None or connection.company_id != execution.company_id:
        raise ProviderPermanentError(
            "Connection does not exist or belongs to another company.",
            code="invalid_connection_reference",
        )

    credential = None
    credential_type = None
    secret_reference = None
    if connection.credential_reference_id is not None:
        resolved = await CredentialService(engine.session, engine._keys).resolve_credential(
            connection.credential_reference_id, company_id=execution.company_id
        )
        credential, credential_type, secret_reference = (
            resolved.payload,
            resolved.credential_type,
            resolved.secret_reference,
        )

    # Node-level options (response_format, temperature, model overrides, ...)
    # flow to the adapter, while connection/routing keys are stripped.
    action_params = {**node.configuration, **params}
    for routing_key in ("connection_id", "project_id", "provider", "operation"):
        action_params.pop(routing_key, None)
    context: ProviderExecutionContext = engine.executor.build_context(
        adapter=adapter,
        connection_id=connection.id,
        company_id=execution.company_id,
        platform_id=connection.platform_id,
        configuration={**connection.configuration, **action_params},
        credential=credential,
        credential_type=credential_type,
        secret_reference=secret_reference,
        operation=operation,
        timeout_seconds=timeout_seconds,
    )
    outcome = await engine.executor.run(adapter, context, adapter.execute, idempotent=spec_idempotent(node))
    if not outcome.success:
        raise outcome.error or ProviderError("The provider operation failed.")
    return outcome.result.metadata or {}


def spec_idempotent(node: WorkflowNode) -> bool:
    return bool((node.execution_settings or {}).get("idempotent", True))


# ------------------------------------------------------------- update_project
async def _update_project(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)

    # The LLM node's step output carries the generated content: parse its JSON
    # payload ({"title", "script", "segments", "cta"}) into project fields.
    llm_output = await _last_provider_output(engine, execution)
    updates: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    if llm_output:
        content = llm_output.get("content")
        if isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"script": content}  # plain text fallback
        elif isinstance(content, dict):
            parsed = content
    script_text = params.get("script_text") or parsed.get("script")
    if isinstance(script_text, str) and script_text.strip():
        updates["script_text"] = script_text
    segments = params.get("script_segments") or parsed.get("segments")
    if isinstance(segments, list):
        updates["script_segments"] = segments
    title = params.get("video_title") or parsed.get("title")
    if isinstance(title, str) and title.strip():
        updates["video_title"] = title[:500]
    cta = params.get("call_to_action") or parsed.get("cta")
    if isinstance(cta, str) and cta.strip():
        updates["call_to_action"] = cta[:500]
    usage = params.get("llm_usage") or (llm_output or {}).get("usage")
    if isinstance(usage, dict) and any(
        usage.get(key) is not None
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "total_cost")
    ):
        usage_metadata = dict(usage)
        model = params.get("llm_model") or (llm_output or {}).get("model")
        if model:
            usage_metadata["model"] = model
        usage_metadata.setdefault(
            "pricing_metadata",
            {"source": "llm_response_usage", "version": "openai_compatible_v1"},
        )
        updates["llm_usage"] = usage_metadata

    for field, value in updates.items():
        setattr(project, field, value)
    if project.status in (ProjectStatus.DRAFT.value, ProjectStatus.SCRIPT_GENERATING.value):
        project.status = ProjectStatus.SCRIPT_READY.value
    await engine.session.flush()
    engine._log(  # noqa: SLF001 - engine-provided logger context
        execution, "info", f"Project {project_id} updated with script output.", node_key=node.node_key
    )
    return {"project_status": project.status, "updated_fields": sorted(updates.keys())}


async def _last_provider_output(engine, execution: WorkflowExecution) -> dict | None:
    """Latest completed provider step output within this execution."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import WorkflowExecutionStep  # noqa: PLC0415

    result = await engine.session.execute(
        select(WorkflowExecutionStep)
        .where(
            WorkflowExecutionStep.execution_id == execution.id,
            WorkflowExecutionStep.node_type == "action.provider",
            WorkflowExecutionStep.status == "completed",
        )
        .order_by(WorkflowExecutionStep.started_at.desc())
        .limit(1)
    )
    step = result.scalar_one_or_none()
    return (step.output or {}) if step else None


# --------------------------------------------------------------- submit_video
async def _submit_video(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)

    # Idempotency gate: a persisted provider_job_id means HeyGen already
    # accepted a paid generation — never submit again (worker restarts,
    # retries, duplicate claims all land here).
    if project.provider_job_id:
        engine._log(  # noqa: SLF001
            execution,
            "info",
            f"Project already submitted to HeyGen (job {project.provider_job_id}); skipping paid resubmission.",
            node_key=node.node_key,
        )
        return {"provider_job_id": project.provider_job_id, "resubmitted": False}

    from sqlalchemy import select  # noqa: PLC0415

    brand = await engine.session.get(ContentBrand, project.brand_id)
    if brand is None or brand.company_id != execution.company_id:
        raise NotFoundError("Brand not found for this project.")
    character = (
        await engine.session.execute(
            select(BrandCharacter).where(
                BrandCharacter.brand_id == brand.id, BrandCharacter.provider == "heygen"
            )
        )
    ).scalar_one_or_none()
    if character is None or character.status != CharacterStatus.ACTIVE.value or not character.provider_avatar_id:
        raise ProviderPermanentError(
            "The brand has no active HeyGen character; create it first.",
            code="character_not_ready",
        )
    if not project.script_text:
        raise ProviderPermanentError(
            "The project has no approved script text.", code="script_missing"
        )

    voice_id = params.get("voice_id") or character.voice_id
    if not voice_id:
        raise ProviderPermanentError(
            "No voice_id configured for the brand character.", code="voice_missing"
        )

    connection_id = node.configuration.get("connection_id") or params.get("connection_id")
    if not connection_id:
        raise ProviderPermanentError(
            "A heygen connection_id is required to submit video generation.",
            code="invalid_configuration",
        )

    # Stable idempotency key: HeyGen replays the original response for a retry
    # within 24h and returns 409 while the first request is in flight, so a
    # worker restart can never start (or bill) a second generation.
    idempotency_key = f"vandad-video-{project.id}"
    metadata = await _run_provider_operation(
        engine,
        execution,
        node,
        {
            "params": {
                "avatar_id": character.provider_avatar_id,
                "voice_id": str(voice_id),
                "script": project.script_text,
                "idempotency_key": idempotency_key,
                "motion_prompt": params.get("motion_prompt")
                or (brand.visual_config or {}).get("motion_prompt"),
                "expressiveness": params.get("expressiveness")
                or (brand.visual_config or {}).get("expressiveness"),
            },
        },
        connection_id=uuid.UUID(str(connection_id)),
        operation="submit_video",
        timeout_seconds=timeout_seconds,
    )
    provider_job_id = metadata.get("provider_job_id")
    if not provider_job_id:
        raise ProviderPermanentError(
            "HeyGen submission returned no job id.", code="heygen_bad_response"
        )

    project.provider_job_id = str(provider_job_id)
    project.provider_status = "submitted"
    project.status = ProjectStatus.VIDEO_GENERATING.value
    project.submission_metadata = {
        "submitted_at": datetime.now(UTC).isoformat(),
        "provider_api": "heygen_v3_videos",
        "idempotency_key": idempotency_key,
        "avatar_id": character.provider_avatar_id,
        "avatar_kind": character.avatar_kind,
        "voice_id": str(voice_id),
        "brand_video": {"width": brand.video_width, "height": brand.video_height},
        "provider_reported_status": metadata.get("status"),
    }
    AuditService(engine.session).record(
        event_type="video.submitted",
        action="submit",
        company_id=execution.company_id,
        resource_type="content_project",
        resource_id=project.id,
        metadata={"provider_job_id": str(provider_job_id), "brand": brand.name},
    )
    await engine.session.flush()
    return {"provider_job_id": str(provider_job_id), "resubmitted": False}


# ----------------------------------------------------------------- poll_video
async def _poll_video(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    if not project.provider_job_id:
        raise ProviderPermanentError(
            "The project has no persisted HeyGen job id; submission was never accepted.",
            code="not_submitted",
        )
    connection_id = node.configuration.get("connection_id") or params.get("connection_id")
    if not connection_id:
        raise ProviderPermanentError("A heygen connection_id is required to poll.", code="invalid_configuration")

    metadata = await _run_provider_operation(
        engine,
        execution,
        node,
        {"params": {"provider_job_id": project.provider_job_id}},
        connection_id=uuid.UUID(str(connection_id)),
        operation="video_status",
        timeout_seconds=timeout_seconds,
    )
    project.provider_status = str(metadata.get("status", "processing"))
    project.provider_video_url = metadata.get("video_url")
    cost = dict(project.cost_metadata or {})
    cost["provider"] = "heygen"
    cost["provider_api"] = "heygen_v3_videos"
    if isinstance(metadata.get("duration"), (int, float)):
        cost["reported_duration_seconds"] = metadata["duration"]
    for field in ("width", "height", "completed_at", "thumbnail_url", "subtitle_url"):
        if metadata.get(field) is not None:
            cost[field] = metadata[field]
    # HeyGen does not report a per-video price in the status response. Record
    # the pricing source/version and leave actual_cost unset rather than
    # inventing a number; an operator/billing sync can fill it in.
    cost["pricing_metadata"] = {
        "source": "heygen_v3_videos_status",
        "version": "v3",
        "actual_cost_available": False,
    }
    project.cost_metadata = cost
    await engine.session.flush()
    return metadata


# ------------------------------------------------------------- download_video
async def _download_video(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    video_url = params.get("video_url") or project.provider_video_url
    if not video_url:
        raise ProviderPermanentError("No provider video URL to download.", code="video_url_missing")
    if engine.artifact_storage is None:
        raise ProviderPermanentError("Artifact storage is not configured.", code="storage_unavailable")

    artifact_service = ArtifactService(engine.session, engine.artifact_storage, max_size_bytes=2 * 1024**3)

    async def _video_chunks() -> Any:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            async with client.stream("GET", str(video_url)) as response:
                if response.status_code != 200:
                    raise ProviderTransientError(
                        f"Video download responded with status {response.status_code}.",
                        code="download_failed",
                    )
                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk

    artifact = await artifact_service.store_artifact(
        execution.id,
        company_id=execution.company_id,
        kind=ArtifactKind.VIDEO.value,
        content_type="video/mp4",
        chunks=_video_chunks(),
        filename=f"raw-{project.provider_job_id}.mp4",
    )
    project.raw_artifact_id = artifact.id
    project.provider_status = "downloaded"
    cost = dict(project.cost_metadata or {})
    cost["raw_artifact"] = {
        "artifact_id": str(artifact.id),
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "content_type": artifact.content_type,
    }
    project.cost_metadata = cost
    await engine.session.flush()
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Raw video stored as artifact {artifact.id} ({artifact.size_bytes} bytes).",
        node_key=node.node_key,
    )
    return {"artifact_id": str(artifact.id), "size_bytes": artifact.size_bytes, "sha256": artifact.sha256}


# --------------------------------------------------------------- render_video
async def _render_video(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    if not project.raw_artifact_id:
        raise ProviderPermanentError("No raw video artifact to render.", code="raw_artifact_missing")
    if engine.artifact_storage is None:
        raise ProviderPermanentError("Artifact storage is not configured.", code="storage_unavailable")

    from app.core.config import get_settings  # noqa: PLC0415
    from app.services.video_render_service import VideoRenderService  # noqa: PLC0415

    settings = get_settings()
    brand = await engine.session.get(ContentBrand, project.brand_id)
    if brand is None or brand.company_id != execution.company_id:
        raise NotFoundError("Brand not found for this project.")

    render_service = VideoRenderService(
        engine.session,
        engine.artifact_storage,
        max_size_bytes=2 * 1024**3,
        ffmpeg_bin=str(node.configuration.get("ffmpeg_bin", settings.video_ffmpeg_bin)),
        ffprobe_bin=str(node.configuration.get("ffprobe_bin", settings.video_ffprobe_bin)),
        fonts_dir=str(node.configuration.get("fonts_dir", settings.video_fonts_dir)),
        font_name=str(node.configuration.get("font_name", settings.video_font_name)),
    )
    subtitle_text = params.get("subtitle_text") or project.script_text or ""
    artifact, subtitle_path_used = await render_service.render_branded_video(
        execution_id=execution.id,
        company_id=execution.company_id,
        raw_artifact_id=uuid.UUID(str(project.raw_artifact_id)),
        script_text=str(subtitle_text),
        brand_name=brand.name,
        width=brand.video_width,
        height=brand.video_height,
        visual_config=brand.visual_config or {},
        timeout_seconds=min(timeout_seconds, 1800.0),
    )
    project.final_artifact_id = artifact.id
    project.status = ProjectStatus.COMPLETED.value
    cost = dict(project.cost_metadata or {})
    cost["final_artifact"] = {
        "artifact_id": str(artifact.id),
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "content_type": artifact.content_type,
        "width": brand.video_width,
        "height": brand.video_height,
    }
    cost["render"] = {
        "service": "ffmpeg",
        "subtitles": "persian_ass",
        "font": str(node.configuration.get("font_name", "Vazirmatn")),
    }
    project.cost_metadata = cost
    await engine.session.flush()
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Final video rendered and stored as artifact {artifact.id}.",
        node_key=node.node_key,
    )
    return {
        "artifact_id": str(artifact.id),
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


# ============================================================================
# Scene-based (non-avatar) pipeline
# ============================================================================
def _scene_service(engine):  # noqa: ANN001 - WorkflowEngine
    """Build a SceneVideoService bound to the engine's session and storage."""
    from app.core.config import get_settings  # noqa: PLC0415
    from app.services.scene_video_service import SceneVideoService  # noqa: PLC0415

    if engine.scene_video_registry is None:
        raise ProviderPermanentError(
            "The scene-video provider registry is not configured.",
            code="scene_registry_unavailable",
        )
    if engine.artifact_storage is None:
        raise ProviderPermanentError(
            "Artifact storage is not configured.", code="storage_unavailable"
        )
    settings = get_settings()
    return SceneVideoService(
        engine.session,
        engine.scene_video_registry,
        storage=engine.artifact_storage,
        daily_quota=settings.scene_video_daily_quota,
        max_attempts=settings.scene_video_max_scene_attempts,
        max_size_bytes=settings.artifact_max_size_bytes,
    )


async def _brand_for(
    engine, execution: WorkflowExecution, project: ContentProject
) -> ContentBrand:
    brand = await engine.session.get(ContentBrand, project.brand_id)
    if brand is None or brand.company_id != execution.company_id:
        raise NotFoundError("Brand not found for this project.")
    return brand


def _as_uuid_or_none(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ProviderPermanentError(
            "The provided artifact id is not a UUID.", code="invalid_configuration"
        ) from exc


# --------------------------------------------------------- generate_scenario
async def _generate_scenario(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    from app.schemas.scenario import ScenarioRequest  # noqa: PLC0415
    from app.services.scenario_service import ScenarioService  # noqa: PLC0415

    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    brand = await _brand_for(engine, execution, project)

    connection_id = node.configuration.get("connection_id") or params.get("connection_id")
    if not connection_id:
        raise ProviderPermanentError(
            "An LLM connection_id is required to generate the scenario.",
            code="invalid_configuration",
        )

    cta_config = brand.cta_config or {}
    options = project.scene_options or {}
    request = ScenarioRequest(
        topic=project.topic,
        target_duration_seconds=int(
            params.get("target_duration_seconds")
            or options.get("target_duration_seconds")
            or node.configuration.get("target_duration_seconds", 30)
        ),
        scene_count=(
            params.get("scene_count")
            or options.get("scene_count")
            or node.configuration.get("scene_count")
        ),
        brand_name=brand.display_name or brand.name,
        brand_tone=brand.tone,
        character_name=(brand.visual_config or {}).get("character_name") or brand.display_name or brand.name,
        character_description=(brand.visual_config or {}).get("character_description")
        or (brand.visual_config or {}).get("character_reference_prompt"),
        product_context="\\n".join(
            value for value in (
                brand.description,
                (brand.visual_config or {}).get("product_context"),
                (brand.visual_config or {}).get("product_features"),
            )
            if isinstance(value, str) and value.strip()
        ) or None,
        cta_text=params.get("cta_text") or cta_config.get("text"),
        hashtags=list(cta_config.get("hashtags") or []),
        language=brand.language,
    )

    service = ScenarioService(
        engine.session, engine.registry, engine.executor, engine._keys  # noqa: SLF001
    )
    scenario, metadata = await service.generate(
        company_id=execution.company_id,
        connection_id=uuid.UUID(str(connection_id)),
        request=request,
        timeout_seconds=min(timeout_seconds, 180.0),
    )

    project.scenario = scenario.model_dump(mode="json")
    project.script_text = scenario.narration
    project.script_segments = [scene.model_dump(mode="json") for scene in scenario.scenes]
    project.video_title = scenario.title[:500]
    project.call_to_action = scenario.cta[:500] if scenario.cta else None
    usage = metadata.get("usage")
    if isinstance(usage, dict) and any(
        usage.get(key) is not None
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "total_cost")
    ):
        usage_metadata = dict(usage)
        if metadata.get("model"):
            usage_metadata["model"] = metadata["model"]
        usage_metadata.setdefault(
            "pricing_metadata",
            {"source": "llm_response_usage", "version": "openai_compatible_v1"},
        )
        project.llm_usage = usage_metadata
    if project.status in (
        ProjectStatus.DRAFT.value,
        ProjectStatus.SCRIPT_GENERATING.value,
        ProjectStatus.SCRIPT_FAILED.value,
    ):
        project.status = ProjectStatus.SCRIPT_READY.value
    await engine.session.flush()
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Scenario generated for project {project_id} ({len(scenario.scenes)} scenes).",
        node_key=node.node_key,
    )
    return {
        "project_status": project.status,
        "scene_count": len(scenario.scenes),
        "title": scenario.title,
        "total_duration_seconds": scenario.total_duration_seconds,
    }


# ------------------------------------------------------------ prepare_scenes
async def _prepare_scenes(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    from app.core.config import get_settings  # noqa: PLC0415
    from app.schemas.scenario import Scenario  # noqa: PLC0415

    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    if not project.scenario:
        raise ProviderPermanentError(
            "The project has no generated scenario to prepare.", code="scenario_missing"
        )
    scenario = Scenario.model_validate(project.scenario)
    settings = get_settings()
    provider_id = (
        params.get("provider_id")
        or node.configuration.get("provider_id")
        or project.scene_video_provider
        or settings.scene_video_provider
    )
    service = _scene_service(engine)
    jobs = await service.prepare_scenes(project, scenario, provider_id=str(provider_id))
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Prepared {len(jobs)} scene jobs for project {project_id}.",
        node_key=node.node_key,
    )
    return {
        "project_status": project.status,
        "provider": str(provider_id),
        "job_count": len(jobs),
        "job_ids": [str(job.id) for job in jobs],
    }


# ------------------------------------------------------- submit_scene_videos
async def _submit_scene_videos(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    service = _scene_service(engine)
    summary = await service.submit_pending(project, execution_id=execution.id)
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Submitted {summary['submitted']} scene video(s) for project {project_id}.",
        node_key=node.node_key,
    )
    return summary


# --------------------------------------------------------- poll_scene_videos
async def _poll_scene_videos(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    service = _scene_service(engine)

    # A failed scene with retry budget left is reset to ``pending`` by the poll
    # handler; submitting before polling is what actually retries it.
    await service.submit_pending(project, execution_id=execution.id)
    summary = await service.poll_jobs(project)
    if not summary["all_terminal"]:
        raise ProviderTransientError(
            "Scene videos are still generating.", code="scene_generation_in_progress"
        )
    if summary["failed"]:
        raise ProviderPermanentError(
            "One or more scene videos failed to generate.", code="scene_generation_failed"
        )
    if summary["cancelled"]:
        raise ProviderPermanentError(
            "Scene video generation was cancelled.", code="scene_generation_cancelled"
        )
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"All {summary['completed']} scene videos are ready for project {project_id}.",
        node_key=node.node_key,
    )
    return summary


# ------------------------------------------------------ retrieve_scene_videos
async def _retrieve_scene_videos(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    service = _scene_service(engine)
    summary = await service.retrieve_completed(project, execution_id=execution.id)
    if summary["failed"]:
        raise ProviderPermanentError(
            "One or more scene videos could not be downloaded.", code="scene_download_failed"
        )
    if summary["completed"] < summary["total"]:
        raise ProviderTransientError(
            "Scene videos are not all downloaded yet.", code="scene_download_incomplete"
        )
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Downloaded {summary['downloaded']} scene video(s) for project {project_id}.",
        node_key=node.node_key,
    )
    return summary


# ----------------------------------------------------------- assemble_scenes
async def _assemble_scenes(
    engine, execution: WorkflowExecution, node: WorkflowNode, params: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    from app.core.config import get_settings  # noqa: PLC0415
    from app.services.scene_assembly_service import SceneAssemblyService  # noqa: PLC0415

    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    brand = await _brand_for(engine, execution, project)
    if engine.artifact_storage is None:
        raise ProviderPermanentError("Artifact storage is not configured.", code="storage_unavailable")

    service = _scene_service(engine)
    jobs = await service.list_jobs(project)
    if not jobs:
        raise ProviderPermanentError("No scene jobs to assemble.", code="scenes_missing")
    if any(job.artifact_id is None for job in jobs):
        raise ProviderPermanentError(
            "Every scene must be downloaded before assembly.", code="scene_artifact_missing"
        )

    settings = get_settings()
    assembly = SceneAssemblyService(
        engine.session,
        engine.artifact_storage,
        ffmpeg_bin=settings.video_ffmpeg_bin,
        ffprobe_bin=settings.video_ffprobe_bin,
        fonts_dir=settings.video_fonts_dir,
        font_name=settings.video_font_name,
        max_size_bytes=settings.artifact_max_size_bytes,
    )
    narration_artifact_id = _as_uuid_or_none(
        params.get("narration_artifact_id") or node.configuration.get("narration_artifact_id")
    )
    artifact, metadata = await assembly.assemble(
        execution_id=execution.id,
        company_id=execution.company_id,
        project=project,
        brand=brand,
        scene_jobs=jobs,
        narration_artifact_id=narration_artifact_id,
        allow_silent=bool(params.get("allow_silent", False) or node.configuration.get("allow_silent", False)),
        width=brand.video_width or settings.scene_video_output_width,
        height=brand.video_height or settings.scene_video_output_height,
        fps=settings.scene_video_output_fps,
        timeout_seconds=min(timeout_seconds, settings.scene_assembly_timeout_seconds),
    )
    project.final_artifact_id = artifact.id
    project.assembly_metadata = metadata
    project.status = ProjectStatus.COMPLETED.value
    project.error_message = None
    AuditService(engine.session).record(
        event_type="scene.assembled",
        action="assemble",
        company_id=execution.company_id,
        resource_type="content_project",
        resource_id=project.id,
        metadata={
            "artifact_id": str(artifact.id),
            "size_bytes": artifact.size_bytes,
            "scenes": metadata.get("scenes"),
            "narration": metadata.get("narration"),
        },
    )
    await engine.session.flush()
    engine._log(  # noqa: SLF001
        execution,
        "info",
        f"Final scene video assembled as artifact {artifact.id}.",
        node_key=node.node_key,
    )
    return {
        "artifact_id": str(artifact.id),
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "duration_seconds": metadata.get("duration_seconds"),
        "narration": metadata.get("narration"),
    }

