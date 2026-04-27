"""Steward birth — boot pack assembly, ``camc run``, persistence.

Public API:
    spawn_steward(project_dir, workflow_path, ...) -> agent_id
    is_steward_alive(project_dir, *, camc_status=...) -> bool
    load_steward_pointer(project_dir) -> dict | None

The ``ensure_steward`` orchestration (check pointer → check alive →
respawn if dead → reuse if alive) lives in the engine startup hook so
that callers don't accidentally double-spawn.

Spawning is deliberately separate from event emission (A7) and from
the compaction handoff (Phase B): one camc subprocess per spawn,
nothing more. We rely on ``registry.hooks.on_agent_spawned`` to keep
the project agent registry consistent.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from camflow.backend.persistence import load_state, save_state_atomic
from camflow.registry import on_agent_spawned, set_current_steward


# Pointer file lives at the project's .camflow/ root (not in steward/)
# so it's easy for callers to find without knowing the per-agent layout.
STEWARD_POINTER_FILE = "steward.json"

# Phase B: per-agent private directory layout (see camflow.paths).
# These constants describe the FILE NAMES inside that directory; the
# directory itself is `.camflow/steward/` per `camflow.paths.steward_dir()`.
STEWARD_PROMPT_FILE = "prompt.txt"          # was steward-prompt.txt
STEWARD_HISTORY_FILE = "session.log"        # was steward-history.log
STEWARD_SUMMARY_FILE = "summary.md"         # was steward-summary.md
STEWARD_ARCHIVE_FILE = "archive.md"         # was steward-archive.md


# ---- subprocess plumbing ------------------------------------------------


CAMC_BIN = shutil.which("camc") or "camc"


def _resolve_pointer_path(project_dir: str | os.PathLike) -> str:
    return str(Path(project_dir) / ".camflow" / STEWARD_POINTER_FILE)


def _resolve_camflow_dir(project_dir: str | os.PathLike) -> Path:
    p = Path(project_dir) / ".camflow"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_steward_dir(project_dir: str | os.PathLike) -> Path:
    """``.camflow/steward/`` — Steward's private directory."""
    from camflow import paths
    return paths.steward_dir(project_dir)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def _short_id() -> str:
    """8-hex-char id for the steward name (`steward-7c2a3f12`)."""
    return secrets.token_hex(4)


# ---- pointer file -------------------------------------------------------


def load_steward_pointer(project_dir: str | os.PathLike) -> dict[str, Any] | None:
    """Return the ``steward.json`` dict, or ``None`` if absent.

    The pointer survives engine restarts (unlike ``state.json`` it is
    project-scoped, not flow-scoped). Engine resume reads it to decide
    reattach vs respawn.
    """
    return load_state(_resolve_pointer_path(project_dir), default=None)


def _write_steward_pointer(
    project_dir: str | os.PathLike, payload: dict[str, Any]
) -> None:
    save_state_atomic(_resolve_pointer_path(project_dir), payload)


# ---- liveness check -----------------------------------------------------


