"""Workflow node types and their configuration contracts.

Every node type declares which configuration keys are required, which are
optional (with types/bounds), and how its configuration is validated. The
graph validator enforces these before a workflow can be published; the engine
relies on the same declarations at runtime. All configuration is declarative
data — nothing here is ever executed as code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class NodeType:
    TRIGGER_MANUAL = "trigger.manual"
    ACTION_PROVIDER = "action.provider"
    CONTROL_CONDITION = "control.condition"
    DATA_TRANSFORM = "data.transform"
    CONTROL_DELAY = "control.delay"
    CONTROL_COMPLETE = "control.complete"
    CONTENT_UPDATE_PROJECT = "content.update_project"
    CONTENT_SUBMIT_VIDEO = "content.submit_video"
    CONTENT_POLL_VIDEO = "content.poll_video"
    CONTENT_DOWNLOAD_VIDEO = "content.download_video"
    CONTENT_RENDER_VIDEO = "content.render_video"
    CONTENT_GENERATE_SCENARIO = "content.generate_scenario"
    CONTENT_PREPARE_SCENES = "content.prepare_scenes"
    CONTENT_SUBMIT_SCENE_VIDEOS = "content.submit_scene_videos"
    CONTENT_POLL_SCENE_VIDEOS = "content.poll_scene_videos"
    CONTENT_RETRIEVE_SCENE_VIDEOS = "content.retrieve_scene_videos"
    CONTENT_ASSEMBLE_SCENES = "content.assemble_scenes"


VALID_NODE_TYPES = frozenset(
    {
        NodeType.TRIGGER_MANUAL,
        NodeType.ACTION_PROVIDER,
        NodeType.CONTROL_CONDITION,
        NodeType.DATA_TRANSFORM,
        NodeType.CONTROL_DELAY,
        NodeType.CONTROL_COMPLETE,
        NodeType.CONTENT_UPDATE_PROJECT,
        NodeType.CONTENT_SUBMIT_VIDEO,
        NodeType.CONTENT_POLL_VIDEO,
        NodeType.CONTENT_DOWNLOAD_VIDEO,
        NodeType.CONTENT_RENDER_VIDEO,
        NodeType.CONTENT_GENERATE_SCENARIO,
        NodeType.CONTENT_PREPARE_SCENES,
        NodeType.CONTENT_SUBMIT_SCENE_VIDEOS,
        NodeType.CONTENT_POLL_SCENE_VIDEOS,
        NodeType.CONTENT_RETRIEVE_SCENE_VIDEOS,
        NodeType.CONTENT_ASSEMBLE_SCENES,
    }
)

TRIGGER_NODE_TYPES = frozenset({NodeType.TRIGGER_MANUAL})

# Execution-setting bounds shared by every node type.
MAX_NODE_TIMEOUT_SECONDS = 1800  # long media operations (download/render)
MAX_NODE_RETRIES = 60
MAX_DELAY_SECONDS = 3600


def node_types() -> list[str]:
    return sorted(VALID_NODE_TYPES)


def is_valid_node_type(node_type: str) -> bool:
    return node_type in VALID_NODE_TYPES


@dataclass(frozen=True)
class ConfigIssue:
    """One configuration problem, safe to show to API clients."""

    field: str
    message: str


def _require_uuid(value: Any, field_name: str, issues: list[ConfigIssue]) -> None:
    if not isinstance(value, str):
        issues.append(ConfigIssue(field_name, "must be a UUID string"))
        return
    try:
        uuid.UUID(value)
    except ValueError:
        issues.append(ConfigIssue(field_name, "must be a valid UUID"))


def _validate_provider_action(config: dict[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    connection_id = config.get("connection_id")
    if connection_id is None:
        issues.append(ConfigIssue("connection_id", "is required"))
    else:
        _require_uuid(connection_id, "connection_id", issues)
    operation = config.get("operation", "execute")
    if operation not in ("execute", "validate_connection", "check_health"):
        issues.append(
            ConfigIssue(
                "operation",
                "must be one of 'execute', 'validate_connection', 'check_health'",
            )
        )
    return issues


def _validate_condition(config: dict[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    expression = config.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        issues.append(ConfigIssue("expression", "is required and must be a string"))
        return issues
    # Imported lazily to avoid a circular import with the evaluator module.
    from app.workflows.expressions import ExpressionError, safe_evaluate

    try:
        safe_evaluate(expression, variables={"trigger": {}, "nodes": {}, "node": {"output": {}}})
    except ExpressionError as exc:
        issues.append(ConfigIssue("expression", f"invalid expression: {exc}"))
    return issues


def _validate_transform(config: dict[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    mappings = config.get("mappings")
    if not isinstance(mappings, dict) or not mappings:
        issues.append(ConfigIssue("mappings", "is required and must be a non-empty object"))
        return issues
    if len(mappings) > 100:
        issues.append(ConfigIssue("mappings", "must contain at most 100 entries"))
    for key, value in mappings.items():
        if not isinstance(key, str) or not key:
            issues.append(ConfigIssue("mappings", "keys must be non-empty strings"))
        if isinstance(value, dict) and set(value.keys()) - {"$template", "$from"}:
            issues.append(ConfigIssue(f"mappings.{key}", "objects may only use $template or $from"))
    return issues


def _validate_delay(config: dict[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    seconds = config.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        issues.append(ConfigIssue("seconds", "is required and must be a number"))
    elif not (0 < float(seconds) <= MAX_DELAY_SECONDS):
        issues.append(
            ConfigIssue("seconds", f"must be between 0 (exclusive) and {MAX_DELAY_SECONDS}")
        )
    return issues


def _validate_complete(config: dict[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    outcome = config.get("outcome", "completed")
    if outcome not in ("completed", "failed"):
        issues.append(ConfigIssue("outcome", "must be 'completed' or 'failed'"))
    return issues


def _validate_trigger_manual(config: dict[str, Any]) -> list[ConfigIssue]:
    return []


def _validate_content_node(config: dict[str, Any]) -> list[ConfigIssue]:
    # project_id arrives via input_mapping from the trigger; connection_id is
    # bound at workflow provisioning time.
    return []


_VALIDATORS: dict[str, Callable[[dict[str, Any]], list[ConfigIssue]]] = {
    NodeType.TRIGGER_MANUAL: _validate_trigger_manual,
    NodeType.ACTION_PROVIDER: _validate_provider_action,
    NodeType.CONTROL_CONDITION: _validate_condition,
    NodeType.DATA_TRANSFORM: _validate_transform,
    NodeType.CONTROL_DELAY: _validate_delay,
    NodeType.CONTROL_COMPLETE: _validate_complete,
    NodeType.CONTENT_UPDATE_PROJECT: _validate_content_node,
    NodeType.CONTENT_SUBMIT_VIDEO: _validate_content_node,
    NodeType.CONTENT_POLL_VIDEO: _validate_content_node,
    NodeType.CONTENT_DOWNLOAD_VIDEO: _validate_content_node,
    NodeType.CONTENT_RENDER_VIDEO: _validate_content_node,
    NodeType.CONTENT_GENERATE_SCENARIO: _validate_content_node,
    NodeType.CONTENT_PREPARE_SCENES: _validate_content_node,
    NodeType.CONTENT_SUBMIT_SCENE_VIDEOS: _validate_content_node,
    NodeType.CONTENT_POLL_SCENE_VIDEOS: _validate_content_node,
    NodeType.CONTENT_RETRIEVE_SCENE_VIDEOS: _validate_content_node,
    NodeType.CONTENT_ASSEMBLE_SCENES: _validate_content_node,
}

# Default execution settings merged with per-node overrides by the engine.
DEFAULT_EXECUTION_SETTINGS: dict[str, Any] = field(default_factory=lambda: {
    "timeout_seconds": 60,
    "max_retries": 0,
    "backoff_seconds": 1.0,
    "continue_on_error": False,
    "idempotent": True,
})


def validate_node_configuration(node_type: str, configuration: dict[str, Any]) -> list[ConfigIssue]:
    """Validate one node's configuration; returns a list of issues (empty = valid)."""
    if not is_valid_node_type(node_type):
        return [ConfigIssue("node_type", f"unknown node type {node_type!r}")]
    validator = _VALIDATORS[node_type]
    return validator(configuration if isinstance(configuration, dict) else {})


