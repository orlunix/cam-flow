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
import atexit
import json
import os
import re
import secrets
import signal
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
    """Substitute every `{{expr}}` in s with its evaluated value.
    Strict — missing fields raise ExprError. (Workflow author must
    supply state defaults instead of relying on optional markers.)
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


# ─── Workflow loading & validation ────────────────────────────────────

def load_workflow(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def validate_workflow(wf: dict, project_root: Path | None = None) -> list[str]:
    """Validate a workflow's structure.

    If `project_root` is given, also validate that every `uses: skill.X`
    resolves to a real SKILL.md (built-in or skillm). Pass None to skip
    skill-resolution checks (useful for unit tests with mocks-only workflows).
    """
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
        # Skill / agent existence check (only if project_root provided)
        uses = n.get("uses", "")
        if project_root and uses.startswith("skill."):
            skill_name = uses[len("skill."):]
            if _resolve_skill_md_path(skill_name, project_root) is None:
                errors.append(
                    f"{nid}.uses: skill.{skill_name} not found "
                    f"(not in built-ins, not in <project>/.claude/skills, "
                    f"not in skillm library)"
                )
        if project_root and uses.startswith("agent."):
            agent_name = uses[len("agent."):]
            if _resolve_agent_md_path(agent_name, project_root) is None:
                errors.append(
                    f"{nid}.uses: agent.{agent_name} not found "
                    f"(no AGENT.md in built-ins or <project>/.claude/agents)"
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


def parse_workflow_yaml(text: str, project_root: Path | None = None) -> dict:
    """Parse a YAML string into a workflow dict and validate it.

    Strips optional ```yaml fences. Raises WorkflowParseError on:
      - empty / whitespace-only input
      - invalid YAML
      - non-dict top level
      - any validate_workflow error (incl. skill.X existence if project_root given).
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
    errors = validate_workflow(wf, project_root=project_root)
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


# ─── Run dir layout ─────────────────────────────────────────────────────
# A camflow project keeps exactly ONE current run on disk plus a rolling
# archive of past runs:
#
#   <project>/.camflow/run/                  ← current run (always here)
#   <project>/.camflow/archives/<stamp>-<status>/   ← past runs
#
# When a new run starts, the previous .camflow/run/ (if any) is moved to
# archives/. No timestamped run dir per invocation; no nesting noise.

_RUN_DIRNAME = "run"
_ARCHIVES_DIRNAME = "archives"


def _project_camflow_dir(project_root: Path) -> Path:
    return project_root / ".camflow"


def _default_run_dir(project_root: Path) -> Path:
    """Return <project>/.camflow/run/, archiving any prior run first."""
    cam = _project_camflow_dir(project_root)
    run = cam / _RUN_DIRNAME
    if run.exists() and any(run.iterdir()):
        _archive_run_dir(run, cam / _ARCHIVES_DIRNAME)
    run.mkdir(parents=True, exist_ok=True)
    return run


def _archive_run_dir(run_dir: Path, archives_root: Path) -> Path | None:
    """Move run_dir to archives_root/<stamp>-<status>/. Best-effort.

    The status suffix is derived from the run's halt.json or trace tail —
    so an archived run dir's name immediately tells you the outcome
    (success / failure / halted / unknown).
    """
    if not run_dir.exists():
        return None
    archives_root.mkdir(parents=True, exist_ok=True)
    status = _peek_run_status(run_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = archives_root / f"{stamp}-{status}"
    n = 1
    while target.exists():
        target = archives_root / f"{stamp}-{status}-{n}"
        n += 1
    run_dir.rename(target)
    return target


def _peek_run_status(run_dir: Path) -> str:
    """Best-effort inspection: success / failure / halted / unknown."""
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
    # plan-mode parent dir: aggregate over child runs (planner/, main/).
    for sub in ("main", "planner"):
        if (run_dir / sub).is_dir():
            s = _peek_run_status(run_dir / sub)
            if s != "unknown":
                return s
    return "unknown"


_VALID_STATUSES = {"success", "failure", "skipped"}


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
        # this run. Falls back to cwd if run_dir is custom and not under
        # any .camflow/ tree.
        parts = self.run_dir.resolve().parts
        if ".camflow" in parts:
            idx = parts.index(".camflow")
            self.project_root = Path(*parts[:idx]) if idx > 0 else Path("/")
        else:
            self.project_root = Path.cwd().resolve()

        # Make sure camflow's built-in skills + agents are installed in the
        # project so they appear under <project>/.claude/skills/ and
        # <project>/.claude/agents/, alongside any user-managed installs.
        _ensure_builtin_skills_installed(self.project_root)
        _ensure_builtin_agents_installed(self.project_root)

        # Write our PID so `camflow stop <run_dir>` can SIGTERM us.
        self.pid_path.write_text(str(os.getpid()))

        # Run id + camc tag — generated once per Run so the crash-safety
        # net (and _exec_skill / _exec_agent) can spawn agents under a
        # unique tag without recomputing path-based heuristics. Captured
        # in trace events too for debug correlation.
        self.run_id = _gen_run_id()
        self.run_id_for_tag = self.run_id  # kept for backward-compat name
        self.tag = f"camflow:{self.run_id}"

        # Crash-safety net: ensure spawned agents die even if the runtime
        # exits abnormally (uncaught exception, SIGTERM, OOM-kill of parent
        # shell). Normal path already kills via try/finally inside the
        # node executors — this is just defense in depth.
        self._installed_handlers = False
        atexit.register(self._cleanup_orphan_agents)
        try:
            self._prev_sigterm = signal.signal(signal.SIGTERM,
                                               self._signal_cleanup)
            self._prev_sigint = signal.signal(signal.SIGINT,
                                              self._signal_cleanup)
            self._installed_handlers = True
        except (ValueError, OSError):
            # signal.signal raises ValueError if not on main thread.
            # Tests / embedded use is fine without the signal handlers —
            # atexit alone still catches the common cases.
            pass

    def _cleanup_orphan_agents(self) -> None:
        n = _kill_run_tagged_agents(self.run_id_for_tag)
        if n > 0:
            print(f"camflow: killed {n} orphan agent(s) on exit",
                  file=sys.stderr)

    def _signal_cleanup(self, signum, _frame) -> None:
        # Triggers atexit, which runs _cleanup_orphan_agents.
        sys.exit(128 + signum)

    def cleanup(self) -> None:
        """Remove the runner.pid file at end of run + uninstall the
        crash-safety net (we exited normally; no orphans to chase)."""
        try:
            atexit.unregister(self._cleanup_orphan_agents)
        except Exception:
            pass
        if self._installed_handlers:
            try:
                signal.signal(signal.SIGTERM, self._prev_sigterm)
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (ValueError, OSError):
                pass
            self._installed_handlers = False
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
        nodes_view = {nid: {"latest": {"output": atts[-1]}}
                      for nid, atts in self.attempts.items() if atts}
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
        return a[-1]["status"] in _VALID_STATUSES

    def ready_nodes(self) -> list[dict]:
        """A node is ready iff:
          - it has no terminal attempt yet, OR it is marked retry-pending; AND
          - none of its `needs` are themselves retry-pending; AND
          - all `needs` have a success attempt.
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

    Layout (flat — attempt dir IS the workspace, no nested subdir):
      <run_dir>/nodes/<id>/attempt-<n>/
        ├── input.json
        ├── prompt.txt          (skill/agent only)
        ├── agent_output.json   (agent-written; appears once it finishes)
        ├── agent.id            (camc agent ID; for cleanup)
        └── output.json         (runner-managed; written after the call returns)

    Tools, skills, and agents all share this context-builder so the
    runtime has one place to wire up materials + workspace.
    """
    att_dir = run.run_dir / "nodes" / node["id"] / f"attempt-{attempt_n}"
    workspace = att_dir
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
    if uses.startswith("skill."):
        skill_name = uses[len("skill."):]
        prompt = _build_skill_prompt(
            run.workflow, node, inputs,
            skill_name=skill_name, run=run,
        )
        (workspace / "prompt.txt").write_text(prompt)
        ctx["prompt_text"] = prompt
    # agent.X builds its own prompt in _exec_agent (different shape:
    # autonomous mode embeds AGENT.md as the role spec).

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
        return _exec_agent(uses[len("agent."):], actx, run, node, attempt_n)
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


# ─── Skill resolution (camflow built-ins + skillm) ─────────────────────

def _camflow_repo_root() -> Path:
    """Where camflow itself is installed — has `skills/` with built-ins."""
    return Path(__file__).resolve().parents[2]


def _builtin_skills_root() -> Path:
    return _camflow_repo_root() / "skills"


def _list_builtin_skills() -> list[str]:
    """Names of all camflow-shipped skills (each is a dir with SKILL.md)."""
    root = _builtin_skills_root()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "SKILL.md").exists()
    )


def _list_skillm_skills() -> list[str]:
    """Names of all skills currently in skillm's library on this machine.

    Discovers via `~/.skillm/repos/*/<name>/SKILL.md`. We don't shell out to
    `skillm list` because its output is rich-formatted; the filesystem is
    cheaper and equivalent.
    """
    repos = Path.home() / ".skillm" / "repos"
    if not repos.is_dir():
        return []
    found = set()
    for repo in repos.iterdir():
        if not repo.is_dir():
            continue
        for skill_dir in repo.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                found.add(skill_dir.name)
    return sorted(found)


def _ensure_builtin_skills_installed(project_root: Path) -> None:
    """Symlink camflow's built-in skills into <project>/.claude/skills/.

    Idempotent. If a real (non-symlink) directory already exists at the
    target name, leave it alone — assume the user has overridden.
    """
    src_root = _builtin_skills_root()
    if not src_root.is_dir():
        return
    target_root = project_root / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    for src in src_root.iterdir():
        if not src.is_dir() or not (src / "SKILL.md").exists():
            continue
        target = target_root / src.name
        try:
            if target.is_symlink():
                if target.resolve() == src.resolve():
                    continue  # already correctly linked
                target.unlink()
            elif target.exists():
                # User-overridden real dir; respect it.
                continue
            target.symlink_to(src.resolve())
        except OSError:
            # Filesystem doesn't support symlinks (e.g., some Windows
            # configs) — silent fallback to direct resolver lookup.
            pass


def _resolve_skill_md_path(skill_name: str, project_root: Path) -> Path | None:
    """Find a skill's SKILL.md. Lookup order:

      1. <project>/.claude/skills/<name>/SKILL.md   (project-installed)
      2. ~/.claude/skills/<name>/SKILL.md           (global skillm install)
      3. ~/.skillm/repos/*/<name>/SKILL.md          (skillm library)
      4. <camflow-repo>/skills/<name>/SKILL.md      (built-in fallback if symlink missing)

    Returns the first existing path, or None.
    """
    candidates = [
        project_root / ".claude" / "skills" / skill_name / "SKILL.md",
        Path.home() / ".claude" / "skills" / skill_name / "SKILL.md",
    ]
    repos = Path.home() / ".skillm" / "repos"
    if repos.is_dir():
        for repo in repos.iterdir():
            if repo.is_dir():
                candidates.append(repo / skill_name / "SKILL.md")
    candidates.append(_builtin_skills_root() / skill_name / "SKILL.md")
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_skill_template(skill_name: str, run: Run) -> str | None:
    """Read the skill's SKILL.md content, or return None if not found."""
    p = _resolve_skill_md_path(skill_name, run.project_root)
    return p.read_text() if p else None


