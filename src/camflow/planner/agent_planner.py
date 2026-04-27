"""Agent-based Planner — replaces the single-shot LLM call with a
camc-spawned Claude Code session that explores the project, drafts
the workflow yaml, self-validates, iterates, and writes the final
file before exiting.

See ``docs/design-next-phase.md`` §4 for the full motivation. The
short version: a one-shot LLM Planner outsources its quality to
upstream scouts and the Manager skill's GATHER phase. When input is
thin, output is thin. An agent Planner can self-explore, self-critique
and self-validate — quality becomes the agent's own responsibility.

Lifecycle:

  1. Caller (smooth-mode driver, ``camflow plan``, or engine replan)
     calls ``generate_workflow_via_agent(...)``.
  2. We write the user's NL request to ``.camflow/plan-request.txt``,
     build the agent's boot pack, register the agent in agents.json,
     and ``camc run`` it.
  3. The agent works alone — Read/Glob/Grep + Bash (allowlisted) +
     ``camflow plan-tool validate`` and ``camflow plan-tool write``.
  4. We poll ``.camflow/workflow.yaml`` for appearance (PRIMARY signal
     of completion) and run final validation when it shows up.
  5. We mark the agent ``completed`` in the registry and ``camc rm``
     it.

Failure-tolerant:

  - Agent crashes, times out, or produces invalid yaml → returns a
    ``PlannerAgentError`` with diagnostic detail; caller decides
    whether to fall back to legacy or surface to the user.
  - All ``camc`` shell-outs are dependency-injected (``camc_runner`` /
    ``camc_status`` / ``camc_remover``) so unit tests never spawn a
    real agent.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from camflow.engine.dsl import validate_workflow as validate_dsl
from camflow.planner.validator import validate_plan_quality
from camflow.registry import (
    on_agent_finalized,
    on_agent_killed,
    on_agent_spawned,
)


CAMC_BIN = shutil.which("camc") or "camc"

# Canonical workflow / rationale paths — stay at the project's
# .camflow/ root so the engine and downstream tools always find them.
WORKFLOW_FILE = "workflow.yaml"
RATIONALE_FILE = "plan-rationale.md"

# Phase B: Planner's private files live under
# .camflow/flows/<flow_id>/planner/ (see camflow.paths). These are
# the file names inside that directory.
REQUEST_FILE = "request.txt"        # was plan-request.txt at .camflow/ root
PROMPT_FILE = "prompt.txt"          # was planner-prompt.txt at .camflow/ root
WARNINGS_FILE = "warnings.txt"      # was plan-warnings.txt

DEFAULT_TIMEOUT_SECONDS = 180  # 3 minutes — design §4.6 said up to ~2m
DEFAULT_POLL_INTERVAL = 2.0


# ---- public errors -----------------------------------------------------


class PlannerAgentError(RuntimeError):
    """Raised when the agent fails to produce a valid workflow."""


# ---- result type -------------------------------------------------------


@dataclass(frozen=True)
class PlannerResult:
    workflow: dict[str, Any]
    workflow_path: str
    rationale_path: str | None
    warnings: list[str]
    agent_id: str
    duration_s: float


# ---- helpers -----------------------------------------------------------


def _camflow_dir(project_dir: str | os.PathLike) -> Path:
    p = Path(project_dir) / ".camflow"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _short_id() -> str:
    return secrets.token_hex(4)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


_AGENT_ID_RE = re.compile(r"agent\s+([0-9a-f]{6,12})")
_AGENT_ID_RE_ALT = re.compile(r"ID:\s+([0-9a-f]{6,12})")


def _parse_agent_id(stdout: str) -> str | None:
    for pat in (_AGENT_ID_RE, _AGENT_ID_RE_ALT):
        m = pat.search(stdout)
        if m:
            return m.group(1)
    return None


# ---- boot pack ---------------------------------------------------------


_BOOT_TEMPLATE = """\
You are a Planner agent for a camflow project. Your job is to turn a
user's natural-language request into a validated camflow workflow yaml,
and to do it well enough that the engine can execute it without
hand-holding.

PROJECT
─────
{project_dir}

USER REQUEST  (verbatim from the human)
────────────
{request}

YOUR DELIVERABLES  (drop both before exiting)
─────────────────
  .camflow/{WORKFLOW_FILE}       — the workflow that will run
  .camflow/{RATIONALE_FILE}      — short notes on WHY you picked
                                   these nodes / methodologies / tools
                                   (Steward reads this when explaining
                                   decisions to the user later)

