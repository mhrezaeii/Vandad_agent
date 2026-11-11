"""Agent execution contracts, handler registry and dispatcher."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.agents import AgentRegistry
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


class AgentDispatcher:
    """Dispatch only registered agent/capability pairs; never execute DB-defined code."""

    def __init__(self, registry: AgentRegistry, handlers: list[AgentHandler]) -> None:
        self.registry = registry
        self.handlers = {handler.agent_key: handler for handler in handlers}
        if len(self.handlers) != len(handlers):
            raise ValueError("Agent handler keys must be unique.")
        for key, handler in self.handlers.items():
            definition = registry.get(key)
            if not definition.dispatch_ready:
                raise ValueError(f"Handler '{key}' is registered but its agent is not dispatch-ready.")
            if not handler.capabilities.issubset(set(definition.capabilities)):
                raise ValueError(f"Handler '{key}' exposes an unregistered capability.")

    async def dispatch(self, agent_key: str, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        definition = self.registry.get(agent_key)
        if capability not in definition.capabilities:
            raise ConflictError("The requested capability is not registered for this agent.")
        handler = self.handlers.get(agent_key)
        if handler is None or capability not in handler.capabilities:
            raise ConflictError("No execution handler is available for this agent capability.")
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
