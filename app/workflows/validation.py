"""Workflow graph validation.

Runs on every version before it can be published (and on demand via
``POST /workflows/{id}/validate``). Detects: unknown node types, invalid
configuration, edges referencing missing nodes, missing trigger, multiple
triggers, trigger with incoming edges, cyclic dependencies, unreachable
nodes, invalid input/output mappings, and (for provider action nodes)
references to connections that do not exist or belong to another company.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlatformConnection, WorkflowNode
from app.security.redaction import redact_text
from app.workflows.nodes import (
    TRIGGER_NODE_TYPES,
    VALID_NODE_TYPES,
    NodeType,
    validate_execution_settings,
    validate_input_mapping,
    validate_node_configuration,
    validate_output_mapping,
)


@dataclass(frozen=True)
class ValidationIssue:
    """One validation problem, safe for API clients (no internal details)."""

    code: str
    message: str
    node_key: str | None = None

    def as_dict(self) -> dict:
        payload = {"code": self.code, "message": redact_text(self.message)}
        if self.node_key:
            payload["node_key"] = self.node_key
        return payload


@dataclass
class ValidatedGraph:
    """A validated graph ready for execution."""

    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    edges: list[tuple[str, str, str | None]] = field(default_factory=list)  # from, to, condition
    trigger_key: str | None = None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues


class GraphValidator:
    """Validates a version's node/edge rows against company resources."""

    def __init__(self, session: AsyncSession, *, company_id: uuid.UUID) -> None:
        self.session = session
        self.company_id = company_id

    async def validate(self, nodes: list[WorkflowNode], edges: list[tuple[str, str, str | None]]) -> ValidatedGraph:
        graph = ValidatedGraph()
        graph.edges = list(edges)

        if not nodes:
            graph.issues.append(ValidationIssue("empty_graph", "workflow must contain at least one node"))
            return graph

        keys = set()
        for node in nodes:
            if node.node_key in keys:
                graph.issues.append(ValidationIssue("duplicate_node", "duplicate node key", node.node_key))
            keys.add(node.node_key)
            graph.nodes[node.node_key] = node

        # Node type + configuration contracts.
        for node in nodes:
            if node.node_type not in VALID_NODE_TYPES:
                graph.issues.append(
                    ValidationIssue("unknown_node_type", f"unknown node type {node.node_type!r}", node.node_key)
                )
                continue
            for issue in validate_node_configuration(node.node_type, node.configuration):
                graph.issues.append(
                    ValidationIssue("invalid_configuration", f"{issue.field} {issue.message}", node.node_key)
                )
            for issue in validate_execution_settings(node.execution_settings):
                graph.issues.append(
                    ValidationIssue("invalid_execution_settings", f"{issue.field} {issue.message}", node.node_key)
                )
            for issue in validate_input_mapping(node.input_mapping):
                graph.issues.append(
                    ValidationIssue("invalid_input_mapping", f"{issue.field} {issue.message}", node.node_key)
                )
            for issue in validate_output_mapping(node.output_mapping):
                graph.issues.append(
                    ValidationIssue("invalid_output_mapping", f"{issue.field} {issue.message}", node.node_key)
                )

        # Edges reference existing nodes.
        for from_key, to_key, _condition in graph.edges:
            if from_key not in keys:
                graph.issues.append(
                    ValidationIssue("invalid_edge", f"edge source '{from_key}' does not exist", from_key)
                )
            if to_key not in keys:
                graph.issues.append(
                    ValidationIssue("invalid_edge", f"edge target '{to_key}' does not exist", to_key)
                )
            if from_key == to_key:
                graph.issues.append(
                    ValidationIssue("invalid_edge", "self-referencing edge", from_key)
                )

        # Exactly one trigger; triggers never receive edges.
        triggers = [node for node in nodes if node.node_type in TRIGGER_NODE_TYPES]
        if len(triggers) == 0:
            graph.issues.append(ValidationIssue("missing_trigger", "workflow needs exactly one trigger node"))
        elif len(triggers) > 1:
            graph.issues.append(
                ValidationIssue("multiple_triggers", "workflow must have exactly one trigger node")
            )
        else:
            graph.trigger_key = triggers[0].node_key
        incoming = {to_key for _from, to_key, _c in graph.edges}
        for node in triggers:
            if node.node_key in incoming:
                graph.issues.append(
                    ValidationIssue(
                        "trigger_with_incoming_edge",
                        "trigger nodes cannot have incoming edges",
                        node.node_key,
                    )
                )

        # Cycles: any cycle is unsupported in this engine version.
        cycle = self._find_cycle(nodes, graph.edges)
        if cycle:
            graph.issues.append(
                ValidationIssue(
                    "cyclic_dependency",
                    "cyclic dependencies are not supported: " + " -> ".join(cycle),
                    cycle[0],
                )
            )

        # Reachability from the trigger (only when a trigger exists).
        if graph.trigger_key:
            reachable = self._reachable(graph.trigger_key, graph.edges)
            for node in nodes:
                if node.node_key not in reachable:
                    graph.issues.append(
                        ValidationIssue("unreachable_node", "node is not reachable from the trigger", node.node_key)
                    )

        # Provider action nodes: connection existence + tenant ownership.
        for node in nodes:
            if node.node_type != NodeType.ACTION_PROVIDER:
                continue
            connection_id = node.configuration.get("connection_id")
            if not isinstance(connection_id, str):
                continue  # shape already reported by config validation
            try:
                connection_uuid = uuid.UUID(connection_id)
            except ValueError:
                continue
            connection = await self.session.get(PlatformConnection, connection_uuid)
            if connection is None or connection.company_id != self.company_id:
                graph.issues.append(
                    ValidationIssue(
                        "invalid_connection_reference",
                        "connection does not exist or belongs to another company",
                        node.node_key,
                    )
                )

        return graph

    @staticmethod
    def _find_cycle(nodes: list[WorkflowNode], edges: list[tuple[str, str, str | None]]) -> list[str] | None:
        adjacency: dict[str, list[str]] = {}
        for from_key, to_key, _condition in edges:
            adjacency.setdefault(from_key, []).append(to_key)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node.node_key: WHITE for node in nodes}
        path: list[str] = []

        def visit(key: str) -> list[str] | None:
            color[key] = GRAY
            path.append(key)
            for target in adjacency.get(key, []):
                if color.get(target, BLACK) == GRAY:
                    cycle_start = path.index(target)
                    return path[cycle_start:] + [target]
                if color.get(target, BLACK) == WHITE:
                    found = visit(target)
                    if found:
                        return found
            path.pop()
            color[key] = BLACK
            return None

        for node in nodes:
            if color[node.node_key] == WHITE:
                found = visit(node.node_key)
                if found:
                    return found
        return None

    @staticmethod
    def _reachable(trigger_key: str, edges: list[tuple[str, str, str | None]]) -> set[str]:
        adjacency: dict[str, list[str]] = {}
        for from_key, to_key, _condition in edges:
            adjacency.setdefault(from_key, []).append(to_key)
        seen = {trigger_key}
        queue = [trigger_key]
        while queue:
            current = queue.pop()
            for target in adjacency.get(current, []):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return seen


async def load_version_graph(
    session: AsyncSession, version_id: uuid.UUID
) -> tuple[list[WorkflowNode], list[tuple[str, str, str | None]]]:
    """Load a version's nodes and edges as plain tuples for the validator/engine."""
    nodes = list(
        (
            await session.execute(
                select(WorkflowNode).where(WorkflowNode.version_id == version_id).order_by(WorkflowNode.node_key)
            )
        ).scalars().all()
    )
    from app.models import WorkflowEdge

    edge_rows = list(
        (
            await session.execute(
                select(WorkflowEdge)
                .where(WorkflowEdge.version_id == version_id)
                .order_by(WorkflowEdge.from_key, WorkflowEdge.edge_order)
            )
        ).scalars().all()
    )
    edges = [(edge.from_key, edge.to_key, edge.condition_expression) for edge in edge_rows]
    return nodes, edges


class WorkflowValidationError(Exception):
    """Raised when an operation requires a valid graph but issues exist."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__(f"workflow validation failed with {len(issues)} issue(s)")