YOUR TOOLS
──────────
You have full Read / Glob / Grep / Write / Bash access. Two custom
tools are exposed via the camflow CLI:

  camflow plan-tool validate <path>
        — runs DSL + plan-quality validators on a yaml file. Prints
          {{"ok": bool, "errors": [...], "warnings": [...]}} on stdout.
          Exit 0 = clean, exit 1 = errors. Quality warnings are
          non-fatal. Call this every time you draft or revise.

  camflow plan-tool write <path>
        — atomic write yaml from stdin to a path under .camflow/.
          Refuses to write if the yaml is invalid or escapes the
          .camflow sandbox. Use this for the FINAL workflow.yaml; for
          plan-rationale.md just use the Write tool directly.

YOUR LOOP
─────────
1. UNDERSTAND. Read CLAUDE.md if present; glance at skills/ and
   ~/.claude/agents/ to learn what's available. Read docs/strategy.md
   §4 (DSL) for the grammar.

2. DRAFT. Write a candidate workflow to `.camflow/workflow-draft.yaml`.
   Pick a methodology per node (default | rca | brainstorm | reactive).
   For long-running nodes add a `verify` command. Express OUTCOMES,
   not OUTPUTS — `verify: pytest -q tests/` is right;
   `verify: "tests pass"` is not.

3. SELF-CRITIQUE.
     - Long nodes → preflight + verify present?
     - Methodology routing — was rca/brainstorm chosen for the right
       reason?
     - Are transitions exhaustive (every reachable state covered)?
     - Are tool allowlists tight?
     - Did I name OUTCOME, not OUTPUT?

4. VALIDATE.
     bash$ camflow plan-tool validate .camflow/workflow-draft.yaml
   Read the JSON result. Fix every error. Triage every warning —
   either fix it or note in plan-rationale.md why you accepted it.

5. LOOP 2-4 until validate exits 0.

6. WRITE the final workflow:
     bash$ cat .camflow/workflow-draft.yaml | \\
           camflow plan-tool write --project-dir {project_dir} \\
                                   .camflow/{WORKFLOW_FILE}

7. WRITE plan-rationale.md (use the Write tool):
     - Why this decomposition?
     - Why these methodologies?
     - Any warnings you knowingly accepted, with reasoning?
     - Any clarify-with-user nodes you inserted because the request
       was thin?

8. STOP. The orchestrator polls for workflow.yaml; once both files
   exist it will detect completion and clean you up.

CONSTRAINTS
───────────
- English only in the yaml and rationale.
- Don't write outside `.camflow/` (the write tool enforces this).
- Don't shell out to anything that mutates the user's environment
  (no `git push`, `npm install`, `pip install`, etc.). You are the
  PLANNER, not the executor.
- If the request is too thin, do NOT block on stdin asking. Insert a
  `clarify-with-user` node into the yaml that surfaces the question
  at runtime, document it in plan-rationale.md, and write the file.

