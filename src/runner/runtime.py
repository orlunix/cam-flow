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
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from . import camc_lib as camc
from .expr import (
    ExprError, eval_expr, render_deep, _render_str,
)
from .parse import (
    WorkflowParseError, load_workflow, parse_workflow_yaml,
    validate_workflow,
)
from .paths import (
    archive_run_dir, default_run_dir, gen_run_id, utcnow_iso,
)


# Public re-exports for legacy callers (tests, examples) that still
# import these names from `runner.runtime`.
__all_helpers__ = [
    "ExprError", "eval_expr", "render_deep",
    "WorkflowParseError", "load_workflow", "parse_workflow_yaml",
    "validate_workflow",
    "default_run_dir", "gen_run_id", "utcnow_iso", "archive_run_dir",
]


# Internal aliases — historical names used inside this module.
_utcnow_iso = utcnow_iso
_gen_run_id = gen_run_id
_default_run_dir = default_run_dir
_archive_run_dir = archive_run_dir


_VALID_STATUSES = {"success", "failure", "skipped", "halted"}


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
        n = camc.kill_by_tag(self.tag)
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


# camc constants — keep aliases for any external callers; the canonical
# values live in camc_lib.py.
_SKILL_TIMEOUT_S = camc.DEFAULT_SKILL_TIMEOUT_S
_OUTPUT_FILENAME = "agent_output.json"


def _skill_kickoff_instruction() -> str:
    """Append to every skill prompt: tell the agent how to deliver output."""
    return (
        "\n\n# Delivery protocol\n"
        f"Write the final envelope JSON to `{_OUTPUT_FILENAME}` in your "
        "current working directory. Do not print it; the runner reads the file. "
        "Once you've written the file, do nothing else; the runner will close "
        "the session."
    )


def _exec_skill(name: str, actx: dict, run: Run, node: dict, attempt_n: int) -> dict:
    """Run a one-shot LLM "skill" via camc.

    The lifecycle (spawn → wait for agent_output.json → kill) is in
    `camc.run_and_collect`. Here we just turn the resulting envelope
    into a node-level result with strict status validation.
    """
    workspace = actx["workspace"]
    prompt = actx["prompt_text"] + _skill_kickoff_instruction()
    agent_name = f"{node['id']}-attempt-{attempt_n}"
    try:
        _agent_id, env = camc.run_and_collect(
            prompt=prompt,
            workspace=workspace,
            name=agent_name,
            tag=run.tag,
            output_file=_OUTPUT_FILENAME,
            timeout_s=_SKILL_TIMEOUT_S,
            write_id_to=workspace / "agent.id",
        )
    except camc.CamcTimeout as e:
        return _empty_envelope("failure",
                               error={"code": "AGENT_TIMEOUT", "message": str(e)})
    except camc.CamcError as e:
        return _empty_envelope("failure",
                               error={"code": "CAMC_RUN_FAILED", "message": str(e)})
    except json.JSONDecodeError as e:
        return _empty_envelope("failure", error={
            "code": "AGENT_BAD_OUTPUT",
            "message": f"agent_output.json not JSON: {e}",
        })

    # Strict status: agent must return one of the valid values. Any other
    # string is a bug we surface explicitly, not silently coerce.
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
        "metrics": env.get("metrics", {}) or {},
        "artifacts": env.get("artifacts", []) or [],
    }


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

    Differs from _exec_skill in:
      - prompt assembled from AGENT.md (autonomous role spec) instead of
        a single-turn skill template;
      - longer default timeout (autonomous agents do multi-step tool use).
    The lifecycle (spawn → wait → kill) is shared via camc.run_and_collect.
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
    agent_runtime_name = f"{node['id']}-attempt-{attempt_n}"

    try:
        _agent_id, env = camc.run_and_collect(
            prompt=prompt,
            workspace=workspace,
            name=agent_runtime_name,
            tag=run.tag,
            output_file=_OUTPUT_FILENAME,
            timeout_s=camc.DEFAULT_AGENT_TIMEOUT_S,
            write_id_to=workspace / "agent.id",
        )
    except camc.CamcTimeout as e:
        return _empty_envelope("failure",
                               error={"code": "AGENT_TIMEOUT", "message": str(e)})
    except camc.CamcError as e:
        return _empty_envelope("failure",
                               error={"code": "CAMC_RUN_FAILED", "message": str(e)})
    except json.JSONDecodeError as e:
        return _empty_envelope("failure", error={
            "code": "AGENT_BAD_OUTPUT",
            "message": f"agent_output.json not JSON: {e}",
        })

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
        "metrics": env.get("metrics", {}) or {},
        "artifacts": env.get("artifacts", []) or [],
    }


# ─── Verify ────────────────────────────────────────────────────────────

