"""camflow runtime — single-file workflow engine.

Implements docs/spec.md:
- Workflow state machine: running / done / halted
- Node state machine:     waiting / running / done (+ result success/fail)
- Halt is workflow-level only; nodes have no halted state.
- Run + Verify are paired (design + QA), share the same `steps` checklist.
- Verify defaults to LLM agent; opt-in `command` for mechanical gating.
- Skill / tool registry is strict (load fails on unresolved reference).
- Retry is internal counter; previous envelope auto-injected as input.previous.

Non-LLM execution goes through the standard library; every LLM
invocation goes through camc_lib.run_and_collect().
"""

from __future__ import annotations

import argparse
import ast
import atexit
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import camc_lib as camc
from .assets import (
    _builtin_planner_dir,
    _camflow_repo_root,
    _resolve_skill_path,
    _resolve_tool_path,
)


# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

VALID_STATUSES = frozenset({"success", "fail"})
VALID_TYPES = frozenset({"string", "integer", "number", "boolean", "array"})

OUTPUT_FILENAME = "agent_output.json"

# Phase B auto-replan hard ceiling. Even if a workflow declares
# max_replans larger than this, runtime caps to prevent runaway loops.
_MAX_REPLANS_HARD_CEILING = 3


# ═══════════════════════════════════════════════════════════════════════
#  EXPRESSIONS + TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

class ExprError(Exception):
    pass


_ALLOWED_AST = (
    ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.Subscript,
    ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Load,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or, ast.Not,
)


def _expr_walk(node, ctx):
    if isinstance(node, ast.Expression):
        return _expr_walk(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "true":  return True
        if node.id == "false": return False
        if node.id == "null":  return None
        if node.id not in ctx:
            raise ExprError(f"undefined name: {node.id}")
        return ctx[node.id]
    if isinstance(node, ast.Attribute):
        obj = _expr_walk(node.value, ctx)
        if isinstance(obj, dict):
            if node.attr not in obj:
                raise ExprError(f"missing key: .{node.attr}")
            return obj[node.attr]
        if obj is None:
            raise ExprError(f"None has no attr {node.attr}")
        raise ExprError(f"can't access .{node.attr} on {type(obj).__name__}")
    if isinstance(node, ast.Subscript):
        obj = _expr_walk(node.value, ctx)
        idx = _expr_walk(node.slice, ctx)
        if isinstance(obj, list):
            try:
                return obj[idx]
            except (IndexError, TypeError) as e:
                raise ExprError(f"subscript: {e}")
        if isinstance(obj, dict):
            # Strict mode: missing dict key is an ExprError (mirrors the
            # attribute branch). No silent None / empty-string fallback.
            if idx not in obj:
                raise ExprError(f"missing key: [{idx!r}]")
            return obj[idx]
        raise ExprError(f"can't subscript {type(obj).__name__}")
    if isinstance(node, ast.Compare):
        left = _expr_walk(node.left, ctx)
        for op, right_node in zip(node.ops, node.comparators):
            right = _expr_walk(right_node, ctx)
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
            return all(_expr_walk(v, ctx) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_expr_walk(v, ctx) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _expr_walk(node.operand, ctx)
    raise ExprError(f"unsupported syntax: {type(node).__name__}")


def eval_expr(s: str, ctx: dict):
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"parse error: {e}")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED_AST):
            raise ExprError(f"forbidden syntax: {type(n).__name__}")
    return _expr_walk(tree, ctx)