# ─── Agent resolution (built-in autonomous agents) ─────────────────────

def _builtin_agents_root() -> Path:
    return _camflow_repo_root() / "agents"


def _list_builtin_agents() -> list[str]:
    """Names of camflow-shipped agents.

    Source-of-truth layout: <repo>/agents/<name>/AGENT.md (subdirectory).
    The runtime renames to a flat <name>.md when installing into a
    project's .claude/agents/ (Claude Code's subagent discovery format).
    """
    root = _builtin_agents_root()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "AGENT.md").exists()
    )


def _ensure_builtin_agents_installed(project_root: Path) -> None:
    """Install camflow's built-in agents into <project>/.claude/agents/.

    Source: <repo>/agents/<name>/AGENT.md (subdirectory + AGENT.md, our
    canonical layout — same shape as skillm skills).
    Target: <project>/.claude/agents/<name>.md (flat file — Claude Code's
    subagent discovery format).

    The install is just rename-via-symlink: target_path → AGENT.md.
    Claude Code natively discovers .claude/agents/*.md and exposes them
    as slash-agents (callable via /name), so once installed any
    camc-spawned session in this project can /name-invoke them.

    Idempotent. Respects user overrides (if a real file already exists
    at the target, leave it).
    """
    src_root = _builtin_agents_root()
    if not src_root.is_dir():
        return
    target_root = project_root / ".claude" / "agents"
    target_root.mkdir(parents=True, exist_ok=True)
    for src_dir in src_root.iterdir():
        if not src_dir.is_dir():
            continue
        src_md = src_dir / "AGENT.md"
        if not src_md.exists():
            continue
        target = target_root / f"{src_dir.name}.md"   # rename to flat file
        try:
            if target.is_symlink():
                if target.resolve() == src_md.resolve():
                    continue
                target.unlink()
            elif target.exists():
                continue  # user override
            target.symlink_to(src_md.resolve())
        except OSError:
            pass


