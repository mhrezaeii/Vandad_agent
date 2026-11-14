"""AI-native content-production node handlers for the workflow engine.

The active path generates a validated scenario, submits scene prompts to the
selected AI video provider, polls and retrieves provider artifacts, and asks
the provider to compose and quality-review the final video. Legacy character
and avatar execution handlers are intentionally absent.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.integrations.providers.base import (
    ProviderPermanentError,
    ProviderTransientError,
)
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    ContentProject,
    PlatformKnowledgeSnapshot,
    ProjectStatus,
    WorkflowExecution,
    WorkflowNode,
)
from app.services.audit_service import AuditService

logger = logging.getLogger("app.content_nodes")

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


async def sync_project_workflow_failure(engine, execution: WorkflowExecution) -> None:
    """Mirror a terminal workflow failure onto the owning content project.

    Without this sync, a project whose script/video workflow failed stays in
    its in-flight status forever (e.g. ``script_generating``), so the product
    API rejects every retry with 409. Only the matching in-flight status is
    advanced, so a late-arriving stale execution can never clobber newer
    project state.
    """
    from app.services.content_service import (  # noqa: PLC0415 - avoid import cycle
        SCENE_SCRIPT_WORKFLOW_NAME,
        SCENE_VIDEO_WORKFLOW_NAME,
        SCRIPT_WORKFLOW_NAME,
    )

    project_id_raw = (execution.input_metadata or {}).get("project_id")
    if not project_id_raw:
        return
    try:
        project_id = uuid.UUID(str(project_id_raw))
    except ValueError:
        return
    project = await engine.session.get(ContentProject, project_id)
    if project is None or project.company_id != execution.company_id:
        return

    detail = f"Workflow '{execution.workflow_name}' ended with status '{execution.status}'."
    if execution.workflow_name in (SCRIPT_WORKFLOW_NAME, SCENE_SCRIPT_WORKFLOW_NAME):
        if project.status != ProjectStatus.SCRIPT_GENERATING.value:
            return
        project.status = ProjectStatus.SCRIPT_FAILED.value
        project.error_message = detail
    elif execution.workflow_name == SCENE_VIDEO_WORKFLOW_NAME:
        if project.status not in (
            ProjectStatus.VIDEO_GENERATION_PENDING.value,
            ProjectStatus.SCENES_PREPARED.value,
            ProjectStatus.VIDEO_GENERATING.value,
            ProjectStatus.VIDEO_GENERATED.value,
            ProjectStatus.ASSEMBLING.value,
        ):
            return
        # The approved scenario is intact; reverting keeps video generation
        # retryable through the product API.
        project.status = ProjectStatus.APPROVED.value
        project.error_message = detail
    else:
        return
    engine._log(  # noqa: SLF001 - engine-provided logger context
        execution,
        "warning",
        f"Project {project_id} moved to '{project.status}' after workflow failure.",
    )
    await engine.session.flush()


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


def _translate_scene_video_error(exc: Exception) -> None:
    from app.integrations.video.base import SceneVideoPermanentError, SceneVideoTransientError

    if isinstance(exc, SceneVideoTransientError):
        raise ProviderTransientError(exc.message, code=exc.code) from exc
    if isinstance(exc, SceneVideoPermanentError):
        raise ProviderPermanentError(exc.message, code=exc.code) from exc


# ------------------------------------------------------------- update_project
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
    from app.integrations.tts import tts_settings_from  # noqa: PLC0415

    return SceneVideoService(
        engine.session,
        engine.scene_video_registry,
        storage=engine.artifact_storage,
        daily_quota=settings.scene_video_daily_quota,
        max_attempts=settings.scene_video_max_scene_attempts,
        max_size_bytes=settings.artifact_max_size_bytes,
        screenshots_root=settings.platform_screenshots_root,
        tts_settings=tts_settings_from(settings),
    )


async def _brand_for(
    engine, execution: WorkflowExecution, project: ContentProject
) -> ContentBrand:
    brand = await engine.session.get(ContentBrand, project.brand_id)
    if brand is None or brand.company_id != execution.company_id:
        raise NotFoundError("Brand not found for this project.")
    return brand


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
    linked_platform_ids = list((await engine.session.scalars(
        select(BrandPlatformLink.platform_id).where(
            BrandPlatformLink.company_id == execution.company_id,
            BrandPlatformLink.brand_id == brand.id,
        )
    )).all())
    platform_knowledge_context = None
    if linked_platform_ids:
        snapshots = list((await engine.session.scalars(
            select(PlatformKnowledgeSnapshot)
            .where(
                PlatformKnowledgeSnapshot.company_id == execution.company_id,
                PlatformKnowledgeSnapshot.platform_id.in_(linked_platform_ids),
            )
            .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
        )).all())
        latest_by_platform = {}
        for snapshot in snapshots:
            latest_by_platform.setdefault(str(snapshot.platform_id), snapshot)
        compact_knowledge = []
        fact_groups = {
            "products_and_services": 3,
            "capabilities": 5,
            "workflows": 3,
            "target_audiences": 3,
            "differentiators": 3,
        }
        for platform_id, snapshot in latest_by_platform.items():
            knowledge = snapshot.knowledge_json or {}
            item = {
                "platform_id": platform_id,
                "summary": str(knowledge.get("summary") or "")[:1000],
                "unknowns": [str(value)[:180] for value in (knowledge.get("unknowns") or [])[:6]],
            }
            for group, limit in fact_groups.items():
                item[group] = [
                    {
                        "name": str(fact.get("name") or "")[:100],
                        "description": str(fact.get("description") or "")[:240],
                        "evidence": str(fact.get("evidence") or "")[:160],
                        "source_url": str(fact.get("source_url") or "")[:500],
                    }
                    for fact in (knowledge.get(group) or [])[:limit]
                    if isinstance(fact, dict)
                ]
            compact_knowledge.append(item)
        while compact_knowledge:
            encoded_knowledge = json.dumps(
                compact_knowledge, ensure_ascii=False, separators=(",", ":")
            )
            if len(encoded_knowledge) <= 11000:
                platform_knowledge_context = encoded_knowledge
                break
            compact_knowledge.pop()
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
        platform_knowledge_context=platform_knowledge_context,
    )

    service = ScenarioService(
        engine.session, engine.registry, engine.executor, engine._keys  # noqa: SLF001
    )
    raw_task_id = (execution.input_metadata or {}).get("agent_task_id")
    try:
        task_id = uuid.UUID(str(raw_task_id)) if raw_task_id else None
    except (TypeError, ValueError) as exc:
        raise ProviderPermanentError(
            "The workflow contains an invalid agent task budget reference.",
            code="invalid_task_budget_context",
        ) from exc
    from app.services.scenario_service import ScenarioGenerationError  # noqa: PLC0415

    try:
        scenario, metadata = await service.generate(
            company_id=execution.company_id,
            connection_id=uuid.UUID(str(connection_id)),
            request=request,
            timeout_seconds=min(timeout_seconds, 180.0),
            task_id=task_id,
        )
    except ScenarioGenerationError as exc:
        # Translate so the engine records a clean node failure instead of
        # crashing on a non-Provider exception.
        if exc.retryable:
            raise ProviderTransientError(exc.message, code=exc.code) from exc
        raise ProviderPermanentError(exc.message, code=exc.code) from exc

    # Deterministic quality gate (چک کردن سناریو): the LLM output is
    # stochastic, so a review failure is retryable — the engine regenerates
    # before the workflow is allowed to fail.
    from app.services.scenario_review import review_scenario  # noqa: PLC0415

    review_issues = review_scenario(
        scenario,
        target_duration_seconds=request.target_duration_seconds,
        language=brand.language or "fa",
    )
    if review_issues:
        raise ProviderTransientError(
            "Scenario review failed: " + " | ".join(review_issues[:8]),
            code="scenario_review_failed",
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
    try:
        jobs = await service.prepare_scenes(project, scenario, provider_id=str(provider_id))
    except Exception as exc:
        _translate_scene_video_error(exc)
        raise
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
    try:
        summary = await service.submit_pending(project, execution_id=execution.id)
    except Exception as exc:
        _translate_scene_video_error(exc)
        raise
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
    try:
        await service.submit_pending(project, execution_id=execution.id)
        summary = await service.poll_jobs(project)
    except Exception as exc:
        _translate_scene_video_error(exc)
        raise
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
    try:
        summary = await service.retrieve_completed(project, execution_id=execution.id)
    except Exception as exc:
        _translate_scene_video_error(exc)
        raise
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
    """Request AI-provider composition; never assemble clips locally."""
    project_id = _project_id(params, node)
    project = await _require_project(engine, execution, project_id)
    brand = await _brand_for(engine, execution, project)
    service = _scene_service(engine)
    brand_context = {
        "name": brand.display_name or brand.name,
        "description": brand.description,
        "tone": brand.tone,
        "language": brand.language,
        "visual_config": dict(brand.visual_config or {}),
        "cta_config": dict(brand.cta_config or {}),
        "width": brand.video_width,
        "height": brand.video_height,
        "script": project.script_text,
        "call_to_action": project.call_to_action,
    }
    try:
        artifact, metadata = await service.finalize_project_video(
            project, execution_id=execution.id, width=brand.video_width,
            height=brand.video_height, brand_context=brand_context,
        )
    except Exception as exc:
        from app.integrations.video.base import SceneVideoPermanentError, SceneVideoTransientError
        if isinstance(exc, SceneVideoTransientError):
            raise ProviderTransientError(exc.message, code=exc.code) from exc
        if isinstance(exc, SceneVideoPermanentError):
            raise ProviderPermanentError(exc.message, code=exc.code) from exc
        raise

    project.final_artifact_id = artifact.id
    project.assembly_metadata = metadata
    project.status = ProjectStatus.COMPLETED.value
    project.error_message = None
    AuditService(engine.session).record(
        event_type="scene.ai_composed", action="compose",
        company_id=execution.company_id, resource_type="content_project",
        resource_id=project.id,
        metadata={"artifact_id": str(artifact.id), "size_bytes": artifact.size_bytes,
                  "provider": metadata.get("provider"),
                  "provider_job_id": metadata.get("provider_job_id"),
                  "production_path": metadata.get("production_path")},
    )
    await engine.session.flush()
    engine._log(execution, "info",
        f"Provider final video stored as artifact {artifact.id}; no local assembly performed.",
        node_key=node.node_key)
    return {"artifact_id": str(artifact.id), "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256, "provider": metadata.get("provider"),
            "production_path": "ai_provider_composition"}