_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def render_str(s: str, ctx: dict) -> str:
    """Substitute `{{expr}}` in s with eval_expr(expr, ctx).
    Strict — missing fields raise ExprError. No `?` optional marker.
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
        return render_str(obj, ctx)
    if isinstance(obj, dict):
        return {k: render_deep(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_deep(v, ctx) for v in obj]
    return obj


# ═══════════════════════════════════════════════════════════════════════
#  YAML LOADING + STRUCTURAL VALIDATION
# ═══════════════════════════════════════════════════════════════════════

class WorkflowParseError(Exception):
    pass


_FENCE_RE = re.compile(r"^```(?:yaml|yml)?\s*\n?|\n?```\s*$",
                       re.IGNORECASE | re.MULTILINE)


def load_workflow(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_workflow_yaml(text: str, project_root: Path | None = None) -> dict:
    """Parse + validate a YAML string. Raises WorkflowParseError on any
    problem. Strips ```yaml fences if present (Planner output)."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    if not cleaned:
        raise WorkflowParseError("empty input")
    try:
        wf = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise WorkflowParseError(f"invalid YAML: {e}") from e
    if not isinstance(wf, dict):
        raise WorkflowParseError(
            f"top level is not a dict (got {type(wf).__name__})"
        )
    errors = validate_workflow(wf, project_root=project_root)
    if errors:
        raise WorkflowParseError(
            "validation failed:\n  " + "\n  ".join(errors)
        )
    return wf


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_KNOWN_VERIFY_KEYS = {"criterion", "command", "human", "timeout"}


def validate_workflow(wf: dict, project_root: Path | None = None) -> list[str]:
    """Return list of validation error strings. Empty list = OK.

    With project_root, also resolves skill/tool references to disk —
    workflow load FAILS if any referenced skill or tool is missing.

    Codex review finding 6: tightened — checks id/goal types, steps
    element types, needs element types, retry int range, unknown verify
    keys, and filesystem-safe node IDs (so attempt-N/ paths can't be
    coerced into directory traversal).
    """
    errors = []
    if not isinstance(wf, dict):
        return ["workflow is not a dict"]
    nodes = wf.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["workflow.nodes is missing or empty"]
    if "context" in wf and not isinstance(wf["context"], str):
        errors.append("workflow.context: must be a string")
    # Top-level Workflow.goal — optional in v1.1, but if present it MUST be
    # a string (the supplement landed 2026-05-05 expects compiled user
    # workflows to start emitting it; existing fixtures stay valid by
    # leaving it absent).
    if "goal" in wf and not isinstance(wf["goal"], str):
        errors.append("workflow.goal: must be a string when present")
    # on_halt: opt-in auto-replan (Phase B). Default behavior (no field /
    # explicit "manual") preserves Phase A: halt persists, operator
    # types `camflow replan`. With "replan", runtime auto-invokes
    # Planner re-entry on halt up to max_replans.
    if "on_halt" in wf:
        if wf["on_halt"] not in ("manual", "replan"):
            errors.append(
                "workflow.on_halt: must be \"manual\" or \"replan\" "
                f"when present (got {wf['on_halt']!r})")
    if "max_replans" in wf:
        v = wf["max_replans"]
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(
                "workflow.max_replans: must be an int when present")
        elif v < 0 or v > _MAX_REPLANS_HARD_CEILING:
            errors.append(
                f"workflow.max_replans: must be 0..{_MAX_REPLANS_HARD_CEILING} "
                f"(hard ceiling); got {v}")

    # Guard: each nodes[] element must be a dict before we can call .get().
    # Otherwise we'd raise AttributeError mid-pass and lose the rest of
    # the errors. Record the bad indices and skip them per-node below.
    non_dict_indices = {i for i, n in enumerate(nodes)
                        if not isinstance(n, dict)}
    for i in sorted(non_dict_indices):
        errors.append(
            f"nodes[{i}]: must be a dict (got "
            f"{type(nodes[i]).__name__})"
        )

    ids = [n.get("id") if isinstance(n, dict) else None for n in nodes]
    for i in ids:
        if not isinstance(i, str) or not i:
            errors.append("a node is missing or has non-string 'id'")
            break
    if len(ids) != len(set(ids)):
        errors.append(f"duplicate node ids: {ids}")
    id_set = {i for i in ids if isinstance(i, str) and i}

    for idx, n in enumerate(nodes):
        if idx in non_dict_indices:
            continue  # already reported; can't introspect a non-dict
        nid = n.get("id", "<?>")

        # Required fields presence
        for k in ("goal", "steps", "run"):
            if k not in n:
                errors.append(f"{nid}: missing required field '{k}'")

        # id: filesystem-safe (used as attempt dir name)
        if isinstance(nid, str) and nid != "<?>" and not _NODE_ID_RE.match(nid):
            errors.append(
                f"{nid}.id: must be filesystem-safe "
                f"(match {_NODE_ID_RE.pattern})"
            )

        # goal: non-empty string
        goal = n.get("goal")
        if goal is not None and (not isinstance(goal, str) or not goal.strip()):
            errors.append(f"{nid}.goal: must be a non-empty string")

        # steps: non-empty list of non-empty strings
        steps = n.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or not steps:
                errors.append(
                    f"{nid}.steps: must be a non-empty list of strings"
                )
            else:
                for i, s in enumerate(steps):
                    if not isinstance(s, str) or not s.strip():
                        errors.append(
                            f"{nid}.steps[{i}]: must be a non-empty string"
                        )

        # needs: list of strings
        needs = n.get("needs")
        if needs is not None:
            if not isinstance(needs, list):
                errors.append(f"{nid}.needs: must be a list of node ids")
            else:
                for i, dep in enumerate(needs):
                    if not isinstance(dep, str):
                        errors.append(
                            f"{nid}.needs[{i}]: must be a string node id"
                        )

        # retry: int >= 0 (reject negative, non-int, float)
        if "retry" in n:
            rv = n["retry"]
            if not isinstance(rv, int) or isinstance(rv, bool) or rv < 0:
                errors.append(
                    f"{nid}.retry: must be a non-negative int (got {rv!r})"
                )

        # Run mutex
        run = n.get("run") or {}
        if not isinstance(run, dict):
            errors.append(f"{nid}.run: must be a dict")
            continue
        has_skill = "skill" in run
        has_tool = "tool" in run
        if not (has_skill ^ has_tool):
            errors.append(f"{nid}.run: must have exactly one of `skill` or `tool`")
        # run.skill / run.tool values must be non-empty strings
        # (otherwise _resolve_skill_path / _resolve_tool_path get
        # garbage and TypeError later).
        if has_skill:
            sv = run["skill"]
            if not isinstance(sv, str) or not sv.strip():
                errors.append(
                    f"{nid}.run.skill: must be a non-empty string "
                    f"(got {type(sv).__name__})"
                )
        if has_tool:
            tv = run["tool"]
            if not isinstance(tv, str) or not tv.strip():
                errors.append(
                    f"{nid}.run.tool: must be a non-empty string "
                    f"(got {type(tv).__name__})"
                )

        # Verify mutex + unknown-keys check
        verify = n.get("verify")
        if verify is not None:
            if not isinstance(verify, dict):
                errors.append(f"{nid}.verify: must be a dict")
            else:
                unknown = set(verify.keys()) - _KNOWN_VERIFY_KEYS
                if unknown:
                    errors.append(
                        f"{nid}.verify: unknown keys {sorted(unknown)}; "
                        f"allowed: {sorted(_KNOWN_VERIFY_KEYS)}"
                    )
                v_keys = {k for k in ("criterion", "command", "human")
                          if k in verify}
                if len(v_keys) > 1:
                    errors.append(
                        f"{nid}.verify: at most one of `criterion`, "
                        f"`command`, `human` (got {sorted(v_keys)})"
                    )
                # Each verify-method key must carry a non-empty string.
                for k in ("criterion", "command", "human"):
                    if k in verify:
                        v = verify[k]
                        if not isinstance(v, str) or not v.strip():
                            errors.append(
                                f"{nid}.verify.{k}: must be a non-empty string"
                            )
                # verify.timeout (only meaningful for command); positive int.
                if "timeout" in verify:
                    tv = verify["timeout"]
                    if (not isinstance(tv, int)
                            or isinstance(tv, bool) or tv < 1):
                        errors.append(
                            f"{nid}.verify.timeout: must be a positive int "
                            f"(got {tv!r})"
                        )
        # needs references valid ids (only if needs is a well-formed list)
        n_needs = n.get("needs")
        if isinstance(n_needs, list):
            for dep in n_needs:
                if isinstance(dep, str) and dep not in id_set:
                    errors.append(f"{nid}.needs: unknown node '{dep}'")
        # output_schema types
        schema = n.get("output_schema") or {}
        if not isinstance(schema, dict):
            errors.append(f"{nid}.output_schema: must be a dict")
        else:
            for fk, ft in schema.items():
                if not isinstance(fk, str) or not fk:
                    errors.append(
                        f"{nid}.output_schema: field names must be "
                        f"non-empty strings (got {fk!r})"
                    )
                    continue
                if ft not in VALID_TYPES:
                    errors.append(
                        f"{nid}.output_schema.{fk}: unknown type {ft!r}; "
                        f"allowed: {sorted(VALID_TYPES)}"
                    )
        # skill / tool existence (only with project_root, and only when
        # the value is a non-empty string — otherwise the type-error
        # we already recorded above is the right outcome, and we'd
        # raise TypeError inside _resolve_*_path on garbage input).
        if project_root is not None:
            sv = run.get("skill")
            tv = run.get("tool")
            if has_skill and isinstance(sv, str) and sv.strip():
                if not _resolve_skill_path(sv, project_root):
                    errors.append(
                        f"{nid}.run.skill: '{sv}' not found "
                        f"(no skills/{sv}/SKILL.md in project or repo)"
                    )
            if has_tool and isinstance(tv, str) and tv.strip():
                if not _resolve_tool_path(tv, project_root):
                    errors.append(
                        f"{nid}.run.tool: '{tv}' not found or not "
                        f"executable (relative to {project_root})"
                    )

    # cycle detection on `needs` graph (skip non-dict nodes — they
    # were already flagged above; building needs_map for them would
    # crash with the same AttributeError we're guarding against).
    needs_map = {
        n["id"]: list(n.get("needs", []) or [])
        for n in nodes
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n["id"]
        and isinstance(n.get("needs", []), list)
    }
    visited, stack = set(), set()

    def dfs(u):
        if u in stack:
            errors.append(f"cycle through node '{u}'")
            return
        if u in visited:
            return
        stack.add(u)
        for v in needs_map.get(u, []):
            if v in id_set:
                dfs(v)
        stack.discard(u)
        visited.add(u)

    for nid in id_set:
        dfs(nid)
    return errors


# Skill / tool resolution lives in `assets.py`, imported above.


# ═══════════════════════════════════════════════════════════════════════
#  RUN DIR + ID
# ═══════════════════════════════════════════════════════════════════════

RUN_DIRNAME = "run"
ARCHIVES_DIRNAME = "archives"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gen_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


# ─── CamFlow-managed agent naming ─────────────────────────────────────
#
# Every camc child agent CamFlow spawns gets a short, human-readable
# name so `camc list` is scannable and operators can see at a glance
# which agents are CamFlow-owned and which workflow/node they belong
# to. Naming convention (per the user's spec):
#
#   work / skill agent : cf__{flow}_{node}_a{attempt}
#   verifier agent     : cf__{flow}_{node}_v{attempt}
#
# Slug rules: lowercase a-z0-9 only, separators collapsed to "_",
# leading/trailing "_" stripped. Caps: flow ~8 chars, node ~14 chars.
# When truncation occurs, append a tiny stable hash so distinct
# inputs that share a prefix don't collide on display.
#
# These names are for human readability ONLY. Camc identifies agents
# by their hex `id`; the cleanup/filter source-of-truth remains the
# `camflow:<run_id>` tag, which is unchanged.

_FLOW_SLUG_CAP = 8
_NODE_SLUG_CAP = 14
_AGENT_NAME_HASH_LEN = 4
_SLUG_SEP_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, *, cap: int) -> str:
    """Sanitize+truncate `text` for use as a slug component.

    Lowercases, collapses runs of non-alnum chars to a single "_",
    strips leading/trailing "_". When the sanitized form exceeds
    `cap`, returns the first (cap - 1 - hash_len) chars + "_" + a
    short stable hash of the ORIGINAL input (so two inputs that
    truncate to the same prefix are still distinguishable). Never
    returns the empty string — falls back to a hash-only slug when
    sanitizing produces nothing alphanumeric.
    """
    if not text:
        return _short_hash("", n=_AGENT_NAME_HASH_LEN)
    s = _SLUG_SEP_RE.sub("_", text.lower()).strip("_")
    if not s:
        return _short_hash(text, n=_AGENT_NAME_HASH_LEN)
    if len(s) <= cap:
        return s
    keep = max(0, cap - 1 - _AGENT_NAME_HASH_LEN)
    head = s[:keep].rstrip("_")
    return f"{head}_{_short_hash(text, n=_AGENT_NAME_HASH_LEN)}"


def _short_hash(text: str, *, n: int = 4) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def _make_agent_name(flow: Optional[str], node_id: str,
                     attempt_n: int, *,
                     verifier: bool = False,
                     fallback_flow: str = "") -> str:
    """Compose a CamFlow-managed agent display name.

    `flow` is normally the workflow's top-level `workflow:` field;
    `fallback_flow` (e.g. workflow.run_id) is used when `flow` is
    missing or sanitizes to empty. The returned name is bounded
    (~30 chars) and stable across runs.
    """
    flow_input = flow or fallback_flow or "wf"
    flow_slug = _slug(flow_input, cap=_FLOW_SLUG_CAP)
    node_slug = _slug(node_id or "n", cap=_NODE_SLUG_CAP)
    suffix = f"_v{attempt_n}" if verifier else f"_a{attempt_n}"
    return f"cf__{flow_slug}_{node_slug}{suffix}"


def default_run_dir(project_root: Path) -> Path:
    """Return <project>/.camflow/run/, archiving any prior run first."""
    cam = project_root / ".camflow"
    run = cam / RUN_DIRNAME
    if run.exists() and any(run.iterdir()):
        archive_run_dir(run, cam / ARCHIVES_DIRNAME)
    run.mkdir(parents=True, exist_ok=True)
    return run


def archive_run_dir(run_dir: Path, archives_root: Path) -> Path | None:
    """Move run_dir to archives_root/<stamp>-<status>[-<suffix>]/."""
    if not run_dir.exists():
        return None
    archives_root.mkdir(parents=True, exist_ok=True)
    status = _peek_run_status(run_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    extra = os.environ.get("CAMFLOW_ARCHIVE_SUFFIX", "").strip()
    suffix = f"-{extra}" if extra else ""
    target = archives_root / f"{stamp}-{status}{suffix}"
    n = 1
    while target.exists():
        target = archives_root / f"{stamp}-{status}{suffix}-{n}"
        n += 1
    run_dir.rename(target)
    return target


def _peek_run_status(run_dir: Path) -> str:
    if (run_dir / "halt.json").exists():
        return "halted"
    trace = run_dir / "trace.jsonl"
    if trace.exists():
        try:
            lines = trace.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1])
                if last.get("event") == "workflow_completed":
                    return last.get("status") or "unknown"
        except Exception:
            pass
    return "unknown"


# ═══════════════════════════════════════════════════════════════════════
#  ENVELOPE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def empty_envelope(status: str = "fail",
                   error: dict | None = None,
                   feedback: str | None = None,
                   data: dict | None = None) -> dict:
    """Build an envelope. status is required; rest auto-fill."""
    return {
        "status": status,
        "data": data if data is not None else {},
        "error": error,
        "feedback": feedback,
        "request_human": False,
    }


def normalize_envelope(raw: dict) -> dict:
    """Coerce arbitrary dict into an envelope shape, validating
    status. Returns either a clean envelope or a failure envelope with
    BAD_STATUS if status is invalid.
    """
    status = raw.get("status")
    if status not in VALID_STATUSES:
        return empty_envelope(
            "fail",
            error={"code": "BAD_STATUS",
                   "message": f"agent returned status={status!r}, "
                              f"expected one of {sorted(VALID_STATUSES)}"},
        )
    return {
        "status": status,
        "data": raw.get("data") or {},
        "error": raw.get("error"),
        "feedback": raw.get("feedback"),
        "request_human": bool(raw.get("request_human", False)),
    }


def _envelope_requests_workflow_halt(envelope: dict) -> bool:
    """True when a failing envelope is explicitly asking CamFlow to halt.

    This is distinct from ordinary node failure. Ordinary failure can spend
    retry budget; an explicit halt/replan signal should reach workflow-level
    halt handling immediately so manual/auto replan can run.
    """
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(data, dict):
        if data.get("halt") is True or data.get("replan_required") is True:
            return True

    error = envelope.get("error") if isinstance(envelope, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code in {"ORACLE_HALT", "CAMFLOW_REPLAN_REQUIRED"}


# ═══════════════════════════════════════════════════════════════════════
#  PROMPT BUILDERS
# ═══════════════════════════════════════════════════════════════════════

def _format_schema_for_prompt(schema: dict) -> str:
    """Render output_schema dict as human-readable list."""
    if not schema:
        return "(no specific fields required; data may be any object)"
    lines = []
    for k, t in schema.items():
        lines.append(f"  - {k}: {t}")
    return "\n".join(lines)


def _format_steps_for_prompt(steps: list[str]) -> str:
    return "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))


def build_run_prompt(node: "Node", input_dict: dict,
                     skill_md: str | None = None,
                     workflow_context: str | None = None,
                     workflow_goal: str | None = None) -> str:
    """Compose the prompt for a run agent (skill mode).

    Layout:
      [skill template]
      [Workflow Goal]         ← persistent run objective, optional
      [Workflow Context]      ← shared across every node, optional
      [Goal]                  ← this Node.goal
      [Steps]
      [Upstream Outputs]      ← auto-injected from `needs`, optional
      [Note: previous]         ← only on retry
      [Output schema + delivery protocol]

    `workflow_goal` is the v1.1 Workflow.goal (the persistent run
    objective from top-level YAML `goal:`). When supplied, it appears
    as a dedicated section before Workflow Context so the goal-driven
    retry instructions can actually re-read it.
    """
    parts = []
    if skill_md:
        parts.append(skill_md.strip())
    if workflow_goal and workflow_goal.strip():
        parts.append(f"# Workflow Goal\n{workflow_goal.strip()}")
    if workflow_context and workflow_context.strip():
        parts.append(f"# Workflow Context\n{workflow_context.strip()}")
    parts.append(f"# Goal\n{node.goal}")
    parts.append(
        "# Steps (you MUST do these, in order)\n"
        + _format_steps_for_prompt(node.steps)
    )
    upstream = input_dict.get("upstream") or {}
    if upstream:
        sections = []
        for dep_id, env in upstream.items():
            sections.append(
                f"## {dep_id}\n```json\n"
                + json.dumps(env, indent=2, ensure_ascii=False)
                + "\n```"
            )
        parts.append("# Upstream Outputs\n" + "\n\n".join(sections))
    # Retry-note BEFORE Output (matches spec §8 ordering: Goal, Steps,
    # Upstream Outputs, Note: previous attempt failed, Output).
    if "previous" in input_dict:
        parts.append(
            "# Note: previous attempt failed\n"
            "```json\n"
            + json.dumps(input_dict["previous"], indent=2, ensure_ascii=False)
            + "\n```\n"
            "Goal-driven retry — before redoing this attempt:\n"
            "1. Re-read the # Workflow Goal section above (the persistent "
            "Workflow.goal). What outcome must the run as a whole prove? "
            "(If # Workflow Goal is absent, fall back to the Workflow "
            "Context block.)\n"
            "2. Re-read the # Goal section (this Node.goal). Which part of "
            "the Workflow.goal does this node advance or prove?\n"
            "3. Read previous.feedback (or error.message) as evidence of "
            "what is still missing — not as a diff to literally apply.\n"
            "4. If the local plan is sound, fix the specific gap "
            "previous.feedback names and proceed.\n"
            "5. If the gap is structural (the plan itself is wrong, you "
            "need data the DAG didn't fetch, the node goal can't be met "
            "by a local fix), fail clearly with status=fail and a concrete "
            "error.message naming the plan mismatch — do NOT loop on "
            "unrelated edits to make the same local error go away. Retry "
            "is a bounded safety net, not progress."
        )
    schema_section = _format_schema_for_prompt(node.output_schema)
    parts.append(
        f"# Output\n"
        f"Write a single JSON envelope to `{OUTPUT_FILENAME}` in your "
        f"current working directory:\n\n"
        f"```json\n"
        f"{{\n"
        f'  "status": "success" | "fail",\n'
        f'  "data": {{ ... }},\n'
        f'  "error": null | {{"code": "...", "message": "..."}},\n'
        f'  "feedback": null,\n'
        f'  "request_human": false\n'
        f"}}\n"
        f"```\n\n"
        f"## data shape (required when status=success)\n"
        f"{schema_section}\n\n"
        f"## Rules\n"
        f"- status MUST be exactly \"success\" or \"fail\" (not \"ok\", "
        f"not \"done\", not \"completed\").\n"
        f"- success → data MUST contain ALL fields above with matching types.\n"
        f"- fail → error MUST be `{{code: <short>, message: <human readable>}}`.\n"
        f"- Set request_human=true if the input is so unclear/ambiguous that "
        f"only a human can resolve it (skips retry, halts workflow).\n"
        f"- Don't print to stdout. Don't use markdown code fences in the file.\n"
        f"- Once written, do nothing else. The runner will close the session."
    )
    return "\n\n".join(parts)


def build_verify_prompt(node: "Node", run_envelope: dict,
                        workflow_context: str | None = None,
                        workflow_goal: str | None = None) -> str:
    """Compose the prompt for the verify-agent (default verify path).

    Verify-agent's data shape is fixed: {approved, step_results, reasoning}.

    `workflow_goal` is the v1.1 Workflow.goal (persistent run
    objective). When supplied, the evaluator can judge whether this
    node's run actually advanced the persistent goal, not only
    whether the local checklist was satisfied.
    """
    criterion = (node.verify_config or {}).get("criterion") or ""
    parts = [
        f"You are evaluating whether the previous node `{node.id}` did its job.",
    ]
    if workflow_goal and workflow_goal.strip():
        parts.append(f"# Workflow Goal\n{workflow_goal.strip()}")
    if workflow_context and workflow_context.strip():
        parts.append(f"# Workflow Context\n{workflow_context.strip()}")
    if criterion:
        parts.append(f"# Criterion\n{criterion}")
    parts.append(f"# Goal (same as run's)\n{node.goal}")
    parts.append(
        "# Steps that should have been done (your checklist — verify each)\n"
        + _format_steps_for_prompt(node.steps)
    )
    parts.append(
        "# Envelope produced by run\n```json\n"
        + json.dumps(run_envelope, indent=2, ensure_ascii=False)
        + "\n```"
    )
    parts.append(
        "# Your job\n"
        "For EACH step above, decide whether it was done correctly based on "
        "the envelope and any files in this directory.\n"
        "approved = true ONLY if ALL steps pass.\n"
        "On reject: be specific about what failed — your text becomes the "
        "feedback the run agent sees on its next attempt."
    )
    parts.append(
        "# Evidence protocol (no hollow approves)\n"
        "Every step_result you mark `passed: true` MUST cite **concrete\n"
        "evidence** in the `evidence` field — not just a feeling.\n"
        "\n"
        "Acceptable evidence:\n"
        "  * a verbatim quote from `envelope.data.<field>` "
        "(e.g. `data.root_cause = \"null deref at line 42\"`).\n"
        "  * a file path + line range you actually inspected via "
        "Read/Bash, with the relevant lines quoted.\n"
        "  * literal output of a check command you ran in this directory "
        "(e.g. `pytest -q` → \"5 passed in 0.3s\").\n"
        "\n"
        "NOT acceptable evidence:\n"
        "  * \"the envelope says the step was done\" — that's the run "
        "agent's claim, not verification.\n"
        "  * \"looks correct\" / \"seems fine\" / \"the summary "
        "mentions it\" — vague.\n"
        "  * echoing back the step text — the step is what you're "
        "checking, not evidence the step was done.\n"
        "\n"
        "If you can't find concrete evidence for a step, mark it "
        "`passed: false` and explain in `reasoning` what's missing. "
        "Better to bounce work back to the run agent with specific "
        "feedback than to rubber-stamp.\n"
        "\n"
        "Steps with `passed: false` should also have `evidence` filled "
        "with whatever you DID find (or `\"<missing>\"` if literally "
        "nothing exists), so the run agent on retry can see what you "
        "looked at."
    )
    parts.append(
        f"# Output\n"
        f"Write to `{OUTPUT_FILENAME}`:\n\n"
        f"```json\n"
        f"{{\n"
        f'  "status": "success",\n'
        f'  "data": {{\n'
        f'    "approved": true | false,\n'
        f'    "step_results": [\n'
        f'      {{\n'
        f'        "step": 1,\n'
        f'        "passed": true | false,\n'
        f'        "evidence": "<verbatim quote / file:line / cmd output; '
        f'REQUIRED>",\n'
        f'        "reasoning": "<one sentence why this step passed or failed>"\n'
        f'      }},\n'
        f"      ...\n"
        f"    ],\n"
        f'    "reasoning": "<one-sentence overall>"\n'
        f"  }},\n"
        f'  "error": null,\n'
        f'  "feedback": null,\n'
        f'  "request_human": false\n'
        f"}}\n"
        f"```\n\n"
        f"step_results MUST have exactly {len(node.steps)} entries, one per step."
    )
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
#  RUN EXECUTORS
# ═══════════════════════════════════════════════════════════════════════

