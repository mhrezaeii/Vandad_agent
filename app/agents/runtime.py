"""Agent execution contracts, handler registry and dispatcher."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from sqlalchemy import select

from app.agents import AgentRegistry
from app.agents.contracts import has_capability_contract, validate_capability_payload
from app.core.exceptions import ConflictError, NotFoundError
from app.models import Platform
from app.services.platform_discovery_service import PlatformDiscoveryService
from app.services.platform_knowledge_service import PlatformKnowledgeService


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    company_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    worker_id: str
    payload: dict[str, Any]
    session_factory: Any
    provider_registry: Any
    provider_executor: Any
    master_key_box: Any
    cancel_event: asyncio.Event | None = None
    progress_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    workflow_queue: Any = None
    scene_video_registry: Any = None
    default_scene_video_provider: str = "mock"
    is_production: bool = False

    async def report_progress(self, stage: str, **details: Any) -> None:
        self.raise_if_cancelled()
        if self.progress_callback is not None:
            await self.progress_callback(stage, details)

    def raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentExecutionCancelled("Agent task cancellation was requested.")


class AgentExecutionCancelled(Exception):
    """Raised when cooperative cancellation is observed at a safe boundary."""


class AgentHandler(Protocol):
    agent_key: str
    capabilities: frozenset[str]

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]: ...


class AgentCapabilityRouter:
    """Resolve only explicitly registered (agent, capability) pairs."""

    def __init__(self, registry: AgentRegistry, handlers: list[AgentHandler]) -> None:
        self.registry = registry
        self.handlers = {handler.agent_key: handler for handler in handlers}
        if len(self.handlers) != len(handlers):
            raise ValueError("Agent handler keys must be unique.")
        self._routes: dict[tuple[str, str], AgentHandler] = {}
        for agent_key, handler in self.handlers.items():
            definition = registry.get(agent_key)
            if not definition.dispatch_ready:
                raise ValueError(
                    f"Handler '{agent_key}' is registered but its agent is not dispatch-ready."
                )
            for capability in handler.capabilities:
                if capability not in definition.capabilities:
                    raise ValueError(
                        f"Handler '{agent_key}' exposes an unregistered capability."
                    )
                if not has_capability_contract(agent_key, capability):
                    raise ValueError(
                        f"Handler '{agent_key}' capability '{capability}' has no input contract."
                    )
                route = (agent_key, capability)
                if route in self._routes:
                    raise ValueError(f"Duplicate agent capability route: {route!r}.")
                self._routes[route] = handler

    def resolve(self, agent_key: str, capability: str) -> AgentHandler:
        definition = self.registry.get(agent_key)
        if capability not in definition.capabilities:
            raise ConflictError("The requested capability is not registered for this agent.")
        handler = self._routes.get((agent_key, capability))
        if handler is None:
            raise ConflictError("No execution handler is available for this agent capability.")
        return handler

    def list_routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._routes))


class AgentDispatcher:
    """Dispatch via the explicit code-owned Tool/Capability Router contract."""

    def __init__(self, registry: AgentRegistry, handlers: list[AgentHandler]) -> None:
        self.registry = registry
        self.router = AgentCapabilityRouter(registry, handlers)
        self.handlers = self.router.handlers

    async def dispatch(self, agent_key: str, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        handler = self.router.resolve(agent_key, capability)
        try:
            validated_payload = validate_capability_payload(
                agent_key, capability, context.payload
            )
        except ValueError as exc:
            raise ConflictError(
                "Agent task payload does not match the registered capability contract."
            ) from exc
        if validated_payload != context.payload:
            context = replace(context, payload=validated_payload)
        result = await handler.execute(capability, context)
        if not isinstance(result, dict):
            raise TypeError("Agent handlers must return a JSON object.")
        return result


class MonitoringAgentHandler:
    """First executable agent: public platform discovery and grounded knowledge extraction."""

    agent_key = "monitoring"
    capabilities = frozenset({
        "platform.monitor",
        "platform.discover",
        "platform.change.detect",
        "platform.knowledge.extract",
    })

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        await context.report_progress("monitoring.started", capability=capability)
        payload = context.payload
        try:
            platform_id = uuid.UUID(str(payload["platform_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Monitoring tasks require a valid platform_id.") from exc

        async with context.session_factory() as session:
            platform = await session.get(Platform, platform_id)
            if platform is None or platform.company_id != context.company_id:
                raise NotFoundError("Platform not found for this company.")

            discovery_result = None
            if capability in {"platform.monitor", "platform.discover", "platform.change.detect"}:
                # Browser-authenticated actions are intentionally not exposed by this first handler.
                await context.report_progress("monitoring.discovery.started")
                discovery_result = await PlatformDiscoveryService(session).discover(
                    platform,
                    mode="public",
                    max_pages=max(1, min(int(payload.get("max_pages", 10)), 25)),
                )
                await context.report_progress("monitoring.discovery.completed")
                if capability != "platform.monitor":
                    await session.commit()
                    return {"capability": capability, **discovery_result}
                # Make discovered source documents visible to the knowledge query
                # in this same unit of work before extracting the product model.
                await session.flush()

            if capability in {"platform.knowledge.extract", "platform.monitor"}:
                context.raise_if_cancelled()
                raw_connection_id = payload.get("connection_id")
                try:
                    connection_id = uuid.UUID(str(raw_connection_id)) if raw_connection_id else None
                except (ValueError, TypeError) as exc:
                    raise ValueError("connection_id must be a valid UUID when supplied.") from exc
                await context.report_progress("monitoring.knowledge.started")
                snapshot = await PlatformKnowledgeService(
                    session,
                    context.provider_registry,
                    context.provider_executor,
                    context.master_key_box,
                ).extract(
                    platform=platform,
                    connection_id=connection_id,
                    task_id=context.task_id,
                    max_documents=max(1, min(int(payload.get("max_documents", 20)), 20)),
                )
                await context.report_progress("monitoring.knowledge.completed", snapshot_id=str(snapshot.id))
                await session.commit()
                knowledge_result = {
                    "snapshot_id": str(snapshot.id),
                    "platform_id": str(platform.id),
                    "source_fingerprint": snapshot.source_fingerprint,
                    "model": snapshot.model,
                }
                if capability == "platform.monitor":
                    return {
                        "capability": capability,
                        "discovery": discovery_result,
                        "knowledge": knowledge_result,
                    }
                return {"capability": capability, **knowledge_result}

        raise ConflictError("Unsupported monitoring capability.")


class ContentAgentHandler:
    """Run the full content production pipeline from one agent task.

    The handler owns stage order and dependencies; the browser only observes.
    Monitoring and analysis are real backend work, scenario text comes from the
    configured GPT provider, and video rendering still runs through the existing
    workflow engine and provider registry.
    """

    agent_key = "content"
    capabilities = frozenset({
        "content.scenario.generate",
        "content.analysis.select",
        "content.production.run",
    })

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        if capability == "content.scenario.generate":
            return await self._generate_scenario(context)
        if capability == "content.analysis.select":
            return await self._analyse(context)
        if capability == "content.production.run":
            return await self._run_production(context)
        raise ConflictError("Unsupported content capability.")

    # ------------------------------------------------------------ scenario only
    async def _generate_scenario(self, context: AgentExecutionContext) -> dict[str, Any]:
        await context.report_progress("content.scenario.started")

        def _required_uuid(field: str) -> uuid.UUID:
            try:
                return uuid.UUID(str(context.payload[field]))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"Content scenario tasks require a valid {field}.") from exc

        project_id = _required_uuid("project_id")
        from app.services.content_scenario_service import ContentScenarioService

        async with context.session_factory() as session:
            result = await ContentScenarioService(
                session,
                context.provider_registry,
                context.provider_executor,
                context.master_key_box,
            ).generate(
                company_id=context.company_id,
                project_id=project_id,
                connection_id=None,
                timeout_seconds=max(
                    1.0, min(float(context.payload.get("timeout_seconds", 180)), 180.0)
                ),
                task_id=context.task_id,
            )
            await context.report_progress("content.scenario.completed", project_id=str(project_id))
            await session.commit()
            return {"capability": "content.scenario.generate", **result}

    # ---------------------------------------------------------------- analysis
    async def _analyse(self, context: AgentExecutionContext) -> dict[str, Any]:
        from app.services.content_analysis_service import ContentAnalysisService

        try:
            brand_id = uuid.UUID(str(context.payload["brand_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Analysis tasks require a valid brand_id.") from exc
        await context.report_progress("analysis.started", stage_status="running")
        async with context.session_factory() as session:
            analysis = await ContentAnalysisService(session).select(
                company_id=context.company_id,
                brand_id=brand_id,
                topic=context.payload.get("topic"),
                limit=int(context.payload.get("limit") or 10),
            )
            await session.commit()
        await context.report_progress(
            "analysis.completed",
            stage_status="completed",
            topic=analysis.get("selected_topic"),
            candidate_count=analysis.get("candidate_count"),
            duplicate_count=analysis.get("duplicate_count"),
            source_count=analysis.get("source_count"),
            brand_fit=analysis.get("brand_fit"),
        )
        return {
            "capability": "content.analysis.select",
            "analysis": analysis,
            "selected_topic": analysis.get("selected_topic"),
            "brand_fit": analysis.get("brand_fit"),
            "candidate_count": analysis.get("candidate_count"),
            "duplicate_count": analysis.get("duplicate_count"),
        }

    # ------------------------------------------------------- one-click pipeline
    async def _run_production(self, context: AgentExecutionContext) -> dict[str, Any]:
        from app.core.config import get_settings
        from app.models import ContentProject, ProjectStatus

        payload = context.payload
        company_id = context.company_id
        task_id = context.task_id
        try:
            brand_id = uuid.UUID(str(payload["brand_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Production tasks require a valid brand_id.") from exc

        topic_hint = (payload.get("topic") or "").strip() or None
        source_url = (payload.get("source_url") or "").strip() or None
        connection_id: uuid.UUID | None = None
        if payload.get("connection_id"):
            try:
                connection_id = uuid.UUID(str(payload["connection_id"]))
            except (ValueError, TypeError) as exc:
                raise ValueError("Production tasks require a valid connection_id.") from exc
        duration = int(payload.get("target_duration_seconds") or 30)
        scene_count = payload.get("scene_count")
        provider_id = (payload.get("provider_id") or "").strip() or None
        provider_fallbacks = [
            str(item).strip()
            for item in (payload.get("provider_fallbacks") or [])
            if str(item).strip()
        ]
        character_key = (payload.get("character_key") or "").strip() or None
        character_image_url = (payload.get("character_image_url") or "").strip() or None
        character_description = (payload.get("character_description") or "").strip() or None
        wait_seconds = max(0.0, float(payload.get("video_wait_seconds") or 0))
        poll_seconds = max(1.0, float(payload.get("video_poll_seconds") or 3))

        if (get_settings().scene_video_generation_mode or "").lower() == "single_shot" and not (
            character_key or character_image_url
        ):
            # Fail fast before paying for monitoring/GPT, but only when the
            # effective provider actually needs a reference image. Local mock
            # runs and text-to-video providers do not.
            requires_reference = True
            registry = context.scene_video_registry
            effective_provider = (
                provider_id or context.default_scene_video_provider or ""
            ).strip()
            if registry is not None and effective_provider:
                try:
                    requires_reference = bool(
                        registry.get(effective_provider).requires_reference_image
                    )
                except Exception:  # noqa: BLE001 - unknown provider fails closed
                    requires_reference = True
            if requires_reference:
                raise ConflictError(
                    "Single-shot video generation requires a character reference image."
                )

        await context.report_progress("production.started", stage_status="running")

        # Phase A: resolve the brand, detect a retry, and guarantee a source.
        brand_language = "fa-IR"
        brand_name = ""
        project_id: uuid.UUID | None = None
        needs_scenario = True
        async with context.session_factory() as session:
            service = self._content_service(session, context)
            brand = await service.get_brand(brand_id, company_id=company_id)
            if brand.status != "active":
                raise ConflictError("The selected brand is not active.")
            brand_language = brand.language
            brand_name = brand.display_name or brand.name
            existing = await self._find_project_for_task(session, company_id, task_id)
            if existing is not None:
                project_id = existing.id
                needs_scenario = not (
                    existing.scenario and existing.status not in {"draft", "script_failed"}
                )
                if existing.status in {"failed", "cancelled"}:
                    raise ConflictError(
                        existing.error_message
                        or "The previous production attempt failed; start a new task."
                    )
            else:
                await self._ensure_source_platform(
                    session, service, brand, source_url, company_id
                )
            await session.commit()

        # Phase B: real monitoring of the brand's linked sources. Recent
        # knowledge is reused so the production run does not re-crawl right
        # after the operator refreshed the same brand on the page.
        analysis: dict[str, Any] | None = None
        if needs_scenario:
            from app.services.brand_memory_service import brand_knowledge_freshness
            from app.services.platform_monitor_service import PlatformMonitorService

            async with context.session_factory() as session:
                freshness = await brand_knowledge_freshness(
                    session,
                    company_id=company_id,
                    brand_id=brand_id,
                    max_age_hours=get_settings().content_knowledge_max_age_hours,
                )
            if freshness["fresh"]:
                await context.report_progress(
                    "monitoring.reused",
                    stage_status="completed",
                    platform_count=freshness["platform_count"],
                    source_count=freshness["snapshot_count"],
                )
            else:
                await context.report_progress("monitoring.started", stage_status="running")
                async with context.session_factory() as session:
                    monitor = await PlatformMonitorService(
                        session,
                        context.provider_registry,
                        context.provider_executor,
                        context.master_key_box,
                    ).monitor_brand(
                        brand_id=brand_id,
                        company_id=company_id,
                        connection_id=connection_id,
                    )
                platforms = monitor.get("platforms") or []
                if not monitor.get("snapshot_available"):
                    raise ConflictError(
                        "Monitoring did not produce verified knowledge: "
                        + self._monitoring_failure_detail(monitor)
                    )
                await context.report_progress(
                    "monitoring.completed",
                    stage_status="completed",
                    platform_count=len(platforms),
                    source_count=sum(
                        1
                        for item in platforms
                        if item.get("status") in {"ok", "skipped"}
                    ),
                )

            # Phase C: deterministic analysis over the new snapshots.
            from app.services.content_analysis_service import ContentAnalysisService

            await context.report_progress("analysis.started", stage_status="running")
            async with context.session_factory() as session:
                analysis = await ContentAnalysisService(session).select(
                    company_id=company_id,
                    brand_id=brand_id,
                    topic=topic_hint,
                    limit=10,
                )
                await session.commit()
            await context.report_progress(
                "analysis.completed",
                stage_status="completed",
                topic=analysis.get("selected_topic"),
                candidate_count=analysis.get("candidate_count"),
                duplicate_count=analysis.get("duplicate_count"),
                source_count=analysis.get("source_count"),
                brand_fit=analysis.get("brand_fit"),
            )

        # Phase D: create the scene project once and persist the retry marker.
        async with context.session_factory() as session:
            service = self._content_service(session, context)
            project = await session.get(ContentProject, project_id) if project_id else None
            if project is None:
                selected_topic = (analysis or {}).get("selected_topic") or topic_hint
                if not selected_topic:
                    raise ConflictError("No content topic could be selected from monitoring.")
                project = await service.create_scene_project(
                    scope=self._scope(context),
                    brand_id=brand_id,
                    topic=selected_topic,
                    target_duration_seconds=duration,
                    scene_count=scene_count,
                    provider_id=provider_id,
                    character_key=character_key,
                    character_image_url=character_image_url,
                    character_description=character_description,
                )
                project_id = project.id
            options = dict(project.scene_options or {})
            if analysis is not None:
                options["analysis"] = analysis
            options["agent_task_id"] = str(task_id)
            project.scene_options = options
            await session.commit()
            project_status = project.status
            project_scenario = project.scenario
            project_options = dict(project.scene_options or {})
            project_topic = project.topic

        await context.report_progress(
            "project.ready", stage_status="completed", project_id=str(project_id)
        )

        # Phase E: GPT scenario plus the deterministic quality gate. A rejected
        # or malformed generation is regenerated once with the same evidence
        # before the task fails, so one bad model response does not waste the run.
        if project_status in {"draft", "script_failed"}:
            from app.services.content_scenario_service import ContentScenarioService
            from app.services.scenario_service import ScenarioGenerationError

            review_error: str | None = None
            for attempt in (1, 2):
                if attempt == 1:
                    await context.report_progress(
                        "scenario.started",
                        stage_status="running",
                        project_id=str(project_id),
                        attempt=attempt,
                    )
                try:
                    async with context.session_factory() as session:
                        await ContentScenarioService(
                            session,
                            context.provider_registry,
                            context.provider_executor,
                            context.master_key_box,
                        ).generate(
                            company_id=company_id,
                            project_id=project_id,
                            connection_id=connection_id,
                            timeout_seconds=180.0,
                            task_id=task_id,
                            analysis=(
                                project_options.get("analysis")
                                if isinstance(project_options.get("analysis"), dict)
                                else analysis
                            ),
                        )
                        await session.commit()
                except ScenarioGenerationError:
                    if attempt == 2:
                        raise
                    await context.report_progress(
                        "scenario.retry",
                        stage_status="waiting",
                        project_id=str(project_id),
                        attempt=2,
                    )
                    continue

                async with context.session_factory() as session:
                    project = await session.get(ContentProject, project_id)
                    if project is None:
                        raise NotFoundError("Content project not found for this company.")
                    issues = self._review_project(project, brand_language)
                    if not issues:
                        project_status = project.status
                        project_scenario = project.scenario
                        project_options = dict(project.scene_options or {})
                        review_error = None
                        break
                    review_error = (
                        "Scenario quality review failed: " + "; ".join(issues[:5])
                    )
                    project.status = ProjectStatus.SCRIPT_FAILED.value
                    project.error_message = review_error
                    failed_options = dict(project.scene_options or {})
                    failed_options["review_issues"] = issues[:10]
                    project.scene_options = failed_options
                    await session.commit()
                if attempt == 1:
                    await context.report_progress(
                        "scenario.retry",
                        stage_status="waiting",
                        project_id=str(project_id),
                        attempt=2,
                    )
            if review_error is not None:
                raise ConflictError(review_error)
            await context.report_progress(
                "scenario.completed",
                stage_status="completed",
                topic=project_topic,
                title=(project_scenario or {}).get("title"),
                scene_count=len((project_scenario or {}).get("scenes") or []),
                duration_seconds=(project_scenario or {}).get("total_duration_seconds"),
            )
        elif project_status == "script_ready":
            async with context.session_factory() as session:
                project = await session.get(ContentProject, project_id)
                if project is None:
                    raise NotFoundError("Content project not found for this company.")
                issues = self._review_project(project, brand_language)
                if issues:
                    raise ConflictError(
                        "Scenario quality review failed: " + "; ".join(issues[:5])
                    )

        # Phase F: automated operator gate (the review above is the approval).
        if project_status == "script_ready":
            await context.report_progress("scenario.approval.started", stage_status="running")
            async with context.session_factory() as session:
                service = self._content_service(session, context)
                approved = await service.approve_scene_scenario(
                    project_id, scope=self._scope(context)
                )
                await session.commit()
                project_status = approved.status
            await context.report_progress("scenario.approved", stage_status="completed")
        elif project_status not in {
            "approved",
            "scenes_prepared",
            "video_generation_pending",
            "video_generating",
            "video_generated",
            "assembling",
            "completed",
        }:
            raise ConflictError(
                f"Production cannot continue from status '{project_status}'."
            )

        # Phase G: dispatch through the existing video workflow engine.
        video_execution_id: uuid.UUID | None = None
        if project_status == "approved":
            selected_provider = (
                provider_id
                or (project_options.get("provider_id") if project_options else None)
                or context.default_scene_video_provider
                or ""
            )
            selected_provider = str(selected_provider).strip()
            if selected_provider == "mock" and context.is_production:
                raise ConflictError(
                    "The mock scene-video provider is forbidden in production."
                )
            await context.report_progress(
                "video.dispatch.started",
                stage_status="running",
                project_id=str(project_id),
                provider_id=selected_provider or None,
            )
            async with context.session_factory() as session:
                service = self._content_service(session, context)
                updated = await service.generate_scene_video(
                    project_id,
                    scope=self._scope(context),
                    provider_id=selected_provider or None,
                    task_id=task_id,
                    provider_fallbacks=provider_fallbacks,
                )
                await session.commit()
                project_status = updated.status
                video_execution_id = updated.video_workflow_execution_id
            await context.report_progress(
                "video.dispatched",
                stage_status="running",
                project_id=str(project_id),
                workflow_execution_id=(
                    str(video_execution_id) if video_execution_id else None
                ),
                provider_id=selected_provider or None,
                video_status=project_status,
            )
        else:
            async with context.session_factory() as session:
                project = await session.get(ContentProject, project_id)
                video_execution_id = project.video_workflow_execution_id if project else None

        # Phase H: bounded wait, then verify the persisted final artifact.
        if project_status != "completed":
            await context.report_progress(
                "video.waiting",
                stage_status="waiting",
                project_id=str(project_id),
                video_status=project_status,
            )
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while wait_seconds > 0:
                context.raise_if_cancelled()
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(poll_seconds)
                async with context.session_factory() as session:
                    current = await session.get(ContentProject, project_id)
                    if current is None:
                        raise NotFoundError("Content project not found for this company.")
                    project_status = current.status
                    if project_status in {"failed", "cancelled"}:
                        raise ConflictError(
                            current.error_message
                            or f"Video production ended with status '{project_status}'."
                        )
                if project_status == "completed":
                    break

        async with context.session_factory() as session:
            project = await session.get(ContentProject, project_id)
            if project is None:
                raise NotFoundError("Content project not found for this company.")
            project_status = project.status
            final_artifact_id = project.final_artifact_id
            video_execution_id = project.video_workflow_execution_id
            scenario = project.scenario or {}
            assembly = project.assembly_metadata or {}
            project_options = dict(project.scene_options or {})
            error_message = project.error_message

        if project_status in {"failed", "cancelled"}:
            raise ConflictError(
                error_message or f"Production ended with status '{project_status}'."
            )
        if project_status == "completed" and final_artifact_id is None:
            raise ConflictError("Video production completed without a persisted artifact.")

        qc_status = self._qc_status(assembly)
        video_status = (
            "completed"
            if project_status == "completed"
            else "in_progress"
            if project_status
            in {
                "approved",
                "scenes_prepared",
                "video_generation_pending",
                "video_generating",
                "video_generated",
                "assembling",
            }
            else project_status
        )
        final_ready = project_status == "completed"
        await context.report_progress(
            "delivery.completed" if final_ready else "video.wait.timeout",
            stage_status="completed" if final_ready else "waiting",
            project_id=str(project_id),
            video_status=video_status,
            final_video_ready=final_ready,
            scene_count=len(scenario.get("scenes") or []),
            duration_seconds=scenario.get("total_duration_seconds"),
        )
        return {
            "capability": "content.production.run",
            "project_id": str(project_id),
            "project_status": project_status,
            "workflow_execution_id": (
                str(video_execution_id) if video_execution_id else None
            ),
            "video_status": video_status,
            "final_video_ready": final_ready,
            "final_artifact_id": str(final_artifact_id) if final_artifact_id else None,
            "title": scenario.get("title") or project.video_title,
            "scene_count": len(scenario.get("scenes") or []),
            "total_duration_seconds": scenario.get("total_duration_seconds"),
            "qc_status": qc_status,
            "brand_name": brand_name,
            "analysis": self._analysis_summary(
                project_options.get("analysis")
                if isinstance(project_options.get("analysis"), dict)
                else analysis
            ),
        }

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _scope(context: AgentExecutionContext):  # noqa: ANN205 - CompanyScope
        from app.security.authorization import CompanyScope, Principal

        return CompanyScope(
            company_id=context.company_id,
            principal=Principal(
                subject="agent-worker",
                company_ids=frozenset({str(context.company_id)}),
            ),
        )

    @staticmethod
    def _content_service(session, context: AgentExecutionContext):  # noqa: ANN001
        from app.services.content_service import ContentService

        return ContentService(
            session,
            context.provider_registry,
            context.provider_executor,
            context.master_key_box,
            workflow_queue=context.workflow_queue,
            scene_video_registry=context.scene_video_registry,
        )

    @staticmethod
    async def _find_project_for_task(
        session, company_id: uuid.UUID, task_id: uuid.UUID
    ):  # noqa: ANN001, ANN205
        from app.models import ContentProject

        return await session.scalar(
            select(ContentProject)
            .where(
                ContentProject.company_id == company_id,
                ContentProject.scene_options["agent_task_id"].astext == str(task_id),
            )
            .order_by(ContentProject.created_at.desc())
        )

    @staticmethod
    async def _ensure_source_platform(
        session,  # noqa: ANN001
        service,  # noqa: ANN001
        brand,  # noqa: ANN001
        source_url: str | None,
        company_id: uuid.UUID,
    ) -> None:
        from app.services.brand_source_service import ensure_brand_source

        await ensure_brand_source(session, service, brand, source_url, company_id)

    @staticmethod
    def _review_project(project, language: str) -> list[str]:  # noqa: ANN001
        from app.schemas.scenario import Scenario
        from app.services.scenario_review import review_scenario

        if not project.scenario:
            return ["scenario is missing."]
        try:
            scenario = Scenario.model_validate(project.scenario)
        except Exception as exc:  # noqa: BLE001 - validation text is the review issue
            return [f"scenario does not match the required schema: {exc}"]
        options = project.scene_options or {}
        target = int(options.get("target_duration_seconds") or scenario.target_duration_seconds)
        return review_scenario(
            scenario, target_duration_seconds=target, language=language
        )

    @staticmethod
    def _monitoring_failure_detail(monitor: dict[str, Any]) -> str:
        platforms = monitor.get("platforms") or []
        details = [
            f"{item.get('platform')}: {item.get('detail') or item.get('status')}"
            for item in platforms
            if item.get("status") in {"error", "skipped"}
        ]
        return "; ".join(details[:5]) or str(monitor.get("status") or "no_knowledge")

    @staticmethod
    def _qc_status(assembly: dict[str, Any]) -> str | None:
        quality = assembly.get("quality_review")
        if isinstance(quality, dict):
            status = quality.get("status") or (
                "passed" if quality.get("accepted") is True else None
            )
            if status:
                return str(status)
        if isinstance(assembly.get("output_validation"), dict):
            return "validated"
        if assembly.get("artifact_id"):
            return "assembled"
        return None

    @staticmethod
    def _analysis_summary(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
        if not analysis:
            return None
        return {
            "selected_topic": analysis.get("selected_topic"),
            "selection_reason": analysis.get("selection_reason"),
            "brand_fit": analysis.get("brand_fit"),
            "candidate_count": analysis.get("candidate_count"),
            "duplicate_count": analysis.get("duplicate_count"),
            "source_count": analysis.get("source_count"),
            "platform_count": analysis.get("platform_count"),
            "sources": [
                item.get("source_url")
                for item in (analysis.get("sources") or [])[:5]
                if isinstance(item, dict) and item.get("source_url")
            ],
        }




class VideoProductionAgentHandler:
    """Route approved projects to the existing avatar or scene workflow pipeline."""

    agent_key = "video"
    capabilities = frozenset({"video.avatar.generate", "video.scene.generate"})

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        await context.report_progress("video.routing.started", capability=capability)
        if context.workflow_queue is None:
            raise ConflictError("The workflow queue is unavailable; video generation was not dispatched.")
        try:
            project_id = uuid.UUID(str(context.payload["project_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Video tasks require a valid project_id.") from exc

        from app.models import ContentProject
        from app.security.authorization import CompanyScope, Principal
        from app.services.content_service import ContentService

        async with context.session_factory() as session:
            project = await session.get(ContentProject, project_id)
            if project is None or project.company_id != context.company_id:
                raise NotFoundError("Content project not found for this company.")
            if capability == "video.scene.generate" and project.video_mode != "scene":
                raise ConflictError("The requested scene route does not match this project mode.")
            if capability == "video.avatar.generate" and project.video_mode != "avatar":
                raise ConflictError("The requested avatar route does not match this project mode.")

            # A worker may die after the workflow outbox commit but before the
            # agent-task acknowledgement. Return the existing execution instead
            # of submitting a second paid render on recovery.
            if project.video_workflow_execution_id is not None and project.status in {
                "video_generating", "video_generation_pending", "video_generated", "assembling", "completed"
            }:
                return {
                    "capability": capability,
                    "project_id": str(project.id),
                    "project_status": project.status,
                    "workflow_execution_id": str(project.video_workflow_execution_id),
                    "replayed": True,
                }

            if project.status != "approved":
                raise ConflictError("Human approval is required before video generation.")

            if capability == "video.scene.generate":
                provider_id = str(
                    context.payload.get("provider_id")
                    or (project.scene_options or {}).get("provider_id")
                    or context.default_scene_video_provider
                )
                if provider_id == "mock" and context.is_production:
                    raise ConflictError("The mock scene-video provider is forbidden in production.")
            else:
                provider_id = "heygen"

            scope = CompanyScope(
                company_id=context.company_id,
                principal=Principal(
                    subject="agent-worker",
                    company_ids=frozenset({str(context.company_id)}),
                ),
            )
            service = ContentService(
                session,
                context.provider_registry,
                context.provider_executor,
                context.master_key_box,
                workflow_queue=context.workflow_queue,
                scene_video_registry=context.scene_video_registry,
            )
            if capability == "video.scene.generate":
                updated = await service.generate_scene_video(
                    project_id,
                    scope=scope,
                    provider_id=provider_id,
                    task_id=context.task_id,
                    provider_fallbacks=context.payload.get("provider_fallbacks"),
                )
            elif capability == "video.avatar.generate":
                try:
                    connection_id = uuid.UUID(str(context.payload["connection_id"]))
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError("Avatar video tasks require a valid connection_id.") from exc
                updated = await service.generate_video(
                    project_id, scope=scope, connection_id=connection_id, task_id=context.task_id
                )
            else:
                raise ConflictError("Unsupported video production capability.")

            cost_metadata = updated.cost_metadata or {}
            effective_provider_id = updated.scene_video_provider or provider_id
            await context.report_progress(
                "video.workflow.dispatched",
                project_id=str(updated.id),
                workflow_execution_id=str(updated.video_workflow_execution_id),
                provider_id=effective_provider_id,
                cost_status=cost_metadata.get("status", "budget_check_pending"),
            )
            await session.commit()
            return {
                "capability": capability,
                "project_id": str(updated.id),
                "project_status": updated.status,
                "workflow_execution_id": str(updated.video_workflow_execution_id),
                "provider_id": effective_provider_id,
                "budget_reservation_id": cost_metadata.get("budget_reservation_id"),
                "cost_status": cost_metadata.get("status", "budget_check_pending"),
            }