def validate_execution_settings(settings: dict[str, Any]) -> list[ConfigIssue]:
    """Validate the shared execution-settings envelope."""
    issues: list[ConfigIssue] = []
    if not isinstance(settings, dict):
        return [ConfigIssue("execution_settings", "must be an object")]

    timeout = settings.get("timeout_seconds")
    if timeout is not None and (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not (0 < float(timeout) <= MAX_NODE_TIMEOUT_SECONDS)
    ):
        issues.append(ConfigIssue("timeout_seconds", f"must be a number in (0, {MAX_NODE_TIMEOUT_SECONDS}]"))

    retries = settings.get("max_retries")
    if retries is not None and (
        not isinstance(retries, int) or isinstance(retries, bool) or not (0 <= retries <= MAX_NODE_RETRIES)
    ):
        issues.append(ConfigIssue("max_retries", f"must be an integer in [0, {MAX_NODE_RETRIES}]"))

    backoff = settings.get("backoff_seconds")
    if backoff is not None and (
        not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or float(backoff) < 0
    ):
        issues.append(ConfigIssue("backoff_seconds", "must be a non-negative number"))

    for flag in ("continue_on_error", "idempotent"):
        value = settings.get(flag)
        if value is not None and not isinstance(value, bool):
            issues.append(ConfigIssue(flag, "must be a boolean"))
    return issues


def validate_input_mapping(mapping: dict[str, Any]) -> list[ConfigIssue]:
    """Input mappings: literal values or resolvable path strings / templates."""
    issues: list[ConfigIssue] = []
    if not isinstance(mapping, dict):
        return [ConfigIssue("input_mapping", "must be an object")]
    from app.workflows.mapping import (
        TEMPLATE_PATTERN,
        is_reference_path,
        validate_reference_path,
    )

    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            issues.append(ConfigIssue("input_mapping", "keys must be non-empty strings"))
            continue
        if isinstance(value, str) and is_reference_path(value):
            from app.workflows.mapping import validate_reference_path

            issue = validate_reference_path(value)
            if issue:
                issues.append(ConfigIssue(f"input_mapping.{key}", issue))
        elif isinstance(value, dict) and "$template" in value:
            template = value["$template"]
            if not isinstance(template, str):
                issues.append(ConfigIssue(f"input_mapping.{key}", "$template must be a string"))
            else:
                for match in TEMPLATE_PATTERN.finditer(template):
                    path = match.group(1).strip()
                    issue = validate_reference_path(path)
                    if issue:
                        issues.append(ConfigIssue(f"input_mapping.{key}", issue))
    return issues


def validate_output_mapping(mapping: dict[str, Any]) -> list[ConfigIssue]:
    """Output mappings project provider results: {key: 'result.<path>'}."""
    issues: list[ConfigIssue] = []
    if not isinstance(mapping, dict):
        return [ConfigIssue("output_mapping", "must be an object")]
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            issues.append(ConfigIssue("output_mapping", "keys must be non-empty strings"))
        if not isinstance(value, str) or not value.startswith("result."):
            issues.append(
                ConfigIssue(f"output_mapping.{key}", "must be a 'result.<path>' string")
            )
    return issues
