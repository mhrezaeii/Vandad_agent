"""Safe expression evaluator and mapping resolution tests (no DB required)."""

from __future__ import annotations

import pytest

from app.workflows.expressions import ExpressionError, safe_evaluate, safe_evaluate_bool
from app.workflows.mapping import (
    interpolate_template,
    is_reference_path,
    project_output,
    resolve_input_mapping,
    resolve_path,
    validate_reference_path,
)

CONTEXT = {
    "trigger": {"priority": 2, "customer": {"name": "Acme"}, "tags": ["vip", "b2b"]},
    "nodes": {
        "fetch": {"status": "completed", "output": {"status": 200, "ok": True, "latency": 12.5}},
        "check": {"status": "completed", "output": {"matched": False}},
    },
}


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("trigger.priority > 1", True),
        ("trigger.priority >= 2 and nodes.fetch.output.ok", True),
        ("nodes.check.output.matched", False),
        ("not nodes.check.output.matched", True),
        ("'vip' in trigger.tags", True),
        ("trigger.customer.name == 'Acme'", True),
        ("nodes.fetch.output.status == 200 or trigger.priority > 100", True),
        ("(trigger.priority + 0) == 2", True),  # parentheses
        ("-1 < 0", True),
        ("nodes.fetch.status == 'completed'", True),
    ],
)
def test_valid_expressions(expression, expected):
    assert safe_evaluate_bool(expression, variables=CONTEXT) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__",
        "open('/etc/passwd')",
        "(lambda: 1)()",
        "[x for x in trigger.tags]",
        "trigger.__class__",
        "exec('x=1')",
        "trigger.priority; import os",
        "unknown_variable",
        "",
    ],
)
def test_unsafe_or_invalid_expressions_rejected(expression):
    with pytest.raises(ExpressionError):
        safe_evaluate(expression, variables=CONTEXT)


def test_expression_length_and_depth_limits():
    with pytest.raises(ExpressionError):
        safe_evaluate("1" * 600, variables={})
    with pytest.raises(ExpressionError):
        safe_evaluate("not " * 40 + "True", variables={})


def test_missing_paths_resolve_to_none():
    assert safe_evaluate("nodes.missing.output.x == None", variables=CONTEXT) is True
    assert resolve_path("nodes.nobody.output.x", CONTEXT) is None


# --- mapping resolution -----------------------------------------------------
def test_reference_path_detection_and_validation():
    assert is_reference_path("$trigger.priority")
    assert is_reference_path("nodes.fetch.output.status")
    assert not is_reference_path("literal-value")
    assert validate_reference_path("nodes.fetch.output.status") is None
    assert validate_reference_path("nodes.fetch.bogus") is not None
    assert validate_reference_path("bogus.path") is not None


def test_input_mapping_resolution():
    mapping = {
        "url": "https://api.example.com/{{ trigger.customer.name }}",
        "priority": "$trigger.priority",
        "flag": "nodes.fetch.output.ok",
        "static": "hello",
        "nested": {"$from": "trigger.customer.name"},
    }
    resolved = resolve_input_mapping(mapping, CONTEXT)
    assert resolved == {
        "url": "https://api.example.com/Acme",
        "priority": 2,
        "flag": True,
        "static": "hello",
        "nested": "Acme",
    }


def test_template_interpolation_missing_values_become_empty():
    assert interpolate_template("Hello {{ trigger.customer.name }}!", CONTEXT) == "Hello Acme!"
    assert interpolate_template("x {{ trigger.nope }} y", CONTEXT) == "x  y"


def test_output_projection():
    result = {"status": 200, "message": "done", "latency_ms": 3.2}
    assert project_output({"http_status": "result.status"}, result) == {"http_status": 200}
    assert project_output({}, result) == result  # no mapping -> raw output
    assert project_output({"missing": "result.nope.deep"}, result) == {"missing": None}