def exec_tool(tool_path: Path, input_dict: dict, workspace: Path) -> dict:
    """Run a shell tool. stdin = input.json, stdout = envelope JSON.

    Persistence rule (codex review finding 4): every tool attempt writes
    its raw stdout to BOTH `agent_output.json` (the producer's literal
    output, mirroring spec §11 layout where every attempt has
    agent_output.json) and `raw_stdout.txt` (kept as an extra debug
    artifact). `output.json` is written separately by execute_attempt
    and holds the runtime-validated envelope.

    On timeout, return a TOOL_TIMEOUT fail envelope rather than letting
    subprocess.TimeoutExpired propagate (which would crash the runner).
    The scheduler then handles retry/halt normally.
    """
    def _persist_stdout(text: str) -> None:
        (workspace / "agent_output.json").write_text(text or "")
        (workspace / "raw_stdout.txt").write_text(text or "")

    # Surface dag_revision in env too — convenient for tool wrappers
    # that prefer env vars (curl bodies, simple shells) over JSON
    # parsing. The same value is also in the JSON stdin under
    # "dag_revision" for tools that already parse the input dict.
    rev = input_dict.get("dag_revision")
    env = {**os.environ, "CAMFLOW_WORKSPACE": str(workspace)}
    if rev is not None:
        env["CAMFLOW_DAG_REVISION"] = str(rev)
    try:
        proc = subprocess.run(
            [str(tool_path)],
            input=json.dumps(input_dict),
            capture_output=True, text=True,
            cwd=str(workspace),
            env=env,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        partial = e.stdout
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        _persist_stdout(partial or "")
        return empty_envelope(
            "fail",
            error={"code": "TOOL_TIMEOUT",
                   "message": f"tool exceeded {e.timeout}s timeout"},
        )
    _persist_stdout(proc.stdout or "")
    if proc.returncode != 0:
        return empty_envelope(
            "fail",
            error={"code": "TOOL_NONZERO_EXIT",
                   "message": f"tool exited {proc.returncode}: "
                              f"{(proc.stderr or '').strip()[:200]}"},
        )
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return empty_envelope(
            "fail",
            error={"code": "TOOL_BAD_OUTPUT",
                   "message": f"tool stdout not JSON: {e}"},
        )
    return normalize_envelope(raw)


def exec_skill(skill_md: str, node: "Node", input_dict: dict,
               workspace: Path, attempt_n: int, run_id_tag: str,
               workflow_context: str | None = None,
               workflow_goal: str | None = None,
               flow: str | None = None,
               fallback_flow: str = "") -> dict:
    """Spawn a camc agent loaded with the skill template + run prompt.

    `flow` is the source workflow's display name (top-level
    `workflow:` field) used for human-readable agent names; falls
    back to `fallback_flow` (e.g. run_id) when missing.
    """
    prompt = build_run_prompt(node, input_dict, skill_md=skill_md,
                              workflow_context=workflow_context,
                              workflow_goal=workflow_goal)
    (workspace / "prompt.txt").write_text(prompt)
    agent_name = _make_agent_name(flow, node.id, attempt_n,
                                  fallback_flow=fallback_flow)
    try:
        _aid, raw = camc.run_and_collect(
            prompt=prompt,
            workspace=workspace,
            name=agent_name,
            tag=run_id_tag,
            output_file=OUTPUT_FILENAME,
            timeout_s=camc.DEFAULT_SKILL_TIMEOUT_S,
            write_id_to=workspace / "agent.id",
        )
    except camc.CamcTimeout as e:
        return empty_envelope(
            "fail", error={"code": "AGENT_TIMEOUT", "message": str(e)})
    except camc.CamcError as e:
        return empty_envelope(
            "fail", error={"code": "CAMC_RUN_FAILED", "message": str(e)})
    except json.JSONDecodeError as e:
        return empty_envelope(
            "fail",
            error={"code": "AGENT_BAD_OUTPUT",
                   "message": f"{OUTPUT_FILENAME} not JSON: {e}"})
    return normalize_envelope(raw)


# ═══════════════════════════════════════════════════════════════════════
#  VERIFIERS
# ═══════════════════════════════════════════════════════════════════════

def auto_schema_check(envelope: dict, schema: dict) -> tuple[bool, str]:
    """Field-presence + type check against output_schema."""
    if not schema:
        return True, ""
    data = envelope.get("data") or {}
    type_check = {
        "string":  lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, bool) is False and isinstance(v, int),
        "number":  lambda v: isinstance(v, bool) is False and isinstance(v, (int, float)),
        "boolean": lambda v: isinstance(v, bool),
        "array":   lambda v: isinstance(v, list),
    }
    for key, ftype in schema.items():
        if key not in data:
            return False, f"schema: missing field '{key}' in data"
        check = type_check.get(ftype)
        if check and not check(data[key]):
            return False, (
                f"schema: field '{key}' has wrong type "
                f"(expected {ftype}, got {type(data[key]).__name__})"
            )
    return True, ""


def verify_with_command(cmd_template: str, workflow: "Workflow", node: "Node",
                        envelope: dict, attempt_n: int,
                        timeout: int = 60) -> tuple[bool, str]:
    """Render cmd template, run bash, gate on exit code."""
    ctx = workflow.expr_ctx(current_output=envelope)
    try:
        cmd = render_str(cmd_template, ctx)
    except ExprError as e:
        return False, f"verify command template error: {e}"
    cwd = workflow.run_dir / "nodes" / node.id / f"attempt-{attempt_n}"
    cwd.mkdir(parents=True, exist_ok=True)
    # Always write agent_output.json so cmds can read uniformly across
    # mock / tool / skill paths.
    (cwd / OUTPUT_FILENAME).write_text(json.dumps(envelope, indent=2))
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True,
            timeout=timeout, cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return False, f"verify command timed out after {timeout}s: {cmd[:80]}"
    if proc.returncode != 0:
        snippet = (proc.stderr or proc.stdout).strip()[:300]
        return False, f"verify command exited {proc.returncode}: {snippet}"
    return True, "ok"


def verify_with_human(node: "Node", envelope: dict,
                      prompt_text: str) -> tuple[bool, str]:
    """Show envelope + prompt to user via stdin/stdout.

    User must type 'approve' (case-insensitive, whitespace-trimmed) to
    accept. Anything else is treated as feedback for the next retry.
    EOF / no-TTY → reject with feedback "no TTY available for human verify".
    """
    if not sys.stdin.isatty():
        return False, "no TTY available for human verify"

    print(f"\n─── Human verify required: {node.id} ───", file=sys.stderr)
    data = envelope.get("data") or {}
    print(json.dumps(data, indent=2, ensure_ascii=False), file=sys.stderr)
    print(file=sys.stderr)
    print(prompt_text.strip(), file=sys.stderr)
    print("\nType 'approve' to accept, or describe what to change:",
          file=sys.stderr)
    sys.stderr.flush()
    try:
        line = input("> ")
    except EOFError:
        return False, "stdin closed before human verify response"
    if line.strip().lower() == "approve":
        return True, "approved by human"
    return False, line.strip()


def verify_with_agent(node: "Node", workflow: "Workflow", envelope: dict,
                      attempt_n: int) -> tuple[bool, str]:
    """Spawn a verify-agent (using built-in evaluator skill or override).

    Returns (approved, feedback). Feedback comes from the agent's
    `data.reasoning` (and step_results details on reject).
    """
    sub_dir = workflow.run_dir / "nodes" / node.id / f"attempt-{attempt_n}" / "verify"
    sub_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_verify_prompt(
        node, envelope,
        workflow_context=workflow.spec.get("context"),
        workflow_goal=workflow.goal,
    )
    (sub_dir / "prompt.txt").write_text(prompt)
    agent_name = _make_agent_name(
        workflow.spec.get("workflow"), node.id, attempt_n,
        verifier=True, fallback_flow=getattr(workflow, "run_id", ""))
    try:
        _aid, raw = camc.run_and_collect(
            prompt=prompt,
            workspace=sub_dir,
            name=agent_name,
            tag=workflow.tag,
            output_file=OUTPUT_FILENAME,
            timeout_s=camc.DEFAULT_SKILL_TIMEOUT_S,
        )
    except camc.CamcTimeout as e:
        return False, f"verify-agent timeout: {e}"
    except camc.CamcError as e:
        return False, f"verify-agent failed to run: {e}"
    except json.JSONDecodeError as e:
        return False, f"verify-agent bad output: {e}"
    if raw.get("status") != "success":
        return False, (
            f"verify-agent did not return success: "
            f"{(raw.get('error') or {}).get('message', '?')}"
        )
    data = raw.get("data") or {}

    # Structural shape check (codex review finding 7). The fixed
    # evaluator data shape per spec §9: approved bool, reasoning str,
    # step_results list of length len(node.steps); each item has
    # int step / bool passed / str evidence / str reasoning. Empty-
    # evidence-on-approve is intentionally NOT enforced — that's a
    # prompt-protocol concern (spec §9), not a runtime gate.
    shape_problems = _verify_agent_shape_errors(data, len(node.steps))
    if shape_problems:
        return False, (
            "verify-agent returned malformed data: " + "; ".join(shape_problems)
        )

    approved = bool(data["approved"])
    reasoning = data["reasoning"]
    if not approved:
        # Append step-level details so feedback is actionable.
        bad_steps = [sr for sr in data["step_results"] if not sr["passed"]]
        if bad_steps:
            reasoning += "\nFailed steps: " + json.dumps(bad_steps,
                                                         ensure_ascii=False)
    return approved, reasoning