def _resolve_agent_md_path(name: str, project_root: Path) -> Path | None:
    """Find an agent's definition. Lookup order — covers both target
    (flat file installed by camflow) and source (subdirectory in repo):

      1. <project>/.claude/agents/<name>.md          (project-installed flat)
      2. ~/.claude/agents/<name>.md                  (user-global flat)
      3. <camflow-repo>/agents/<name>/AGENT.md       (built-in source)
    """
    for p in [
        project_root / ".claude" / "agents" / f"{name}.md",
        Path.home() / ".claude" / "agents" / f"{name}.md",
        _builtin_agents_root() / name / "AGENT.md",
    ]:
        if p.exists():
            return p
    return None


def _load_agent_md(name: str, run: Run) -> str | None:
    p = _resolve_agent_md_path(name, run.project_root)
    return p.read_text() if p else None


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


def _kill_run_tagged_agents(run_id_for_tag: str) -> int:
    """Best-effort: kill every running camc agent tagged camflow:<run_id>.

    Used as a crash-safety net (atexit + signal handlers). The normal
    success path already kills agents in _exec_skill / _exec_agent's
    `finally` blocks; this function catches the cases where those
    `finally` blocks never ran — runtime exceptions, SIGTERM, SIGKILL
    of the parent shell, OOM-kill, etc. Returns count of agents killed.
    Never raises.
    """
    tag = f"camflow:{run_id_for_tag}"
    killed = 0
    try:
        proc = subprocess.run(["camc", "--json", "ls"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return 0
        for a in json.loads(proc.stdout):
            if a.get("status") != "running":
                continue
            tags = (a.get("task") or {}).get("tags") or []
            if tag not in tags:
                continue
            aid = a.get("id") or ""
            if aid:
                _camc_kill(aid)
                killed += 1
    except Exception:
        pass
    return killed


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

    Lifecycle:
      1. Workspace + prompt already prepared by _build_agent_context().
      2. `camc run` spawns the agent in workspace as cwd.
      3. Wait for `agent_output.json` to land.
      4. Parse it. Schema mismatch / bad envelope → fail and let the
         outer main_loop retry the whole node.
      5. `camc rm --archive` cleans up tmux + DB record + history.
    """
    workspace = actx["workspace"]
    prompt = actx["prompt_text"] + _skill_kickoff_instruction()
    output_path = workspace / _OUTPUT_FILENAME
    agent_name = f"{node['id']}-attempt-{attempt_n}"

    agent_id, err = _camc_run(workspace, prompt, agent_name, run.tag)
    if not agent_id:
        return _empty_envelope("failure", error={
            "code": "CAMC_RUN_FAILED", "message": err,
        })
    (workspace / "agent.id").write_text(agent_id)

    try:
        ok, err = _wait_for_output(output_path, None, _SKILL_TIMEOUT_S)
        if not ok:
            return _empty_envelope("failure", error={
                "code": "AGENT_TIMEOUT", "message": err,
                "details": {"agent_id": agent_id},
            })
        try:
            env = json.loads(output_path.read_text())
        except json.JSONDecodeError as e:
            return _empty_envelope("failure", error={
                "code": "AGENT_BAD_OUTPUT",
                "message": f"agent_output.json not JSON: {e}",
                "details": {"agent_id": agent_id},
            })

        # Pull metrics if camc tracks them
        status = _camc_status(agent_id)
        metrics = dict(env.get("metrics") or {})
        if cost := status.get("cost_estimate"):
            metrics["camc_cost_usd"] = cost
        if started := status.get("started_at"):
            metrics["camc_started_at"] = started

        # Strict status: agent must return one of the two valid values.
        # Any other string (ok / done / completed / unknown) is a bug we
        # surface as an explicit failure, not a silent coerce.
        out_status = env.get("status")
        if out_status not in _VALID_STATUSES:
            return _empty_envelope("failure", error={
                "code": "BAD_STATUS",
                "message": f"agent returned status={out_status!r}, "
                           f"expected one of {sorted(_VALID_STATUSES)}",
            })
        return {
            "status": out_status,
            "data": env.get("data", {}) or {},
            "error": env.get("error"),
            "metrics": metrics,
            "artifacts": env.get("artifacts", []) or [],
        }
    finally:
        # 5. Always kill the agent — no leaked tmux sessions.
        _camc_kill(agent_id)


# ─── Agent execution (autonomous mode) ─────────────────────────────────

def _build_agent_prompt(role_md: str, workflow: dict, node: dict,
                        inputs: dict, project_root: Path) -> str:
    """Compose the autonomous agent's kickoff prompt.

    Sections:
      - AGENT.md (role + capabilities + protocol)
      - Workflow goal
      - Node task
      - Inputs (JSON), with available_skills list embedded if not already in inputs
      - Output schema reminder
      - Delivery instruction (write agent_output.json, then stop)
    """
    parts = [role_md.strip()]

    if wg := (workflow.get("goal") or "").strip():
        parts.append(f"# Workflow goal\n{wg}")
    if ng := (node.get("goal") or "").strip():
        parts.append(f"# Your task\n{ng}")

    enriched_inputs = dict(inputs)
    if "available_skills" not in enriched_inputs:
        enriched_inputs["available_skills"] = sorted(
            set(_list_builtin_skills() + _list_skillm_skills())
        )
    if "available_agents" not in enriched_inputs:
        enriched_inputs["available_agents"] = sorted(_list_builtin_agents())
    parts.append(
        "# Inputs\n```json\n"
        + json.dumps(enriched_inputs, indent=2, ensure_ascii=False)
        + "\n```"
    )

    schema = node.get("output_schema") or {}
    schema_block = (
        json.dumps(schema, indent=2) if schema
        else "(any object — node declared no schema)"
    )
    parts.append(
        "# Envelope shape — write THIS JSON to "
        f"`{_OUTPUT_FILENAME}`\n"
        "```json\n"
        "{\n"
        '  "status": "success",            // exact string. Allowed values:\n'
        '                                  //   "success"  — work done, data populated\n'
        '                                  //   "halted"   — give up, set error.code/message\n'
        '                                  //   "failure"  — runtime/tooling error\n'
        f'  "data": <object matching this schema>: {schema_block},\n'
        '  "error": null,                  // or {"code": "...", "message": "..."} when not success\n'
        '  "metrics": {},                  // optional; may include numeric counters/cost\n'
        '  "artifacts": []                 // optional; list of files you produced\n'
        "}\n"
        "```\n"
        "DO NOT use other status strings (no 'ok', 'done', 'completed', etc.). "
        "The runtime only recognizes the four enum values above."
    )

    parts.append(
        "# Delivery protocol\n"
        f"Write the envelope JSON to `{_OUTPUT_FILENAME}` in your current "
        "working directory. Do not print it; the runner reads the file. "
        "If the runner sends follow-up feedback during this session, "
        "treat it as a schema-correction request — update the SAME file "
        "and stop. Once you've written the file, do nothing else; the "
        "runner will close the session."
    )
    return "\n\n".join(parts)


def _exec_agent(name: str, actx: dict, run: Run, node: dict, attempt_n: int) -> dict:
    """Run an autonomous camc agent loaded with agents/<name>.md.

    Differs from _exec_skill in that the agent is autonomous: it can use
    Claude Code tools (Read/Write/Bash/Glob/Grep), read multiple skills,
    and decide its own multi-step path. Same envelope contract though —
    writes agent_output.json when done, runner picks up.
    """
    workspace = actx["workspace"]
    role_md = _load_agent_md(name, run)
    if role_md is None:
        return _empty_envelope("failure", error={
            "code": "AGENT_NOT_FOUND",
            "message": f"agents/{name}.md not found",
        })

    prompt = _build_agent_prompt(role_md, run.workflow, node,
                                 actx["inputs"], run.project_root)
    (workspace / "prompt.txt").write_text(prompt)

    output_path = workspace / _OUTPUT_FILENAME
    agent_runtime_name = f"{node['id']}-attempt-{attempt_n}"

    # 1. Spawn — agent is given the project as cwd-ish via --add-dir so it
    # can Read/Write project paths (.claude/skills/, etc.). Workspace is
    # the agent's main working dir.
    proc = subprocess.run(
        ["camc", "run",
         "--path", str(workspace),
         "--name", agent_runtime_name,
         "--tag", run.tag,
         prompt],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return _empty_envelope("failure", error={
            "code": "CAMC_RUN_FAILED",
            "message": f"camc run exited {proc.returncode}: {proc.stderr.strip()[:300]}",
        })
    m = _AGENT_ID_RE.search(proc.stdout)
    if not m:
        return _empty_envelope("failure", error={
            "code": "CAMC_RUN_BAD_OUTPUT",
            "message": f"could not parse agent ID from camc run output:\n{proc.stdout[:500]}",
        })
    agent_id = m.group(1)
    (workspace / "agent.id").write_text(agent_id)

    # Autonomous agents get a longer timeout — they're doing multi-step
    # tool use, not a single LLM turn.
    agent_timeout_s = int(os.environ.get("CAMFLOW_AGENT_TIMEOUT", "1800"))

    last_mtime: float | None = None
    schema = node.get("output_schema") or {}

    try:
        # 2. Wait for first output
        ok, err = _wait_for_output(output_path, last_mtime, agent_timeout_s)
        if not ok:
            return _empty_envelope("failure", error={
                "code": "AGENT_TIMEOUT", "message": err,
                "details": {"agent_id": agent_id},
            })
        last_mtime = output_path.stat().st_mtime

        # 3. Self-correction loop on schema mismatch (same as skills)
        for inner_attempt in range(_SKILL_INNER_RETRIES + 1):
            try:
                env = json.loads(output_path.read_text())
            except json.JSONDecodeError as e:
                return _empty_envelope("failure", error={
                    "code": "AGENT_BAD_OUTPUT",
                    "message": f"agent_output.json not JSON: {e}",
                    "details": {"agent_id": agent_id},
                })

            if schema:
                data = env.get("data") or {}
                missing = [k for k in schema if k not in data]
            else:
                missing = []
            if not missing:
                break
            if inner_attempt >= _SKILL_INNER_RETRIES:
                run.trace("agent_self_correct_exhausted",
                          node=node["id"], attempt=attempt_n,
                          extra={"missing": missing,
                                 "inner_attempts": inner_attempt + 1})
                break
            feedback = (
                f"Schema check failed. Missing required field(s) in `data`: "
                f"{missing}. Update {_OUTPUT_FILENAME} with all required "
                f"fields, then stop."
            )
            run.trace("agent_self_correct", node=node["id"], attempt=attempt_n,
                      extra={"inner_attempt": inner_attempt + 1, "missing": missing})
            _camc_send(agent_id, feedback)
            ok, err = _wait_for_output(output_path, last_mtime, agent_timeout_s)
            if not ok:
                return _empty_envelope("failure", error={
                    "code": "AGENT_TIMEOUT_SELF_CORRECT",
                    "message": err, "details": {"agent_id": agent_id},
                })
            last_mtime = output_path.stat().st_mtime

        status = _camc_status(agent_id)
        metrics = dict(env.get("metrics") or {})
        if cost := status.get("cost_estimate"):
            metrics["camc_cost_usd"] = cost
        if started := status.get("started_at"):
            metrics["camc_started_at"] = started

        # Strict status: agent must return one of the two valid values.
        # Any other string (ok / done / completed / unknown) is a bug we
        # surface as an explicit failure, not a silent coerce.
        out_status = env.get("status")
        if out_status not in _VALID_STATUSES:
            return _empty_envelope("failure", error={
                "code": "BAD_STATUS",
                "message": f"agent returned status={out_status!r}, "
                           f"expected one of {sorted(_VALID_STATUSES)}",
            })
        return {
            "status": out_status,
            "data": env.get("data", {}) or {},
            "error": env.get("error"),
            "metrics": metrics,
            "artifacts": env.get("artifacts", []) or [],
        }
    finally:
        _camc_kill(agent_id)


# ─── Verify ────────────────────────────────────────────────────────────

def run_verify(run: Run, node: dict, output: dict,
               attempt_n: int = 1) -> tuple[bool, str]:
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
    for idx, rule in enumerate(node.get("verify") or []):
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
            ok, reason = _run_verify_agent(rule, run, node, output,
                                           attempt_n, idx)
            if not ok:
                return False, reason
        elif rtype == "workflow_yaml":
            # Pure validation: checks the named string field parses as a
            # well-formed workflow.yaml (incl. skill/agent resolution).
            # Does NOT modify the workflow — the YAML stays exactly as
            # the agent wrote it. Used by `camflow plan`'s plan node so
            # bad YAML triggers retry-with-feedback loops.
            field = rule.get("field", "workflow_yaml")
            data = output.get("data") or {}
            yaml_text = data.get(field, "")
            if not isinstance(yaml_text, str) or not yaml_text.strip():
                return False, f"workflow_yaml: field `data.{field}` is empty or not a string"
            try:
                parse_workflow_yaml(yaml_text, project_root=run.project_root)
            except WorkflowParseError as e:
                return False, f"workflow_yaml verify failed: {e}"
        else:
            return False, f"unknown verify type: {rtype}"
    return True, "ok"


def _run_verify_agent(rule: dict, run: Run, node: dict, output: dict,
                      attempt_n: int, rule_idx: int) -> tuple[bool, str]:
    """Spawn an evaluator (agent.evaluator or skill.evaluator) to judge
    whether `output.data` meets `rule.criterion`. Returns (approved, reason).

    Resolution: rule.agent / rule.skill explicit override → else
    agent.evaluator if AGENT.md exists → else skill.evaluator.
    rule.mock = {approved, reasoning} bypasses the spawn (tests).
    """
    criterion = rule.get("criterion") or ""
    if not criterion:
        return False, "verify type=agent: missing required `criterion`"

    if isinstance(rule.get("mock"), dict):
        m = rule["mock"]
        return (True, "ok (mock)") if m.get("approved") else \
               (False, f"verify agent rejected: {m.get('reasoning', '<no reason>')}")

    if rule.get("agent"):
        kind, name = "agent", rule["agent"]
    elif rule.get("skill"):
        kind, name = "skill", rule["skill"]
    elif _resolve_agent_md_path("evaluator", run.project_root):
        kind, name = "agent", "evaluator"
    else:
        kind, name = "skill", "evaluator"

    sub_dir = (run.run_dir / "nodes" / node["id"]
               / f"attempt-{attempt_n}" / f"verify-{kind}-{rule_idx}")
    sub_dir.mkdir(parents=True, exist_ok=True)
    inputs = {
        "output_being_judged": {k: output.get(k) for k in ("status", "data", "error")},
        "criterion": criterion,
        "node_id": node["id"],
        "node_goal": node.get("goal", ""),
    }
    (sub_dir / "input.json").write_text(json.dumps(inputs, indent=2))

    sub_node = {
        "id": f"{node['id']}__verify_{kind}_{rule_idx}",
        "goal": f"Judge whether `{node['id']}` output meets: {criterion}",
        "uses": f"{kind}.{name}",
        "input": inputs,
        "output_schema": {"approved": "boolean", "reasoning": "string",
                          "issues": "array"},
    }

    if kind == "agent":
        prompt = _build_agent_prompt(_load_agent_md(name, run) or "",
                                     run.workflow, sub_node, inputs,
                                     run.project_root)
    else:
        prompt = _build_skill_prompt(run.workflow, sub_node, inputs,
                                     skill_name=name, run=run)
    (sub_dir / "prompt.txt").write_text(prompt)
    actx = {"att_dir": sub_dir, "workspace": sub_dir,
            "inputs": inputs, "prompt_text": prompt}
    env = (_exec_agent if kind == "agent" else _exec_skill)(
        name, actx, run, sub_node, attempt_n=1)
    (sub_dir / "output.json").write_text(json.dumps(env, indent=2))

    if env.get("status") != "success":
        return False, (f"verify {kind}.{name} failed to run: "
                       f"{(env.get('error') or {}).get('message', 'unknown')}")
    data = env.get("data") or {}
    if not data.get("approved"):
        reason = data.get("reasoning") or "(no reasoning provided)"
        if data.get("issues"):
            reason += f" Issues: {data['issues']}"
        return False, f"verify {kind}.{name} rejected: {reason}"
    return True, "ok"


# ─── Main run loop ─────────────────────────────────────────────────────

def _persist_attempt(run: Run, nid: str, attempt_n: int, env: dict) -> None:
    d = run.run_dir / "nodes" / nid / f"attempt-{attempt_n}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.json").write_text(json.dumps(env, indent=2))


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

        # `when` evaluation — false → skip (no propagation; downstream
        # treats `skipped` like `success` for needs-resolution).
        when_expr = node.get("when")
        if when_expr and nid not in run.retry_pending:
            try:
                when_ok = bool(eval_expr(when_expr, run.expr_ctx()))
            except ExprError as e:
                run.attempts[nid].append(_empty_envelope("skipped", error={
                    "code": "WHEN_ERROR", "message": str(e),
                }))
                run.trace("node_skipped", node=nid, attempt=1,
                          reason=f"when error: {e}")
                continue
            if not when_ok:
                run.attempts[nid].append(_empty_envelope("skipped"))
                _persist_attempt(run, nid, 1, run.attempts[nid][-1])
                run.trace("node_skipped", node=nid, attempt=1,
                          reason=f"when=false: {when_expr}")
                continue

        # Always pass a dict (never None) so templates like {{retry.feedback}}
        # remain renderable on the first attempt (feedback="" by default).
        retry_ctx = run.retry_pending.pop(nid, {"feedback": "", "attempt": 1})
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
            ok, reason = run_verify(run, node, env, attempt_n=attempt_n)
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

        # retry decision: re-run THIS node only.
        #   * success + retry.until is false  → retry self
        #   * failure + retry policy present  → retry self
        # Otherwise: nothing to do; outer loop's terminal check decides
        # workflow-level success vs failure (failure is terminal, no halt).
        retry_policy = node.get("retry")
        if not retry_policy:
            continue

        ctx = run.expr_ctx()
        should_retry = False
        retry_reason = ""

        if env["status"] == "success" and "until" in retry_policy:
            try:
                until_ok = bool(eval_expr(retry_policy["until"], ctx))
            except ExprError as e:
                run.trace("workflow_failed",
                          reason=f"retry.until eval error: {e}")
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

        if not should_retry:
            continue

        max_n_raw = retry_policy.get("max_attempts", 3)
        if isinstance(max_n_raw, str):
            max_n_raw = _render_str(max_n_raw, ctx)
        try:
            max_n = int(max_n_raw)
        except (TypeError, ValueError):
            max_n = 3
        if len(run.attempts[nid]) >= max_n:
            run.trace("retry_exhausted", node=nid,
                      reason=f"max_attempts={max_n} reached")
            # Force this node terminal as failure so the outer terminal
            # check picks it up next iteration.
            if env["status"] == "success":
                run.attempts[nid][-1] = dict(env, status="failure",
                                             error={"code": "RETRY_EXHAUSTED",
                                                    "message": f"until never satisfied "
                                                               f"after {max_n} attempts"})
            continue

        feedback_raw = retry_policy.get("feedback", "")
        feedback = (_render_str(feedback_raw, ctx)
                    if isinstance(feedback_raw, str) else feedback_raw)
        run.retry_pending[nid] = {
            "feedback": feedback,
            "attempt": len(run.attempts[nid]) + 1,
        }
        run.trace("retry_triggered", node=nid,
                  reason=retry_reason, feedback=feedback)


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
    project = Path(args.workflow).resolve().parent
    # Make sure built-in skills + agents are present in the project before
    # we check that referenced skills/agents resolve.
    _ensure_builtin_skills_installed(project)
    _ensure_builtin_agents_installed(project)
    errs = validate_workflow(wf, project_root=project)
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

    if args.run_dir:
        run_dir = Path(args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Default layout: <project>/.camflow/run/. Any prior run there
        # is automatically moved to <project>/.camflow/archives/<stamp>/.
        run_dir = _default_run_dir(project)

    print(f"run_dir: {run_dir}")
    result = run_workflow(wf, state, run_dir)
    print(f"result:  {result}")
    return _result_to_exit_code(result)


def _result_to_exit_code(result: str) -> int:
    """Map workflow result to standard exit codes:
      success → 0
      anything else (failure, deadlock) → 1
    """
    return 0 if result == "success" else 1


def _planner_bootstrap_workflow() -> dict:
    """Two-node scaffold for `camflow plan`. Each node spawns a single
    autonomous agent (workflow IS multi-agent; agents don't kick off
    workflows):

      1. search_skills  (agent.skill-searcher, autonomous)
         Walks ~/.skillm/repos and <project>/.claude/skills/ itself,
         uses Glob/Grep/Read to discover and filter SKILL.md files.
         Returns relevant subset with descriptions. Keeps Planner's
         context lean — Planner doesn't read SKILL.md files at all.
      2. plan           (agent.planner, autonomous)
         Receives only the filtered subset (`relevant_skills`) plus the
         goal, designs the DAG, emits workflow.yaml. Verified by
         `type: workflow_yaml` and retries with feedback on failure.

    Inputs (via state):
      goal             required, NL description
      state_schema     optional, schema for the produced workflow's state
    Output (data of `plan` node):
      workflow_yaml    string — the user's runnable DAG.
    """
    return {
        "workflow": "planner-bootstrap",
        "version": "0.6",
        "goal": "Plan a workflow: search relevant skills, then design the DAG.",
        "nodes": [
            {
                "id": "search_skills",
                "goal": "Discover skills relevant to the goal by walking the "
                        "skillm repository and project skills dirs. Don't "
                        "read every SKILL.md — Glob/Grep first, Read selectively.",
                "uses": "agent.skill-searcher",
                "input": {
                    "goal": "{{state.goal}}",
                },
                "output_schema": {
                    "relevant_skills": "array",
                    "reasoning": "string",
                    "examined_count": "integer",
                    "total_count": "integer",
                },
                "verify": [
                    {"type": "rule",
                     "assert": "output.data.relevant_skills != null"},
                ],
            },
            {
                "id": "plan",
                "goal": "Run the Planner agent on the goal, using only the "
                        "relevant skills surfaced by search_skills.",
                "needs": ["search_skills"],
                "uses": "agent.planner",
                "input": {
                    "goal": "{{state.goal}}",
                    "state_schema": "{{state.state_schema}}",
                    "relevant_skills": "{{nodes.search_skills.latest.output.data.relevant_skills}}",
                    "search_reasoning": "{{nodes.search_skills.latest.output.data.reasoning}}",
                    "previous_validation_error": "{{retry.feedback}}",
                },
                "output_schema": {"workflow_yaml": "string"},
                "verify": [
                    {"type": "rule",
                     "assert": "output.data.workflow_yaml != ''"},
                    # Validates the YAML the planner produced. On failure
                    # retry kicks in with the error string as feedback.
                    {"type": "workflow_yaml"},
                ],
                "retry": {
                    "max_attempts": 3,
                    # On verify failure env.error.message contains the
                    # workflow_yaml verify reason — fed back as feedback
                    # for the planner agent's next attempt.
                    "feedback": "{{nodes.plan.latest.output.error.message}}",
                },
            },
        ],
    }


# Back-compat: keep old name as alias so any external caller doesn't break.
_planner_workflow = _planner_bootstrap_workflow


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

    state_schema = ""
    if args.state_schema:
        with open(args.state_schema) as f:
            doc = yaml.safe_load(f)
        state_schema = doc.get("state", doc) if isinstance(doc, dict) else ""

    cwd = Path.cwd()
    if args.run_dir:
        parent = Path(args.run_dir)
        parent.mkdir(parents=True, exist_ok=True)
    else:
        # Default: <cwd>/.camflow/run/ with planner/ + main/ subdirs.
        # Any prior run is auto-archived to .camflow/archives/<stamp>/.
        parent = _default_run_dir(cwd)
    planner_dir = parent / "planner"

    print(f"planner: {planner_dir}", file=sys.stderr)

    # Make sure built-ins (skills + agents) are installed in the project so
    # both the skill_searcher agent and the planner agent can discover them
    # via filesystem walks.
    _ensure_builtin_skills_installed(cwd)
    _ensure_builtin_agents_installed(cwd)

    # Step 1: run the 2-node planner bootstrap (search_skills → plan).
    # The skill_searcher agent walks ~/.skillm/repos and project skills
    # directories itself — no catalog injection needed.
    planner_wf = _planner_bootstrap_workflow()
    planner_state = {"goal": args.goal, "state_schema": state_schema}
    planner_result = run_workflow(planner_wf, planner_state, planner_dir)
    if planner_result != "success":
        print(f"PLANNER {planner_result}: see {planner_dir}/trace.jsonl",
              file=sys.stderr)
        return _result_to_exit_code(planner_result)

    # Step 2: extract the produced workflow. The plan node's verify chain
    # already includes type=workflow_yaml, which means by the time we get
    # here the YAML has been parsed + skill-resolved successfully (or the
    # plan node retried up to max_attempts and halted). Find the latest
    # attempt's output.
    plan_attempts_dir = planner_dir / "nodes" / "plan"
    attempt_dirs = sorted(plan_attempts_dir.glob("attempt-*"),
                          key=lambda p: int(p.name.split("-")[1]))
    plan_output = json.loads(
        (attempt_dirs[-1] / "output.json").read_text()
    )
    yaml_text = (plan_output.get("data") or {}).get("workflow_yaml", "")
    produced_wf = parse_workflow_yaml(yaml_text, project_root=cwd)

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



def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]

    if not argv:
        print(
            "Usage:\n"
            "  camflow <workflow.yaml> [--state STATE] [--run-dir DIR] [--validate]\n"
            "  camflow plan \"<goal>\" [--out FILE] [--run --state FILE]\n"
            "\n"
            "Inspect a run:  cat .camflow/run/trace.jsonl\n"
            "Stop a run:     kill $(cat .camflow/run/runner.pid)\n",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "plan":
        return _plan_command(argv[1:])
    if cmd in ("-h", "--help"):
        return _run_command(argv)
    return _run_command(argv)


if __name__ == "__main__":
    sys.exit(main())
