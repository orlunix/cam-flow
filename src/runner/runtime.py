"""Minimal Agent Workflow Runner — implements docs/spec.md v0.6.

One file, no LLM. Skill / agent execution is stubbed; tool nodes shell out.
Node execution modes supported in v1:

  uses: tool.<name>   — run ./tools/<name>.sh, stdin=input.json, stdout=envelope JSON
  mock: { ... }       — node returns the literal envelope (testing)

skill.* / agent.* raise NOT_IMPLEMENTED in v0.6 (LLM hookup is v0.7).

CLI:
    python runner.py <workflow.yaml> [--state state.json] [--run-dir DIR]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ─── Expression evaluator (spec App. A) ────────────────────────────────

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
        # YAML-style boolean / null literals
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


# ─── Template renderer (spec App. B) ───────────────────────────────────

_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _render_str(s: str, ctx: dict) -> str:
    def repl(m):
        expr = m.group(1).strip()
        optional = expr.endswith("?")
        if optional:
            expr = expr[:-1].rstrip()
        try:
            v = eval_expr(expr, ctx)
        except ExprError:
            if optional:
                return ""
            raise
        if v is None:
            return "" if optional else "null"
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


# ─── Workflow loading & validation ────────────────────────────────────

def load_workflow(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def validate_workflow(wf: dict) -> list[str]:
    errors = []
    if not isinstance(wf, dict):
        return ["workflow is not a dict"]
    nodes = wf.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["workflow.nodes is missing or empty"]

    ids = [n.get("id") for n in nodes]
    if any(not i for i in ids):
        errors.append("a node is missing 'id'")
    if len(ids) != len(set(ids)):
        errors.append("duplicate node ids")
    id_set = set(ids)

    for n in nodes:
        nid = n.get("id", "<?>")
        if "uses" not in n and "mock" not in n:
            errors.append(f"{nid}: must have 'uses' or 'mock'")
        for dep in n.get("needs", []) or []:
            if dep not in id_set:
                errors.append(f"{nid}.needs: unknown node '{dep}'")
        retry = n.get("retry")
        if retry:
            if "until" not in retry or "max_attempts" not in retry:
                errors.append(f"{nid}.retry: requires until / max_attempts")
            if "target" in retry:
                errors.append(
                    f"{nid}.retry.target: unsupported in v0.6 — retry only "
                    f"re-runs the current node; remove the target field"
                )

    # cycle detection on `needs` graph
    needs_map = {n["id"]: list(n.get("needs", []) or []) for n in nodes if "id" in n}
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


class WorkflowParseError(Exception):
    """Raised when text → workflow dict conversion or validation fails."""


_FENCE_RE = re.compile(r"^```(?:yaml|yml)?\s*\n?|\n?```\s*$",
                       re.IGNORECASE | re.MULTILINE)


def parse_workflow_yaml(text: str) -> dict:
    """Parse a YAML string into a workflow dict and validate it.

    Strips optional ```yaml fences. Raises WorkflowParseError on:
      - empty / whitespace-only input
      - invalid YAML
      - non-dict top level
      - any validate_workflow error.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:yaml|yml)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    cleaned = cleaned.strip()
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
    errors = validate_workflow(wf)
    if errors:
        raise WorkflowParseError(
            "validation failed:\n  " + "\n  ".join(errors)
        )
    return wf


# ─── Run state ──────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gen_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _empty_envelope(status: str, error: dict | None = None) -> dict:
    return {
        "status": status,
        "data": {},
        "error": error,
        "metrics": {},
        "artifacts": [],
    }


class Run:
    def __init__(self, workflow: dict, state: dict, run_dir: Path,
                 resume: bool = False):
        self.workflow = workflow
        self.nodes = workflow["nodes"]
        self.nodes_by_id = {n["id"]: n for n in self.nodes}
        self.state = state
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.attempts: dict[str, list[dict]] = {n["id"]: [] for n in self.nodes}
        self.retry_pending: dict[str, dict | None] = {}  # node_id -> retry_ctx (or None)
        self.step = 0
        self.trace_path = self.run_dir / "trace.jsonl"
        self.pid_path = self.run_dir / "runner.pid"

        if not resume:
            (self.run_dir / "workflow.yaml").write_text(
                yaml.safe_dump(workflow, sort_keys=False)
            )
            (self.run_dir / "state.json").write_text(json.dumps(state, indent=2))
        else:
            # Continue step numbering from existing trace so global ordering
            # holds across resume sessions.
            if self.trace_path.exists():
                self.step = sum(1 for _ in self.trace_path.open())

        # project_root: the directory that *contains* the .camflow/ holding
        # this run. Works for both the conventional layout
        # (<proj>/.camflow/runs/<id>) and the nested plan-command layout
        # (<proj>/.camflow/runs/<id>/{planner,main}). Falls back to cwd
        # if run_dir is custom and not under any .camflow/ tree.
        parts = self.run_dir.resolve().parts
        if ".camflow" in parts:
            idx = parts.index(".camflow")
            self.project_root = Path(*parts[:idx]) if idx > 0 else Path("/")
        else:
            self.project_root = Path.cwd().resolve()

        # Write our PID so `camflow stop <run_dir>` can SIGTERM us.
        self.pid_path.write_text(str(os.getpid()))

    def cleanup(self) -> None:
        """Remove the runner.pid file at end of run."""
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

    # trace ---------------------------------------------------------------
    def trace(self, event: str, **fields):
        self.step += 1
        rec = {"step": self.step, "ts": _utcnow_iso(), "event": event, **fields}
        with self.trace_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    # context for templates / expressions ---------------------------------
    def expr_ctx(self, retry_ctx: dict | None = None,
                 current_output: dict | None = None) -> dict:
        nodes_view: dict[str, Any] = {}
        for nid, atts in self.attempts.items():
            if not atts:
                continue
            # 1-indexed attempts: pad index 0 with None per spec App. B
            attempts_indexed = [None] + [{"output": a} for a in atts]
            nodes_view[nid] = {
                "latest": {"output": atts[-1]},
                "attempts": attempts_indexed,
            }
        ctx = {"state": self.state, "nodes": nodes_view}
        if retry_ctx is not None:
            ctx["retry"] = retry_ctx
        if current_output is not None:
            ctx["output"] = current_output
        return ctx

    # ready logic ---------------------------------------------------------
    def is_done(self, nid: str) -> bool:
        a = self.attempts[nid]
        if not a:
            return False
        if nid in self.retry_pending:
            return False
        return a[-1]["status"] in ("success", "skipped", "failure")

    def ready_nodes(self) -> list[dict]:
        """A node is ready iff:
          - it has no terminal attempt yet, OR it is marked retry-pending; AND
          - none of its `needs` are themselves retry-pending; AND
          - all `needs` have a success/skipped attempt.
        """
        ready = []
        for n in self.nodes:
            nid = n["id"]
            # already terminal and not pending re-exec → skip
            if nid not in self.retry_pending and self.attempts[nid]:
                continue
            needs = n.get("needs", []) or []
            if all(d not in self.retry_pending
                   and self.attempts[d]
                   and self.attempts[d][-1]["status"] in ("success", "skipped")
                   for d in needs):
                ready.append(n)
        return ready


# ─── Node execution ────────────────────────────────────────────────────

def _build_agent_context(run: Run, node: dict, attempt_n: int,
                         inputs: dict) -> dict:
    """Prepare everything an agent needs to run: a per-attempt workspace
    directory, the rendered inputs as `input.json`, and (for skill/agent)
    the compiled prompt as `prompt.txt`.

    Layout:
      <run_dir>/nodes/<id>/attempt-<n>/
        ├── workspace/           ← the agent's view: prompt + inputs (+ files it produces)
        │   ├── input.json
        │   └── prompt.txt       (skill/agent only)
        └── output.json          ← runner-managed, written after the call returns

    Tools, skills, and (future) agents all share this context-builder so the
    runtime has one place to wire up materials + workspace.
    """
    att_dir = run.run_dir / "nodes" / node["id"] / f"attempt-{attempt_n}"
    workspace = att_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    (workspace / "input.json").write_text(
        json.dumps(inputs, indent=2, ensure_ascii=False)
    )

    ctx = {
        "att_dir": att_dir,
        "workspace": workspace,
        "inputs": inputs,
        "prompt_text": None,
    }

    uses = node.get("uses", "")
    if uses.startswith(("skill.", "agent.")):
        skill_name = uses.split(".", 1)[1] if "." in uses else None
        prompt = _build_skill_prompt(
            run.workflow, node, inputs,
            skill_name=skill_name, run=run,
        )
        (workspace / "prompt.txt").write_text(prompt)
        ctx["prompt_text"] = prompt

    return ctx


def execute_node(run: Run, node: dict, attempt_n: int,
                 retry_ctx: dict | None) -> dict:
    """Render input, build agent context, dispatch by uses/mock."""
    expr_ctx = run.expr_ctx(retry_ctx=retry_ctx)
    inputs = render_deep(node.get("input", {}) or {}, expr_ctx)

    # Mock mode bypasses workspace setup.
    if "mock" in node:
        m = node["mock"]
        return {
            "status": m.get("status", "success"),
            "data": m.get("data", {}) or {},
            "error": m.get("error"),
            "metrics": m.get("metrics", {}) or {},
            "artifacts": m.get("artifacts", []) or [],
        }

    # Real executor — build workspace + materials.
    actx = _build_agent_context(run, node, attempt_n, inputs)

    uses = node.get("uses", "")
    if uses.startswith("tool."):
        return _exec_tool(uses[len("tool."):], actx, run, node["id"], attempt_n)
    if uses.startswith("skill."):
        return _exec_skill(uses[len("skill."):], actx, run, node, attempt_n)
    if uses.startswith("agent."):
        return _empty_envelope("failure", error={
            "code": "NOT_IMPLEMENTED",
            "message": f"agent.X (autonomous via camc) is v0.8; use skill.X for v0.7",
        })
    return _empty_envelope("failure", error={
        "code": "BAD_USES",
        "message": f"unrecognized uses: {uses!r}",
    })


def _exec_tool(name: str, actx: dict, run: Run,
               node_id: str, attempt_n: int) -> dict:
    """Run tools/<name>.sh with input JSON on stdin; expect envelope JSON on stdout.

    Tool gets these env vars for context:
      CAMFLOW_RUN_DIR, CAMFLOW_NODE_ID, CAMFLOW_ATTEMPT, CAMFLOW_WORKSPACE
    The tool's cwd is the workspace dir, so files it writes land beside its inputs.
    """
    tool_path = run.project_root / "tools" / f"{name}.sh"
    if not tool_path.exists():
        return _empty_envelope("failure", error={
            "code": "TOOL_NOT_FOUND",
            "message": f"tools/{name}.sh not found",
        })
    workspace = actx["workspace"]
    env = {
        **os.environ,
        "CAMFLOW_RUN_DIR": str(run.run_dir),
        "CAMFLOW_NODE_ID": node_id,
        "CAMFLOW_ATTEMPT": str(attempt_n),
        "CAMFLOW_WORKSPACE": str(workspace),
    }
    try:
        proc = subprocess.run(
            [str(tool_path)],
            input=json.dumps(actx["inputs"]),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        return _empty_envelope("failure", error={
            "code": "TOOL_TIMEOUT", "message": f"tool {name} exceeded 300s",
        })
    # Persist raw stdout for debug, regardless of outcome.
    (workspace / "raw_stdout.txt").write_text(proc.stdout or "")
    if proc.stderr:
        (workspace / "raw_stderr.txt").write_text(proc.stderr)
    if proc.returncode != 0:
        return _empty_envelope("failure", error={
            "code": "TOOL_NONZERO",
            "message": f"tool {name} exited {proc.returncode}: {proc.stderr.strip()[:500]}",
        })
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return _empty_envelope("failure", error={
            "code": "TOOL_BAD_OUTPUT",
            "message": f"tool {name} stdout not JSON: {e}",
        })
    # normalize
    return {
        "status": out.get("status", "success"),
        "data": out.get("data", {}) or {},
        "error": out.get("error"),
        "metrics": out.get("metrics", {}) or {},
        "artifacts": out.get("artifacts", []) or [],
    }


# ─── Skill execution via Claude CLI ────────────────────────────────────

def _load_skill_template(skill_name: str, run: Run) -> str | None:
    """If `<project>/prompts/<skill_name>.md` exists, return its contents.

    The runner looks in two places:
      1. `<project>/prompts/<name>.md` (project-local, e.g. examples/foo/prompts/...)
      2. `<repo>/prompts/<name>.md` (the runner's own bundled prompts)

    First match wins. Returns None if neither exists.
    """
    candidates = [
        run.project_root / "prompts" / f"{skill_name}.md",
        Path(__file__).resolve().parents[2] / "prompts" / f"{skill_name}.md",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text()
    return None


def _build_skill_prompt(workflow: dict, node: dict, inputs: dict,
                        skill_name: str | None = None,
                        run: Run | None = None) -> str:
    """Compile a single-shot prompt per spec §11.

    Sections (in order):
      [Skill template] | Workflow goal | Node task | Inputs (JSON) | Output schema.

    The skill template (from `prompts/<skill_name>.md`) is prepended verbatim
    so it can carry rules / spec primers / reference material that don't fit
    in node.goal.
    """
    parts = []
    if skill_name and run is not None:
        if tpl := _load_skill_template(skill_name, run):
            parts.append(tpl.strip())
    if wg := (workflow.get("goal") or "").strip():
        parts.append(f"# Workflow goal\n{wg}")
    if ng := (node.get("goal") or "").strip():
        parts.append(f"# Your task\n{ng}")
    if inputs:
        parts.append(
            "# Inputs\n```json\n"
            + json.dumps(inputs, indent=2, ensure_ascii=False)
            + "\n```"
        )
    schema = node.get("output_schema") or {}
    schema_desc = (
        json.dumps(schema, indent=2)
        if schema else
        "(any object — no schema declared)"
    )
    parts.append(
        "# Output format\n"
        "Reply with ONLY a JSON object matching this envelope. "
        "No prose, no markdown fence, just JSON:\n"
        "```\n"
        "{\n"
        '  "status": "success",\n'
        f'  "data": <object matching this schema>: {schema_desc},\n'
        '  "error": null,\n'
        '  "metrics": {},\n'
        '  "artifacts": []\n'
        "}\n"
        "```"
    )
    return "\n\n".join(parts)


