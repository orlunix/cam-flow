"""CAM Phase workflow engine — main execution loop.

Responsibilities (from docs/cam-phase-plan.md):
  - Load workflow.yaml, initial state
  - Execute nodes via cmd_runner or agent_runner
  - Classify errors; retry transient with same prompt, task with context prompt
  - Inject lessons + last_failure into prompts
  - Accumulate lessons (dedup + FIFO prune) from agent new_lesson
  - Track current_agent_id in state for orphan recovery on resume
  - Atomic state saves, fsync'd trace appends
  - Per-node and per-workflow timeouts
  - Loop detection via max_node_executions
  - Signal handlers for clean shutdown
  - Dry-run (static walk), progress reporting
"""

import copy
import os
import secrets
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field

from camflow.backend.cam.agent_runner import run_agent, RESULT_FILE
from camflow.backend.cam.brainstorm import (
    build_brainstorm_prompt,
    collect_failure_summaries,
)
from camflow.backend.cam.cmd_runner import run_cmd
from camflow.backend.cam.orphan_handler import (
    ACTION_NO_ORPHAN,
    decide_orphan_action,
    handle_orphan,
)
from camflow.backend.cam.progress import format_progress_line, write_progress
from camflow.backend.cam.prompt_builder import build_prompt, build_retry_prompt
from camflow.backend.cam.tracer import approx_token_count, build_trace_entry
from camflow.backend.persistence import (
    append_trace_atomic,
    load_state,
    save_state_atomic,
)
from camflow.engine.checkpoint import checkpoint_after_success
from camflow.engine.dsl import classify_do, load_workflow, validate_workflow
from camflow.engine.error_classifier import classify_error, retry_mode
from camflow.engine.escalation import get_escalation_level
from camflow.engine.methodology_router import select_methodology_label
from camflow.engine.monitor import (
    DEFAULT_HEARTBEAT_INTERVAL,
    EngineLock,
    HeartbeatThread,
    heartbeat_path,
    is_process_alive,
    is_stale,
    load_heartbeat,
)
from camflow.engine.state import apply_updates, init_state
from camflow.engine.state_enricher import enrich_state, init_structured_fields
from camflow.engine.transition import resolve_next
from camflow.registry import on_agent_finalized, on_agent_spawned


STATE_FILENAME = "state.json"
TRACE_FILENAME = "trace.log"
ENGINE_LOG_FILENAME = "engine.log"

# Terminal-but-resumable statuses: a prior run left state in one of these
# and the user re-invoked `camflow run` — treat that as an implicit resume.
RESUMABLE_TERMINAL_STATUSES = ("failed", "interrupted", "engine_error", "aborted")


@dataclass
class EngineConfig:
    poll_interval: int = 5
    node_timeout: int = 600
    workflow_timeout: int = 3600
    max_retries: int = 3
    max_node_executions: int = 10
    dry_run: bool = False
    force_restart: bool = False
    state_filename: str = STATE_FILENAME
    trace_filename: str = TRACE_FILENAME
    heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL
    # `reset=True` means "fresh start": wipe prior state.json and
    # heartbeat.json before running. The `camflow run` subcommand sets
    # this; `camflow resume` leaves it False. Distinguishing the two
    # here (instead of at the CLI) keeps the lock acquisition and the
    # wipe atomic — no other engine can sneak in between them.
    reset: bool = False


# ---- Helpers --------------------------------------------------------------


def _init_runtime_state(state):
    """Ensure runtime bookkeeping fields exist in state.

    Also initializes the six-section structured fields via
    init_structured_fields so enrich_state can work against a consistent
    shape from step one.

    ``flow_id`` is generated once per fresh run; ``camflow resume``
    preserves it (state is loaded, not re-init'd) so registry and trace
    correlations survive engine restarts.
    """
    state.setdefault("node_execution_count", {})
    state.setdefault("current_agent_id", None)
    state.setdefault("flow_id", f"flow_{secrets.token_hex(4)}")
    init_structured_fields(state)
    return state


def _classify_cmd_error(result):
    """Build an error-classifier-compatible error dict from a cmd result."""
    if result.get("status") != "fail":
        return None
    err = result.get("error")
    if err and isinstance(err, dict) and err.get("code"):
        return err
    return {"code": "CMD_FAIL", "retryable": True, "reason": result.get("summary", "")}


def _classify_agent_error(result):
    """Build error dict from an agent result."""
    if result.get("status") != "fail":
        return None
    err = result.get("error")
    if err and isinstance(err, dict) and err.get("code"):
        return err
    return {"code": "NODE_FAIL", "retryable": True, "reason": result.get("summary", "")}


# ---- Engine ---------------------------------------------------------------


