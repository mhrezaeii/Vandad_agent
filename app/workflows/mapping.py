"""Input/output mapping resolution for workflow nodes.

Paths (both in mappings and inside ``{{ }}`` templates) follow one grammar:

- ``trigger.<path>``        — execution input (``$trigger`` alias accepted)
- ``nodes.<key>.output.<path>`` — a completed node's output
- ``nodes.<key>.status``    — a node's terminal status

Everything else is a literal. Resolution is read-only dictionary traversal —
no evaluation, no code execution.
"""

from __future__ import annotations

import re
from typing import Any

TRIGGER_PREFIXES = ("$trigger.", "trigger.")
NODES_PREFIX = "nodes."
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

_MAX_PATH_DEPTH = 12


def is_reference_path(value: str) -> bool:
    """True when a string should be resolved as a context reference."""
    return value.startswith(TRIGGER_PREFIXES) or value.startswith(NODES_PREFIX)


def validate_reference_path(path: str) -> str | None:
    """Return an error message for an invalid reference path, or None."""
    if path.startswith(NODES_PREFIX):
        parts = path[len(NODES_PREFIX):].split(".")
        if len(parts) < 2 or not parts[0]:
            return "node reference must be 'nodes.<key>.output.<field>' or 'nodes.<key>.status'"
        if parts[1] not in ("output", "status"):
            return "node reference second segment must be 'output' or 'status'"
        if parts[1] == "output" and len(parts) < 3:
            return "output reference must include at least one field: 'nodes.<key>.output.<field>'"
        if len(parts) > _MAX_PATH_DEPTH:
            return "node reference path is too deep"
        return None
    if path.startswith(TRIGGER_PREFIXES):
        parts = path.split(".", 1)
        if len(parts) < 2 or not parts[1]:
            return "trigger reference must include a field: '$trigger.<field>'"
        return None
    return "references must start with '$trigger.', 'trigger.' or 'nodes.'"


def resolve_path(path: str, context: dict[str, Any]) -> Any:
    """Resolve a reference path against the execution context; None if absent."""
    if path.startswith(NODES_PREFIX):
        parts = path[len(NODES_PREFIX):].split(".")
        node_entry = context.get("nodes", {}).get(parts[0])
        if node_entry is None:
            return None
        target = node_entry.get(parts[1]) if len(parts) > 1 else None
        for segment in parts[2:]:
            if isinstance(target, dict):
                target = target.get(segment)
            else:
                return None
        return target
    for prefix in TRIGGER_PREFIXES:
        if path.startswith(prefix):
            target: Any = context.get("trigger", {})
            for segment in path[len(prefix):].split("."):
                if isinstance(target, dict):
                    target = target.get(segment)
                else:
                    return None
            return target
    return None


def resolve_input_mapping(mapping: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Materialize a node's input mapping into concrete parameter values."""
    resolved: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, str) and is_reference_path(value):
            resolved[key] = resolve_path(value, context)
        elif isinstance(value, dict) and "$template" in value:
            resolved[key] = interpolate_template(value["$template"], context)
        elif isinstance(value, dict) and "$from" in value:
            source = value["$from"]
            resolved[key] = (
                resolve_path(source, context) if isinstance(source, str) else None
            )
        else:
            resolved[key] = value
    return resolved


def interpolate_template(template: str, context: dict[str, Any]) -> str:
    """Replace ``{{ path }}`` tokens with resolved context values."""

    def _replace(match: re.Match[str]) -> str:
        value = resolve_path(match.group(1).strip(), context)
        return "" if value is None else str(value)

    return TEMPLATE_PATTERN.sub(_replace, template)


def project_output(output_mapping: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Project a provider result through the node's output mapping."""
    if not output_mapping:
        return result
    projected: dict[str, Any] = {}
    for key, path in output_mapping.items():
        target: Any = result
        for segment in str(path).split("."):
            if isinstance(target, dict):
                target = target.get(segment)
            else:
                target = None
                break
        projected[key] = target
    return projected
