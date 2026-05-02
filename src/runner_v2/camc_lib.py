"""Thin Pythonic wrapper around the `camc` CLI.

camc is the only path camflow uses to invoke LLMs (no claude -p, no
SDK). Every camc detail goes through here; the rest of the runtime
just calls `run_and_collect()` and gets back an envelope dict.

Public API:
- spawn(prompt, workspace, name, tag) -> agent_id
- send(agent_id, text)
- kill(agent_id, archive=True)
- kill_by_tag(tag) -> int_killed
- status(agent_id) -> dict
- wait_for_file(workspace, filename, timeout_s) -> Path
- run_and_collect(...) -> (agent_id, envelope_dict)   ← composite

Errors raise CamcError (or subclass CamcTimeout); callers turn them
into envelope failures with appropriate error codes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


# ─── Configuration ─────────────────────────────────────────────────────

# By default, no timeout — runtime waits indefinitely for the sub-agent's
# output file. Real engineering tasks (smake builds, model training, P4
# syncs) routinely run longer than any sane fixed cap. If the agent is
# truly stuck, the user can `kill $(cat .camflow/run/runner.pid)`. If you
# want a hard ceiling for an automated/CI run, set CAMFLOW_SKILL_TIMEOUT.
#
# Env var values:
#   CAMFLOW_SKILL_TIMEOUT=N (positive int)  → wait at most N seconds
#   CAMFLOW_SKILL_TIMEOUT unset / "0" / "" → wait forever (default)
def _parse_timeout_env(name: str) -> int | None:
    v = (os.environ.get(name) or "").strip()
    if not v or v == "0":
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except ValueError:
        return None

DEFAULT_SKILL_TIMEOUT_S = _parse_timeout_env("CAMFLOW_SKILL_TIMEOUT")
POLL_INTERVAL_S = float(os.environ.get("CAMFLOW_SKILL_POLL_INTERVAL", "2"))

# camc's `run` prints "Starting <tool> agent <hex>"; we regex it out
# until camc supports `run --json`.
_AGENT_ID_RE = re.compile(r"^Starting [a-z]+ agent ([0-9a-f]{6,})", re.MULTILINE)


# ─── Errors ────────────────────────────────────────────────────────────

class CamcError(Exception):
    """Any camc CLI invocation failure (spawn / kill / status / etc)."""


class CamcTimeout(CamcError):
    """Wait for agent output exceeded the timeout."""


# ─── Single-step primitives ────────────────────────────────────────────

def spawn(prompt: str, workspace: Path, name: str, tag: str) -> str:
    """`camc run` → return agent_id. Raises CamcError on any failure."""
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
    """`camc --json status <id>` → dict. Best-effort: returns {} on failure
    (used for opportunistic metric pull, never raises)."""
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
                              archives conversation history, removes
                              DB record. The recommended path.
    archive=False           → `camc kill <id>`: kills tmux only,
                              leaves DB record. Faster; used by the
                              crash-safety net.
    """
    cmd = (["camc", "rm", "--archive", agent_id] if archive
           else ["camc", "kill", agent_id])
    subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def send(agent_id: str, text: str) -> None:
    """`camc send <id> --text TEXT` (push input into a running agent)."""
    subprocess.run(["camc", "send", agent_id, "--text", text],
                   capture_output=True, text=True, timeout=10)


def kill_by_tag(tag: str) -> int:
    """List agents → filter by `task.tags` containing `tag` → kill each.

    Crash-safety net: every agent the runtime spawns is tagged with the
    workflow's run tag, so on abnormal exit (atexit / SIGTERM / OOM) a
    single call here cleans up every agent that run started.

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
                kill(aid, archive=False)  # safety net: speed > archiving
                killed += 1
    except Exception:
        pass
    return killed


# ─── File-watch primitive ──────────────────────────────────────────────

def wait_for_file(workspace: Path, filename: str,
                  timeout_s: int | None = None,
                  *, since_mtime: float | None = None) -> Path:
    """Poll until <workspace>/<filename> exists, has fresh mtime, and
    parses as JSON. Returns the Path.

    timeout_s=None (default) → wait forever. The caller (or user) is
    responsible for killing the runner if the agent truly hangs.
    timeout_s>0 → raise CamcTimeout after that many seconds.
    """
    output_path = workspace / filename
    deadline = (time.monotonic() + timeout_s) if timeout_s else None
    while True:
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
                    pass
        if deadline is not None and time.monotonic() >= deadline:
            raise CamcTimeout(
                f"timed out waiting for {output_path.name} after {timeout_s}s"
            )
        time.sleep(POLL_INTERVAL_S)


# ─── The composite — what runtime actually calls ──────────────────────

def run_and_collect(prompt: str, workspace: Path, name: str, tag: str,
                    *,
                    output_file: str = "agent_output.json",
                    timeout_s: int | None = DEFAULT_SKILL_TIMEOUT_S,
                    write_id_to: Path | None = None) -> tuple[str, dict]:
    """Spawn → wait → parse envelope → kill (always). Returns (agent_id, envelope).

    The envelope is the raw dict the agent wrote to `output_file`. The
    caller is responsible for status validation, schema check, and
    deciding what to do with metrics — but it gets a clean dict + the
    agent_id (for trace correlation) and never has to remember to kill.

    `write_id_to`, if given, gets the agent_id written to it before the
    wait — lets callers persist `agent.id` in the workspace for debug.

    Raises:
      CamcError on spawn failure or other camc invocation problem.
      CamcTimeout if the output file doesn't appear within timeout_s.
    """
    agent_id = spawn(prompt, workspace, name, tag)
    if write_id_to is not None:
        write_id_to.write_text(agent_id)
    try:
        out_path = wait_for_file(workspace, output_file, timeout_s)
        envelope = json.loads(out_path.read_text())
        return agent_id, envelope
    finally:
        kill(agent_id, archive=True)