class Engine:
    def __init__(self, workflow_path, project_dir, config=None):
        self.workflow_path = workflow_path
        self.project_dir = project_dir
        self.config = config or EngineConfig()

        self.state_path = os.path.join(project_dir, ".camflow", self.config.state_filename)
        self.trace_path = os.path.join(project_dir, ".camflow", self.config.trace_filename)
        self.engine_log_path = os.path.join(project_dir, ".camflow", ENGINE_LOG_FILENAME)

        self.workflow = None
        self.state = None
        self.step = 0
        self.workflow_started_at = None
        self._interrupted = False
        self._last_prompt = None  # last prompt sent to an agent, for token-counting
        self._lock = None
        self._heartbeat = None

    # ---- setup -----------------------------------------------------------

    def _install_signal_handlers(self):
        def handler(signum, frame):
            self._interrupted = True
            # We don't try to do fancy cleanup in the signal handler itself —
            # we set a flag and let the main loop exit cleanly after the
            # current node finishes (or after the poll cycle wakes).
            sys.stderr.write(f"\n[signal {signum}] interrupt requested; finishing current node...\n")

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):
            # Might be running in a non-main thread; ignore
            pass

    def _log_engine_error(self, message, exc=None):
        os.makedirs(os.path.dirname(self.engine_log_path), exist_ok=True)
        with open(self.engine_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
            if exc is not None:
                f.write(traceback.format_exc())
                f.write("\n")

    def _load_workflow(self):
        self.workflow = load_workflow(self.workflow_path)
        valid, errors = validate_workflow(self.workflow)
        if not valid:
            raise ValueError(
                "workflow validation failed:\n  - " + "\n  - ".join(errors)
            )

    def _load_or_init_state(self):
        state = load_state(self.state_path)
        if state is None:
            # Use whichever node is declared FIRST in the workflow; fall
            # back to "start" only if the workflow is somehow empty (the
            # DSL validator will already have caught that).
            first_node = (
                next(iter(self.workflow))
                if self.workflow
                else "start"
            )
            state = init_state(first_node)
        self.state = _init_runtime_state(state)

    def _wipe_state_files(self):
        """Remove state.json and heartbeat.json for a clean ``run``.

        Callers must hold ``self._lock`` — otherwise we could destroy
        the live state of a concurrent engine. Leaves trace.log alone
        on purpose: traces accumulate across runs by design so the
        evaluator has long-term history.
        """
        for path in (self.state_path, heartbeat_path(self.project_dir)):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                # Not worth failing the run over — the next save will
                # overwrite anyway. Log and move on.
                pass

    def _save_state(self):
        save_state_atomic(self.state_path, self.state)

    def _append_trace(self, entry):
        append_trace_atomic(self.trace_path, entry)

    # ---- dry-run ---------------------------------------------------------

    def dry_run(self):
        """Static walk of the workflow, happy path (all nodes succeed)."""
        print(f"Dry-run for {self.workflow_path}")
        print("=" * 60)

        visited = []
        pc = "start"
        safety = self.config.max_node_executions * len(self.workflow)

        while pc and safety > 0:
            safety -= 1
            if pc not in self.workflow:
                print(f"ERROR: node '{pc}' referenced but not defined")
                return 1
            if pc in visited and visited.count(pc) >= self.config.max_node_executions:
                print(f"... (node '{pc}' reached max executions; stopping walk)")
                break
            visited.append(pc)
            node = self.workflow[pc]
            print(f"  → {pc:20s}  do: {node.get('do', '?')}")

            # Follow happy-path transition
            fake_result = {"status": "success", "output": {}}
            trans = resolve_next(pc, node, fake_result, self.state or {})
            pc = trans.get("next_pc")

        print("=" * 60)
        print(f"Walked {len(visited)} node(s). Terminal pc = {pc!r}.")
        return 0

    # ---- crash recovery --------------------------------------------------

    def _check_and_recover(self):
        """Make ``camflow run`` idempotent by detecting prior-run state.

        Three paths matter for a re-invocation:

          * status == "done" — workflow already completed, nothing to do.
            Returns the string "already_done" so ``run()`` can short-circuit.
          * status == "running" with a stale heartbeat + dead pid —
            the previous engine crashed mid-node; stay at the current pc
            and continue. The orphan handler (already called later in
            ``run()``) will reap any dangling agent.
          * status in RESUMABLE_TERMINAL_STATUSES — previous run failed;
            flip back to "running" and reset budgets for the current pc
            so the engine gets a fresh attempt. Matches the semantics of
            ``camflow resume --retry`` but without the user having to ask.

        Any other status (waiting, etc.) is left alone — the existing
        "status is not running, nothing to do" guard handles it.
        """
        status = self.state.get("status")

        if status == "done":
            return "already_done"

        if status == "running":
            hb = load_heartbeat(heartbeat_path(self.project_dir))
            if hb is None:
                return None
            if is_stale(hb) and not is_process_alive(hb.get("pid")):
                pid = hb.get("pid")
                print(
                    f"[recover] previous engine (pid {pid}) died mid-run; "
                    f"resuming from node {self.state.get('pc')!r}"
                )
                return "resumed_crash"
            # Heartbeat fresh or pid alive — another engine is genuinely
            # running. The lock acquisition step will reject us.
            return None

        if status in RESUMABLE_TERMINAL_STATUSES:
            pc = self.state.get("pc")
            print(
                f"[recover] auto-resuming from {pc!r} "
                f"(previous status: {status})"
            )
            self.state["status"] = "running"
            self.state.pop("error", None)
            rc = self.state.setdefault("retry_counts", {})
            if pc in rc:
                rc[pc] = 0
            ne = self.state.setdefault("node_execution_count", {})
            if pc in ne:
                ne[pc] = 0
            self._save_state()
            return "resumed_failed"

        return None

    # ---- main loop -------------------------------------------------------

    def run(self):
        self._install_signal_handlers()
        self._load_workflow()

        if self.config.dry_run:
            self._load_or_init_state()
            return self.dry_run()

        # `camflow run` path: acquire lock BEFORE wiping state files so
        # we cannot clobber another live engine's state.json. The CLI
        # sets reset=True for `run`/default invocations; `camflow resume`
        # leaves it False so we take the auto-recovery path below.
        if self.config.reset:
            self._lock = EngineLock(self.project_dir)
            self._lock.acquire()
            self._wipe_state_files()
            self._load_or_init_state()
        else:
            self._load_or_init_state()
            # Crash recovery BEFORE we check status — may flip terminal-
            # but-resumable statuses back to "running" or short-circuit
            # on "done".
            recovery = self._check_and_recover()
            if recovery == "already_done":
                print(
                    f"Workflow already completed (last_pc={self.state.get('pc')!r}). "
                    "Use `camflow run <workflow>` to start over, or "
                    "`camflow resume --from <node>` to re-run from a "
                    "specific node."
                )
                return self.state

            if self.state.get("status") != "running":
                print(
                    f"Workflow status is '{self.state.get('status')}', "
                    "not running. Nothing to do."
                )
                return self.state

            # Acquire the engine lock before spawning any agents. If
            # another engine already holds it, EngineLockError bubbles
            # up to the CLI which prints it and exits non-zero.
            self._lock = EngineLock(self.project_dir)
            self._lock.acquire()

        # Start heartbeat thread AFTER lock acquisition so a rejected
        # run never overwrites the live engine's heartbeat file.
        self._heartbeat = HeartbeatThread(
            self.project_dir,
            state_getter=lambda: self.state,
            interval=self.config.heartbeat_interval,
            workflow_path=os.path.abspath(self.workflow_path),
        )
        self._heartbeat.start()

        self.workflow_started_at = time.time()

        try:
            # Handle orphan agent BEFORE entering the main loop
            try:
                self._handle_orphan_on_start()
            except Exception as e:
                self._log_engine_error("orphan handler failed", e)
                self.state["status"] = "engine_error"
                self.state["error"] = {"message": str(e)}
                self._save_state()
                raise

            # Main loop
            try:
                while self.state.get("status") == "running":
                    if self._interrupted:
                        self.state["status"] = "interrupted"
                        self._save_state()
                        break

                    if self._workflow_timed_out():
                        self.state["status"] = "failed"
                        self.state["error"] = {"code": "WORKFLOW_TIMEOUT"}
                        self._save_state()
                        break

                    if not self._execute_step():
                        break
            except Exception as e:
                self._log_engine_error("main loop crashed", e)
                self.state["status"] = "engine_error"
                self.state["error"] = {"engine_exception": str(e)}
                self._save_state()
                raise
        finally:
            # ALWAYS clean up agents on exit — success, failure, signal,
            # uncaught exception, anything. Two passes: (1) explicitly
            # stop+rm the current agent if state still names one; (2)
            # belt-and-suspenders, sweep the camc registry for any
            # camflow-* leftovers.
            self._cleanup_on_exit()
            # Stop heartbeat + release lock LAST so concurrent `camflow
            # status` invocations see an accurate "engine alive until
            # cleanup finished" story.
            try:
                if self._heartbeat is not None:
                    self._heartbeat.stop()
            except Exception:
                pass
            try:
                if self._lock is not None:
                    self._lock.release()
            except Exception:
                pass

        print()
        print("=" * 60)
        print(f"Workflow finished: status={self.state.get('status')}, last_pc={self.state.get('pc')}")
        print("=" * 60)
        return self.state

    def _cleanup_on_exit(self):
        """Last-resort agent cleanup. Best-effort; never raises."""
        from camflow.backend.cam.agent_runner import (
            _cleanup_agent,
            cleanup_all_camflow_agents,
        )

        try:
            current = (self.state or {}).get("current_agent_id")
            if current:
                _cleanup_agent(current)
                self.state["current_agent_id"] = None
                try:
                    self._save_state()
                except Exception:
                    pass
        except Exception:
            pass

        # Belt and suspenders: any camflow-* agent in the registry,
        # remove it. If the engine is sharing the host with another
        # engine instance this is overly aggressive — but the alternative
        # (leaks) has been worse in practice (6 dead agents observed).
        try:
            cleanup_all_camflow_agents()
        except Exception:
            pass

    def _workflow_timed_out(self):
        if not self.workflow_started_at:
            return False
        return (time.time() - self.workflow_started_at) > self.config.workflow_timeout

    def _handle_orphan_on_start(self):
        if self.config.force_restart:
            if self.state.get("current_agent_id"):
                print(f"[orphan] --force-restart: discarding agent {self.state['current_agent_id']}")
                self.state["current_agent_id"] = None
                self._save_state()
            return

        action, agent_id = decide_orphan_action(self.state, self.project_dir)
        if action == ACTION_NO_ORPHAN:
            return

        print(f"[orphan] detected action={action} agent_id={agent_id}")
        node_id = self.state["pc"]
        node = self.workflow.get(node_id)
        if node is None:
            self.state["status"] = "failed"
            self.state["error"] = {"code": "ORPHAN_NODE_MISSING", "pc": node_id}
            self._save_state()
            return

        ts_start = time.time()
        result, completion_signal = handle_orphan(
            action, agent_id, self.project_dir,
            self.config.node_timeout, self.config.poll_interval,
        )
        ts_end = time.time()

        self._apply_result_and_transition(
            node_id, node, result,
            ts_start=ts_start, ts_end=ts_end,
            agent_id=agent_id,
            exec_mode="camc",
            completion_signal=completion_signal,
            event=f"orphan_{action}",
            attempt=self.state["retry_counts"].get(node_id, 0) + 1,
        )

    def _execute_step(self):
        """Execute one workflow step. Returns False to break the loop."""
        node_id = self.state["pc"]
        node = self.workflow.get(node_id)
        if node is None:
            print(f"ERROR: node '{node_id}' not found in workflow")
            self.state["status"] = "failed"
            self.state["error"] = {"code": "NODE_NOT_FOUND", "pc": node_id}
            self._save_state()
            return False

        # Loop detection. First offence → one rescue brainstorm. Second
        # offence on the same node → give up. ``brainstorm_done_for`` is
        # a list of node_ids that have already consumed their rescue.
        exec_count = self.state["node_execution_count"].get(node_id, 0) + 1
        if exec_count > self.config.max_node_executions:
            done_list = self.state.get("brainstorm_done_for") or []
            if node_id in done_list:
                print(
                    f"ERROR: node '{node_id}' exceeded max_node_executions "
                    f"({self.config.max_node_executions}) after brainstorm rescue"
                )
                self.state["status"] = "failed"
                self.state["error"] = {
                    "code": "LOOP_DETECTED_POST_BRAINSTORM",
                    "node": node_id,
                    "count": exec_count,
                }
                self._save_state()
                return False

            if self._trigger_brainstorm(node_id, node, exec_count):
                # exec_count reset inside _trigger_brainstorm; continue.
                return True

            print(
                f"ERROR: brainstorm for '{node_id}' failed; workflow halting"
            )
            self.state["status"] = "failed"
            self.state["error"] = {
                "code": "BRAINSTORM_FAILED",
                "node": node_id,
                "count": exec_count,
            }
            self._save_state()
            return False
        self.state["node_execution_count"][node_id] = exec_count

        self.step += 1
        attempt = self.state["retry_counts"].get(node_id, 0) + 1
        is_retry = attempt > 1

        node_started_at = time.time()
        write_progress(
            self.project_dir, self.step, node_id, exec_count,
            attempt, self.config.max_retries,
            node_started_at, self.workflow_started_at,
        )
        print()
        print(format_progress_line(
            self.step, node_id, exec_count, attempt,
            self.config.max_retries, _infer_exec_mode(node), 0,
        ))

        ts_start = time.time()
        result, agent_id, completion_signal = self._run_node(
            node_id, node, attempt=attempt, is_retry=is_retry,
        )
        ts_end = time.time()

        return self._apply_result_and_transition(
            node_id, node, result,
            ts_start=ts_start, ts_end=ts_end,
            agent_id=agent_id,
            exec_mode=_infer_exec_mode(node),
            completion_signal=completion_signal,
            event=None,
            attempt=attempt,
        )

    def _run_node(self, node_id, node, attempt, is_retry):
        """Dispatch to cmd_runner or agent_runner. Returns (result, agent_id, signal)."""
        do = node.get("do", "")
        per_node_timeout = node.get("timeout", self.config.node_timeout)

        # DSL v2: preflight gate (cheap, ~seconds) runs before the
        # (potentially expensive) node body. On non-zero exit the body
        # is skipped entirely and the node fails as PREFLIGHT_FAIL.
        preflight_result = self._run_preflight(node)
        if preflight_result is not None:
            self._last_prompt = None
            return (preflight_result, None, "preflight_fail")

        kind, body = classify_do(do)

        if kind == "invalid":
            self._last_prompt = None
            return (
                {
                    "status": "fail",
                    "summary": f"invalid 'do' field: {body}",
                    "state_updates": {},
                    "error": {"code": "INVALID_DO", "do": do, "reason": body},
                },
                None,
                "invalid_do",
            )

        if kind == "shell":
            from camflow.engine.input_ref import resolve_refs
            command = resolve_refs(body, self.state)
            print(f"  shell: {command}")
            self._last_prompt = None
            result = run_cmd(command, self.project_dir, timeout=per_node_timeout)
            return (result, None, None)

        # agent / skill / inline all go through camc in CAM phase.
        agent_def = None
        inline_task = None
        if kind == "agent":
            agent_def = self._resolve_agent_def(body)
        elif kind == "skill":
            # Run the skill inside the current agent session.
            original_task = node.get("with", "") or ""
            inline_task = (
                f"Invoke the skill named '{body}' and follow its instructions. "
                + original_task
            )
        elif kind == "inline":
            # `do` is the full free-text prompt; no `with` expected.
            inline_task = body

        if is_retry:
            prev_result = self.state.get("last_failure") or {}
            prev_summary = prev_result.get("summary")
            prompt = build_retry_prompt(
                node_id, node, self.state, attempt,
                max_attempts=self.config.max_retries,
                previous_summary=prev_summary,
                agent_def=agent_def,
                inline_task=inline_task,
            )
        else:
            prompt = build_prompt(
                node_id, node, self.state,
                agent_def=agent_def, inline_task=inline_task,
            )
        # Stash prompt so _finish_step can compute prompt_tokens for trace
        self._last_prompt = prompt

        # Save current_agent_id BEFORE starting so we can detect orphans
        # (agent starts inside run_agent; we update state after start_agent returns)
        # For simplicity we pre-clear; run_agent writes before polling.
        self.state["current_agent_id"] = None
        self._save_state()

        # The following call doesn't give us agent_id until after start;
        # we refactor: use low-level start/finalize for explicit state save.
        from camflow.backend.cam.agent_runner import (
            _wait_for_result,
            finalize_agent,
            kill_existing_camflow_agents,
            start_agent,
        )

        # Defense in depth: kill any lingering camflow-* agent from a
        # crashed previous run so we never accumulate. Cheap idempotent op.
        kill_existing_camflow_agents()

        allowed_tools = node.get("allowed_tools") if isinstance(node, dict) else None
        try:
            agent_id = start_agent(
                node_id, prompt, self.project_dir,
                allowed_tools=allowed_tools,
            )
        except RuntimeError as e:
            return (
                {
                    "status": "fail",
                    "summary": f"failed to launch agent: {e}",
                    "state_updates": {},
                    "error": {"code": "CAMC_ERROR", "message": str(e)},
                },
                None,
                "launch_failed",
            )

        self.state["current_agent_id"] = agent_id
        self.state["current_node_started_at"] = time.time()
        self._save_state()

        # Register the worker in the project agent registry and emit
        # `agent_spawned` to trace.log. Failures here must not crash
        # the engine — registry is observability, not correctness.
        try:
            on_agent_spawned(
                self.project_dir,
                role="worker",
                agent_id=agent_id,
                spawned_by=f"engine ({self.state['flow_id']} step {self.step})",
                flow_id=self.state["flow_id"],
                node_id=node_id,
                prompt_file=os.path.join(self.project_dir, ".camflow", "node-prompt.txt"),
            )
        except Exception as e:
            self._log_engine_error(
                f"registry write failed for spawn agent_id={agent_id}", e
            )

        result_path = os.path.join(self.project_dir, RESULT_FILE)
        completion_signal, _ = _wait_for_result(
            agent_id, result_path, per_node_timeout, self.config.poll_interval,
        )
        result = finalize_agent(agent_id, completion_signal, self.project_dir)

        # Flip the registry status (alive → completed/failed) and emit
        # `agent_completed` / `agent_failed` to trace.log.
        try:
            duration_ms = None
            if self.state.get("current_node_started_at"):
                duration_ms = int(
                    (time.time() - self.state["current_node_started_at"]) * 1000
                )
            on_agent_finalized(
                self.project_dir,
                agent_id=agent_id,
                result=result,
                flow_id=self.state["flow_id"],
                duration_ms=duration_ms,
                completion_signal=completion_signal,
                result_file=os.path.join(self.project_dir, ".camflow", "node-result.json"),
            )
        except Exception as e:
            self._log_engine_error(
                f"registry write failed for finalize agent_id={agent_id}", e
            )

        # Plan-level verify: if the node declares a verify cmd and the
        # agent reported success, run the cmd and downgrade the result to
        # fail on non-zero exit. Lets workflow authors mandate a
        # programmatic proof that the agent's claimed success actually
        # holds (e.g. run pytest, lint, a schema check) without a
        # separate cmd node.
        self._apply_verify_cmd(node, result)

        self.state["current_agent_id"] = None
        self.state["current_node_started_at"] = None
        self._save_state()

        return (result, agent_id, completion_signal)

    def _run_preflight(self, node):
        """Run the node's `preflight` cmd if present. Return None on
        pass or no preflight; return a result dict (with error code
        PREFLIGHT_FAIL) if the preflight cmd exited non-zero.

        Preflight is the "can I even start?" gate that sits before
        every expensive node body. Intentionally short timeout (60 s)
        — a preflight that hangs is a broken preflight.
        """
        if not isinstance(node, dict):
            return None
        preflight = node.get("preflight")
        if not preflight:
            return None

        from camflow.engine.input_ref import resolve_refs
        resolved = resolve_refs(preflight, self.state)
        print(f"  preflight: {resolved}")
        try:
            proc = subprocess.run(
                resolved, shell=True, cwd=self.project_dir,
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                return {
                    "status": "fail",
                    "summary": f"preflight failed: {resolved}",
                    "state_updates": {},
                    "error": {
                        "code": "PREFLIGHT_FAIL",
                        "exit_code": proc.returncode,
                        "stdout": (proc.stdout or "")[-500:],
                        "stderr": (proc.stderr or "")[-200:],
                    },
                }
        except subprocess.TimeoutExpired:
            return {
                "status": "fail",
                "summary": f"preflight timed out: {resolved}",
                "state_updates": {},
                "error": {"code": "PREFLIGHT_TIMEOUT"},
            }
        except Exception as exc:
            return {
                "status": "fail",
                "summary": f"preflight error: {exc}",
                "state_updates": {},
                "error": {"code": "PREFLIGHT_ERROR", "message": str(exc)},
            }
        return None

    def _resolve_agent_def(self, name):
        """Resolve an agent name to a loaded definition.

        Every `agent <name>` form (including `agent claude`) goes
        through `load_agent_definition`. If the file doesn't exist,
        `load_agent_definition` returns None — the node runs
        anonymously. For new workflows that want an anonymous default,
        use inline prompts (`do: "<task>"`) instead of `agent claude`.
        """
        if not name:
            return None
        # Lazy import so test fixtures can monkey-patch agent_loader.
        from camflow.backend.cam.agent_loader import load_agent_definition
        try:
            return load_agent_definition(name)
        except ValueError as e:
            print(f"  warning: agent definition '{name}' malformed: {e}")
            return None

    def _apply_verify_cmd(self, node, result):
        """Run the node's `verify` cmd if present; override status on failure.

        Only runs when the agent declared success — a verify pass on an
        already-failing agent result would be meaningless. Template
        substitution ({{state.x}}) is applied to the cmd string. Verify
        runs with a short 30 s timeout since it should be a quick proof,
        not real work.
        """
        if not isinstance(node, dict):
            return
        verify_cmd = node.get("verify")
        if not verify_cmd or result.get("status") != "success":
            return

        from camflow.engine.input_ref import resolve_refs
        resolved = resolve_refs(verify_cmd, self.state)
        try:
            proc = subprocess.run(
                resolved, shell=True, cwd=self.project_dir,
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                result["status"] = "fail"
                result["summary"] = f"verify failed: {resolved}"
                result["error"] = {
                    "code": "VERIFY_FAIL",
                    "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "")[-500:],
                    "stderr": (proc.stderr or "")[-200:],
                }
        except subprocess.TimeoutExpired:
            result["status"] = "fail"
            result["summary"] = f"verify timed out: {resolved}"
            result["error"] = {"code": "VERIFY_TIMEOUT"}
        except Exception as exc:
            result["status"] = "fail"
            result["summary"] = f"verify error: {exc}"
            result["error"] = {"code": "VERIFY_ERROR", "message": str(exc)}

    def _trigger_brainstorm(self, node_id, node, exec_count):
        """One-shot rescue attempt for a node that hit max_node_executions.

        Spawns a short brainstorm agent (outside the DSL transition
        graph), parses its ``state_updates.new_strategy`` recommendation
        back into state, resets the node's exec_count + retry_count, and
        records the node in ``state.brainstorm_done_for`` so a second
        offence escalates to a real ``failed`` instead of looping
        brainstorms.

        Returns True if the caller should continue the main loop
        (rescue succeeded); False to halt the workflow.
        """
        print(
            f"\n[brainstorm] node '{node_id}' exceeded max_node_executions "
            f"({self.config.max_node_executions}) — spawning rescue agent"
        )
        failures = collect_failure_summaries(self.trace_path, node_id)
        prompt = build_brainstorm_prompt(node_id, node, failures, exec_count)
        brainstorm_id = f"brainstorm-{node_id}"

        try:
            result, _agent_id, _signal = run_agent(
                brainstorm_id, prompt, self.project_dir,
                timeout=self.config.node_timeout,
                poll_interval=self.config.poll_interval,
            )
        except Exception as exc:
            print(f"[brainstorm] spawn raised {type(exc).__name__}: {exc}")
            return False

        if result.get("status") != "success":
            summary = result.get("summary") or "(no summary)"
            print(f"[brainstorm] agent returned non-success: {summary}")
            return False

        updates = result.get("state_updates") or {}
        new_strategy = (updates.get("new_strategy") or "").strip()
        if not new_strategy:
            print("[brainstorm] agent returned no new_strategy; halting")
            return False

        done = list(self.state.get("brainstorm_done_for") or [])
        if node_id not in done:
            done.append(node_id)
        self.state["brainstorm_done_for"] = done
        self.state["new_strategy"] = new_strategy
        self.state["node_execution_count"][node_id] = 0
        retry_counts = self.state.get("retry_counts")
        if isinstance(retry_counts, dict) and node_id in retry_counts:
            retry_counts[node_id] = 0
        self._save_state()

        preview = new_strategy if len(new_strategy) <= 200 else new_strategy[:200] + "…"
        print(f"[brainstorm] recorded new_strategy: {preview}")
        print("[brainstorm] exec_count reset to 0; resuming workflow")
        return True

    def _apply_result_and_transition(self, node_id, node, result, ts_start, ts_end,
                                     agent_id, exec_mode, completion_signal, event, attempt):
        """Enrich state, apply retry logic, resolve transition.

        Stateless model: state is the source of truth between nodes. After a
        node returns, enrich_state() merges the result into the six-section
        structured state so the NEXT prompt (always a fresh agent) sees it.
        """
        input_state = copy.deepcopy(self.state)

        status = result.get("status")
        error = self._classify_error(node, result)
        mode = retry_mode(error)

        # Pre-enrichment lesson capture (for trace visibility). enrich_state
        # will do the real merge, but we want to know whether a lesson was
        # added for the trace entry.
        pre_lessons = list(self.state.get("lessons", []))

        # Capture cmd stdout/stderr as test_output for cmd nodes that failed.
        do = node.get("do", "")
        cmd_output = None
        if do.startswith("cmd ") and status == "fail":
            cmd_output = (result.get("output") or {}).get("stdout_tail")

        # MERGE the node result into structured state
        enrich_state(self.state, node_id, result, cmd_output=cmd_output)

        # Determine whether a lesson was added (for trace)
        lesson_added = None
        if len(self.state.get("lessons", [])) > len(pre_lessons):
            lesson_added = self.state["lessons"][-1]

        # Also apply any ad-hoc state_updates keys that enrich_state doesn't
        # manage (new_lesson, files_touched, resolved, next_steps, active_task
        # are all handled there — apply the remainder for backward compat).
        extra_updates = dict(result.get("state_updates") or {})
        for managed in ("new_lesson", "files_touched", "modified_files",
                        "key_files", "resolved", "next_steps", "active_task",
                        "detail", "lines"):
            extra_updates.pop(managed, None)
        apply_updates(self.state, extra_updates)

        if status == "fail":
            # Plan-level override wins over engine default, per
            # architecture Plan/Runtime boundary.
            node_max_retries = node.get("max_retries") if isinstance(node, dict) else None
            max_retries = node_max_retries if isinstance(node_max_retries, int) else self.config.max_retries
            retry_count = self.state["retry_counts"].get(node_id, 0)
            if retry_count + 1 < max_retries:
                # Budget not exhausted — retry same node next iter.
                # enrich_state has already recorded the attempt in
                # state.failed_approaches and state.blocked.
                self.state["retry_counts"][node_id] = retry_count + 1
                transition = {
                    "workflow_status": "running",
                    "next_pc": node_id,
                    "resume_pc": None,
                    "reason": f"retry {retry_count + 1}/{max_retries} ({mode})",
                }
                self._finish_step(
                    node_id, node, result, input_state, transition,
                    ts_start, ts_end, agent_id, exec_mode,
                    completion_signal, lesson_added,
                    event=event or "retry",
                    attempt=attempt, retry_mode_val=mode,
                )
                return True  # continue
            # budget exhausted — fall through to DSL transition

        # Resolve the DSL transition for success or exhausted-retry fail
        transition = resolve_next(node_id, node, result, self.state)

        # On success: reset retry counter for this node
        if status == "success":
            self.state["retry_counts"][node_id] = 0

        next_pc = transition["next_pc"]
        workflow_status = transition["workflow_status"]

        self._finish_step(
            node_id, node, result, input_state, transition,
            ts_start, ts_end, agent_id, exec_mode,
            completion_signal, lesson_added,
            event=event,
            attempt=attempt, retry_mode_val=mode if status == "fail" else None,
        )

        self.state["pc"] = next_pc
        self.state["status"] = workflow_status
        self._save_state()

        return workflow_status == "running"

    def _finish_step(self, node_id, node, result, input_state, transition,
                     ts_start, ts_end, agent_id, exec_mode, completion_signal,
                     lesson_added, event, attempt, retry_mode_val):
        """Write trace and save state once per step."""
        # Evaluation fields (see docs/evaluation.md §2)
        allowed_tools = node.get("allowed_tools") if isinstance(node, dict) else None
        prompt_tokens = (
            approx_token_count(self._last_prompt) if self._last_prompt else None
        )
        methodology_label = select_methodology_label(node_id, node)
        escalation_level = get_escalation_level(input_state, node_id)
        tools_available = len(allowed_tools) if allowed_tools else None

        entry = build_trace_entry(
            step=self.step,
            node_id=node_id,
            node=node,
            input_state=input_state,
            node_result=result,
            output_state=self.state,
            transition=transition,
            ts_start=ts_start,
            ts_end=ts_end,
            attempt=attempt,
            is_retry=attempt > 1,
            retry_mode=retry_mode_val,
            agent_id=agent_id,
            exec_mode=exec_mode,
            completion_signal=completion_signal,
            lesson_added=lesson_added,
            event=event,
            prompt_tokens=prompt_tokens,
            tools_available=tools_available,
            context_position="first",  # HQ.1 shipped — CONTEXT is at prompt start
            enricher_enabled=True,
            fenced=True,
            methodology=methodology_label,
            escalation_level=escalation_level,
        )
        self._append_trace(entry)
        self._save_state()

        # §6.1 Checkpoint: auto-commit after successful agent nodes
        if (
            result.get("status") == "success"
            and exec_mode == "camc"
            and event != "retry"
        ):
            checkpoint_after_success(
                self.project_dir,
                node_id,
                self.step,
                result.get("summary") or "",
            )

        status_str = result.get("status", "?")
        summary = result.get("summary") or ""
        reason = transition.get("reason") if transition else ""
        print(f"  → {status_str}: {summary}")
        if reason:
            print(f"  transition: {node_id} → {transition.get('next_pc')!r} ({reason})")

    def _classify_error(self, node, result):
        if result.get("status") != "fail":
            return None
        do = node.get("do", "")
        if do.startswith("cmd "):
            return _classify_cmd_error(result)
        # agent-style
        err = result.get("error")
        if err and isinstance(err, dict) and err.get("code"):
            return err
        return _classify_agent_error(result)


def _infer_exec_mode(node):
    do = node.get("do", "")
    if do.startswith("cmd "):
        return "cmd"
    return "camc"


# ---- thin legacy wrapper -------------------------------------------------


def run(workflow_path, project_dir, max_steps=None, dry_run=False, config=None):
    """Back-compat entry point. Returns final state dict."""
    cfg = config or EngineConfig()
    if dry_run:
        cfg.dry_run = True
    if max_steps is not None:
        cfg.max_node_executions = max(cfg.max_node_executions, max_steps)
    eng = Engine(workflow_path, project_dir, cfg)
    return eng.run()
