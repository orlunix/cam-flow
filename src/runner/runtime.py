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

    ids = [n.get("id") for n in nodes]
    for i in ids:
        if not isinstance(i, str) or not i:
            errors.append("a node is missing or has non-string 'id'")
            break
    if len(ids) != len(set(ids)):
        errors.append(f"duplicate node ids: {ids}")
    id_set = {i for i in ids if isinstance(i, str) and i}

    for n in nodes:
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
                if "human" in verify and not isinstance(verify["human"], str):
                    errors.append(
                        f"{nid}.verify.human: must be a string (the prompt to show the user)"
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
                if ft not in VALID_TYPES:
                    errors.append(
                        f"{nid}.output_schema.{fk}: unknown type {ft!r}; "
                        f"allowed: {sorted(VALID_TYPES)}"
                    )
        # skill / tool existence (only with project_root)
        if project_root is not None:
            if has_skill:
                if not _resolve_skill_path(run["skill"], project_root):
                    errors.append(
                        f"{nid}.run.skill: '{run['skill']}' not found "
                        f"(no skills/{run['skill']}/SKILL.md in project or repo)"
                    )
            if has_tool:
                if not _resolve_tool_path(run["tool"], project_root):
                    errors.append(
                        f"{nid}.run.tool: '{run['tool']}' not found or not "
                        f"executable (relative to {project_root})"
                    )

    # cycle detection on `needs` graph
    needs_map = {n["id"]: list(n.get("needs", []) or [])
                 for n in nodes if "id" in n}
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
                     workflow_context: str | None = None) -> str:
    """Compose the prompt for a run agent (skill mode).

    Layout:
      [skill template]
      [Workflow Context]      ← shared across every node, optional
      [Goal]
      [Steps]
      [Upstream Outputs]      ← auto-injected from `needs`, optional
      [Note: previous]         ← only on retry
      [Output schema + delivery protocol]
    """
    parts = []
    if skill_md:
        parts.append(skill_md.strip())
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
            "Read `feedback` (or `error.message`) to know what went wrong; "
            "address it in this attempt."
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
                        workflow_context: str | None = None) -> str:
    """Compose the prompt for the verify-agent (default verify path).

    Verify-agent's data shape is fixed: {approved, step_results, reasoning}.
    """
    criterion = (node.verify_config or {}).get("criterion") or ""
    parts = [
        f"You are evaluating whether the previous node `{node.id}` did its job.",
    ]
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

    try:
        proc = subprocess.run(
            [str(tool_path)],
            input=json.dumps(input_dict),
            capture_output=True, text=True,
            cwd=str(workspace),
            env={**os.environ, "CAMFLOW_WORKSPACE": str(workspace)},
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
               workflow_context: str | None = None) -> dict:
    """Spawn a camc agent loaded with the skill template + run prompt."""
    prompt = build_run_prompt(node, input_dict, skill_md=skill_md,
                              workflow_context=workflow_context)
    (workspace / "prompt.txt").write_text(prompt)
    agent_name = f"{node.id}-attempt-{attempt_n}"
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
    )
    (sub_dir / "prompt.txt").write_text(prompt)
    try:
        _aid, raw = camc.run_and_collect(
            prompt=prompt,
            workspace=sub_dir,
            name=f"{node.id}-verify-{attempt_n}",
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
                              workflow_context=workflow.spec.get("context"))
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

        self.trace_path = run_dir / "trace.jsonl"
        self.pid_path = run_dir / "runner.pid"

        if not resume:
            (run_dir / "workflow.yaml").write_text(
                yaml.safe_dump(spec, sort_keys=False)
            )
        else:
            if self.trace_path.exists():
                self.step_n = sum(1 for _ in self.trace_path.open())

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

    # ─── Persistence + tracing ────────────────────────────────────────

    def trace(self, event: str, **fields):
        self.step_n += 1
        rec = {"step": self.step_n, "ts": utcnow_iso(),
               "event": event, **fields}
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
                 max_attempts: Optional[int] = None) -> str:
    """Execute a workflow → return final lifecycle state ('done' or 'halted').

    `resume_with_run` is for the resume command — caller pre-builds a
    Workflow with prior attempts replayed.

    `max_attempts` (debug): halt cleanly after that many node-attempts
    via a breakpoint-kind halt. Resume continues from there.
    """
    wf = resume_with_run if resume_with_run is not None else \
        Workflow(workflow, run_dir)
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
    result = run_workflow(user_spec, run_dir, max_attempts=args.steps)
    print(f"result: {result}", file=sys.stderr)
    return _result_to_exit(result)


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
            "\n"
            "Inspect a run:  cat .camflow/run/trace.jsonl\n"
            "Stop a run:     kill $(cat .camflow/run/runner.pid)\n",
            file=sys.stderr,
        )
        return 2 if argv else 1
    cmd = argv[0]
    if cmd == "run":
        return _cmd_run(argv[1:])
    if cmd == "resume":
        return _cmd_resume(argv[1:])
    print(f"ERROR: unknown subcommand '{cmd}'. "
          f"Try `camflow --help`.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