def _verify_agent_shape_errors(data: dict, expected_len: int) -> list[str]:
    """Return a list of structural-shape error strings for a verify
    agent's `data` dict. Empty list = OK.

    Structural only — types and lengths. Non-empty evidence is NOT
    enforced (prompt-side concern; see Evidence Protocol in spec §9).
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["data is not a dict"]
    if not isinstance(data.get("approved"), bool):
        problems.append("`approved` must be a bool")
    if not isinstance(data.get("reasoning"), str):
        problems.append("`reasoning` must be a string")
    sr = data.get("step_results")
    if not isinstance(sr, list):
        problems.append("`step_results` must be a list")
        return problems
    if len(sr) != expected_len:
        problems.append(
            f"`step_results` has {len(sr)} entries, expected {expected_len}"
        )
    for i, item in enumerate(sr):
        if not isinstance(item, dict):
            problems.append(f"step_results[{i}] is not a dict")
            continue
        if not isinstance(item.get("step"), int) or isinstance(item.get("step"), bool):
            problems.append(f"step_results[{i}].step must be an int")
        if not isinstance(item.get("passed"), bool):
            problems.append(f"step_results[{i}].passed must be a bool")
        if not isinstance(item.get("evidence"), str):
            problems.append(f"step_results[{i}].evidence must be a string")
        if not isinstance(item.get("reasoning"), str):
            problems.append(f"step_results[{i}].reasoning must be a string")
    return problems


# ═══════════════════════════════════════════════════════════════════════
#  NODE
# ═══════════════════════════════════════════════════════════════════════

class Node:
    """A single workflow node — data + lifecycle + run/verify behavior.

    Public attrs (everything you'd want from outside):
      static (set at load):
        id, goal, steps, needs, output_schema, retry_max
      runtime (mutated during execution):
        lifecycle, result, retry_count, output, history
      config (used by behaviors):
        run_config    {skill: ...} or {tool: ...}
        verify_config None (default agent) | {criterion}|{command}|{human}

    Behavior is via methods: run(), verify(), execute_attempt().
    """

    def __init__(self, *, id: str, goal: str, steps: list[str],
                 needs: list[str],
                 output_schema: dict, retry_max: int,
                 run_config: dict, verify_config: Optional[dict]):
        # ── static ──
        self.id = id
        self.goal = goal
        self.steps = steps
        self.needs = needs
        self.output_schema = output_schema
        self.retry_max = retry_max
        self.run_config = run_config
        self.verify_config = verify_config
        # ── runtime ──
        self.lifecycle = "waiting"
        self.result: Optional[str] = None
        self.retry_count = 0
        self.output: Optional[dict] = None
        self.history: list[dict] = []

    # ─── Loading ──────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        run = d.get("run") or {}
        return cls(
            id=d["id"],
            goal=d["goal"],
            steps=list(d["steps"]),
            needs=list(d.get("needs") or []),
            output_schema=dict(d.get("output_schema") or {}),
            retry_max=int(d.get("retry", 1)),
            run_config={k: run[k] for k in ("skill", "tool") if k in run},
            verify_config=(dict(d["verify"]) if d.get("verify") else None),
        )

    # ─── Lifecycle helpers ────────────────────────────────────────────

    def is_done(self) -> bool:
        return self.lifecycle == "done"

    def is_ready(self, all_nodes: dict[str, "Node"]) -> bool:
        if self.lifecycle == "done":
            return False
        for dep in self.needs:
            up = all_nodes.get(dep)
            if up is None or up.lifecycle != "done" or up.result != "success":
                return False
        return True

    def public_view(self) -> dict:
        """What templates see as `nodes.<id>`."""
        return {"output": self.output or {}}

    # ─── Execute one attempt (run + verify) ───────────────────────────

    def execute_attempt(self, workflow: "Workflow", attempt_n: int) -> dict:
        """One attempt = run + verify + persist. Returns final envelope.

        Side effects:
          - writes input.json + output.json to the attempt dir
          - emits trace events
          - mutates self.history / self.output (caller does lifecycle moves)
        """
        att_dir = workflow.run_dir / "nodes" / self.id / f"attempt-{attempt_n}"
        att_dir.mkdir(parents=True, exist_ok=True)

        # Build input by auto-collecting upstream + previous-attempt feedback.
        # No user-authored templates — `run.input` has been removed.
        upstream = {}
        for dep_id in self.needs:
            up = workflow.nodes_by_id.get(dep_id)
            if up is not None and up.output is not None:
                upstream[dep_id] = up.output
        rendered: dict = {}
        if upstream:
            rendered["upstream"] = upstream
        if attempt_n > 1 and self.history:
            rendered["previous"] = self.history[-1]
        # Tools that talk to external systems (e.g. an oracle that
        # tracks dag_revisions) need to know which DAG revision is
        # active. Skill agents reading input.json get the same signal.
        rendered["dag_revision"] = workflow.dag_revision
        (att_dir / "input.json").write_text(
            json.dumps(rendered, indent=2, ensure_ascii=False)
        )

        workflow.trace("node_started", node=self.id, attempt=attempt_n,
                       reason=("retry" if attempt_n > 1 else "needs satisfied"))

        # Run.
        envelope = self.run(workflow, rendered, att_dir, attempt_n)

        # Verify (only when run reported success).
        if envelope["status"] == "success":
            workflow.trace("verify_started", node=self.id, attempt=attempt_n)
            ok, feedback = self.verify(workflow, envelope, attempt_n)
            if not ok:
                envelope["status"] = "fail"
                envelope["error"] = {"code": "VERIFY_FAIL", "message": feedback}
                envelope["feedback"] = feedback
                workflow.trace("verify_failed", node=self.id, attempt=attempt_n,
                               reason=feedback)
            else:
                envelope["feedback"] = feedback
                workflow.trace("verify_completed", node=self.id, attempt=attempt_n)

        # Persist final envelope + record on node.
        (att_dir / "output.json").write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False)
        )
        self.history.append(envelope)
        self.output = envelope

        if envelope["status"] == "success":
            workflow.trace("node_completed", node=self.id, attempt=attempt_n,
                           status="success")
        else:
            workflow.trace("node_failed", node=self.id, attempt=attempt_n,
                           reason=(envelope.get("error") or {}).get("message", "?"))
        return envelope

    def run(self, workflow: "Workflow", input_dict: dict, att_dir: Path,
            attempt_n: int) -> dict:
        """Do the work — dispatch to skill or tool executor."""
        if "skill" in self.run_config:
            skill_name = self.run_config["skill"]
            skill_path = _resolve_skill_path(skill_name, workflow.project_root)
            skill_md = skill_path.read_text() if skill_path else ""
            return exec_skill(skill_md, self, input_dict, att_dir,
                              attempt_n, workflow.tag,
                              workflow_context=workflow.spec.get("context"),
                              workflow_goal=workflow.goal,
                              flow=workflow.spec.get("workflow"),
                              fallback_flow=workflow.run_id)
        if "tool" in self.run_config:
            tool_path = _resolve_tool_path(self.run_config["tool"],
                                           workflow.project_root)
            if not tool_path:
                return empty_envelope(
                    "fail",
                    error={"code": "TOOL_NOT_FOUND",
                           "message": f"tool not found or not -x: "
                                      f"{self.run_config['tool']}"},
                )
            return exec_tool(tool_path, input_dict, att_dir)
        # validation should have caught this; defensive:
        return empty_envelope(
            "fail",
            error={"code": "BAD_RUN_CONFIG",
                   "message": f"node {self.id} run has neither skill nor tool"},
        )

    def verify(self, workflow: "Workflow", envelope: dict,
               attempt_n: int) -> tuple[bool, str]:
        """Schema check, then user-declared verify (or default agent)."""
        # 1. auto schema
        ok, reason = auto_schema_check(envelope, self.output_schema)
        if not ok:
            return False, reason
        # 2. configured verify
        cfg = self.verify_config
        if cfg is None:
            # Default: agent verify with steps as criterion.
            return verify_with_agent(self, workflow, envelope, attempt_n)
        if "command" in cfg:
            return verify_with_command(
                cfg["command"], workflow, self, envelope, attempt_n,
                timeout=int(cfg.get("timeout", 60)),
            )
        if "human" in cfg:
            return verify_with_human(self, envelope, cfg["human"])
        # criterion (default agent path with explicit override criterion)
        return verify_with_agent(self, workflow, envelope, attempt_n)


# ═══════════════════════════════════════════════════════════════════════
#  WORKFLOW — the runtime instance (composer / scheduler; doesn't run)
# ═══════════════════════════════════════════════════════════════════════

class Workflow:
    """One execution of a workflow.

    Owns:
      - the run dir + persistence (trace.jsonl, halt.json, runner.pid)
      - the node graph + their lifecycle state
      - the camc run tag (for crash-safety net)
      - the workflow-level state machine (running/done/halted)

    Workflow does NOT itself run() or verify() — Node does. Workflow only
    schedules nodes (`execute_dag`).
    """

    def __init__(self, spec: dict, run_dir: Path,
                 *, resume: bool = False,
                 replan: bool = False,
                 project_root: Optional[Path] = None):
        self.spec = spec
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_by_id: dict[str, Node] = {
            n["id"]: Node.from_dict(n) for n in spec["nodes"]
        }
        self.lifecycle = "running"
        self.step_n = 0
        self.run_id = gen_run_id()
        self.tag = f"camflow:{self.run_id}"

        # Workflow.goal — the persistent objective for the run, per the
        # 2026-05-05 goal-driven supplement §3.1. Optional in v1.1; when
        # present it MUST be a string (validate_workflow enforces).
        self.goal: Optional[str] = (spec.get("goal")
                                    if isinstance(spec, dict) else None)
        # Phase B opt-in auto-replan. Default behavior preserves Phase A.
        self.on_halt: str = (
            (spec.get("on_halt") if isinstance(spec, dict) else None)
            or "manual"
        )
        # Effective max_replans: clamp to [0, hard_ceiling]. If on_halt is
        # "replan" but no explicit max declared, default to 1 (single
        # bounded auto-recovery — the supplement's safety-net default).
        declared_max = (spec.get("max_replans")
                        if isinstance(spec, dict) else None)
        if isinstance(declared_max, int) and not isinstance(declared_max, bool):
            self.max_replans = max(0, min(declared_max,
                                          _MAX_REPLANS_HARD_CEILING))
        else:
            self.max_replans = 1 if self.on_halt == "replan" else 0

        self.trace_path = run_dir / "trace.jsonl"
        self.pid_path = run_dir / "runner.pid"

        # project_root: explicit override (used by builtin Planner workflow)
        # OR derived from run_dir's .camflow ancestor.
        if project_root is not None:
            self.project_root = project_root
        else:
            parts = run_dir.resolve().parts
            if ".camflow" in parts:
                idx = parts.index(".camflow")
                self.project_root = Path(*parts[:idx]) if idx > 0 else Path("/")
            else:
                self.project_root = Path.cwd().resolve()

        # dag_revision: which compiled-DAG revision is currently active.
        # User-workflow trace events get tagged with this; Planner-internal
        # workflows skip the field (their run_dir is .camflow/run/planner/,
        # i.e. has a 'planner' parts segment).
        self._is_user_workflow = self._detect_user_workflow_role()
        self.dag_revision: int = 1

        if replan:
            # Replan: caller (camflow replan) has already written the new
            # active workflow.yaml AND recorded dag_revisions/<N>/. Pick
            # up that revision; preserve the existing trace.jsonl so the
            # halt-then-replan story stays continuous on disk.
            if self.trace_path.exists():
                self.step_n = sum(1 for _ in self.trace_path.open())
            if self._is_user_workflow:
                self.dag_revision = self._latest_recorded_revision() or 1
            # Skill agents run inside camc-spawned tmux sessions and
            # inherit env from the runtime process. exec_tool sets
            # CAMFLOW_DAG_REVISION explicitly per call, but skill-node
            # bash invocations (e.g. agents calling wrapper scripts)
            # only see what the runtime process exported. Mirror the
            # active revision into os.environ so wrappers like
            # `${CAMFLOW_DAG_REVISION:-1}` resolve correctly across
            # both tool and skill paths.
            if self._is_user_workflow:
                os.environ["CAMFLOW_DAG_REVISION"] = str(self.dag_revision)
        elif not resume:
            (run_dir / "workflow.yaml").write_text(
                yaml.safe_dump(spec, sort_keys=False)
            )
            # Record the active execution DAG before user nodes run, so a
            # future replay tool can reconstruct which plan was active for
            # each node attempt. Mechanical; no scheduling/retry/verify
            # behavior change. Skipped for the Planner-internal workflow —
            # the user-facing active DAG is the one needing replay
            # bookkeeping.
            if self._is_user_workflow:
                self._record_dag_revision(
                    revision=1,
                    parent_revision=None,
                    reason="initial_plan",
                )
                os.environ["CAMFLOW_DAG_REVISION"] = str(self.dag_revision)
        else:
            if self.trace_path.exists():
                self.step_n = sum(1 for _ in self.trace_path.open())
            if self._is_user_workflow:
                # On resume, pick up the last revision number from the
                # recorded directory so trace tagging stays consistent.
                self.dag_revision = self._latest_recorded_revision() or 1
                os.environ["CAMFLOW_DAG_REVISION"] = str(self.dag_revision)

        self.pid_path.write_text(str(os.getpid()))

        # Crash-safety net.
        self._installed = False
        atexit.register(self._on_exit_cleanup)
        try:
            self._prev_term = signal.signal(signal.SIGTERM, self._on_signal)
            self._prev_int = signal.signal(signal.SIGINT, self._on_signal)
            self._installed = True
        except (ValueError, OSError):
            pass

    # ─── DAG revision (per goal-driven supplement §3.6) ───────────────

    def _detect_user_workflow_role(self) -> bool:
        """True iff this Workflow is the user-facing active DAG.

        Heuristic: the Planner builtin runs inside <project>/.camflow/run/
        planner/, so its run_dir has a 'planner' segment under .camflow/.
        Real user workflows live one level shallower (just .camflow/run/).
        Misclassification is a forward-incompatibility risk if a user
        ever names their project literally "planner"; that's accepted as
        an MVP simplification per the supplement's "keep it mechanical".
        """
        parts = self.run_dir.resolve().parts
        return not (".camflow" in parts and "planner" in parts)

    def _dag_revisions_dir(self) -> Path:
        return self.run_dir / "dag_revisions"

    @staticmethod
    def _rev_dirname(n: int) -> str:
        return f"{n:04d}"

    def _latest_recorded_revision(self) -> Optional[int]:
        d = self._dag_revisions_dir()
        if not d.is_dir():
            return None
        nums = []
        for sub in d.iterdir():
            if sub.is_dir() and sub.name.isdigit():
                nums.append(int(sub.name))
        return max(nums) if nums else None

    def _record_dag_revision(self, *, revision: int,
                             parent_revision: Optional[int],
                             reason: str) -> None:
        """Copy the active workflow.yaml into dag_revisions/<NNNN>/ +
        manifest.json. Mechanical; no scheduling/retry/verify change.
        """
        rev_dir = self._dag_revisions_dir() / self._rev_dirname(revision)
        rev_dir.mkdir(parents=True, exist_ok=True)
        active = self.run_dir / "workflow.yaml"
        if active.exists():
            (rev_dir / "workflow.yaml").write_text(active.read_text())
        manifest = {
            "revision": revision,
            "parent_revision": parent_revision,
            "reason": reason,
            "workflow_goal": self.goal,  # may be None for legacy/synthetic
        }
        (rev_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False)
        )
        self.dag_revision = revision

    # ─── Persistence + tracing ────────────────────────────────────────

    def trace(self, event: str, **fields):
        self.step_n += 1
        rec = {"step": self.step_n, "ts": utcnow_iso(),
               "event": event}
        # Tag user-workflow events with the active DAG revision so a
        # future replay tool can reconstruct which plan was active for
        # each node attempt (supplement §3.6). Planner-internal workflows
        # skip this field — the active execution DAG is the user's.
        if self._is_user_workflow:
            rec["dag_revision"] = self.dag_revision
        rec.update(fields)
        with self.trace_path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ─── Template context ─────────────────────────────────────────────

    def expr_ctx(self, current_output: dict | None = None) -> dict:
        """Template namespace dict.

        Only `nodes.<id>.output` namespace, plus `output.X` when
        verifying (current envelope under check). No `state` / `inputs`.
        """
        nodes_view = {nid: n.public_view() for nid, n in self.nodes_by_id.items()
                      if n.output is not None}
        ctx = {"nodes": nodes_view}
        if current_output is not None:
            ctx["output"] = current_output
        return ctx

    # ─── DAG scheduling ───────────────────────────────────────────────

    def ready_nodes(self) -> list[Node]:
        return [n for n in self.nodes_by_id.values()
                if n.is_ready(self.nodes_by_id)]

    def all_done(self) -> bool:
        return all(n.is_done() for n in self.nodes_by_id.values())

    # ─── Halt + cleanup ───────────────────────────────────────────────

    def halt(self, halted_node: Node, reason: str, envelope: dict,
             *, propagate_fail: bool = True) -> None:
        """Trip the workflow-level halted state + write halt.json.

        propagate_fail=True (default): real halt — every not-yet-done
        node gets marked done+fail (so downstream is consistent in the
        trace). Used by retry-exhausted, request_human, deadlock, etc.

        propagate_fail=False: soft halt — used for `--steps N`
        breakpoints. Other nodes keep their state (waiting / done /
        running) so resume continues seamlessly. halt.json carries
        kind="breakpoint" so resume knows not to bump retry_max.
        """
        kind = "halt" if propagate_fail else "breakpoint"
        halt_info = {
            "halted_node": halted_node.id,
            "retry_count": halted_node.retry_count,
            "reason": reason,
            "envelope": envelope,
            "trace_step": self.step_n + 1,
            "kind": kind,
        }
        (self.run_dir / "halt.json").write_text(
            json.dumps(halt_info, indent=2, ensure_ascii=False)
        )
        if propagate_fail:
            for n in self.nodes_by_id.values():
                if not n.is_done():
                    n.lifecycle = "done"
                    n.result = "fail"
                    n.output = empty_envelope(
                        "fail",
                        error={"code": "UPSTREAM_HALTED",
                               "message": f"halted by '{halted_node.id}'"},
                    )
        event = "workflow_halted" if propagate_fail else "workflow_paused"
        self.trace(event, node=halted_node.id, reason=reason)
        self.lifecycle = "halted"

    # ─── DAG execution (the scheduler — Workflow's only behavior) ─────

    def execute_dag(self, *, max_attempts: Optional[int] = None) -> str:
        """Schedule and run all nodes. Returns 'done' or 'halted'.

        Workflow doesn't run() or verify() — it picks ready nodes and
        delegates each attempt to Node.execute_attempt().

        max_attempts: if given, halt cleanly (kind="breakpoint") after
        that many node-attempts complete. Counts each
        Node.execute_attempt() call, so retries also count. Resume
        continues from where the step-halt left off without resetting
        the halted node's state.
        """
        attempts_run = 0
        while self.lifecycle == "running":
            ready = self.ready_nodes()
            if not ready:
                if self.all_done():
                    self.trace("workflow_completed", status="success")
                    self.lifecycle = "done"
                    return "done"
                # Deadlock (shouldn't happen on valid DAG with success-only deps,
                # but be defensive). Halt the first not-done node.
                for n in self.nodes_by_id.values():
                    if not n.is_done():
                        self.halt(n, "deadlock: no ready nodes",
                                  empty_envelope(
                                      "fail",
                                      error={"code": "DEADLOCK",
                                             "message": "no ready nodes"}))
                        return "halted"
                self.lifecycle = "done"
                return "done"

            # Pick first ready by declaration order in YAML.
            node_ids_in_order = list(self.nodes_by_id.keys())
            ready_set = {n.id for n in ready}
            node = next(self.nodes_by_id[nid] for nid in node_ids_in_order
                        if nid in ready_set)
            node.lifecycle = "running"

            # Next attempt number = how many attempts already on disk + 1.
            # We use len(node.history), not retry_count, so this is robust
            # after resume / rerun where history is restored from disk.
            attempt_n = len(node.history) + 1
            envelope = node.execute_attempt(self, attempt_n)
            attempts_run += 1

            # Explicit human-handoff request → halt immediately, skip retry.
            if envelope.get("request_human"):
                self.trace("node_requested_human", node=node.id,
                           reason=(envelope.get("error") or {}).get("message", ""))
                node.lifecycle = "done"
                node.result = "fail"
                self.halt(node, "node requested human", envelope)
                return "halted"

            if envelope["status"] == "success":
                node.lifecycle = "done"
                node.result = "success"
                if max_attempts is not None and attempts_run >= max_attempts:
                    self.halt(node,
                              f"step limit reached ({max_attempts} attempts)",
                              envelope, propagate_fail=False)
                    return "halted"
                continue

            # status = fail. Decide: retry or halt.
            if _envelope_requests_workflow_halt(envelope):
                reason = ((envelope.get("error") or {}).get("message")
                          or envelope.get("feedback")
                          or "node requested workflow halt")
                self.trace("explicit_halt_requested", node=node.id,
                           retry_count=node.retry_count,
                           retry_max=node.retry_max,
                           reason=reason)
                node.lifecycle = "done"
                node.result = "fail"
                self.halt(node, reason, envelope)
                return "halted"

            if node.retry_count < node.retry_max:
                node.retry_count += 1
                self.trace("retry_triggered", node=node.id,
                           retry_count=node.retry_count,
                           retry_max=node.retry_max,
                           reason=(envelope.get("error") or {}).get("message", "?"))
                if max_attempts is not None and attempts_run >= max_attempts:
                    self.halt(node,
                              f"step limit reached ({max_attempts} attempts)",
                              envelope, propagate_fail=False)
                    return "halted"
                # node.lifecycle stays "running"; loop picks it again next iter.
                continue

            # Out of retries.
            self.trace("retry_exhausted", node=node.id,
                       retry_max=node.retry_max)
            node.lifecycle = "done"
            node.result = "fail"
            self.halt(node, f"retry exhausted (retry_max={node.retry_max})",
                      envelope)
            return "halted"

        # Loop exited because lifecycle != "running".
        return self.lifecycle

    def cleanup(self) -> None:
        """End-of-run: remove pid file + uninstall safety net."""
        try:
            atexit.unregister(self._on_exit_cleanup)
        except Exception:
            pass
        if self._installed:
            try:
                signal.signal(signal.SIGTERM, self._prev_term)
                signal.signal(signal.SIGINT, self._prev_int)
            except (ValueError, OSError):
                pass
            self._installed = False
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

    def _on_exit_cleanup(self):
        n = camc.kill_by_tag(self.tag)
        if n > 0:
            print(f"camflow: killed {n} orphan agent(s) on exit",
                  file=sys.stderr)

    def _on_signal(self, signum, _frame):
        sys.exit(128 + signum)


# ═══════════════════════════════════════════════════════════════════════
#  MAIN LOOP — Workflow.execute_dag (the scheduler)
# ═══════════════════════════════════════════════════════════════════════

def run_workflow(workflow: dict, run_dir: Path,
                 *, resume_with_run: Optional["Workflow"] = None,
                 max_attempts: Optional[int] = None,
                 replan: bool = False) -> str:
    """Execute a workflow → return final lifecycle state ('done' or 'halted').

    `resume_with_run` is for the resume command — caller pre-builds a
    Workflow with prior attempts replayed.

    `max_attempts` (debug): halt cleanly after that many node-attempts
    via a breakpoint-kind halt. Resume continues from there.

    `replan` is for the replan command — caller has already written
    the new active workflow.yaml + recorded dag_revisions/<N>/, so
    Workflow skips those side effects and reuses the existing run_dir.
    """
    wf = resume_with_run if resume_with_run is not None else \
        Workflow(workflow, run_dir, replan=replan)
    if resume_with_run is None:
        wf.trace("workflow_started", run_id=wf.run_id)
    try:
        return wf.execute_dag(max_attempts=max_attempts)
    finally:
        wf.cleanup()


# ═══════════════════════════════════════════════════════════════════════
#  RESUME
# ═══════════════════════════════════════════════════════════════════════

def _summarize_run(run_dir: Path) -> dict:
    """Read run dir → list of node attempts (used by resume)."""
    if not run_dir.exists():
        raise FileNotFoundError(f"run dir not found: {run_dir}")
    summary = {"workflow": None, "nodes": []}
    wf_path = run_dir / "workflow.yaml"
    if wf_path.exists():
        wf = yaml.safe_load(wf_path.read_text())
        summary["workflow"] = wf
        for n in wf.get("nodes") or []:
            nid = n["id"]
            attempt_dir = run_dir / "nodes" / nid
            attempts = []
            if attempt_dir.exists():
                for ad in sorted(attempt_dir.glob("attempt-*"),
                                 key=lambda p: int(p.name.split("-")[1])):
                    op = ad / "output.json"
                    if op.exists():
                        attempts.append(json.loads(op.read_text()))
            summary["nodes"].append({"id": nid, "attempts": attempts})
    return summary


def _cmd_resume(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="camflow resume",
                                description="Resume a halted workflow.")
    p.add_argument("run_dir")
    p.add_argument("--feedback", default="",
                   help="extra feedback injected into halted node's input.previous.feedback")
    p.add_argument("--steps", type=int, default=None,
                   help="(debug) advance only N node-attempts, then halt again")
    args = p.parse_args(argv)
    if args.steps is not None and args.steps < 1:
        print("ERROR: --steps must be >= 1", file=sys.stderr)
        return 1

    rd = Path(args.run_dir)
    halt_path = rd / "halt.json"
    if not halt_path.exists():
        print(f"ERROR: not halted (no halt.json at {halt_path})",
              file=sys.stderr)
        return 1
    halt_info = json.loads(halt_path.read_text())
    workflow = yaml.safe_load((rd / "workflow.yaml").read_text())

    wf = Workflow(workflow, rd, resume=True)
    # Replay history.
    summary = _summarize_run(rd)
    for nrec in summary["nodes"]:
        node = wf.nodes_by_id.get(nrec["id"])
        if node is None or not nrec["attempts"]:
            continue
        node.history = list(nrec["attempts"])
        last = node.history[-1]
        node.output = last
        node.retry_count = len(nrec["attempts"]) - 1
        # Resumed-from node will reset to running below (real-halt path);
        # others stay done.
        if last.get("status") == "success":
            node.lifecycle = "done"
            node.result = "success"
        else:
            node.lifecycle = "done"
            node.result = "fail"

    halted_id = halt_info["halted_node"]
    is_breakpoint = halt_info.get("kind") == "breakpoint"

    if is_breakpoint:
        # Step / breakpoint halt. The naive replay above sets the
        # halted node to lifecycle="done", result="fail" if its last
        # envelope is fail — but a breakpoint can fire AFTER a failed
        # attempt with retry budget remaining (i.e., retry_triggered
        # already incremented retry_count, but the next attempt didn't
        # run because we paused). In that mid-retry case the node is
        # NOT really done — restore it to lifecycle="waiting" so the
        # scheduler picks it up.
        #
        # Detection: halt.json captured retry_count AT halt time. If
        # that's higher than what we'd deduce from on-disk attempts
        # (len(history) - 1), retry_triggered fired and we're paused
        # mid-retry.
        halted_node = wf.nodes_by_id.get(halted_id)
        halt_rc = int(halt_info.get("retry_count", 0))
        deduced_rc = (len(halted_node.history) - 1) if (
            halted_node and halted_node.history
        ) else 0
        is_mid_retry = (halted_node is not None
                        and halted_node.history
                        and halt_rc > deduced_rc)
        if is_mid_retry:
            halted_node.lifecycle = "waiting"
            halted_node.result = None
            halted_node.retry_count = halt_rc
            if args.feedback:
                halted_node.history[-1] = {
                    **halted_node.history[-1],
                    "feedback": args.feedback,
                }
                halted_node.output = halted_node.history[-1]
        else:
            # Breakpoint after a successful attempt (or before first
            # attempt). Node states on disk are correct as-is.
            if args.feedback:
                print("WARNING: --feedback ignored on breakpoint resume "
                      "(no in-flight failed attempt to inject into).",
                      file=sys.stderr)
    else:
        # Real halt — reset the halted node so it gets re-executed; bump
        # retry budget by 1 so resume actually has a try.
        node = wf.nodes_by_id[halted_id]
        node.lifecycle = "waiting"
        node.result = None
        if args.feedback:
            # Splice user-provided feedback into the last envelope so it
            # appears in input.previous.feedback on next attempt.
            if node.history:
                node.history[-1] = {**node.history[-1],
                                    "feedback": args.feedback}
                node.output = node.history[-1]
        # Allow at least one more attempt past retry_max from before.
        node.retry_max = max(node.retry_max + 1, node.retry_count + 1)

    wf.lifecycle = "running"
    halt_path.unlink()  # clear; if it halts again, we'll write fresh
    wf.trace("workflow_resumed", node=halted_id,
             retry_count=wf.nodes_by_id[halted_id].retry_count,
             feedback_len=len(args.feedback),
             from_breakpoint=is_breakpoint)

    print(f"resuming {halted_id}{' (breakpoint)' if is_breakpoint else ''}",
          file=sys.stderr)
    result = run_workflow(workflow, rd, resume_with_run=wf,
                          max_attempts=args.steps)
    print(f"result:  {result}", file=sys.stderr)
    return _result_to_exit(result)


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def _result_to_exit(result: str) -> int:
    return {"done": 0, "halted": 2}.get(result, 1)


def _downstream_set(wf: "Workflow", root_id: str) -> set[str]:
    """Return {root_id} ∪ all transitive descendants in the DAG.

    Used by `_cmd_rerun` to find every node that must be reset when a
    given node is re-executed (their inputs depend on the target).
    """
    children: dict[str, list[str]] = {nid: [] for nid in wf.nodes_by_id}
    for n in wf.nodes_by_id.values():
        for dep in n.needs:
            children.setdefault(dep, []).append(n.id)
    result: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in result:
            continue
        result.add(cur)
        stack.extend(children.get(cur, []))
    return result


def _do_rerun(rd: Path, node_id: str, feedback: str,
              steps: Optional[int]) -> int:
    """Body of rerun (factored from CLI parsing).

    Re-execute `node_id` plus all its downstream descendants in the
    workflow stored under `rd`. Upstream nodes keep their state.
    Used by `camflow run --from <node>` and (for backcompat) the
    legacy `_cmd_rerun` test entry point.
    """
    wf_path = rd / "workflow.yaml"
    if not wf_path.exists():
        print(f"ERROR: no workflow.yaml at {wf_path}", file=sys.stderr)
        return 1
    workflow = yaml.safe_load(wf_path.read_text())

    wf = Workflow(workflow, rd, resume=True)

    # Replay history (same as resume).
    summary = _summarize_run(rd)
    for nrec in summary["nodes"]:
        node = wf.nodes_by_id.get(nrec["id"])
        if node is None or not nrec["attempts"]:
            continue
        node.history = list(nrec["attempts"])
        last = node.history[-1]
        node.output = last
        node.retry_count = len(nrec["attempts"]) - 1
        node.lifecycle = "done"
        node.result = "success" if last.get("status") == "success" else "fail"

    if node_id not in wf.nodes_by_id:
        print(f"ERROR: node '{node_id}' not in workflow. "
              f"Known: {sorted(wf.nodes_by_id)}", file=sys.stderr)
        return 1

    # Reset target + every descendant. They re-execute; their previous
    # attempts stay on disk under attempt-N/ (new ones append as N+1).
    targets = _downstream_set(wf, node_id)
    for nid in sorted(targets):
        node = wf.nodes_by_id[nid]
        node.lifecycle = "waiting"
        node.result = None
        # Splice user feedback into ONLY the explicit target's last attempt
        # (downstream nodes get automatic upstream injection on next run).
        if nid == node_id and feedback and node.history:
            node.history[-1] = {**node.history[-1], "feedback": feedback}
            node.output = node.history[-1]
        # Bump retry_max so the scheduler will actually try again.
        node.retry_max = max(node.retry_max + 1, node.retry_count + 1)

    wf.lifecycle = "running"
    halt_path = rd / "halt.json"
    if halt_path.exists():
        halt_path.unlink()

    wf.trace("workflow_rerun", node=node_id,
             downstream=sorted(targets - {node_id}),
             feedback_len=len(feedback))

    print(f"rerunning {node_id}"
          + (f" (+ {len(targets)-1} downstream)" if len(targets) > 1 else ""),
          file=sys.stderr)
    result = run_workflow(workflow, rd, resume_with_run=wf,
                          max_attempts=steps)
    print(f"result: {result}", file=sys.stderr)
    return _result_to_exit(result)


def _cmd_rerun(argv: list[str]) -> int:
    """LEGACY thin wrapper around _do_rerun — preserved so existing
    test fixtures (`_cmd_rerun([str(rd), "node_id"])`) keep working.
    NOT routed from main(); production users invoke
    `camflow run --from <node_id> [--run-dir <path>]`."""
    p = argparse.ArgumentParser(prog="camflow rerun (legacy)")
    p.add_argument("run_dir")
    p.add_argument("node_id")
    p.add_argument("--feedback", default="")
    p.add_argument("--steps", type=int, default=None)
    args = p.parse_args(argv)
    if args.steps is not None and args.steps < 1:
        print("ERROR: --steps must be >= 1", file=sys.stderr)
        return 1
    return _do_rerun(Path(args.run_dir), args.node_id,
                     args.feedback, args.steps)


def _cmd_run(argv: list[str]) -> int:
    """`camflow run` dispatcher.

    Two modes:
      - With prompt: fresh — compile via Planner, then execute.
      - With --from <node_id> (no prompt): re-execute that node and
        all its downstream descendants on an existing run dir.
    """
    p = argparse.ArgumentParser(
        prog="camflow run",
        description="Compile a prompt into a workflow via Planner, "
                    "then run it. Or re-execute from a specific node "
                    "with --from.",
    )
    p.add_argument("prompt", nargs="?", default=None,
                   help="natural-language task prompt (fresh-run mode)")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="(fresh-run only) pause after Planner compiles, "
                        "let you approve the workflow.yaml before execution")
    p.add_argument("--from", dest="from_node", default=None,
                   help="(rerun mode) id of a node in the existing run "
                        "to re-execute, along with its downstream "
                        "descendants. Cannot be combined with a prompt.")
    p.add_argument("--feedback", default="",
                   help="(rerun mode) optional human feedback spliced into "
                        "the target node's last envelope")
    p.add_argument("--steps", type=int, default=None,
                   help="(debug) halt cleanly after N node-attempts")
    p.add_argument("--run-dir", default=None,
                   help="run directory (default: ./.camflow/run/)")
    args = p.parse_args(argv)
    if args.steps is not None and args.steps < 1:
        print("ERROR: --steps must be >= 1", file=sys.stderr)
        return 1

    has_prompt = bool(args.prompt and args.prompt.strip())
    if has_prompt and args.from_node:
        print("ERROR: cannot combine a prompt with --from. "
              "Either provide a prompt (fresh run) OR --from <node> "
              "(rerun on existing run dir).", file=sys.stderr)
        return 1
    if not has_prompt and not args.from_node:
        print("ERROR: camflow run needs either a prompt OR --from <node>.\n"
              "Examples:\n"
              "  camflow run \"<your task description>\"\n"
              "  camflow run --from <node_id>",
              file=sys.stderr)
        return 1
    if has_prompt and args.feedback:
        print("ERROR: --feedback only valid with --from (rerun mode).",
              file=sys.stderr)
        return 1
    if not has_prompt and args.interactive:
        print("ERROR: -i / --interactive only valid with a prompt "
              "(fresh-run mode).", file=sys.stderr)
        return 1

    project = Path.cwd().resolve()
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    elif args.from_node:
        # Rerun mode default: use existing ./.camflow/run/ directly.
        # Do NOT call default_run_dir() — that archives the prior run,
        # which is exactly the data we need to operate on.
        run_dir = project / ".camflow" / RUN_DIRNAME
    else:
        # Fresh-run default: archive any prior run, get a clean dir.
        run_dir = default_run_dir(project)

    # ── Rerun mode ─────────────────────────────────────────────────────
    if args.from_node:
        return _do_rerun(run_dir, args.from_node, args.feedback, args.steps)

    # ── Fresh-run mode ─────────────────────────────────────────────────
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist the user's prompt at run-dir root for debug + resume.
    (run_dir / "prompt.txt").write_text(args.prompt)

    # ── Phase 1: run the Planner workflow ──────────────────────────────
    planner_dir = _builtin_planner_dir()
    planner_yaml = planner_dir / "workflow.yaml"
    if not planner_yaml.exists():
        print(f"ERROR: builtin Planner missing at {planner_yaml}",
              file=sys.stderr)
        return 1
    planner_spec = yaml.safe_load(planner_yaml.read_text())

    # Inject user prompt at the top of Planner's context.
    base_ctx = planner_spec.get("context") or ""
    planner_spec["context"] = (
        f"# Original user prompt\n{args.prompt.strip()}\n\n"
        f"---\n\n{base_ctx}"
    )

    # Interactive mode: swap render_yaml's verify to require human approval
    # of the compiled workflow.yaml before runtime executes it. Default
    # (no -i): render_yaml uses its declared agent/criterion verify and
    # the runtime just runs the result.
    if args.interactive:
        for n in planner_spec["nodes"]:
            if n["id"] == "render_yaml":
                n["verify"] = {
                    "human": (
                        "Review the proposed workflow.yaml below.\n"
                        "Type 'approve' to accept and have the runtime "
                        "execute it.\n"
                        "Otherwise, describe what to change and the "
                        "Planner will revise."
                    )
                }
                break

    errors = validate_workflow(planner_spec, project_root=planner_dir)
    if errors:
        for e in errors:
            print(f"ERROR (Planner): {e}", file=sys.stderr)
        return 1

    planner_run_dir = run_dir / "planner"
    print(f"compiling prompt via Planner → {planner_run_dir}", file=sys.stderr)
    planner_wf = Workflow(planner_spec, planner_run_dir,
                          project_root=planner_dir)
    planner_wf.trace("workflow_started", run_id=planner_wf.run_id,
                     role="planner")
    try:
        planner_result = planner_wf.execute_dag()
    finally:
        planner_wf.cleanup()

    if planner_result != "done":
        print(f"Planner halted ({planner_result}). See "
              f"{planner_run_dir}/halt.json for details.", file=sys.stderr)
        return _result_to_exit(planner_result)

    # ── Phase 2: extract Planner's yaml_text and run it ────────────────
    render_node = planner_wf.nodes_by_id.get("render_yaml")
    if render_node is None or not render_node.output:
        print("ERROR: Planner finished but produced no render_yaml output",
              file=sys.stderr)
        return 1
    yaml_text = (render_node.output.get("data") or {}).get("yaml_text")
    if not yaml_text:
        print("ERROR: Planner's render_yaml output is missing yaml_text",
              file=sys.stderr)
        return 1

    try:
        user_spec = parse_workflow_yaml(yaml_text)
    except WorkflowParseError as e:
        print(f"ERROR: Planner produced invalid YAML: {e}", file=sys.stderr)
        return 1

    errors = validate_workflow(user_spec, project_root=project)
    if errors:
        for e in errors:
            print(f"ERROR (compiled workflow): {e}", file=sys.stderr)
        return 1

    print(f"executing compiled workflow → {run_dir}", file=sys.stderr)
    result = _execute_with_optional_auto_replan(
        user_spec, run_dir, max_attempts=args.steps)
    print(f"result: {result}", file=sys.stderr)
    return _result_to_exit(result)


def _execute_with_optional_auto_replan(
        user_spec: dict, run_dir: Path,
        *, max_attempts: Optional[int] = None,
        replan: bool = False) -> str:
    """Run the user workflow; on halt, if `on_halt: replan` is declared
    in the spec, automatically perform `_perform_replan` and re-execute
    up to `max_replans` times. Without `on_halt: replan`, behaves
    identically to a direct `run_workflow` call (Phase A behavior
    preserved).

    Phase B opt-in. Bounded by spec `max_replans` (clamped to
    [0, _MAX_REPLANS_HARD_CEILING]). When the cap is reached the run
    halts and the operator can still type `camflow replan` manually.
    """
    on_halt = user_spec.get("on_halt") or "manual"
    declared_max = user_spec.get("max_replans")
    if isinstance(declared_max, int) and not isinstance(declared_max, bool):
        max_replans = max(0, min(declared_max, _MAX_REPLANS_HARD_CEILING))
    else:
        max_replans = 1 if on_halt == "replan" else 0

    # First execution.
    result = run_workflow(user_spec, run_dir,
                          max_attempts=max_attempts, replan=replan)

    if on_halt != "replan":
        return result

    replan_count = 0
    while result == "halted" and replan_count < max_replans:
        # Confirm halt artifact actually exists (vs. e.g. breakpoint —
        # we only auto-replan on real halts, never on --steps debug stops).
        halt_path = run_dir / "halt.json"
        if not halt_path.exists():
            break
        try:
            halt_info = json.loads(halt_path.read_text())
        except json.JSONDecodeError:
            break
        if halt_info.get("kind") != "halt":
            # breakpoint or unexpected kind — don't auto-replan.
            break

        replan_count += 1
        print(f"on_halt=replan: auto-replanning (attempt "
              f"{replan_count}/{max_replans}) → {run_dir}",
              file=sys.stderr)
        try:
            outcome = _perform_replan(
                run_dir, reason="auto_replan_after_halt",
                replan_count=replan_count)
        except _ReplanError as e:
            print(f"auto-replan aborted: {e}", file=sys.stderr)
            break

        new_spec = outcome["user_spec"]
        new_rev = outcome["new_revision"]
        print(f"executing replanned workflow (revision {new_rev}) → "
              f"{run_dir}", file=sys.stderr)
        result = run_workflow(new_spec, run_dir,
                              max_attempts=max_attempts, replan=True)
        # Carry forward on_halt / max_replans from the new spec — the
        # Planner may keep, raise, or drop them. Re-clamp.
        on_halt = new_spec.get("on_halt") or "manual"
        if on_halt != "replan":
            break
        nm = new_spec.get("max_replans")
        if isinstance(nm, int) and not isinstance(nm, bool):
            max_replans = max(replan_count,
                              min(nm, _MAX_REPLANS_HARD_CEILING))
        # else leave max_replans as-is.

    return result


# ═══════════════════════════════════════════════════════════════════════
#  REPLAN — manual halt-time Planner re-entry (Phase A)
# ═══════════════════════════════════════════════════════════════════════
#
# Phase A: prompt-based Planner re-entry on a halted run dir. The
# operator types `camflow replan <run_dir>`; this function:
#   1. Reads the halt artifacts + prior compiled workflow.yaml + the
#      original user prompt.
#   2. Builds a "Replan Context" block summarising what halted and why.
#   3. Re-invokes the existing builtin Planner workflow with the
#      original prompt + that context.
#   4. Records the new compiled workflow.yaml as
#      dag_revisions/<N+1>/ with parent_revision=N and
#      reason="manual_replan_after_halt".
#   5. Archives the prior nodes/ + halt.json into
#      dag_revisions/<N>/ for replay.
#   6. Re-executes the new DAG (conservative: re-run all nodes; per
#      supplement §4.4, until invalidation rules exist).
#
# This is **logical Planner re-entry**, NOT full Planner residency:
# the Planner workflow is invoked again (a second time, with extended
# context) — there's no resident Planner process. Auto-replan triggered
# from runtime on halt is deferred to Phase B.

_REPLAN_CONTEXT_HEADER = "# Replan Context"
_REPLAN_RECENT_TRACE_LIMIT = 30


def _build_replan_context(prior_workflow_yaml: str,
                          halt_info: dict,
                          recent_events: list[dict],
                          prior_revision: int) -> str:
    """Build the replan-context prompt block appended to the user's
    original prompt before re-invoking the Planner workflow."""
    halted_node = halt_info.get("halted_node", "?")
    kind = halt_info.get("kind", "halt")
    reason = halt_info.get("reason", "")
    env = halt_info.get("envelope") or {}
    feedback = env.get("feedback") or (env.get("error") or {}).get("message") or ""
    err_code = (env.get("error") or {}).get("code") or ""

    lines = [_REPLAN_CONTEXT_HEADER]
    lines.append(
        f"The prior CamFlow run halted at dag_revision {prior_revision}. "
        f"This is a replan request: re-design the workflow to address "
        f"the halt and complete the original objective. Same overall "
        f"goal as the user's original prompt above; do NOT silently "
        f"drop requirements."
    )
    lines.append("")
    lines.append("## What halted")
    lines.append(f"- node: `{halted_node}`")
    lines.append(f"- kind: `{kind}`")
    if reason:
        lines.append(f"- reason: {reason}")
    if err_code:
        lines.append(f"- error code: {err_code}")
    if feedback:
        # Truncate long feedback to keep the planner prompt bounded.
        snippet = feedback if len(feedback) <= 800 else feedback[:797] + "..."
        lines.append("- feedback / error message:")
        lines.append("  ```")
        for ln in snippet.splitlines():
            lines.append(f"  {ln}")
        lines.append("  ```")
    lines.append("")
    lines.append(f"## Prior compiled workflow.yaml (revision {prior_revision})")
    lines.append("```yaml")
    # Keep prior YAML bounded too — it can be large for big DAGs.
    yaml_snippet = (prior_workflow_yaml if len(prior_workflow_yaml) <= 4000
                    else prior_workflow_yaml[:3997] + "...")
    lines.append(yaml_snippet)
    lines.append("```")
    if recent_events:
        lines.append("")
        lines.append(
            f"## Recent trace events (last {len(recent_events)})")
        lines.append("```")
        for e in recent_events:
            lines.append(json.dumps(e, ensure_ascii=False))
        lines.append("```")
    lines.append("")
    lines.append("## Retry and explicit halt semantics")
    lines.append(
        "- `retry: N` means N additional attempts after the first attempt; "
        "`retry: 1` means one retry, not zero retries."
    )
    lines.append(
        "- Explicit halt/replan envelopes (`data.halt=true`, "
        "`data.replan_required=true`, or `error.code=ORACLE_HALT`) bypass "
        "node retry and become a workflow-level halt so manual or automatic "
        "replan can run."
    )
    lines.append(
        "- Do not set `retry: 0` merely to propagate an explicit halt. Keep "
        "bounded retry for recoverable non-halt feedback, such as an oracle "
        "returning a `phrase_hint` after a correct path submit."
    )
    lines.append(
        "- On a retry, the previous envelope is available as "
        "`input.previous`; tools and skill agents can use fields like "
        "`input.previous.data.phrase_hint`."
    )
    lines.append("")
    lines.append(
        "Decide whether the halt was a local issue (fix the failing "
        "node's plan) or a structural one (the DAG itself was wrong — "
        "different decomposition / different verify gates / different "
        "skills needed). Emit a new compiled workflow.yaml accordingly. "
        "If the prior plan was nearly right, copy as much of it as "
        "remains valid; don't redesign for the sake of it."
    )
    return "\n".join(lines)


def _archive_prior_revision_artifacts(run_dir: Path,
                                      prior_revision: int) -> None:
    """Move the about-to-be-stale artifacts (nodes/, halt.json) into
    the prior revision's dag_revisions slot so replay tools can
    reconstruct what each revision actually executed."""
    rev_dir = (run_dir / "dag_revisions"
               / f"{prior_revision:04d}")
    rev_dir.mkdir(parents=True, exist_ok=True)
    nodes_src = run_dir / "nodes"
    if nodes_src.is_dir():
        nodes_dest = rev_dir / "nodes"
        if nodes_dest.exists():
            # Already archived once — leave the previous archive intact;
            # rename with a numeric suffix so we don't clobber.
            n = 2
            while (rev_dir / f"nodes-{n}").exists():
                n += 1
            nodes_dest = rev_dir / f"nodes-{n}"
        nodes_src.rename(nodes_dest)
    halt_src = run_dir / "halt.json"
    if halt_src.exists():
        (rev_dir / "halt.json").write_text(halt_src.read_text())
        halt_src.unlink()


class _ReplanError(Exception):
    """Recoverable failure during a replan attempt — caller decides
    whether to retry, report, or abort."""


def _perform_replan(run_dir: Path, *, reason: str,
                    replan_count: Optional[int] = None) -> dict:
    """Shared core: re-invoke Planner with halt context, record the new
    DAG revision, archive prior runtime artifacts, write the new active
    workflow.yaml. Caller is responsible for actually executing the new
    spec via run_workflow(replan=True).

    Used by both `_cmd_replan` (manual CLI) and the auto-replan loop
    (Phase B opt-in via `on_halt: replan`).

    Returns: dict with `user_spec` (parsed compiled workflow) and
    `new_revision` (int). Raises `_ReplanError` on any irrecoverable
    failure (missing artifacts, planner halted, invalid YAML).
    """
    halt_path = run_dir / "halt.json"
    if not halt_path.exists():
        raise _ReplanError(
            f"replan needs a halted run (no halt.json at {halt_path})")
    prompt_path = run_dir / "prompt.txt"
    if not prompt_path.exists():
        raise _ReplanError(
            f"original user prompt not found at {prompt_path}")
    workflow_path = run_dir / "workflow.yaml"
    if not workflow_path.exists():
        raise _ReplanError(
            f"prior workflow.yaml missing at {workflow_path}")

    try:
        halt_info = json.loads(halt_path.read_text())
    except json.JSONDecodeError as e:
        raise _ReplanError(f"halt.json is not valid JSON: {e}")
    original_prompt = prompt_path.read_text()
    prior_yaml_text = workflow_path.read_text()

    # Determine prior revision number from disk.
    rev_dir = run_dir / "dag_revisions"
    prior_revision = 1
    if rev_dir.is_dir():
        nums = [int(sub.name) for sub in rev_dir.iterdir()
                if sub.is_dir() and sub.name.isdigit()]
        if nums:
            prior_revision = max(nums)
    new_revision = prior_revision + 1

    events = _read_trace_events(run_dir / "trace.jsonl")
    recent = events[-_REPLAN_RECENT_TRACE_LIMIT:]
    replan_block = _build_replan_context(
        prior_yaml_text, halt_info, recent, prior_revision)
    extended_prompt = f"{original_prompt.strip()}\n\n{replan_block}\n"

    # ── Phase 1: re-invoke Planner ──────────────────────────────────
    planner_dir = _builtin_planner_dir()
    planner_yaml = planner_dir / "workflow.yaml"
    if not planner_yaml.exists():
        raise _ReplanError(f"builtin Planner missing at {planner_yaml}")
    planner_spec = yaml.safe_load(planner_yaml.read_text())
    base_ctx = planner_spec.get("context") or ""
    planner_spec["context"] = (
        f"# Original user prompt + replan context\n"
        f"{extended_prompt.strip()}\n\n---\n\n{base_ctx}"
    )

    errors = validate_workflow(planner_spec, project_root=planner_dir)
    if errors:
        raise _ReplanError(
            "Planner spec validation failed: " + "; ".join(errors))

    planner_run_dir = run_dir / f"planner-rev{new_revision}"
    print(f"replanning via Planner → {planner_run_dir}", file=sys.stderr)
    planner_wf = Workflow(planner_spec, planner_run_dir,
                          project_root=planner_dir)
    extra_trace = {"role": "planner-replan",
                   "parent_revision": prior_revision,
                   "new_revision": new_revision}
    if replan_count is not None:
        extra_trace["replan_count"] = replan_count
    planner_wf.trace("workflow_started", run_id=planner_wf.run_id,
                     **extra_trace)
    try:
        planner_result = planner_wf.execute_dag()
    finally:
        planner_wf.cleanup()

    if planner_result != "done":
        raise _ReplanError(
            f"Planner halted during replan ({planner_result}); "
            f"see {planner_run_dir}/halt.json")

    render_node = planner_wf.nodes_by_id.get("render_yaml")
    if render_node is None or not render_node.output:
        raise _ReplanError(
            "Planner finished but produced no render_yaml output")
    yaml_text = (render_node.output.get("data") or {}).get("yaml_text")
    if not yaml_text:
        raise _ReplanError(
            "Planner's render_yaml output is missing yaml_text")

    # Resolve project root for skill/tool path validation.
    parts = run_dir.resolve().parts
    if ".camflow" in parts:
        idx = parts.index(".camflow")
        project_root = Path(*parts[:idx]) if idx > 0 else Path("/")
    else:
        project_root = Path.cwd().resolve()

    try:
        user_spec = parse_workflow_yaml(yaml_text,
                                        project_root=project_root)
    except WorkflowParseError as e:
        raise _ReplanError(f"Planner produced invalid YAML on replan: {e}")

    # ── Phase 2: archive prior revision's runtime artifacts ─────────
    _archive_prior_revision_artifacts(run_dir, prior_revision)

    # ── Phase 3: write the new active workflow.yaml + record rev ────
    workflow_path.write_text(yaml.safe_dump(user_spec, sort_keys=False))
    new_rev_dir = run_dir / "dag_revisions" / f"{new_revision:04d}"
    new_rev_dir.mkdir(parents=True, exist_ok=True)
    (new_rev_dir / "workflow.yaml").write_text(workflow_path.read_text())
    manifest: dict = {
        "revision": new_revision,
        "parent_revision": prior_revision,
        "reason": reason,
        "workflow_goal": user_spec.get("goal"),
        "halted_node": halt_info.get("halted_node"),
        "halt_kind": halt_info.get("kind"),
    }
    if replan_count is not None:
        manifest["replan_count"] = replan_count
    (new_rev_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )
    return {"user_spec": user_spec, "new_revision": new_revision}


def _cmd_replan(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="camflow replan",
        description=(
            "Re-invoke the Planner on a halted run with halt context, "
            "record a new DAG revision, and execute it."))
    p.add_argument("run_dir",
                   help="path to the halted run dir (.camflow/run/)")
    p.add_argument("--steps", type=int, default=None,
                   help="(debug) halt cleanly after N node-attempts")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(f"ERROR: run dir not found: {run_dir}", file=sys.stderr)
        return 1
    try:
        outcome = _perform_replan(
            run_dir, reason="manual_replan_after_halt")
    except _ReplanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"executing replanned workflow (revision "
          f"{outcome['new_revision']}) → {run_dir}", file=sys.stderr)
    result = run_workflow(outcome["user_spec"], run_dir,
                          max_attempts=args.steps, replan=True)
    print(f"result: {result}", file=sys.stderr)
    return _result_to_exit(result)


# ═══════════════════════════════════════════════════════════════════════
#  STATUS — read-only run-dir inspector (CLI convenience)
# ═══════════════════════════════════════════════════════════════════════
#
# Strictly read-only: does NOT instantiate Workflow (which writes
# workflow.yaml/pid/dag_revisions on construction). All state
# inference is artifact-driven from disk.

def _is_pid_alive(pid: int) -> bool:
    """POSIX liveness check via signal-0. PermissionError treated as
    alive (process exists, just isn't ours)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_trace_events(trace_path: Path) -> list[dict]:
    """Tolerant trace.jsonl reader. Bad lines are skipped silently."""
    if not trace_path.exists():
        return []
    out: list[dict] = []
    for line in trace_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_workflow_yaml(run_dir: Path) -> dict | None:
    p = run_dir / "workflow.yaml"
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return None


def _summarize_node(run_dir: Path, node_id: str,
                    events: list[dict]) -> dict:
    """Per-node summary from disk + trace. No mutation."""
    nd = run_dir / "nodes" / node_id
    attempts: list[int] = []
    if nd.is_dir():
        for sub in nd.iterdir():
            if sub.is_dir() and sub.name.startswith("attempt-"):
                try:
                    attempts.append(int(sub.name.split("-", 1)[1]))
                except ValueError:
                    pass
    attempts.sort()
    latest_attempt = attempts[-1] if attempts else None

    # Phase from latest trace event for this node.
    phase = "waiting"
    last_status: str | None = None
    for e in events:
        if e.get("node") != node_id:
            continue
        ev = e.get("event")
        if ev == "node_started":
            phase = "running"
        elif ev == "verify_started":
            phase = "verifying"
        elif ev == "verify_failed":
            phase = "running"  # will retry or halt
        elif ev == "verify_completed":
            phase = "running"
        elif ev == "node_completed":
            phase = "done"
            last_status = e.get("status")
        elif ev == "node_failed":
            phase = "done"
            last_status = "fail"
        elif ev == "retry_triggered":
            phase = "retrying"

    info: dict = {
        "id": node_id,
        "phase": phase,
        "latest_attempt": latest_attempt,
        "attempt_count": len(attempts),
        "status": last_status,
    }
    if latest_attempt is not None:
        att_dir = nd / f"attempt-{latest_attempt}"
        info["attempt_dir"] = str(att_dir)
        for fname in ("prompt.txt", "agent_output.json", "output.json"):
            fp = att_dir / fname
            if fp.exists():
                info[fname] = str(fp)
    return info


def _summarize_status(run_dir: Path, *,
                      focus_node: str | None = None,
                      events_limit: int | None = None) -> dict:
    """Build a structured status summary from a run dir's artifacts.

    Pure: never writes, never instantiates Workflow.
    """
    run_dir = Path(run_dir)
    summary: dict = {
        "run_dir": str(run_dir),
        "exists": run_dir.is_dir(),
    }
    if not run_dir.is_dir():
        summary["state"] = "missing"
        return summary

    # PID + liveness.
    pid_path = run_dir / "runner.pid"
    pid: int | None = None
    pid_alive: bool | None = None
    if pid_path.exists():
        try:
            pid = int((pid_path.read_text() or "").strip() or "0") or None
        except ValueError:
            pid = None
        if pid is not None:
            pid_alive = _is_pid_alive(pid)
    summary["pid"] = pid
    summary["pid_alive"] = pid_alive

    # Workflow spec (if Planner has produced one).
    wf = _read_workflow_yaml(run_dir)
    summary["workflow_name"] = (wf.get("workflow") if isinstance(wf, dict)
                                else None)
    summary["workflow_goal"] = (wf.get("goal") if isinstance(wf, dict)
                                else None)
    # Auto-replan policy (Phase B opt-in). on_halt defaults to "manual".
    summary["on_halt"] = (
        (wf.get("on_halt") if isinstance(wf, dict) else None) or "manual")
    declared_max = (wf.get("max_replans") if isinstance(wf, dict)
                    else None)
    if isinstance(declared_max, int) and not isinstance(declared_max, bool):
        summary["max_replans"] = max(0, min(declared_max,
                                            _MAX_REPLANS_HARD_CEILING))
    else:
        summary["max_replans"] = (1 if summary["on_halt"] == "replan"
                                  else 0)
    node_ids: list[str] = []
    if isinstance(wf, dict):
        for n in (wf.get("nodes") or []):
            if isinstance(n, dict) and isinstance(n.get("id"), str):
                node_ids.append(n["id"])
    summary["node_ids"] = node_ids

    # Trace events.
    events = _read_trace_events(run_dir / "trace.jsonl")
    summary["trace_events_total"] = len(events)
    summary["last_event"] = events[-1] if events else None

    # Halt artifact.
    halt_path = run_dir / "halt.json"
    halt_info: dict | None = None
    if halt_path.exists():
        try:
            halt_info = json.loads(halt_path.read_text())
        except json.JSONDecodeError:
            halt_info = None
    summary["halt"] = halt_info

    # State inference (artifact-driven).
    last_event = events[-1] if events else None
    if halt_info is not None:
        state = "halted"
    elif last_event and last_event.get("event") == "workflow_completed":
        state = last_event.get("status") or "done"
    elif pid is not None and pid_alive:
        state = "running"
    elif pid is not None and pid_alive is False:
        state = "stale"
    else:
        state = "unknown"
    summary["state"] = state

    # DAG revisions.
    rev_dir = run_dir / "dag_revisions"
    revisions: list[dict] = []
    if rev_dir.is_dir():
        for sub in sorted(rev_dir.iterdir(),
                          key=lambda p: p.name):
            if not sub.is_dir():
                continue
            mp = sub / "manifest.json"
            man: dict = {"name": sub.name}
            if mp.exists():
                try:
                    man.update(json.loads(mp.read_text()))
                except json.JSONDecodeError:
                    pass
            revisions.append(man)
    summary["dag_revisions"] = revisions
    # replan_count = number of revisions ≥ 2 (rev 1 is the initial plan,
    # not a replan). Sourced from disk so it's accurate even if no
    # trace events are tagged yet for the current revision.
    summary["replan_count"] = sum(
        1 for r in revisions
        if isinstance(r.get("revision"), int) and r["revision"] >= 2)
    # Active revision = most recent (by sorted dir name); also pulled
    # from the last user-tagged trace event when available.
    active_rev: int | None = None
    for e in reversed(events):
        if "dag_revision" in e:
            try:
                active_rev = int(e["dag_revision"])
                break
            except (TypeError, ValueError):
                pass
    if active_rev is None and revisions:
        try:
            active_rev = int(revisions[-1].get("revision")
                             or revisions[-1].get("name"))
        except (TypeError, ValueError):
            pass
    summary["active_dag_revision"] = active_rev

    # Per-node summaries.
    # If workflow.yaml is missing but nodes/ has subdirs, fall back to
    # the on-disk node ids so a partial run still reports something.
    nodes_dir = run_dir / "nodes"
    if not node_ids and nodes_dir.is_dir():
        node_ids = sorted(
            [d.name for d in nodes_dir.iterdir() if d.is_dir()]
        )
        summary["node_ids"] = node_ids

    nodes_info: list[dict] = []
    for nid in node_ids:
        if focus_node and nid != focus_node:
            continue
        nodes_info.append(_summarize_node(run_dir, nid, events))
    summary["nodes"] = nodes_info

    # Progress: done/total.
    done_count = sum(1 for n in nodes_info if n.get("phase") == "done")
    summary["progress"] = {
        "done": done_count,
        "total": len(nodes_info) if focus_node is None else len(node_ids),
    }

    # Current node:
    #   - if halted, the halted node (so "what's the workflow doing?"
    #     answers the user's intuition);
    #   - else the first node in DAG order that isn't done yet;
    #   - else the most recently started node from the trace.
    current: dict | None = None
    if not focus_node:
        if halt_info is not None:
            halted_id = halt_info.get("halted_node")
            for n in nodes_info:
                if n.get("id") == halted_id:
                    current = n
                    break
        if current is None:
            for n in nodes_info:
                if n.get("phase") != "done":
                    current = n
                    break
        if current is None:
            # Fall back to last node mentioned in the trace.
            for e in reversed(events):
                if e.get("event") == "node_started":
                    nid = e.get("node")
                    for n in nodes_info:
                        if n.get("id") == nid:
                            current = n
                            break
                    if current:
                        break
    summary["current_node"] = current

    # Optional: trim trace events for the caller.
    if events_limit is not None and events_limit > 0:
        summary["recent_events"] = events[-events_limit:]
    elif events_limit == 0:
        summary["recent_events"] = []

    return summary


def _render_status_human(summary: dict, *,
                         show_output: bool = False) -> str:
    """One-screen human-readable formatter."""
    if not summary.get("exists"):
        return f"camflow status: no run dir at {summary['run_dir']}\n"

    lines: list[str] = []
    rd = summary["run_dir"]
    state = summary["state"]
    pid = summary.get("pid")
    pid_alive = summary.get("pid_alive")
    if pid is not None:
        liveness = "alive" if pid_alive else ("dead" if pid_alive is False
                                              else "?")
        pid_label = f"  pid: {pid} ({liveness})"
    else:
        pid_label = ""
    lines.append(f"run:    {rd}")
    lines.append(f"state:  {state}{pid_label}")

    if summary.get("workflow_name"):
        lines.append(f"workflow: {summary['workflow_name']}")
    if summary.get("workflow_goal"):
        goal = summary["workflow_goal"].strip()
        if len(goal) > 200:
            goal = goal[:197] + "..."
        lines.append(f"goal:   {goal}")
    if summary.get("active_dag_revision"):
        lines.append(f"dag rev: {summary['active_dag_revision']}")
    # Auto-replan policy line — only shown when the workflow actually
    # opts in. Default ("manual") is the silent baseline.
    if summary.get("on_halt") == "replan":
        used = summary.get("replan_count", 0)
        cap = summary.get("max_replans", 0)
        lines.append(
            f"on_halt: replan (auto-replan used {used}/{cap})"
        )

    prog = summary.get("progress") or {}
    if prog.get("total"):
        lines.append(
            f"nodes:  {prog['done']}/{prog['total']} done"
        )

    cur = summary.get("current_node")
    if cur:
        latest = cur.get("latest_attempt")
        att = f" attempt-{latest}" if latest else ""
        lines.append(
            f"current: {cur['id']} ({cur['phase']}{att})"
        )

    last = summary.get("last_event") or {}
    if last:
        ev = last.get("event", "?")
        ts = last.get("ts", "")
        node = last.get("node", "")
        node_part = f" {node}" if node else ""
        lines.append(f"latest: {ev}{node_part}  {ts}")

    nodes = summary.get("nodes") or []
    if nodes:
        lines.append("")
        lines.append("  node                        phase       attempt  status")
        for n in nodes:
            att = (str(n.get("latest_attempt"))
                   if n.get("latest_attempt") is not None else "—")
            st = n.get("status") or "—"
            lines.append(
                f"  {n['id']:<28}{n['phase']:<11} {att:<8}{st}"
            )

    halt = summary.get("halt")
    if halt:
        lines.append("")
        lines.append("HALT:")
        lines.append(
            f"  node:   {halt.get('halted_node')} (kind={halt.get('kind')})")
        if halt.get("reason"):
            lines.append(f"  reason: {halt['reason']}")
        env = halt.get("envelope") or {}
        fb = env.get("feedback") or (env.get("error") or {}).get("message")
        if fb:
            fb_short = fb if len(fb) <= 200 else fb[:197] + "..."
            lines.append(f"  feedback: {fb_short}")
        lines.append(f"  resume:  camflow resume {rd}")

    # Artifact paths.
    trace_path = Path(rd) / "trace.jsonl"
    if trace_path.exists():
        lines.append("")
        lines.append(f"trace:  {trace_path}")
    if cur:
        for k in ("prompt.txt", "output.json"):
            v = cur.get(k)
            if v:
                lines.append(f"{k:<7} {v}")

    # Recent events (when --events N).
    recent = summary.get("recent_events")
    if recent:
        lines.append("")
        lines.append(f"recent events (last {len(recent)}):")
        for e in recent:
            lines.append(
                f"  step {e.get('step', '?')}: "
                f"{e.get('event')}  {e.get('node', '')}  "
                f"{e.get('ts', '')}".rstrip()
            )

    # Optionally dump the focused node's latest output.json.
    if show_output and nodes:
        n = nodes[0]
        op = n.get("output.json")
        if op and Path(op).exists():
            lines.append("")
            lines.append(f"output ({op}):")
            try:
                lines.append(Path(op).read_text())
            except OSError as e:
                lines.append(f"  (could not read: {e})")

    return "\n".join(lines) + "\n"


def _resolve_status_run_dir(args_run_dir: str | None,
                            planner: bool) -> Path:
    """If --run-dir given, use it; else <project>/.camflow/run/.
    --planner appends /planner for inspecting the Planner sub-run."""
    if args_run_dir:
        rd = Path(args_run_dir).resolve()
    else:
        rd = Path.cwd().resolve() / ".camflow" / "run"
    if planner:
        rd = rd / "planner"
    return rd


def _cmd_status(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="camflow status",
        description="Read-only inspection of a camflow run.")
    p.add_argument("--run-dir", default=None,
                   help="path to .camflow/run/ (default: ./.camflow/run/)")
    p.add_argument("--planner", action="store_true",
                   help="inspect the Planner sub-run instead of the user run")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of human view")
    p.add_argument("--events", type=int, default=None, metavar="N",
                   help="include the last N trace events")
    p.add_argument("--node", default=None, metavar="ID",
                   help="focus output on one node id")
    p.add_argument("--output", action="store_true",
                   help="also dump the focused node's latest output.json")
    args = p.parse_args(argv)

    rd = _resolve_status_run_dir(args.run_dir, args.planner)
    summary = _summarize_status(
        rd, focus_node=args.node, events_limit=args.events)

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print(_render_status_human(summary, show_output=args.output), end="")
    # Exit code: 0 if reachable; 1 if run dir missing.
    return 0 if summary.get("exists") else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "camflow — prompt-driven, multi-agent workflow runner\n"
            "\n"
            "Usage:\n"
            "  camflow run \"<prompt>\"                compile + run (fire-and-forget)\n"
            "  camflow run -i \"<prompt>\"             compile + plan-approval gate, then run\n"
            "  camflow run --steps N \"<prompt>\"      (debug) halt after N node-attempts\n"
            "  camflow run --from <node_id>          re-execute a node + downstream\n"
            "                                          (operates on ./.camflow/run/ by default;\n"
            "                                           use --run-dir to point elsewhere)\n"
            "  camflow resume <run_dir>              resume a halted run\n"
            "  camflow resume <run_dir> --steps N    resume but advance only N more attempts\n"
            "  camflow replan <run_dir>              re-invoke Planner on a halted run, record a new\n"
            "                                          DAG revision, and execute it (manual halt-time\n"
            "                                          replan; auto-replan is opt-in via workflow's\n"
            "                                          `on_halt: replan` + `max_replans: N`)\n"
            "  camflow status                        read-only summary of ./.camflow/run/\n"
            "  camflow status --json                 machine-readable summary\n"
            "  camflow status --events N             include last N trace events\n"
            "  camflow status --node <id>            focus on one node\n"
            "  camflow status --planner              inspect the Planner sub-run\n"
            "\n"
            "Inspect a run:  cat .camflow/run/trace.jsonl  (or `camflow status`)\n"
            "Stop a run:     kill $(cat .camflow/run/runner.pid)\n",
            file=sys.stderr,
        )
        return 0 if argv else 1
    cmd = argv[0]
    if cmd == "run":
        return _cmd_run(argv[1:])
    if cmd == "resume":
        return _cmd_resume(argv[1:])
    if cmd == "replan":
        return _cmd_replan(argv[1:])
    if cmd == "status":
        return _cmd_status(argv[1:])
    print(f"ERROR: unknown subcommand '{cmd}'. "
          f"Try `camflow --help`.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
