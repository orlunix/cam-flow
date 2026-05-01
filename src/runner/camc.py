"""Thin Pythonic wrapper around the `camc` CLI.

Encapsulates every subprocess call to camc so the rest of the runtime
can think in terms of "spawn an agent, wait for its output, kill it"
rather than "run camc with these flags, regex-parse stdout, poll a
file, run camc kill".

Why this lives in its own module:
- camc is the *only* way camflow talks to LLM agents (no claude -p,
  no Anthropic SDK). Every camc detail goes through here.
- Keeps the orchestration loop clean — main_loop / executors call
  high-level methods like `run_and_collect`; subprocess noise is
  contained.
- Easy to mock for tests: monkey-patch `camc.run_and_collect` to
  return a fake envelope.

Public API:
- `spawn(prompt, workspace, name, tag) -> agent_id`
- `wait_for_file(workspace, filename, timeout_s) -> Path`
- `send(agent_id, text)`
- `kill(agent_id, archive=True)`
- `kill_by_tag(tag) -> int_killed`
- `status(agent_id) -> dict`
- `run_and_collect(...) -> envelope_dict`  ← the composite, what
  skill.X / agent.X executors actually call.

Errors raise `CamcError` (or subclass `CamcTimeout`); callers turn
them into envelope failures with appropriate codes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


# ─── Configuration ─────────────────────────────────────────────────────

# Default timeouts, overridable via env vars for cheap test runs.
DEFAULT_SKILL_TIMEOUT_S = int(os.environ.get("CAMFLOW_SKILL_TIMEOUT", "600"))
DEFAULT_AGENT_TIMEOUT_S = int(os.environ.get("CAMFLOW_AGENT_TIMEOUT", "1800"))
POLL_INTERVAL_S = float(os.environ.get("CAMFLOW_SKILL_POLL_INTERVAL", "2"))

# camc's `run` subcommand prints `Starting <tool> agent <hex>` on stdout.
# Until camc supports `run --json`, we regex this to pull the agent ID.
_AGENT_ID_RE = re.compile(r"^Starting [a-z]+ agent ([0-9a-f]{6,})", re.MULTILINE)


# ─── Errors ────────────────────────────────────────────────────────────

class CamcError(Exception):
    """Raised when a camc CLI invocation fails (spawn / kill / status / etc)."""


class CamcTimeout(CamcError):
    """Raised when waiting for an agent's output exceeds timeout."""


# ─── Single-step primitives ────────────────────────────────────────────

def spawn(prompt: str, workspace: Path, name: str, tag: str) -> str:
    """`camc run` → return agent_id.

    Raises CamcError if camc exits non-zero or stdout doesn't carry the
    expected `Starting <tool> agent <id>` line.
    """
    proc = subprocess.run(
        ["camc", "run",
         "--path", str(workspace),
         "--name", name,
         "--tag", tag,
         prompt],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise CamcError(
            f"camc run exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    m = _AGENT_ID_RE.search(proc.stdout)
    if not m:
        raise CamcError(
            f"could not parse agent ID from camc run output:\n{proc.stdout[:500]}"
        )
    return m.group(1)


def status(agent_id: str) -> dict:
    """`camc --json status <id>` → dict. Best-effort: returns {} on any
    failure rather than raising (used for opportunistic metric pull)."""
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


def kill(agent_id: str, *, archive: bool = True) -> None:
    """Tear down agent.

    archive=True (default) → `camc rm --archive <id>`: kills tmux,
                              archives conversation history, removes DB
                              record. The recommended path.
    archive=False           → `camc kill <id>`: kills tmux, leaves DB
                              record as status=killed (legacy).
    """
    if archive:
        cmd = ["camc", "rm", "--archive", agent_id]
    else:
        cmd = ["camc", "kill", agent_id]
    subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def send(agent_id: str, text: str) -> None:
    """`camc send <id> --text TEXT` (push input into the running agent)."""
    subprocess.run(["camc", "send", agent_id, "--text", text],
                   capture_output=True, text=True, timeout=10)


def kill_by_tag(tag: str) -> int:
    """List agents → filter by `task.tags` containing `tag` → kill each.

    Used as a crash-safety net: the runtime tags every agent with
    `camflow:<run_id>`, so on abnormal exit (atexit / SIGTERM / OOM)
    a single call here cleans up every agent that run started.

    Best-effort. Never raises; returns count successfully killed.
    """
    killed = 0
    try:
        proc = subprocess.run(
            ["camc", "--json", "ls"],
            capture_output=True, text=True, timeout=10,
        )
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
                kill(aid, archive=False)  # safety-net path: speed > archiving
                killed += 1
    except Exception:
        pass
    return killed


# ─── File-watch primitive ──────────────────────────────────────────────

def wait_for_file(workspace: Path, filename: str, timeout_s: int,
                  *, since_mtime: float | None = None) -> Path:
    """Poll until <workspace>/<filename> exists, has fresh mtime, and
    parses as JSON. Returns the Path.

    `since_mtime` lets the caller wait for an updated file (e.g., after
    `send()` injects feedback and we want to see a new envelope, not the
    stale one).

    Raises CamcTimeout after `timeout_s`.
    """
    output_path = workspace / filename
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if output_path.exists():
            try:
                mt = output_path.stat().st_mtime
            except OSError:
                mt = None
            if mt is not None and (since_mtime is None or mt > since_mtime):
                try:
                    json.loads(output_path.read_text())
                    return output_path
                except json.JSONDecodeError:
                    pass  # still being written; keep polling
        time.sleep(POLL_INTERVAL_S)
    raise CamcTimeout(
        f"timed out waiting for {output_path.name} after {timeout_s}s"
    )


# ─── The composite — what executors actually call ──────────────────────

def run_and_collect(prompt: str, workspace: Path, name: str, tag: str,
                    *,
                    output_file: str = "agent_output.json",
                    timeout_s: int = DEFAULT_SKILL_TIMEOUT_S,
                    write_id_to: Path | None = None) -> tuple[str, dict]:
    """Spawn → wait → parse envelope → kill (always). Returns (agent_id, envelope).

    The envelope is the raw dict the agent wrote to `output_file`. The
    caller is responsible for status validation, schema check, and
    deciding what to do with metrics — but it gets a clean dict + the
    agent_id (for trace correlation) and never has to remember to kill.

    `write_id_to`, if given, gets the agent_id written to it before the
    wait — lets callers persist `agent.id` in the workspace for debug.

    Raises:
      CamcError on spawn failure or any other camc invocation problem.
      CamcTimeout if the output file doesn't appear within timeout_s.
    """
    agent_id = spawn(prompt, workspace, name, tag)
    if write_id_to is not None:
        write_id_to.write_text(agent_id)
    try:
        out_path = wait_for_file(workspace, output_file, timeout_s)
        envelope = json.loads(out_path.read_text())
        # Opportunistic metric enrichment
        s = status(agent_id)
        metrics = dict(envelope.get("metrics") or {})
        if cost := s.get("cost_estimate"):
            metrics["camc_cost_usd"] = cost
        if started := s.get("started_at"):
            metrics["camc_started_at"] = started
        if metrics:
            envelope["metrics"] = metrics
        return agent_id, envelope
    finally:
        kill(agent_id, archive=True)