def _camc_status(agent_id: str) -> str | None:
    """Probe ``camc status <id>`` and return a status string, or
    ``None`` if camc says the agent is unknown / camc itself failed.

    We use a tight timeout because this is sometimes called on
    every engine startup; a slow camc shouldn't gate the engine.
    """
    try:
        proc = subprocess.run(
            [CAMC_BIN, "status", agent_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    # camc status is human-readable; we just need "is the agent
    # known?" which equals "did camc return success for this id".
    return proc.stdout.strip() or "unknown"


def is_steward_alive(
    project_dir: str | os.PathLike,
    *,
    camc_status: Callable[[str], str | None] | None = None,
) -> bool:
    """True iff there is a recorded Steward AND camc still knows about
    its agent id. Dependency-injectable via ``camc_status`` for tests.
    """
    pointer = load_steward_pointer(project_dir)
    if not pointer or not pointer.get("agent_id"):
        return False
    probe = camc_status or _camc_status
    return probe(pointer["agent_id"]) is not None


# ---- boot pack assembly -------------------------------------------------


_BOOT_TEMPLATE = """\
You are the Steward agent for camflow project ``{project_id}``.

PROJECT
─────
{project_dir}

GOAL  (verbatim from user request, if known)
────
{goal}

PLAN SUMMARY
────────────
{plan_summary}

PLAN RATIONALE  (excerpt)
──────────────
{plan_rationale}

YOUR JOB
────────
- You are this project's persistent assistant. You do NOT go away when
  a flow ends; you stay alive until a human kills you with
  `camflow steward kill` or `camc rm`.
- Engines push events to you as messages prefixed with
  ``[CAMFLOW EVENT]``. Each event is a small JSON describing one
  thing that happened.
- The user talks to you via `camflow chat`. Their messages arrive
  WITHOUT the [CAMFLOW EVENT] prefix — treat unprefixed messages as
  user input.
- You answer the user. You take corrective action via the
  `camflow ctl` CLI.
- You are NOT the dispatcher. Engine decides which node runs next per
  workflow.yaml. You influence the flow by calling `camflow ctl`.
- You do NOT write workflow.yaml. To change the plan, call
  `camflow ctl replan --reason "..."`. The engine will spawn a new
  Planner agent that produces the new yaml.

YOUR TOOLS
──────────
Run `camflow ctl --help` to see the current verb set. As of Phase A
the available verbs are READ-ONLY:

  read-state      print .camflow/state.json
  read-trace      print last N trace.log entries (--tail N --kind X)
  read-events     print last N steward-events.jsonl entries
  read-rationale  print plan-rationale.md
  read-registry   print agents.json (table or --json)

Mutating verbs (pause, resume, kill-worker, spawn, skip, replan)
arrive in Phase B with an autonomy config and confirm flow. Until
they exist, on bad events your role is to *recommend* — describe
what you would do; the human will execute.

DEFAULT BEHAVIOR
────────────────
- node_done success → stay quiet unless the user asks.
- node_failed → state cause + engine's retry decision + your
  recommendation (or "engine handling, no action").
- heartbeat_stale_worker → probe via read-trace; if hung, recommend
  the human run `camc rm <agent-id>` (Phase A has no kill verb yet).
- user "现在状况？" / "what's going on?" → one paragraph; read state
  if needed; do NOT dump trace lines.
- user asks about a past flow → check steward-archive.md first;
  only call read-trace if the answer isn't there.
"""


def _read_or_blank(path: Path, max_chars: int | None = None) -> str:
    if not path.exists():
        return ""
    txt = path.read_text(encoding="utf-8")
    if max_chars is not None and len(txt) > max_chars:
        txt = txt[:max_chars] + "\n... (truncated)"
    return txt


def _summarize_workflow(workflow_path: str | os.PathLike) -> str:
    """Return a one-line-per-node summary, or '(no workflow)'."""
    if not workflow_path:
        return "(no workflow yet)"
    p = Path(workflow_path)
    if not p.exists():
        return f"(workflow file missing at {p})"
    try:
        from camflow.engine.dsl import load_workflow
        wf = load_workflow(str(p))
    except Exception as e:
        return f"(workflow load failed: {e})"
    if not isinstance(wf, dict) or not wf:
        return "(empty workflow)"
    lines = []
    for i, (nid, node) in enumerate(wf.items(), 1):
        if i > 30:
            lines.append(f"  ... (+{len(wf) - 30} more nodes)")
            break
        do = (node or {}).get("do", "")
        do_short = (do[:60] + "...") if len(do) > 60 else do
        lines.append(f"  {i}. {nid:<20}  do: {do_short}")
    return "\n".join(lines) if lines else "(empty workflow)"


def build_boot_pack(
    project_dir: str | os.PathLike,
    workflow_path: str | os.PathLike | None,
) -> str:
    """Render the Steward's boot prompt from workflow + rationale + request."""
    pdir = Path(project_dir)
    camflow_dir = pdir / ".camflow"

    goal = _read_or_blank(camflow_dir / "plan-request.txt", max_chars=2000)
    if not goal:
        goal = "(no goal recorded — running an explicit yaml directly)"

    plan_summary = _summarize_workflow(workflow_path) if workflow_path else "(no plan)"

    plan_rationale = _read_or_blank(
        camflow_dir / "plan-rationale.md", max_chars=4000,
    ) or "(no rationale yet — first flow on this project, perhaps)"

    return _BOOT_TEMPLATE.format(
        project_id=pdir.name or "(unnamed)",
        project_dir=str(pdir.resolve()),
        goal=goal,
        plan_summary=plan_summary,
        plan_rationale=plan_rationale,
    )


# ---- spawn --------------------------------------------------------------


_AGENT_ID_RE = re.compile(r"agent\s+([0-9a-f]{6,12})")
_AGENT_ID_RE_ALT = re.compile(r"ID:\s+([0-9a-f]{6,12})")


def _parse_agent_id(stdout: str) -> str | None:
    for pattern in (_AGENT_ID_RE, _AGENT_ID_RE_ALT):
        m = pattern.search(stdout)
        if m:
            return m.group(1)
    return None


def _default_camc_runner(name: str, project_dir: str, prompt: str) -> str:
    """Default subprocess implementation. Replaced in tests via
    the ``camc_runner`` parameter of ``spawn_steward``."""
    short_prompt = (
        f"Read the file .camflow/steward/{STEWARD_PROMPT_FILE} and "
        f"follow ALL instructions inside it exactly. The file contains "
        f"your full role description, tools, and default behavior."
    )
    proc = subprocess.run(
        [
            CAMC_BIN, "run",
            "--name", name,
            "--path", project_dir,
            short_prompt,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"camc run for steward returned {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    agent_id = _parse_agent_id(proc.stdout)
    if not agent_id:
        raise RuntimeError(
            f"could not parse steward agent id from camc output: "
            f"{proc.stdout[:500]}"
        )
    return agent_id


def spawn_steward(
    project_dir: str | os.PathLike,
    workflow_path: str | os.PathLike | None = None,
    *,
    name_prefix: str = "steward",
    spawned_by: str = "engine",
    camc_runner: Callable[[str, str, str], str] | None = None,
) -> str:
    """Spawn a fresh Steward agent for this project.

    Steps:
      1. Build the boot pack (workflow summary + rationale + role).
      2. Write it to ``.camflow/steward/prompt.txt`` (Phase B private dir).
      3. ``camc run`` the agent (or the injected ``camc_runner``).
      4. Update the agent registry (``on_agent_spawned``) and the
         pointer file (``.camflow/steward.json``).
      5. Return the agent id.

    Caller must hold the engine.lock (in production) so two engines
    can't race to register two Stewards for the same project.
    """
    pdir = str(Path(project_dir).resolve())
    _resolve_camflow_dir(pdir)               # ensure .camflow/ exists
    sdir = _resolve_steward_dir(pdir)        # ensure .camflow/steward/ exists

    # Boot pack to disk first so a separate inspector can read it before
    # camc has a chance to consume it.
    boot_pack = build_boot_pack(pdir, workflow_path)
    prompt_path = sdir / STEWARD_PROMPT_FILE
    prompt_path.write_text(boot_pack, encoding="utf-8")

    name = f"{name_prefix}-{_short_id()}"
    runner = camc_runner or _default_camc_runner

    try:
        agent_id = runner(name, pdir, boot_pack)
    except Exception as exc:
        # Clean the orphan prompt so the next attempt isn't confused.
        sys.stderr.write(f"steward spawn failed: {exc}\n")
        raise

    # Registry + paired trace event in lockstep.
    on_agent_spawned(
        pdir,
        role="steward",
        agent_id=agent_id,
        spawned_by=spawned_by,
        flow_id=None,  # steward is project-scoped, not flow-scoped
        prompt_file=str(prompt_path),
        extra={
            "name": name,
            "memory_files": [
                str(sdir / STEWARD_SUMMARY_FILE),
                str(sdir / STEWARD_ARCHIVE_FILE),
            ],
            "session_log": str(sdir / STEWARD_HISTORY_FILE),
            "flows_witnessed": [],
        },
    )
    set_current_steward(pdir, agent_id)

    _write_steward_pointer(
        pdir,
        {
            "agent_id": agent_id,
            "name": name,
            "spawned_at": _now_iso(),
            "spawned_by": spawned_by,
            "prompt_file": str(prompt_path),
            "summary_path": str(sdir / STEWARD_SUMMARY_FILE),
            "archive_path": str(sdir / STEWARD_ARCHIVE_FILE),
        },
    )

    return agent_id
