"""In-memory registry for approved agent definitions."""
from __future__ import annotations

from collections.abc import Iterable

from app.agents.definitions import BUILTIN_AGENT_DEFINITIONS, AgentDefinition


class UnknownAgentError(LookupError):
    """Raised when a caller references an agent outside the approved catalog."""


class AgentRegistry:
    def __init__(self, definitions: Iterable[AgentDefinition] = BUILTIN_AGENT_DEFINITIONS) -> None:
        items = tuple(definitions)
        self._definitions = {item.key: item for item in items}
        if len(self._definitions) != len(items):
            raise ValueError("Agent definition keys must be unique.")
        capability_owners: dict[str, list[str]] = {}
        for item in items:
            for capability in item.capabilities:
                capability_owners.setdefault(capability, []).append(item.key)
        self._capability_owners = {
            capability: tuple(sorted(agent_keys))
            for capability, agent_keys in capability_owners.items()
        }

    def list_definitions(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def get(self, key: str) -> AgentDefinition:
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise UnknownAgentError(key) from exc

    def find_by_capability(self, capability: str) -> tuple[AgentDefinition, ...]:
        return tuple(
            self._definitions[key]
            for key in self._capability_owners.get(capability, ())
        )


def build_default_agent_registry() -> AgentRegistry:
    return AgentRegistry()
