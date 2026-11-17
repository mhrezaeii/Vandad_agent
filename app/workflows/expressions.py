"""Safe expression evaluation for condition nodes and edge conditions.

Workflows must never execute arbitrary code. This module implements a small,
whitelisted expression language on top of Python's ``ast`` module:

- literals (strings, numbers, booleans, None), lists/tuples of literals
- names: ``trigger``, ``nodes``, ``node`` (bound to the execution context)
- subscript / attribute access for path lookups (``nodes.pay.output.ok``)
- comparisons: == != < <= > >= in not in is is not
- boolean operators: and, or, not
- parentheses

Everything else — function calls, lambdas, comprehensions, attribute access to
dunder names, imports, star-args — is rejected before evaluation. Evaluation
runs with empty builtins and only the supplied variables, so even a
crafted-looking expression has no interpreter surface to abuse.
"""

from __future__ import annotations

import ast

MAX_EXPRESSION_LENGTH = 500
MAX_EXPRESSION_DEPTH = 24

_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Tuple,
    ast.List,
)

_ALLOWED_NAMES = frozenset({"trigger", "nodes", "node", "execution"})


class ExpressionError(ValueError):
    """Raised for any invalid or unsafe expression (message is client-safe)."""


def _validate_node(node: ast.AST, depth: int = 0) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise ExpressionError("expression is too deeply nested")
    if not isinstance(node, _ALLOWED):
        raise ExpressionError(f"disallowed syntax: {type(node).__name__}")
    if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
        raise ExpressionError(f"unknown name: {node.id!r}")
    if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, set, frozenset, dict)):
        raise ExpressionError("disallowed literal type")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ExpressionError("attribute access to private names is not allowed")
    for child in ast.iter_child_nodes(node):
        _validate_node(child, depth + 1)


def _lookup(root: object, node: ast.AST) -> object:
    """Evaluate a validated AST fragment against the context objects."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return root  # root is the variables dict; caller handles names
    if isinstance(node, ast.Attribute):
        base = _evaluate(root, node.value)
        if isinstance(base, dict):
            return base.get(node.attr)
        return getattr(base, node.attr, None) if not node.attr.startswith("_") else None
    if isinstance(node, ast.Subscript):
        base = _evaluate(root, node.value)
        index = node.slice
        if isinstance(index, ast.Slice):
            lower = _evaluate(root, index.lower) if index.lower else None
            upper = _evaluate(root, index.upper) if index.upper else None
            step = _evaluate(root, index.step) if index.step else None
            return base[slice(lower, upper, step)]
        key = _evaluate(root, index)
        if isinstance(base, dict):
            return base.get(key)
        if isinstance(base, (list, tuple, str)):
            return base[key] if isinstance(key, int) else None
        return None
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_evaluate(root, element) for element in node.elts]
    raise ExpressionError("unsupported expression element")


def _evaluate(root: dict, node: ast.AST) -> object:
    """Evaluate a validated expression node. root maps names to values."""
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(root, value) for value in node.values]
        if isinstance(node.op, ast.And):
            # Python's `and` short-circuit semantics.
            outcome: object = values[0]
            for value in values[1:]:
                if not outcome:
                    break
                outcome = value
            return outcome
        # `or`: first truthy value wins.
        for value in values:
            if value:
                return value
        return values[-1] if values else False
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _evaluate(root, node.operand)
        if isinstance(node.op, ast.USub):
            operand = _evaluate(root, node.operand)
            if isinstance(operand, (int, float)) and not isinstance(operand, bool):
                return -operand
            raise ExpressionError("unary minus requires a number")
        raise ExpressionError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _evaluate(root, node.left)
        right = _evaluate(root, node.right)
        return _arith(node.op, left, right)
    if isinstance(node, ast.Compare):
        left = _evaluate(root, node.left)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(root, comparator)
            if isinstance(operator, ast.Eq):
                ok = left == right
            elif isinstance(operator, ast.NotEq):
                ok = left != right
            elif isinstance(operator, ast.Lt):
                ok = _ordered(left, right) < 0
            elif isinstance(operator, ast.LtE):
                ok = _ordered(left, right) <= 0
            elif isinstance(operator, ast.Gt):
                ok = _ordered(left, right) > 0
            elif isinstance(operator, ast.GtE):
                ok = _ordered(left, right) >= 0
            elif isinstance(operator, ast.In):
                ok = _contains(right, left)
            elif isinstance(operator, ast.NotIn):
                ok = not _contains(right, left)
            elif isinstance(operator, ast.Is):
                ok = left is right
            elif isinstance(operator, ast.IsNot):
                ok = left is not right
            else:
                raise ExpressionError("unsupported comparison")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return root.get(node.id)
    return _lookup(root, node)


def _arith(op: ast.AST, left: object, right: object) -> object:
    """Arithmetic restricted to numbers (booleans excluded) and string concat."""
    if isinstance(op, ast.Add) and isinstance(left, str) and isinstance(right, str):
        return left + right
    numeric = (int, float)
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, numeric)
        or not isinstance(right, numeric)
    ):
        raise ExpressionError("arithmetic requires numbers")
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        if right == 0:
            raise ExpressionError("division by zero")
        return left / right
    if isinstance(op, ast.Mod):
        if right == 0:
            raise ExpressionError("modulo by zero")
        return left % right
    raise ExpressionError("unsupported arithmetic operator")


def _ordered(left: object, right: object) -> int:
    """Ordering comparison restricted to same-type numbers/strings."""
    if isinstance(left, bool) or isinstance(right, bool):
        raise ExpressionError("ordering comparisons require numbers or strings")
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (left > right) - (left < right)
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    raise ExpressionError("ordering comparisons require numbers or strings")


def _contains(container: object, item: object) -> bool:
    if isinstance(container, (list, tuple, str, dict)):
        try:
            return item in container
        except TypeError:
            return False
    return False


def safe_evaluate(expression: str, *, variables: dict) -> object:
    """Evaluate a whitelisted expression against *variables*; returns any value."""
    if not isinstance(expression, str):
        raise ExpressionError("expression must be a string")
    if not expression.strip():
        raise ExpressionError("expression is empty")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError("expression is too long")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError("expression has invalid syntax") from exc
    _validate_node(tree)
    return _evaluate(dict(variables), tree.body)


def safe_evaluate_bool(expression: str, *, variables: dict) -> bool:
    """Evaluate an expression and coerce the result to a branch decision."""
    result = safe_evaluate(expression, variables=variables)
    return bool(result)