# Default caps for the camc-based skill executor. Overridable via env vars
# for local debugging — e.g. CAMFLOW_SKILL_TIMEOUT=120 for cheap test runs.
_SKILL_TIMEOUT_S = int(os.environ.get("CAMFLOW_SKILL_TIMEOUT", "600"))
_SKILL_POLL_INTERVAL_S = float(os.environ.get("CAMFLOW_SKILL_POLL_INTERVAL", "2"))
_SKILL_INNER_RETRIES = int(os.environ.get("CAMFLOW_SKILL_INNER_RETRIES", "3"))

_AGENT_ID_RE = re.compile(r"^Starting [a-z]+ agent ([0-9a-f]{6,})", re.MULTILINE)
_OUTPUT_FILENAME = "agent_output.json"


def _skill_kickoff_instruction() -> str:
    """Append to every skill prompt: tell the agent how to deliver output."""
    return (
        "\n\n# Delivery protocol\n"
        f"Write the final envelope JSON to `{_OUTPUT_FILENAME}` in your "
        "current working directory. Do not print it; the runner reads the file. "
        "If the runner sends follow-up feedback in this session, treat it as a "
        "schema-correction request — update the SAME file and stop. "
        "Once you've written the file, do nothing else; the runner will close "
        "the session."
    )


