"""Agent execution contracts, handler registry and dispatcher."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
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
        "platform.discover",
        "platform.change.detect",
        "platform.knowledge.extract",
    })

    async def execute(self, capability: str, context: AgentExecutionContext) -> dict[str, Any]:
        payload = context.payload
        try:
            platform_id = uuid.UUID(str(payload["platform_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Monitoring tasks require a valid platform_id.") from exc

        async with context.session_factory() as session:
            platform = await session.get(Platform, platform_id)
            if platform is None or platform.company_id != context.company_id:
                raise NotFoundError("Platform not found for this company.")

            if capability in {"platform.discover", "platform.change.detect"}:
                # Browser-authenticated actions are intentionally not exposed by this first handler.
                result = await PlatformDiscoveryService(session).discover(
                    platform,
                    mode="public",
                    max_pages=max(1, min(int(payload.get("max_pages", 10)), 25)),
                )
                await session.commit()
                return {"capability": capability, **result}

            if capability == "platform.knowledge.extract":
                try:
                    connection_id = uuid.UUID(str(payload["connection_id"]))
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError("Knowledge extraction requires a valid connection_id.") from exc
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
                await session.commit()
                return {
                    "capability": capability,
                    "snapshot_id": str(snapshot.id),
                    "platform_id": str(platform.id),
                    "source_fingerprint": snapshot.source_fingerprint,
                    "model": snapshot.model,
                }

        raise ConflictError("Unsupported monitoring capability.")
