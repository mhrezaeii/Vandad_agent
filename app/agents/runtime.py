"""Agent execution contracts, handler registry and dispatcher."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

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
                try:
                    connection_id = uuid.UUID(str(payload["connection_id"]))
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError(
                        "Knowledge extraction requires a valid connection_id."
                    ) from exc
                await context.report_progress("monitoring.knowledge.started")
                snapshot = await PlatformKnowledgeService(
                    session,
                    context.provider_registry,
                    context.provider_executor,
                    context.master_key_box,
                ).extract(
                    platform=platform,
                    connection_id=connection_id,
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
    """Generate an approved-brand scenario from its latest company knowledge."""

    agent_key = "content"
    capabilities = frozenset({"content.scenario.generate"})

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        if capability != "content.scenario.generate":
            raise ConflictError("Unsupported content capability.")
        await context.report_progress("content.scenario.started")

        def _required_uuid(field: str) -> uuid.UUID:
            try:
                return uuid.UUID(str(context.payload[field]))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"Content scenario tasks require a valid {field}.") from exc

        project_id = _required_uuid("project_id")
        connection_id = _required_uuid("connection_id")
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
                connection_id=connection_id,
                timeout_seconds=max(
                    1.0, min(float(context.payload.get("timeout_seconds", 180)), 180.0)
                ),
            )
            await context.report_progress("content.scenario.completed", project_id=str(project_id))
            await session.commit()
            return {"capability": capability, **result}



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
                    project_id, scope=scope, provider_id=provider_id
                )
            elif capability == "video.avatar.generate":
                try:
                    connection_id = uuid.UUID(str(context.payload["connection_id"]))
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError("Avatar video tasks require a valid connection_id.") from exc
                updated = await service.generate_video(
                    project_id, scope=scope, connection_id=connection_id
                )
            else:
                raise ConflictError("Unsupported video production capability.")

            cost_metadata = updated.cost_metadata or {}
            await context.report_progress(
                "video.workflow.dispatched",
                project_id=str(updated.id),
                workflow_execution_id=str(updated.video_workflow_execution_id),
                provider_id=provider_id,
                cost_status=cost_metadata.get("status", "budget_check_pending"),
            )
            await session.commit()
            return {
                "capability": capability,
                "project_id": str(updated.id),
                "project_status": updated.status,
                "workflow_execution_id": str(updated.video_workflow_execution_id),
                "provider_id": provider_id,
                "budget_reservation_id": cost_metadata.get("budget_reservation_id"),
                "cost_status": cost_metadata.get("status", "budget_check_pending"),
            }