def run_verify(run: Run, node: dict, output: dict,
               attempt_n: int = 1) -> tuple[bool, str]:
    """Validate a node's envelope against:
      (a) its declared output_schema — automatic field-presence check;
      (b) any user-declared `verify:` rules — only two types:
          - `command`: run a bash command in the attempt dir;
                       gate on exit code 0.
          - `agent`:   spawn an LLM evaluator with a `criterion`.

    Schema check is implicit. No expression-rule type, no workflow_yaml
    type. If you want a value check beyond field-presence, write a
    `command` (e.g. `python3 -c '...exit 0/1...'`) or use `agent`.
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
        if rtype == "command":
            ok, reason = _run_verify_command(rule, run, node, output, attempt_n)
            if not ok:
                return False, reason
        elif rtype == "agent":
            ok, reason = _run_verify_agent(rule, run, node, output,
                                           attempt_n, idx)
            if not ok:
                return False, reason
        else:
            return False, f"unknown verify type: {rtype!r} (only 'command' / 'agent')"
    return True, "ok"


def _run_verify_command(rule: dict, run: Run, node: dict, output: dict,
                        attempt_n: int) -> tuple[bool, str]:
    """Run a bash command in the attempt dir; gate on exit code 0.

    The cmd string is rendered as a template (so `{{state.x}}` works).
    cwd is the attempt dir, where `agent_output.json` (the agent's
    raw envelope) and `output.json` (runner-validated) both live —
    so cmds can read fields via:
        python3 -c "import json,sys; sys.exit(0 if json.load(open('agent_output.json'))['data']['x'] else 1)"
        test -n "$(jq -r .data.patch agent_output.json)"

    Optional `timeout` (seconds) defaults to 60.
    """
    cmd_raw = rule.get("cmd", "")
    if not cmd_raw:
        return False, "verify type=command: missing required `cmd`"
    try:
        cmd = _render_str(cmd_raw, run.expr_ctx(current_output=output))
    except ExprError as e:
        return False, f"verify command template error: {e}"
    timeout = int(rule.get("timeout", 60))
    cwd = run.run_dir / "nodes" / node["id"] / f"attempt-{attempt_n}"
    cwd.mkdir(parents=True, exist_ok=True)
    # Make the envelope readable by the cmd uniformly across exec types
    # (mock nodes don't write agent_output.json themselves).
    (cwd / "agent_output.json").write_text(json.dumps(output, indent=2))
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
        return False, (
            f"verify command exited {proc.returncode}: {snippet}"
        )
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


def _propagate_skip(run: Run, halting_node: str,
                    code: str = "UPSTREAM_HALTED") -> None:
    """Mark every not-yet-attempted node as skipped (with reason).

    Called when the workflow halts so downstream nodes have a clear
    terminal status instead of silently being un-run.
    """
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

    Writes halt.json next to trace.jsonl, marks not-yet-run nodes as
    skipped, and emits workflow_halted. The user can later run
    `camflow resume <run_dir>` to continue from the halted node.
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

        # Node-initiated halt: skill/tool/agent envelope set status=halted.
        if env["status"] == "halted":
            run.trace("node_halted", node=nid, attempt=attempt_n,
                      reason=(env.get("error") or {}).get("message",
                                                          "node returned halted"))
            _halt_workflow(run, nid, attempt_n,
                           reason="node returned status=halted",
                           envelope=env)
            return "halted"

        # retry decision: re-run THIS node only.
        #   * success + retry.until is false  → retry self
        #   * failure + retry policy present  → retry self
        retry_policy = node.get("retry")
        if env["status"] == "failure" and not retry_policy:
            # No retry configured — halt for human/orchestrator handoff.
            _halt_workflow(run, nid, attempt_n,
                           reason="node failed, no retry configured",
                           envelope=env)
            return "halted"
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
            _halt_workflow(run, nid, attempt_n,
                           reason=f"retry exhausted (max_attempts={max_n})",
                           envelope=env)
            return "halted"

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
      halted  → 2  (resume possible via `camflow resume <run_dir>`)
      failure → 1  (irrecoverable / no halt sidecar)
    """
    return {"success": 0, "halted": 2}.get(result, 1)


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
                # Schema check (auto) ensures relevant_skills exists; an
                # empty list is valid (skill_searcher might find nothing).
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
                # Validates the YAML via a one-shot Python: parse via
                # runner.parse.parse_workflow_yaml, exit non-zero on any
                # WorkflowParseError. Cwd is the attempt dir so
                # agent_output.json is right there to read.
                "verify": [
                    {"type": "command",
                     "cmd": (
                         "python3 -c \""
                         "import json,sys;"
                         "from runner.parse import parse_workflow_yaml;"
                         "y=json.load(open('agent_output.json'))['data']['workflow_yaml'];"
                         "y or (print('empty workflow_yaml',file=sys.stderr) or sys.exit(1));"
                         "parse_workflow_yaml(y)"
                         "\"")},
                ],
                "retry": {
                    "max_attempts": 3,
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



def _summarize_run(run_dir: Path) -> dict:
    """Read run dir → structured summary. Used by `camflow resume`
    to replay completed attempts before continuing.
    """
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


def _resume_command(argv: list[str]) -> int:
    """Continue a halted workflow from where it stopped.

    Reads halt.json + workflow.yaml + state.json from <run_dir>, replays
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

    # Mark the halted node for re-execution. --feedback wins over the
    # halt envelope's data.feedback (if any).
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
            "  camflow resume <run_dir> [--feedback TEXT]\n"
            "\n"
            "Inspect a run:  cat .camflow/run/trace.jsonl\n"
            "Stop a run:     kill $(cat .camflow/run/runner.pid)\n",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "plan":
        return _plan_command(argv[1:])
    if cmd == "resume":
        return _resume_command(argv[1:])
    if cmd in ("-h", "--help"):
        return _run_command(argv)
    return _run_command(argv)


if __name__ == "__main__":
    sys.exit(main())