def _camc_run(workspace: Path, prompt: str, name: str, tag: str) -> tuple[str | None, str]:
    """Spawn a camc agent. Returns (agent_id, error_message)."""
    proc = subprocess.run(
        ["camc", "run",
         "--path", str(workspace),
         "--name", name,
         "--tag", tag,
         prompt],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return None, f"camc run exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    m = _AGENT_ID_RE.search(proc.stdout)
    if not m:
        return None, f"could not parse agent ID from camc run output:\n{proc.stdout[:500]}"
    return m.group(1), ""


def _camc_status(agent_id: str) -> dict:
    """Get current camc agent status as dict (best-effort; {} on parse fail)."""
    try:
        proc = subprocess.run(
            ["camc", "--json", "status", agent_id],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def _camc_kill(agent_id: str) -> None:
    subprocess.run(["camc", "kill", agent_id],
                   capture_output=True, text=True, timeout=10)


def _camc_send(agent_id: str, text: str) -> None:
    subprocess.run(["camc", "send", agent_id, "--text", text],
                   capture_output=True, text=True, timeout=10)


def _wait_for_output(output_path: Path, since_mtime: float | None,
                     timeout_s: int) -> tuple[bool, str]:
    """Poll until output_path is written/updated (mtime > since_mtime) and
    contains valid JSON. Returns (ok, error_msg)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if output_path.exists():
            try:
                mt = output_path.stat().st_mtime
            except OSError:
                mt = None
            if mt is not None and (since_mtime is None or mt > since_mtime):
                # File looks fresh. Sanity-check it parses.
                try:
                    json.loads(output_path.read_text())
                    return True, ""
                except json.JSONDecodeError:
                    pass  # still being written; keep polling
        time.sleep(_SKILL_POLL_INTERVAL_S)
    return False, f"timed out waiting for {output_path.name} after {timeout_s}s"


def _exec_skill(name: str, actx: dict, run: Run, node: dict, attempt_n: int) -> dict:
    """Run a one-shot LLM "skill" via camc.

    Lifecycle (per the design principle: runner does as much as possible;
    only LLM reasoning happens in the agent):
      1. Workspace + prompt already prepared by _build_agent_context().
      2. `camc run` spawns the agent in workspace as cwd. Agent ID parsed
         from camc run stdout.
      3. Wait for the agent to write `agent_output.json` to workspace
         (or for a follow-up update to its mtime).
      4. Auto-schema check: if the produced data does not match output_schema,
         `camc send` a feedback message asking the agent to fix it; loop up
         to CAMFLOW_SKILL_INNER_RETRIES times.
      5. `camc kill` to clean up the tmux session.

    All of this is BEFORE the user-declared `verify:` rules run — those happen
    later in the main loop after _exec_skill returns.
    """
    workspace = actx["workspace"]
    prompt = actx["prompt_text"] + _skill_kickoff_instruction()
    output_path = workspace / _OUTPUT_FILENAME
    agent_name = f"{node['id']}-attempt-{attempt_n}"
    # Tag with run_id so cleanup / `camflow stop` later can find these agents.
    # Tag with the unique run_id (the dir name under .camflow/runs/) so
    # cross-run cleanup / stop can find all agents from this run.
    parts = run.run_dir.resolve().parts
    if "runs" in parts:
        i = parts.index("runs")
        run_id_for_tag = parts[i + 1] if i + 1 < len(parts) else parts[-1]
    else:
        run_id_for_tag = run.run_dir.name
    tag = f"camflow:{run_id_for_tag}"

    # 1. Spawn
    agent_id, err = _camc_run(workspace, prompt, agent_name, tag)
    if not agent_id:
        return _empty_envelope("failure", error={
            "code": "CAMC_RUN_FAILED", "message": err,
        })
    (workspace / "agent.id").write_text(agent_id)

    last_mtime: float | None = None
    schema = node.get("output_schema") or {}

    try:
        # 2. Wait for first output
        ok, err = _wait_for_output(output_path, last_mtime, _SKILL_TIMEOUT_S)
        if not ok:
            return _empty_envelope("failure", error={
                "code": "AGENT_TIMEOUT", "message": err, "details": {"agent_id": agent_id},
            })
        last_mtime = output_path.stat().st_mtime

        # 3. Inner self-correction loop on schema mismatch
        for inner_attempt in range(_SKILL_INNER_RETRIES + 1):
            try:
                env = json.loads(output_path.read_text())
            except json.JSONDecodeError as e:
                return _empty_envelope("failure", error={
                    "code": "AGENT_BAD_OUTPUT",
                    "message": f"agent_output.json not JSON: {e}",
                    "details": {"agent_id": agent_id},
                })

            # Pre-emptive schema check (don't wait for run_verify).
            if schema:
                data = env.get("data") or {}
                missing = [k for k in schema if k not in data]
            else:
                missing = []

            if not missing:
                break  # success

            if inner_attempt >= _SKILL_INNER_RETRIES:
                # Out of inner retries — let run_verify catch it the same way.
                # Returning the bad envelope; main loop will mark failure.
                run.trace("skill_self_correct_exhausted", node=node["id"],
                          attempt=attempt_n,
                          extra={"missing": missing,
                                 "inner_attempts": inner_attempt + 1})
                break

            # Send schema-fail feedback to the still-running agent
            feedback = (
                f"Schema check failed. Missing required field(s) in `data`: "
                f"{missing}. Please update {_OUTPUT_FILENAME} with all "
                f"required fields, then stop."
            )
            run.trace("skill_self_correct", node=node["id"], attempt=attempt_n,
                      extra={"inner_attempt": inner_attempt + 1, "missing": missing})
            _camc_send(agent_id, feedback)

            ok, err = _wait_for_output(output_path, last_mtime, _SKILL_TIMEOUT_S)
            if not ok:
                return _empty_envelope("failure", error={
                    "code": "AGENT_TIMEOUT_SELF_CORRECT",
                    "message": err, "details": {"agent_id": agent_id},
                })
            last_mtime = output_path.stat().st_mtime

        # 4. Pull metrics if camc tracks them
        status = _camc_status(agent_id)
        metrics = dict(env.get("metrics") or {})
        if cost := status.get("cost_estimate"):
            metrics["camc_cost_usd"] = cost
        if started := status.get("started_at"):
            metrics["camc_started_at"] = started

        return {
            "status": env.get("status", "success"),
            "data": env.get("data", {}) or {},
            "error": env.get("error"),
            "metrics": metrics,
            "artifacts": env.get("artifacts", []) or [],
        }
    finally:
        # 5. Always kill the agent — no leaked tmux sessions.
        _camc_kill(agent_id)


# ─── Verify ────────────────────────────────────────────────────────────

def run_verify(run: Run, node: dict, output: dict) -> tuple[bool, str]:
    """Validate a node's envelope against (a) its declared output_schema —
    automatic, runs whenever schema is declared — and (b) any user-declared
    `verify:` rules (rule / agent / future types like file/command).

    Schema check is implicit: the user does NOT need to add `{type: schema}`
    to verify. If they do, it's a no-op (kept for backward compat).
    """
    # ── (a) Auto schema check ────────────────────────────────────────
    schema = node.get("output_schema") or {}
    if schema:
        data = output.get("data") or {}
        for key in schema:
            if key not in data:
                return False, f"schema: missing field '{key}' in data"

    # ── (b) User-declared rules ──────────────────────────────────────
    for rule in (node.get("verify") or []):
        rtype = rule.get("type")
        if rtype == "schema":
            # Already handled above; redundant but accepted for back-compat.
            continue
        elif rtype == "rule":
            assertion = rule.get("assert", "")
            ctx = run.expr_ctx(current_output=output)
            try:
                ok = eval_expr(assertion, ctx)
            except ExprError as e:
                return False, f"rule eval error: {e}"
            if not ok:
                return False, f"rule failed: {assertion}"
        elif rtype == "agent":
            return False, "verify type=agent not implemented in v0.7"
        else:
            return False, f"unknown verify type: {rtype}"
    return True, "ok"


# ─── Main run loop ─────────────────────────────────────────────────────

def _persist_attempt(run: Run, nid: str, attempt_n: int, env: dict) -> None:
    d = run.run_dir / "nodes" / nid / f"attempt-{attempt_n}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.json").write_text(json.dumps(env, indent=2))


def _propagate_skip(run: Run, halting_node: str,
                    code: str = "UPSTREAM_HALTED") -> None:
    for nid in run.nodes_by_id:
        if not run.attempts[nid]:
            run.attempts[nid].append(_empty_envelope("skipped", error={
                "code": code,
                "message": f"halted by '{halting_node}'",
            }))
            run.trace("node_skipped", node=nid, attempt=1,
                      reason=f"{code.lower()}")


def _halt_workflow(run: Run, halted_node: str, halted_attempt: int,
                   reason: str, envelope: dict) -> None:
    """Pause execution for human/orchestrator handoff.

    Writes halt.json next to trace.jsonl with a summary, marks all
    not-yet-run nodes as skipped (with reason=upstream_halted), and
    emits workflow_halted.
    """
    halt_info = {
        "halted_node": halted_node,
        "halted_attempt": halted_attempt,
        "reason": reason,
        "envelope": envelope,
        "trace_step": run.step + 1,  # the next event written below
    }
    (run.run_dir / "halt.json").write_text(json.dumps(halt_info, indent=2))
    _propagate_skip(run, halted_node)
    run.trace("workflow_halted",
              node=halted_node, attempt=halted_attempt, reason=reason)


def run_workflow(workflow: dict, state: dict, run_dir: Path,
                 _existing_run: "Run | None" = None) -> str:
    """Run a workflow. Returns 'success' / 'halted' / 'failure'.

    `_existing_run` is internal — used by `resume` to inject a pre-populated
    Run with attempts already loaded from disk.
    """
    if _existing_run is not None:
        run = _existing_run
    else:
        run = Run(workflow, state, run_dir)
        run.trace("workflow_started")

    try:
        return _main_loop(run, workflow)
    finally:
        run.cleanup()


def _main_loop(run: "Run", workflow: dict) -> str:
    while True:
        ready = run.ready_nodes()
        if not ready:
            terminal = all(run.is_done(nid) for nid in run.nodes_by_id)
            if terminal:
                any_failed = any(
                    run.attempts[nid] and run.attempts[nid][-1]["status"] == "failure"
                    for nid in run.nodes_by_id
                )
                if any_failed:
                    run.trace("workflow_failed", reason="node failure terminal")
                    return "failure"
                run.trace("workflow_completed", status="success")
                return "success"
            run.trace("workflow_failed", reason="deadlock: no ready nodes")
            return "failure"

        # pick first by declaration order
        ready_ids = {n["id"] for n in ready}
        node = next(n for n in run.nodes if n["id"] in ready_ids)
        nid = node["id"]

        # `when` evaluation
        when_expr = node.get("when")
        if when_expr and nid not in run.retry_pending:
            try:
                when_ok = bool(eval_expr(when_expr, run.expr_ctx()))
            except ExprError as e:
                run.attempts[nid].append(_empty_envelope("skipped", error={
                    "code": "WHEN_ERROR", "message": str(e),
                }))
                run.trace("node_skipped", node=nid, attempt=1, reason=f"when error: {e}")
                continue
            if not when_ok:
                run.attempts[nid].append(_empty_envelope("skipped"))
                _persist_attempt(run, nid, 1, run.attempts[nid][-1])
                run.trace("node_skipped", node=nid, attempt=1, reason=f"when=false: {when_expr}")
                continue

        # retry context (only injected for the target node of an active retry)
        retry_ctx = run.retry_pending.pop(nid, None) if nid in run.retry_pending else None
        attempt_n = len(run.attempts[nid]) + 1
        run.trace(
            "node_started", node=nid, attempt=attempt_n,
            reason=("retry" if retry_ctx else "needs satisfied"),
        )

        env = execute_node(run, node, attempt_n, retry_ctx=retry_ctx)

        # verify (only if execution reported success)
        if env["status"] == "success" and (
            node.get("output_schema") or node.get("verify")
        ):
            run.trace("verify_started", node=nid, attempt=attempt_n)
            ok, reason = run_verify(run, node, env)
            if not ok:
                env["status"] = "failure"
                env["error"] = {"code": "VERIFY_FAIL", "message": reason}
                run.trace("verify_failed", node=nid, attempt=attempt_n, reason=reason)
            else:
                run.trace("verify_completed", node=nid, attempt=attempt_n)

        run.attempts[nid].append(env)
        _persist_attempt(run, nid, attempt_n, env)

        if env["status"] == "success":
            run.trace("node_completed", node=nid, attempt=attempt_n, status="success")
        else:
            run.trace("node_failed", node=nid, attempt=attempt_n,
                      reason=(env.get("error") or {}).get("message", "unknown"))

        # node-initiated halt: skill/tool returned status=halted explicitly
        if env["status"] == "halted":
            run.trace("node_halted", node=nid, attempt=attempt_n,
                      reason=(env.get("error") or {}).get("message",
                                                          "node returned halted"))
            _halt_workflow(run, nid, attempt_n,
                           reason="node returned status=halted",
                           envelope=env)
            return "halted"

        # retry: simplified semantics — re-run THIS node only
        # Trigger condition:
        #   * env.success and retry.until is false        → retry self
        #   * env.failure and retry is configured         → retry self
        retry_policy = node.get("retry")
        if retry_policy:
            ctx = run.expr_ctx()
            should_retry = False
            retry_reason = ""

            if env["status"] == "success":
                try:
                    until_ok = bool(eval_expr(retry_policy["until"], ctx))
                except ExprError as e:
                    run.trace("workflow_failed",
                              reason=f"retry.until eval error: {e}")
                    _propagate_skip(run, nid, "expression_error")
                    return "failure"
                if not until_ok:
                    should_retry = True
                    retry_reason = f"until=false: {retry_policy['until']}"
            elif env["status"] == "failure":
                should_retry = True
                retry_reason = (
                    "node failed: "
                    + (env.get("error") or {}).get("message", "unknown")
                )

            if should_retry:
                max_n_raw = retry_policy["max_attempts"]
                if isinstance(max_n_raw, str):
                    max_n_raw = _render_str(max_n_raw, ctx)
                try:
                    max_n = int(max_n_raw)
                except (TypeError, ValueError):
                    max_n = 3
                if len(run.attempts[nid]) >= max_n:
                    # Out of attempts → halt for human/orchestrator help
                    run.trace("retry_exhausted", node=nid,
                              reason=f"max_attempts={max_n} reached")
                    _halt_workflow(run, nid, attempt_n,
                                   reason=f"retry exhausted (max_attempts={max_n})",
                                   envelope=env)
                    return "halted"
                # Schedule a self-retry
                feedback_raw = retry_policy.get("feedback", "")
                feedback = (_render_str(feedback_raw, ctx)
                            if isinstance(feedback_raw, str) else feedback_raw)
                run.retry_pending[nid] = {
                    "feedback": feedback,
                    "attempt": len(run.attempts[nid]) + 1,
                }
                run.trace("retry_triggered", node=nid,
                          reason=retry_reason, feedback=feedback)
                continue

        # node failed and no retry → halt (was: workflow_failed)
        if env["status"] == "failure" and not retry_policy:
            run.trace("node_halted", node=nid, attempt=attempt_n,
                      reason="node failed and has no retry policy")
            _halt_workflow(run, nid, attempt_n,
                           reason="node failed, no retry configured",
                           envelope=env)
            return "halted"


# ─── CLI ───────────────────────────────────────────────────────────────

def _run_command(argv: list[str]) -> int:
    """Run a workflow."""
    p = argparse.ArgumentParser(
        prog="camflow", description="Run a workflow YAML (spec v0.6)",
    )
    p.add_argument("workflow", help="path to workflow YAML")
    p.add_argument("--state", default=None, help="JSON file with initial state")
    p.add_argument("--run-dir", default=None,
                   help="run directory (default: <project>/.camflow/runs/<run_id>)")
    p.add_argument("--validate", action="store_true",
                   help="validate the workflow and exit")
    args = p.parse_args(argv)

    wf = load_workflow(args.workflow)
    errs = validate_workflow(wf)
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.validate:
        print("workflow is valid")
        return 0

    state: dict = {}
    if args.state:
        with open(args.state) as f:
            state = json.load(f)

    project = Path(args.workflow).resolve().parent
    run_id = _gen_run_id()
    run_dir = Path(args.run_dir) if args.run_dir else project / ".camflow" / "runs" / run_id

    print(f"run_id:  {run_id}")
    print(f"run_dir: {run_dir}")
    result = run_workflow(wf, state, run_dir)
    print(f"result:  {result}")
    return _result_to_exit_code(result)


def _result_to_exit_code(result: str) -> int:
    """Map workflow result strings to standard exit codes.

      success → 0
      halted  → 2  (resume possible; orchestrator/human should pick up)
      failure → 1  (irrecoverable)
    """
    return {"success": 0, "halted": 2}.get(result, 1)


def _planner_workflow() -> dict:
    """The fixed 1-node workflow that runs the Planner skill.

    Inputs (via state):
      goal           required, NL description of what the user wants
      state_schema   optional, schema for the produced workflow's state
    Output (in nodes.plan.attempt-<n>.output.data):
      workflow_yaml  string — a YAML document parseable by parse_workflow_yaml.
    """
    return {
        "workflow": "planner",
        "version": "0.6",
        "goal": "Generate a runnable workflow.yaml from a natural-language goal.",
        "nodes": [
            {
                "id": "plan",
                "goal": "Produce a complete, valid workflow.yaml.",
                "uses": "skill.planner",
                "input": {
                    "goal": "{{state.goal}}",
                    "state_schema": "{{state.state_schema?}}",
                },
                "output_schema": {"workflow_yaml": "string"},
                "verify": [
                    {"type": "schema"},
                    {"type": "rule",
                     "assert": "output.data.workflow_yaml != ''"},
                ],
            }
        ],
    }


def _plan_command(argv: list[str]) -> int:
    """Plan (and optionally run) a workflow from a natural-language goal.

    Internally:
      1. Build the fixed planner DAG (1 node, skill.planner).
      2. run_workflow() it → get workflow_yaml from node output.
      3. parse_workflow_yaml() to validate the produced DAG.
      4. If --run: run_workflow() the new DAG with --state.
         Else: print the YAML to stdout (or --out file).
    """
    p = argparse.ArgumentParser(
        prog="camflow plan",
        description="Plan (and optionally run) a workflow from an NL goal",
    )
    p.add_argument("goal", help="what you want the workflow to accomplish")
    p.add_argument("--state-schema", default=None,
                   help="path to a YAML file declaring state schema for the "
                        "*produced* workflow")
    p.add_argument("--out", "-o", default=None,
                   help="write the planned YAML here (default: stdout)")
    p.add_argument("--run", action="store_true",
                   help="after planning, run the produced workflow")
    p.add_argument("--state", default=None,
                   help="state JSON for the produced workflow (with --run)")
    p.add_argument("--run-dir", default=None,
                   help="parent run dir; planner sub-runs as run_dir/planner, "
                        "produced workflow runs as run_dir/main")
    args = p.parse_args(argv)

    state_schema = None
    if args.state_schema:
        with open(args.state_schema) as f:
            doc = yaml.safe_load(f)
        state_schema = doc.get("state", doc) if isinstance(doc, dict) else None

    cwd = Path.cwd()
    run_id = _gen_run_id()
    parent = (Path(args.run_dir) if args.run_dir
              else cwd / ".camflow" / "runs" / run_id)
    planner_dir = parent / "planner"

    print(f"run_id:  {run_id}", file=sys.stderr)
    print(f"planner: {planner_dir}", file=sys.stderr)

    # Step 1: run the planner DAG
    planner_wf = _planner_workflow()
    planner_state = {"goal": args.goal}
    if state_schema is not None:
        planner_state["state_schema"] = state_schema
    planner_result = run_workflow(planner_wf, planner_state, planner_dir)
    if planner_result != "success":
        print(f"PLANNER {planner_result}: see {planner_dir}/trace.jsonl",
              file=sys.stderr)
        return _result_to_exit_code(planner_result)

    # Step 2: extract the produced workflow
    plan_output_path = planner_dir / "nodes" / "plan" / "attempt-1" / "output.json"
    plan_output = json.loads(plan_output_path.read_text())
    yaml_text = (plan_output.get("data") or {}).get("workflow_yaml", "")

    # Step 3: parse + validate
    try:
        produced_wf = parse_workflow_yaml(yaml_text)
    except WorkflowParseError as e:
        print(f"PLAN OUTPUT INVALID: {e}", file=sys.stderr)
        print("--- raw output ---", file=sys.stderr)
        print(yaml_text[:1500], file=sys.stderr)
        return 1

    # Step 4a: --out / stdout
    yaml_canonical = yaml.safe_dump(produced_wf, sort_keys=False,
                                    allow_unicode=True)
    if args.out:
        Path(args.out).write_text(yaml_canonical)
        print(f"wrote {args.out}", file=sys.stderr)
    elif not args.run:
        sys.stdout.write(yaml_canonical)

    # Step 4b: --run → also run the produced workflow
    if args.run:
        main_dir = parent / "main"
        print(f"main:    {main_dir}", file=sys.stderr)
        main_state: dict = {}
        if args.state:
            with open(args.state) as f:
                main_state = json.load(f)
        main_result = run_workflow(produced_wf, main_state, main_dir)
        print(f"result:  {main_result}", file=sys.stderr)
        return _result_to_exit_code(main_result)

    return 0


def _summarize_run(run_dir: Path) -> dict:
    """Read run dir → structured summary. Used by status (and resume)."""
    if not run_dir.exists():
        raise FileNotFoundError(f"run dir not found: {run_dir}")

    summary: dict = {
        "run_dir": str(run_dir),
        "running": (run_dir / "runner.pid").exists(),
        "halted": (run_dir / "halt.json").exists(),
        "workflow": None,
        "nodes": [],
        "last_event": None,
    }

    wf_path = run_dir / "workflow.yaml"
    if wf_path.exists():
        wf = yaml.safe_load(wf_path.read_text())
        summary["workflow"] = wf.get("workflow")
        for n in wf.get("nodes") or []:
            nid = n["id"]
            attempt_dir = run_dir / "nodes" / nid
            attempts = []
            if attempt_dir.exists():
                for ad in sorted(attempt_dir.glob("attempt-*")):
                    out = ad / "output.json"
                    if out.exists():
                        env = json.loads(out.read_text())
                        attempts.append({
                            "n": int(ad.name.split("-")[1]),
                            "status": env.get("status"),
                        })
            summary["nodes"].append({
                "id": nid,
                "attempts": attempts,
                "latest_status": attempts[-1]["status"] if attempts else None,
            })

    trace_path = run_dir / "trace.jsonl"
    if trace_path.exists():
        lines = trace_path.read_text().splitlines()
        if lines:
            summary["last_event"] = json.loads(lines[-1])

    halt_path = run_dir / "halt.json"
    if halt_path.exists():
        summary["halt"] = json.loads(halt_path.read_text())

    return summary


def _status_command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="camflow status",
        description="Show progress of a run dir.",
    )
    p.add_argument("run_dir")
    p.add_argument("--json", action="store_true",
                   help="machine-callable JSON output")
    args = p.parse_args(argv)

    rd = Path(args.run_dir)
    try:
        summary = _summarize_run(rd)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    state = (
        "halted" if summary["halted"] else
        "running" if summary["running"] else
        "done"
    )
    last = summary["last_event"] or {}
    print(f"workflow: {summary['workflow']}")
    print(f"run_dir:  {summary['run_dir']}")
    print(f"state:    {state}")
    print(f"last:     {last.get('event','-')} (step {last.get('step','?')})")
    print()
    for n in summary["nodes"]:
        latest = n["latest_status"] or "pending"
        n_att = len(n["attempts"])
        att_str = f" ({n_att} attempts)" if n_att != 1 else ""
        print(f"  {n['id']:<25}  {latest}{att_str}")
    if summary.get("halt"):
        h = summary["halt"]
        print()
        print(f"HALTED at: {h['halted_node']}#{h['halted_attempt']}")
        print(f"reason:    {h['reason']}")
    return 0


def _trace_command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="camflow trace",
        description="Pretty-print or dump trace.jsonl from a run dir.",
    )
    p.add_argument("run_dir")
    p.add_argument("--tail", type=int, default=None,
                   help="show only the last N events")
    p.add_argument("--json", action="store_true",
                   help="raw JSON lines (just cats trace.jsonl)")
    args = p.parse_args(argv)

    trace_path = Path(args.run_dir) / "trace.jsonl"
    if not trace_path.exists():
        print(f"ERROR: {trace_path} not found", file=sys.stderr)
        return 1
    lines = trace_path.read_text().splitlines()
    if args.tail is not None:
        lines = lines[-args.tail:]
    if args.json:
        for line in lines:
            print(line)
        return 0
    for line in lines:
        e = json.loads(line)
        extra = " ".join(
            f"{k}={v}" for k, v in e.items()
            if k not in ("step", "ts", "event", "reason")
        )
        reason = e.get("reason", "")
        if reason and len(reason) > 60:
            reason = reason[:60] + "…"
        print(f"  step{e['step']:>3}  {e['event']:<20}  {extra:<35}  {reason}")
    return 0


def _stop_command(argv: list[str]) -> int:
    """Stop a running camflow workflow.

    v0.7: SIGTERMs the runner process via runner.pid.
    v0.8 (later): also `camc stop` agents tagged with this run_id.
    """
    p = argparse.ArgumentParser(
        prog="camflow stop",
        description="Stop a running workflow (SIGTERM the runner process).",
    )
    p.add_argument("run_dir")
    p.add_argument("--force", action="store_true",
                   help="SIGKILL instead of SIGTERM")
    args = p.parse_args(argv)

    pid_path = Path(args.run_dir) / "runner.pid"
    if not pid_path.exists():
        print(f"ERROR: no runner.pid at {pid_path} (is it running?)",
              file=sys.stderr)
        return 1

    try:
        pid = int(pid_path.read_text().strip())
    except ValueError as e:
        print(f"ERROR: bad runner.pid: {e}", file=sys.stderr)
        return 1

    import signal
    sig = signal.SIGKILL if args.force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        print(f"runner pid {pid} not running (cleaning up stale runner.pid)",
              file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return 1

    print(f"sent {sig.name} to pid {pid}")
    # TODO v0.8: also `camc stop` any agents tagged with run_id
    return 0


def _resume_command(argv: list[str]) -> int:
    """Continue a halted workflow from where it stopped.

    Reads halt.json + workflow.yaml + state.json from <run_dir>, replays the
    completed attempts into a fresh Run, marks the halted node retry-pending,
    and continues the main loop. The user/orchestrator may have edited
    state.json or workflow.yaml between halt and resume — current files win.
    """
    p = argparse.ArgumentParser(
        prog="camflow resume",
        description="Resume a halted workflow.",
    )
    p.add_argument("run_dir")
    p.add_argument("--feedback", default="",
                   help="extra feedback text injected as {{retry.feedback}} "
                        "for the halted node's next attempt")
    args = p.parse_args(argv)

    rd = Path(args.run_dir)
    halt_path = rd / "halt.json"
    if not halt_path.exists():
        print(f"ERROR: not halted (no halt.json at {halt_path})",
              file=sys.stderr)
        return 1

    halt_info = json.loads(halt_path.read_text())
    workflow = yaml.safe_load((rd / "workflow.yaml").read_text())
    state = json.loads((rd / "state.json").read_text())

    # Build a Run in resume mode (don't overwrite workflow.yaml / state.json)
    run = Run(workflow, state, rd, resume=True)

    # Replay existing attempts from disk so cross-node references still work.
    summary = _summarize_run(rd)
    for n in summary["nodes"]:
        nid = n["id"]
        for att in n["attempts"]:
            out = (rd / "nodes" / nid / f"attempt-{att['n']}" / "output.json")
            run.attempts[nid].append(json.loads(out.read_text()))

    # Mark the halted node for re-execution. Use --feedback if provided,
    # otherwise pull from the halt.json envelope's data.feedback (if any).
    halted = halt_info["halted_node"]
    fb = args.feedback or (
        (halt_info.get("envelope") or {})
        .get("data", {})
        .get("feedback", "")
    )
    run.retry_pending[halted] = {
        "feedback": fb,
        "attempt": len(run.attempts[halted]) + 1,
    }
    halt_path.unlink()  # consume halt.json — fresh halt will rewrite
    run.trace("workflow_resumed", node=halted,
              attempt=len(run.attempts[halted]) + 1,
              reason=f"resume from halt.json (feedback len={len(fb)})")

    print(f"resuming {halted} (attempt {len(run.attempts[halted]) + 1})",
          file=sys.stderr)
    result = run_workflow(workflow, state, rd, _existing_run=run)
    print(f"result:  {result}", file=sys.stderr)
    return _result_to_exit_code(result)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]

    if not argv:
        print(
            "Usage:\n"
            "  camflow <workflow.yaml> [--state STATE] [--run-dir DIR] [--validate]\n"
            "  camflow plan \"<goal>\" [--out FILE] [--run --state FILE]\n"
            "  camflow status <run_dir> [--json]\n"
            "  camflow trace  <run_dir> [--tail N] [--json]\n"
            "  camflow resume <run_dir>\n"
            "  camflow stop   <run_dir>\n",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "plan":
        return _plan_command(argv[1:])
    if cmd == "status":
        return _status_command(argv[1:])
    if cmd == "trace":
        return _trace_command(argv[1:])
    if cmd == "resume":
        return _resume_command(argv[1:])
    if cmd == "stop":
        return _stop_command(argv[1:])
    if cmd in ("-h", "--help"):
        return _run_command(argv)  # delegates to argparse help
    # Default mode: argv[0] is a workflow path
    return _run_command(argv)


if __name__ == "__main__":
    sys.exit(main())