EXIT CRITERIA
─────────────
You're done when validate exits 0 AND both deliverables exist on disk.
The orchestrator will clean you up; you don't need to do anything to
exit.
"""


def build_boot_pack(project_dir: str | os.PathLike, request: str) -> str:
    return _BOOT_TEMPLATE.format(
        project_dir=str(Path(project_dir).resolve()),
        request=request.strip() or "(no request provided)",
        WORKFLOW_FILE=WORKFLOW_FILE,
        RATIONALE_FILE=RATIONALE_FILE,
    )


# ---- camc transport (injectable) ---------------------------------------


def _default_camc_runner(name: str, project_dir: str, prompt: str) -> str:
    """Run ``camc run`` for the Planner. Returns the agent id parsed
    from stdout. Raises ``PlannerAgentError`` if camc fails or output
    is unparseable.

    The Planner reads its boot pack from a project-relative path
    derived from where ``generate_workflow_via_agent`` wrote it. We
    re-derive that here from a marker line embedded in the prompt
    (``# camflow-prompt-path: <relpath>``) so the runner doesn't need
    extra args — keeps the dependency-injection signature stable for
    legacy tests that pass a 3-arg runner.
    """
    rel = _extract_prompt_path_marker(prompt) or f".camflow/{PROMPT_FILE}"
    short_prompt = (
        f"Read {rel} and follow ALL instructions there exactly. "
        "Drop workflow.yaml + plan-rationale.md, then stop."
    )
    try:
        proc = subprocess.run(
            [
                CAMC_BIN, "run",
                "--name", name,
                "--path", project_dir,
                short_prompt,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise PlannerAgentError(f"camc run failed: {exc}") from exc

    if proc.returncode != 0:
        raise PlannerAgentError(
            f"camc run exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    agent_id = _parse_agent_id(proc.stdout)
    if not agent_id:
        raise PlannerAgentError(
            "could not parse agent id from camc output: "
            f"{proc.stdout[:500]}"
        )
    return agent_id


_PROMPT_PATH_MARKER_RE = re.compile(
    r"^# camflow-prompt-path:\s*(?P<rel>[\w./-]+)\s*$",
    re.MULTILINE,
)


def _extract_prompt_path_marker(prompt: str) -> str | None:
    """Pull the ``# camflow-prompt-path: <rel>`` marker out of the
    boot pack, if present."""
    m = _PROMPT_PATH_MARKER_RE.search(prompt or "")
    return m.group("rel") if m else None


def _default_camc_remover(agent_id: str) -> None:
    """Best-effort ``camc rm --kill <id>``; never raises."""
    try:
        subprocess.run(
            [CAMC_BIN, "rm", agent_id, "--kill"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _default_camc_status(agent_id: str) -> str | None:
    """Return ``camc status`` output or None. Used to detect early
    death (the agent crashed before writing workflow.yaml)."""
    try:
        proc = subprocess.run(
            [CAMC_BIN, "status", agent_id],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or "unknown"


# ---- main entry --------------------------------------------------------


def generate_workflow_via_agent(
    user_request: str,
    project_dir: str | os.PathLike,
    *,
    flow_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    camc_runner: Callable[[str, str, str], str] | None = None,
    camc_remover: Callable[[str], None] | None = None,
    camc_status: Callable[[str], str | None] | None = None,
) -> PlannerResult:
    """Spawn a Planner agent, wait for it to write the workflow, return
    the parsed workflow. Raises :class:`PlannerAgentError` on any
    failure that prevents producing a valid workflow.

    Args:
        user_request: NL task description. Mirrored to
                      ``.camflow/plan-request.txt`` so the agent reads
                      it via filesystem rather than via prompt.
        project_dir: project root (target ``.camflow/`` lives under it).
        flow_id: optional flow id for registry correlation. Planner
                 agents are conventionally project-scoped (``flow_id``
                 None) but a replan path may want to tie them to a
                 specific flow.
        timeout_seconds: how long to wait for ``workflow.yaml`` to
                         appear before giving up.
        camc_runner / camc_remover / camc_status: dependency-injected
                         transports so tests never shell out.

    Returns:
        :class:`PlannerResult` with the parsed workflow + paths +
        warnings + agent metadata.
    """
    from camflow import paths as camflow_paths

    runner = camc_runner or _default_camc_runner
    remover = camc_remover or _default_camc_remover
    status_probe = camc_status or _default_camc_status

    project_dir = str(Path(project_dir).resolve())
    cf = _camflow_dir(project_dir)

    # Phase B: Planner gets its own private directory.
    # Effective flow_id: caller-provided if any, else a synthetic
    # planner-only flow id so this Planner still gets a tidy private
    # directory even outside an engine flow (e.g. ``camflow plan``).
    effective_flow_id = flow_id or f"planner_{_short_id()}"
    planner_prompt_p = camflow_paths.planner_prompt_path(
        project_dir, effective_flow_id,
    )
    planner_request_p = camflow_paths.planner_request_path(
        project_dir, effective_flow_id,
    )

    # Mirror request to disk in the Planner's private dir so the agent
    # has a reliable copy independent of its boot prompt being truncated.
    planner_request_p.write_text(
        user_request.rstrip() + "\n", encoding="utf-8",
    )

    # Build + write the boot prompt to the private dir. Embed the
    # relative path in the prompt itself as a marker the runner can
    # parse — keeps the runner's 3-arg signature stable.
    rel_prompt = str(
        planner_prompt_p.relative_to(Path(project_dir))
    )
    boot_pack = (
        f"# camflow-prompt-path: {rel_prompt}\n\n"
        + build_boot_pack(project_dir, user_request)
    )
    planner_prompt_p.write_text(boot_pack, encoding="utf-8")

    # Pre-clear stale outputs from a prior planner run in this project,
    # so polling can't pick them up as "this run's success".
    for stale in (WORKFLOW_FILE, RATIONALE_FILE):
        try:
            (cf / stale).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        camflow_paths.planner_warnings_path(
            project_dir, effective_flow_id,
        ).unlink(missing_ok=True)
    except OSError:
        pass

    name = f"planner-{_short_id()}"
    started_at = time.time()

    try:
        agent_id = runner(name, project_dir, boot_pack)
    except PlannerAgentError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrap unexpected
        raise PlannerAgentError(f"camc runner raised: {exc}") from exc

    # Register in agents.json. Best-effort: if registry write fails
    # we still proceed — Planner success doesn't depend on observability.
    try:
        on_agent_spawned(
            project_dir,
            role="planner",
            agent_id=agent_id,
            spawned_by="generate_workflow_via_agent",
            flow_id=flow_id,
            prompt_file=str(planner_prompt_p),
            extra={
                "name": name,
                "request_file": str(planner_request_p),
                "private_dir": str(planner_prompt_p.parent),
            },
        )
    except Exception:
        pass

    workflow_path = cf / WORKFLOW_FILE
    rationale_path = cf / RATIONALE_FILE
    deadline = started_at + timeout_seconds

    try:
        workflow = _wait_for_workflow(
            agent_id,
            workflow_path,
            deadline,
            poll_interval,
            status_probe,
        )
    except PlannerAgentError as exc:
        # Mark the agent failed in the registry, then kill it.
        try:
            on_agent_killed(
                project_dir,
                agent_id=agent_id,
                killed_by="generate_workflow_via_agent",
                reason=str(exc),
                flow_id=flow_id,
                via="planner failure",
            )
        except Exception:
            pass
        try:
            remover(agent_id)
        except Exception:
            pass
        raise

    # Final validation pass — agent's plan-tool validate may have
    # passed but we re-run here as the authoritative check.
    quality_errors, quality_warnings = validate_plan_quality(workflow)
    if quality_errors:
        try:
            on_agent_killed(
                project_dir,
                agent_id=agent_id,
                killed_by="generate_workflow_via_agent",
                reason="final plan-quality validation failed",
                flow_id=flow_id,
                via="planner failure",
            )
        except Exception:
            pass
        try:
            remover(agent_id)
        except Exception:
            pass
        raise PlannerAgentError(
            "agent produced a workflow that fails plan-quality "
            "validation:\n  - " + "\n  - ".join(quality_errors)
        )

    # Persist warnings to the Planner's private dir (non-fatal but visible).
    if quality_warnings:
        try:
            camflow_paths.planner_warnings_path(
                project_dir, effective_flow_id,
            ).write_text(
                "\n".join(quality_warnings) + "\n", encoding="utf-8",
            )
        except OSError:
            pass

    # Mark planner completed in registry, then clean up the agent.
    try:
        on_agent_finalized(
            project_dir,
            agent_id=agent_id,
            result={"status": "success"},
            flow_id=flow_id,
            duration_ms=int((time.time() - started_at) * 1000),
            result_file=str(workflow_path),
        )
    except Exception:
        pass
    try:
        remover(agent_id)
    except Exception:
        pass

    return PlannerResult(
        workflow=workflow,
        workflow_path=str(workflow_path),
        rationale_path=(
            str(rationale_path) if rationale_path.exists() else None
        ),
        warnings=list(quality_warnings),
        agent_id=agent_id,
        duration_s=time.time() - started_at,
    )


# ---- polling -----------------------------------------------------------


def _wait_for_workflow(
    agent_id: str,
    workflow_path: Path,
    deadline: float,
    poll_interval: float,
    status_probe: Callable[[str], str | None],
) -> dict[str, Any]:
    """Block until workflow_path appears + parses, or the deadline
    passes, or camc reports the agent dead. Returns the parsed
    workflow on success."""
    started_at = time.time()
    last_status_check = 0.0
    status_check_interval = max(poll_interval * 5, 10.0)

    while time.time() < deadline:
        if workflow_path.exists():
            try:
                text = workflow_path.read_text(encoding="utf-8")
            except OSError:
                # Racing with agent's own write — re-poll.
                time.sleep(poll_interval)
                continue

            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise PlannerAgentError(
                    f"workflow.yaml exists but parses badly: {exc}"
                ) from exc

            if not isinstance(data, dict) or not data:
                raise PlannerAgentError(
                    "workflow.yaml exists but is empty / not a mapping"
                )

            ok, errors = validate_dsl(data)
            if not ok:
                raise PlannerAgentError(
                    "workflow.yaml exists but fails DSL validation:\n  - "
                    + "\n  - ".join(errors)
                )

            return data

        # Liveness probe is throttled — camc status itself takes time.
        now = time.time()
        if now - last_status_check >= status_check_interval:
            last_status_check = now
            stat = status_probe(agent_id)
            if stat is None:
                raise PlannerAgentError(
                    f"agent {agent_id} disappeared from camc before "
                    "writing workflow.yaml"
                )

        time.sleep(poll_interval)

    elapsed = int(time.time() - started_at)
    raise PlannerAgentError(
        f"timed out after {elapsed}s waiting for {workflow_path}"
    )
