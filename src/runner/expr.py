"""Expression evaluator + template renderer.

Pure module — no project-internal imports.

Two entry points:
- `eval_expr(s, ctx) -> value` — evaluates a single expression like
  `nodes.X.latest.output.data.passed == true` against a ctx dict.
- `render_deep(obj, ctx) -> obj` — walks any string / dict / list and
  substitutes every `{{expr}}` it finds via `eval_expr`.

Strict semantics: missing keys / undefined names raise `ExprError`.
The runtime guarantees `state.X`, `nodes.X.latest.output...`,
`retry.feedback`, and `output.X` are always populated where they're
allowed (see `Run.expr_ctx`); workflow authors must supply state
defaults rather than relying on optional markers.
"""

from __future__ import annotations

import ast
import json
import re


# ─── Expression evaluator ──────────────────────────────────────────────

_ALLOWED_AST = (
    ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.Subscript,
    ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Load,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or, ast.Not,
)


class ExprError(Exception):
    pass


def _walk(node, ctx):
    if isinstance(node, ast.Expression):
        return _walk(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "true":
            return True
        if node.id == "false":
            return False
        if node.id == "null":
            return None
        if node.id not in ctx:
            raise ExprError(f"undefined name: {node.id}")
        return ctx[node.id]
    if isinstance(node, ast.Attribute):
        obj = _walk(node.value, ctx)
        if isinstance(obj, dict):
            if node.attr not in obj:
                raise ExprError(f"missing key: .{node.attr}")
            return obj[node.attr]
        if obj is None:
            raise ExprError(f"None has no attr {node.attr}")
        raise ExprError(f"can't access .{node.attr} on {type(obj).__name__}")
    if isinstance(node, ast.Subscript):
        obj = _walk(node.value, ctx)
        idx = _walk(node.slice, ctx)
        if isinstance(obj, list):
            try:
                return obj[idx]
            except (IndexError, TypeError) as e:
                raise ExprError(f"subscript: {e}")
        if isinstance(obj, dict):
            return obj.get(idx)
        raise ExprError(f"can't subscript {type(obj).__name__}")
    if isinstance(node, ast.Compare):
        left = _walk(node.left, ctx)
        for op, right_node in zip(node.ops, node.comparators):
            right = _walk(right_node, ctx)
            ok = (
                (isinstance(op, ast.Eq) and left == right) or
                (isinstance(op, ast.NotEq) and left != right) or
                (isinstance(op, ast.Lt) and left < right) or
                (isinstance(op, ast.LtE) and left <= right) or
                (isinstance(op, ast.Gt) and left > right) or
                (isinstance(op, ast.GtE) and left >= right)
            )
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_walk(v, ctx) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_walk(v, ctx) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _walk(node.operand, ctx)
    raise ExprError(f"unsupported syntax: {type(node).__name__}")


def eval_expr(s: str, ctx: dict):
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"parse error: {e}")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED_AST):
            raise ExprError(f"forbidden syntax: {type(n).__name__}")
    return _walk(tree, ctx)


# ─── Template renderer ─────────────────────────────────────────────────

_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _render_str(s: str, ctx: dict) -> str:
    """Substitute every `{{expr}}` in s with its evaluated value.

    Strict — missing fields raise ExprError. Workflow authors must
    populate state defaults rather than rely on optional markers.
    """
    def repl(m):
        v = eval_expr(m.group(1).strip(), ctx)
        if v is None:
            return "null"
        if isinstance(v, (dict, list)):
            return json.dumps(v)
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    return _TEMPLATE_RE.sub(repl, s)


def render_deep(obj, ctx: dict):
    if isinstance(obj, str):
        return _render_str(obj, ctx)
    if isinstance(obj, dict):
        return {k: render_deep(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_deep(v, ctx) for v in obj]
    return obj
