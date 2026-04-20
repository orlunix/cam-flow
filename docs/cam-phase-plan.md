# CAM Phase Implementation Plan (v2 — revised after review)

> **HISTORICAL — superseded by [`strategy.md`](strategy.md).**
> This was the working design document during the CAM-phase build-out
> (pre-DSL v2). Specific details may have drifted as the codebase
> landed: retry modes, error classification, orphan handling, lessons
> flow. Where this document and `strategy.md` disagree,
> `strategy.md` is authoritative. Kept here for the reasoning chain
> it captures (why file-first completion detection, why dual-signal
> polling, why two retry modes) rather than as a current spec.

## 0. Context and Current State

**What exists** (`src/camflow/backend/cam/`):
- `cmd_runner.py` — subprocess execution for `cmd` nodes ✅ validated
- `prompt_builder.py` — template + output contract ✅ validated
- `result_reader.py` — reads `.camflow/node-result.json` ✅ validated
- `agent_runner.py` — `camc run` + poll + read result ✅ works with prompt-via-file
- `node_runner.py` — dispatcher by `do` prefix
- `engine.py` — basic main loop

**Validated behaviors**:
- cmd-only workflow runs end-to-end (4 steps, conditional branches correct)
- Real `camc` agent node completes and writes `node-result.json`
- **Key finding**: long multiline prompts corrupt tmux paste — fixed by writing prompt to `.camflow/node-prompt.txt` and passing short `"Read the prompt file..."` instruction to `camc run`

**Gaps this plan addresses**:
- **Lessons are never accumulated or injected** — the core value prop is missing
- **Blind retries** — retrying with same prompt fails the same way
- **Unreliable completion detection** — camc monitor state is buggy (bug #10)
- **Orphan agents not handled** — engine crash leaves agent alive in tmux
- **cmd output is lost** — fix agent is blind to which tests failed and why
- Trace is not crash-safe (non-atomic writes)
- No resume verification
- No engine-failure vs node-failure distinction
- Retry budget (`engine/retry.py` MAX_RETRY=2) not wired into the loop
- No per-node / per-workflow timeout enforcement
- No dry-run mode

**Removed from scope (v0.1)**: replay (agents are non-deterministic — no practical value).

---

## 1. Lessons Accumulation Strategy (CORE VALUE)

### 1.1 The problem

A `fix → test → fix` loop is only useful if the second `fix` agent knows what the first one tried. Without lesson accumulation, it retries the same broken approach.

### 1.2 Data model

State has one dedicated field for persistent lessons, one transient field for the last failure context:

```json
{
  "pc": "fix",
  "status": "running",
  "lessons": [                        // persistent, capped at 10
    "For division, always check zero BEFORE computing reciprocal",
    "factorial(n) requires range(1, n+1), not range(1, n)"
  ],
  "last_failure": {                   // transient, set on fail, cleared on success
    "node_id": "test",
    "summary": "3 tests failed in test_calculator.py",
    "stdout_tail": "...",             // last 2000 chars
    "stderr_tail": "...",             // last 500 chars
    "attempt_count": 2
  }
}
```

### 1.3 Lifecycle — when lessons/context flow

**Source 1: agent explicit lesson.** When an agent writes `node-result.json` with `state_updates.new_lesson = "..."`, engine:
- Appends to `state["lessons"]`
- Deduplicates (exact string match, keep first occurrence)
- If > 10 entries after append: drop oldest (FIFO)
- Logs in trace: `"lesson_added": "..."`

**Source 2: cmd failure.** When a `cmd` node fails:
- Engine captures last 2000 chars of stdout + last 500 chars of stderr
- Writes to `state["last_failure"]` (full struct above)
- Does NOT auto-add to lessons (agent decides what to learn)

**Source 3: agent task failure.** When an `agent` node returns `status=fail`:
- Engine writes `state["last_failure"]` with the agent's `summary` + any `state_updates.error`
- Does NOT auto-add to lessons

**Source 4: agent crash mid-task.** When an agent dies without writing result file:
- Engine tries `camc capture <id>` to grab last 50 lines of screen
- Writes to `state["last_failure"].stdout_tail`
- Logs in trace: `"crashed_mid_task": true`

### 1.4 Injection into prompts

`prompt_builder.py` adds a `LESSONS_AND_CONTEXT` block when `state["lessons"]` or `state["last_failure"]` is non-empty:

```
--- Previous lessons (apply these to your work) ---
1. For division, always check zero BEFORE computing reciprocal
2. factorial(n) requires range(1, n+1), not range(1, n)

--- Last failure in node 'test' (attempt 2/3) ---
3 tests failed in test_calculator.py

Test output (last 2000 chars):
============================= FAILURES =============================
test_factorial_negative:
  assert factorial(-1) raises ValueError
  AssertionError: ...

If this is a retry, try a DIFFERENT approach than your previous attempt.
```

This block is prepended to the `with` field content. Empty lessons/last_failure → block is omitted.

### 1.5 Success clearing

When a node succeeds:
- Clear `state["last_failure"]` (it was solved)
- Keep `state["lessons"]` (permanent knowledge)

### 1.6 Concrete example: first fix vs second fix prompt

**First `fix` attempt prompt** (no prior failure):
```
You are executing workflow node 'fix'.

Task:
Fix EXACTLY ONE bug in calculator.py. Error info: divide() has no zero check.

[output contract]
```

**Second `fix` attempt prompt** (after test failed):
```
You are executing workflow node 'fix'.

--- Previous lessons ---
(empty on first retry)

--- Last failure in node 'test' (attempt 2/3) ---
3 tests failed: test_factorial, test_factorial_negative, test_power_negative_exp

Test output (last 2000 chars):
FAILED test_calculator.py::test_factorial - AssertionError: factorial(5)=24 expected 120
FAILED test_calculator.py::test_factorial_negative - TypeError: ...

If this is a retry, try a DIFFERENT approach.

Task:
Fix EXACTLY ONE bug in calculator.py. Error info: divide() zero check was fixed last round.

[output contract]
```

The second agent now has specific failure evidence it can act on.

---

## 2. Retry with Context (Two Modes)

### 2.1 Failure classification

`engine/error_classifier.py` currently distinguishes `PARSE_ERROR` vs `NODE_FAIL`. Extend to produce **retry mode**:

| Error code | Retry mode | Reasoning |
|-----------|-----------|-----------|
| `PARSE_ERROR` | **transient** | Agent didn't produce valid output — infrastructure issue, retry with same prompt |
| `AGENT_TIMEOUT` | **transient** | Agent didn't respond in time — retry with same prompt |
| `AGENT_CRASH` | **transient** | Agent died — retry with same prompt |
| `CAMC_ERROR` | **transient** | camc binary failure — retry with same prompt |
| `NODE_FAIL` (agent returned status=fail) | **task** | Agent completed but logic failed — retry with context-aware prompt |
| `CMD_FAIL` (cmd exit ≠ 0) | **task** | Command failed — retry via transition (not re-execution of same cmd) |

### 2.2 `build_retry_prompt(node, state, previous_result, attempt)`

New function in `prompt_builder.py`:
- Uses `build_prompt()` as base
- Prepends an explicit "RETRY" header with the previous attempt's summary
- Relies on `state["last_failure"]` being populated (already done by engine)
- Returns a prompt that explicitly flags this is a retry

```
!!! RETRY — ATTEMPT 2 OF 3 !!!

Previous attempt summary: "Fixed divide() zero check but tests still failing"
Result: status=fail
Your previous approach did not work. Read the last_failure context below.
Try a DIFFERENT approach or fix a DIFFERENT bug.

[normal prompt with lessons + last_failure block]
```

### 2.3 Engine retry logic

```python
while state.get("status") == "running":
    node = workflow[state["pc"]]
    attempt = state["retry_counts"].get(node_id, 0) + 1
    is_retry = attempt > 1

    if is_retry:
        prompt = build_retry_prompt(node, state, previous_result, attempt)
    else:
        prompt = build_prompt(node, state)

    result = run_node(node, prompt, ...)
    error = classify_error(result)

    if result.status == "fail":
        mode = retry_mode(error)
        if attempt < max_retries:
            if mode == "transient":
                # Same prompt, bump counter
                state["retry_counts"][node_id] = attempt
                continue
            elif mode == "task":
                # Store context, bump counter, loop will build retry prompt
                state["last_failure"] = build_failure_context(result, ...)
                state["retry_counts"][node_id] = attempt
                continue
        else:
            # Budget exhausted — fall through to if fail transition
            state["last_failure"] = build_failure_context(result, ...)
    else:
        # Success — reset retry counter for this node, clear last_failure
        state["retry_counts"][node_id] = 0
        state["last_failure"] = None

    transition = resolve_next(...)
    ...
```

---

## 3. Agent Completion Detection (File-First, Status-Second)

### 3.1 Why

camc monitor has known reliability issues (bug #10 — "thinking" → reported "idle"). Sole reliance on status causes false-positive completion, which makes engine read a stale or missing `node-result.json`.

### 3.2 Dual-signal polling

`agent_runner.py` polls two signals per interval:

```python
def _wait_for_completion(agent_id, result_path, timeout, poll_interval):
    deadline = time.time() + timeout
    last_status_idle = 0

    while time.time() < deadline:
        time.sleep(poll_interval)

        # PRIMARY: did the result file appear?
        if os.path.exists(result_path):
            # Give a brief moment for fsync, then return
            time.sleep(1)
            return ("file_appeared", None)

        # SECONDARY: check camc status
        status_info = _get_agent_status(agent_id)
        if status_info is None:
            return ("agent_gone", None)

        status = status_info.get("status")
        state = status_info.get("state")

        if status in ("completed", "stopped", "failed"):
            # Agent is definitely done; give file a moment to appear
            time.sleep(2)
            return ("status_terminal", status)

        # Idle needs corroboration across polls (known flakiness)
        if state == "idle":
            last_status_idle += 1
            if last_status_idle >= 3:  # 3 consecutive idle polls ~= 15s
                return ("status_idle_stable", None)
        else:
            last_status_idle = 0

    return ("timeout", None)
```

Return tuple is `(reason, detail)` so engine can log the completion mechanism in trace.

### 3.3 Reconciliation after signal fires

After `_wait_for_completion` returns:
1. Read `node-result.json` via `result_reader.py`
2. If file missing AND signal was `status_idle_stable` — this is a false positive from camc. Treat as `AGENT_CRASH`, try `camc capture` for partial output.
3. If file missing AND signal was `timeout` — kill agent, record `AGENT_TIMEOUT`.
4. If file missing AND signal was `status_terminal` — agent completed without writing result. Record `PARSE_ERROR` with partial capture.
5. If file present — read and return.

---

## 4. Orphan Agent Handling on Resume

### 4.1 Why

Engine crashes or is killed (SIGKILL, OOM, machine reboot) mid-node. The camc agent is still running in its tmux session. Naive resume would start a second agent for the same node — wasted work and conflicting output.

### 4.2 State field

Engine writes `state["current_agent_id"]` **immediately after** `camc run` returns an agent ID, and atomically saves state BEFORE starting the poll loop. On node completion (or timeout/crash), clear this field before saving.

```json
{
  "pc": "fix",
  "status": "running",
  "current_agent_id": "486ffaeb",  // set when agent starts, cleared when done
  "current_node_started_at": "2026-04-13T05:11:32Z"
}
```

### 4.3 Resume logic

On engine startup:

```python
def _adopt_or_recover_orphan(state):
    agent_id = state.get("current_agent_id")
    if not agent_id:
        return None  # clean resume

    status_info = _get_agent_status(agent_id)
    if status_info is None:
        # Agent gone — was it completed before engine crashed?
        return "adopt_or_crash"  # check for result file

    status = status_info.get("status")
    if status == "running":
        # Orphan is alive. Wait for it instead of starting a new one.
        return "wait_for_orphan"
    elif status in ("completed", "stopped"):
        # It finished. Read its result file.
        return "adopt_result"
    elif status == "failed":
        return "retry_after_crash"
    else:
        return "unknown"
```

Then:
- `wait_for_orphan` → resume polling via `_wait_for_completion`
- `adopt_result` → read `node-result.json`, apply transitions normally
- `adopt_or_crash` → if result file exists, adopt it; else treat as crash
- `retry_after_crash` → increment retry counter, build retry prompt

### 4.4 Manual override

`engine --force-restart` flag clears `current_agent_id` without adoption (for cases where the old agent is hopelessly stuck). Default: adopt if possible.

---

## 5. cmd Node Output Capture

### 5.1 Current state

`cmd_runner.py` uses `capture_output=True` but only returns exit code in the result summary. stdout/stderr are included in the dict but never exposed to downstream nodes via `state_updates`.

### 5.2 Enhancement

On cmd completion, engine promotes stdout/stderr into structured state:

```python
def run_cmd(command, cwd, timeout):
    proc = subprocess.run(command, shell=True, cwd=cwd, ...)

    stdout_tail = proc.stdout[-2000:] if proc.stdout else ""
    stderr_tail = proc.stderr[-500:] if proc.stderr else ""

    if proc.returncode == 0:
        return {
            "status": "success",
            "summary": f"cmd succeeded (exit 0)",
            "output": {
                "exit_code": 0,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            "state_updates": {
                "last_cmd_output": stdout_tail,  # quick-access for templates
            },
            "error": None,
        }
    else:
        return {
            "status": "fail",
            "summary": f"cmd failed (exit {proc.returncode})",
            "output": {
                "exit_code": proc.returncode,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            "state_updates": {
                "last_cmd_output": stdout_tail,
                "last_cmd_stderr": stderr_tail,
            },
            "error": {"code": "CMD_FAIL", "exit_code": proc.returncode},
        }
```

### 5.3 Template availability

Downstream nodes can reference:
- `{{state.last_cmd_output}}` — last 2000 chars of stdout
- `{{state.last_cmd_stderr}}` — last 500 chars of stderr
- via `state["last_failure"].stdout_tail` when cmd failure was the source of last_failure

Example `fix` prompt can now include:
```yaml
fix:
  do: agent claude
  with: |
    Fix one bug. Last test output:
    {{state.last_cmd_output}}
```

---

## 6. State Machine Correctness (unchanged from v1)

### 6.1 Re-entrant nodes

`state["retry_counts"][node_id]` is reset to 0 whenever a node succeeds. The overall workflow doesn't care how many times `fix` has run; only the current retry streak matters.

### 6.2 Terminal states

Engine loop stops on: `done`, `failed`, `aborted`, `waiting`, `interrupted`, `engine_error`.

### 6.3 Loop detection

`state["node_execution_count"][node_id]` tracks total executions. If a single node exceeds `MAX_NODE_EXECUTIONS` (default 10), abort with `engine_error: "loop detected"`. Separate from retry budget — this catches infinite workflow-level loops, not agent-level retries.

---

## 7. Trace Logging (Replayable Format)

Even though replay is out of scope, the trace format stays rich for post-mortem debugging.

### 7.1 Per-step trace entry schema

```json
{
  "step": 4,
  "ts_start": "2026-04-13T05:11:32.123Z",
  "ts_end": "2026-04-13T05:11:37.456Z",
  "duration_ms": 5333,
  "node_id": "fix",
  "do": "agent claude",
  "attempt": 2,
  "is_retry": true,
  "retry_mode": "task",
  "input_state": {...},
  "node_result": {...},
  "output_state": {...},
  "transition": {...},
  "agent_id": "486ffaeb",
  "exec_mode": "camc",
  "completion_signal": "file_appeared",
  "lesson_added": null,
  "event": null
}
```

`event` can be: `"crashed_mid_task"`, `"adopted_orphan"`, `"timeout"`, `"loop_detected"`, or `null`.

### 7.2 Trace integrity (crash-safe)

- One JSON object per line (JSONL)
- After each write: `flush()` + `os.fsync(fileno)` before the state file is saved
- State file written atomically (see §8)
- On load, skip trailing malformed lines with a warning

---

## 8. Atomic State Writes

Current `save_state` opens and writes directly. Replace with:

```python
def save_state_atomic(path, state):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.rename(tmp, path)  # atomic on POSIX

    # fsync parent to ensure rename is durable
    dir_fd = os.open(parent, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
```

Same pattern for trace appends (open O_APPEND, write, flush, fsync).

---

## 9. Timeouts and Limits

### 9.1 Per-node timeout

`node.get("timeout", config.node_timeout)` — default 600s. Applies to both cmd and agent nodes. On exceed:
- cmd node: `subprocess.run(timeout=...)` raises `TimeoutExpired`
- agent node: `_wait_for_completion` returns `("timeout", None)`, engine calls `camc stop <id>`, then `camc rm --force`

### 9.2 Workflow timeout

`workflow_timeout` (default 3600s) tracked from engine start. Checked at the top of each loop iteration. On exceed: status=failed, error.code=WORKFLOW_TIMEOUT.

### 9.3 Max retries

`max_retries` (default 3) per node. Tracked in `state["retry_counts"][node_id]`.

### 9.4 Max node executions (loop detection)

`max_node_executions` (default 10) per node across the entire workflow run. Separate counter from retry.

---

## 10. Error Handling

### 10.1 Taxonomy

| Category | Examples | Handling |
|----------|----------|----------|
| **Node fail — task** | cmd exit ≠ 0, agent returns `status=fail` | Trigger `if fail` transition OR context-aware retry |
| **Node fail — transient** | parse error, agent timeout, agent crash, camc error | Retry with same prompt (budget permitting) |
| **Engine error** | Python exception, disk full, camc missing | Log + crash-safe save + exit non-zero |
| **User interrupt** | SIGINT, SIGTERM | Save state with `status=interrupted`, exit 130 |

### 10.2 Signal handlers

Install SIGINT/SIGTERM handlers early. On signal:
- Set `state["status"] = "interrupted"`
- Clear `current_agent_id` only after politely calling `camc stop` (if agent running)
- Atomic save state
- Exit with code 130

### 10.3 Engine-level exception wrapping

Top-level `try/except Exception` around `Engine.run()`. On unexpected exception:
- Log full traceback to `.camflow/engine.log`
- Save state with `status = "engine_error"` and `error.engine_exception = <message>`
- Re-raise

---

## 11. Dry-Run and Progress

### 11.1 Dry-run

`engine.run(dry_run=True)`:
- Performs static walk of the workflow: treats all nodes as success, follows `next` / `if success`
- No subprocess, no camc calls
- Writes plan to stdout: ordered list of nodes that would execute in the happy path
- Also shows the fail branches (reachability analysis)
- Exits 0 if all transitions lead to `done` from `start`; else reports unreachable nodes

### 11.2 Progress reporting

Each step prints:
```
[3] fix (exec 2 of max 10, attempt 2/3) — agent via camc — 12s elapsed
```

Also writes `.camflow/progress.json`:
```json
{
  "step": 3,
  "pc": "fix",
  "node_execution_count": 2,
  "attempt": 2,
  "max_retries": 3,
  "elapsed_seconds": 12,
  "workflow_elapsed": 145
}
```

External tools can poll this file.

---

## 12. Module-Level Changes

### 12.1 Files modified / added

| File | Change |
|------|--------|
| `backend/cam/engine.py` | Refactor to `Engine` class with `EngineConfig` |
| `backend/cam/cmd_runner.py` | Add structured output capture (stdout/stderr tails) |
| `backend/cam/agent_runner.py` | Dual-signal polling, orphan handling, completion reason |
| `backend/cam/prompt_builder.py` | Add `build_retry_prompt`, lessons/last_failure injection |
| `backend/cam/orphan_handler.py` | **NEW** — adopt/recover orphan agents on resume |
| `backend/cam/tracer.py` | **NEW** — build trace entries with all fields |
| `backend/cam/progress.py` | **NEW** — write progress.json |
| `backend/persistence.py` | Add `save_state_atomic`, `append_trace_atomic` |
| `engine/error_classifier.py` | Add `retry_mode(error)` — transient vs task |
| `engine/memory.py` | Add `add_lesson_deduped`, `prune_lessons` |
| `engine/retry.py` | Wire into engine loop properly (configurable max) |

### 12.2 No new dependencies

Pure stdlib only. No changes to pyproject.toml.

---

## 13. Implementation Order

Each step can be tested before the next.

1. **Atomic persistence** — `save_state_atomic`, `append_trace_atomic` in `backend/persistence.py`
2. **Enhanced cmd_runner** — stdout/stderr tails in result
3. **Tracer module** — `build_trace_entry` with all fields
4. **Memory enhancements** — deduped lessons, pruning (FIFO to 10)
5. **Error classifier** — retry_mode (transient vs task)
6. **Prompt builder** — lessons/last_failure injection + `build_retry_prompt`
7. **Agent runner** — dual-signal polling + completion reason
8. **Orphan handler** — module + engine integration
9. **Engine refactor** — `Engine` class, `EngineConfig`, signal handlers, retry loop with context
10. **Timeouts** — per-node + workflow
11. **Loop detection** — max_node_executions
12. **Dry-run** — static walk of workflow
13. **Progress** — stdout line + progress.json

---

## 14. Testing Plan

### 14.1 Unit tests (`tests/unit/`)

| Test file | Coverage |
|-----------|----------|
| `test_transition.py` | Every priority branch of `resolve_next`: abort, wait, if fail, output.*, state.*, goto, next, default done, default failed |
| `test_state.py` | `init_state`, `apply_updates` (empty, overwrite, add), retry_counts reset on success |
| `test_input_ref.py` | `resolve_refs` with missing keys, nested values, special chars, empty |
| `test_node_contract.py` | `validate_result` with all required/missing/invalid field combos |
| `test_dsl.py` | `validate_workflow`: missing `start`, invalid executor, dangling `goto`, unknown fields |
| `test_retry.py` | `should_retry`, `apply_retry`, counter reset on node change |
| `test_recovery.py` | Retry vs reroute decision |
| `test_error_classifier.py` | All error codes → correct retry_mode (transient vs task) |
| `test_memory.py` | `add_lesson_deduped` dedupes, `prune_lessons` FIFO to max 10 |
| `test_persistence.py` | Atomic save: simulate crash between temp write and rename, verify old state still loadable |
| `test_tracer.py` | Build trace entry, verify all fields, deep copy of input_state |
| `test_prompt_builder.py` | Template sub + output contract + lessons/last_failure injection + `build_retry_prompt` RETRY header |
| `test_result_reader.py` | Missing file, malformed JSON, missing required keys, valid result |
| `test_cmd_runner.py` | Success/failure/timeout paths, stdout/stderr truncation |
| `test_orphan_handler.py` | Each branch: no orphan, running, completed with result, completed no result, dead, gone |

### 14.2 Integration tests (`tests/integration/`)

| Test | What it covers | Expected duration |
|------|----------------|-------------------|
| `test_cmd_only.py` | Fast cmd-only workflow end-to-end | < 5s |
| `test_cmd_branch.py` | cmd workflow with `if fail` branch taken | < 5s |
| `test_cmd_output_capture.py` | cmd failure → `state.last_cmd_output` populated + available via `{{state.last_cmd_output}}` | < 5s |
| `test_lessons_flow.py` | Agent writes `new_lesson`, engine dedupes, prunes, injects in next prompt | No real agent (mock) |
| `test_retry_context.py` | Mock agent fails twice then succeeds; verify retry prompts contain last_failure | No real agent (mock) |
| `test_dry_run.py` | Dry-run on calculator workflow → prints plan, no execution | < 1s |
| `test_single_agent_node.py` | One real agent node via camc, verify result + trace | ~ 1m |
| `test_calculator_demo.py` | Full calculator fix→test loop | ~ 10-15m (manual) |

### 14.3 Error injection tests (`tests/error_injection/`)

| Test | Scenario |
|------|----------|
| `test_agent_timeout.py` | Short node_timeout, agent doesn't complete → forced stop + AGENT_TIMEOUT |
| `test_missing_result_file.py` | Agent completes without writing result file → PARSE_ERROR + capture fallback |
| `test_corrupt_state.py` | Write malformed state.json, verify engine fails clearly (not silently) |
| `test_disk_full_simulation.py` | Mock fsync to raise ENOSPC, verify atomic write doesn't corrupt existing state |
| `test_missing_camc.py` | Rename `camc` binary path, verify clear error |
| `test_loop_detection.py` | Build workflow with infinite loop, verify engine aborts at max_node_executions |
| `test_workflow_timeout.py` | Short workflow_timeout, verify engine aborts |

### 14.4 Resume tests (`tests/resume/`)

| Test | Scenario |
|------|----------|
| `test_resume_clean.py` | Engine saves `status=running` at node boundary, restart picks up at that node |
| `test_resume_orphan_running.py` | Fake state with `current_agent_id` pointing to real running agent → adopt + wait |
| `test_resume_orphan_completed.py` | Orphan agent already completed + wrote result → adopt result, advance |
| `test_resume_orphan_dead.py` | Orphan agent killed externally → treat as crash, retry |
| `test_resume_workflow_edited.py` | Resume with workflow.yaml missing the node referenced by `pc` → clear error |
| `test_resume_done.py` | Engine completes, restart → prints "already done", exits 0 |
| `test_signal_sigterm.py` | Send SIGTERM mid-run → state saved with `status=interrupted`, resume works |

### 14.5 Manual validation

- Run `test_calculator_demo.py` manually with real camc agents
- Observe:
  - Agent creation/destruction per node
  - Lessons accumulating in state.json across iterations
  - Retry prompts include failure context
  - Trace entries complete and replayable (as JSON)
  - State.json atomic updates (no partial content visible between steps)
  - Final: all 11 tests pass

---

## 15. Deliverables Checklist

**Core features** (review-driven):
- [ ] Lessons accumulation: dedupe + prune + inject in prompts
- [ ] `state["last_failure"]` populated on node fail, cleared on success
- [ ] `build_retry_prompt` with RETRY header + prior attempt summary
- [ ] Dual-signal agent completion (file primary, status secondary)
- [ ] Orphan agent adoption on resume
- [ ] cmd stdout/stderr tails in `state.last_cmd_output` / `state.last_cmd_stderr`

**Infrastructure**:
- [ ] `save_state_atomic` with temp+rename+fsync
- [ ] `append_trace_atomic` with flush+fsync
- [ ] `EngineConfig` dataclass
- [ ] `Engine` class refactor
- [ ] Signal handler (SIGINT/SIGTERM)
- [ ] Top-level try/except for engine errors
- [ ] `tracer.py` module

**State machine**:
- [ ] Retry counter reset on node success
- [ ] `MAX_NODE_EXECUTIONS` loop detection (separate from retry)
- [ ] Resume verification (pc matches trace + workflow)

**Timeouts / limits**:
- [ ] Per-node timeout
- [ ] Workflow-wide timeout
- [ ] Configurable poll interval
- [ ] Configurable max retries

**UX**:
- [ ] Dry-run mode (`--dry-run` flag)
- [ ] Progress line per step
- [ ] `.camflow/progress.json`

**Tests**:
- [ ] All unit tests in §14.1 pass
- [ ] All integration tests in §14.2 pass (calculator demo may be manual)
- [ ] Error injection tests in §14.3 pass
- [ ] Resume tests in §14.4 pass

**Docs**:
- [ ] Update `README.md` with CAM backend usage
- [ ] Keep `docs/cam-phase-plan.md` (this document)
- [ ] Add `docs/trace-format.md` once stable

---

## 16. Out of Scope (v0.1)

- **Replay** (dropped — agents non-deterministic, no practical value)
- Parallel node execution
- Distributed engine (multi-machine)
- LLM-based supervisor
- Webhook event ingress (spec exists but not implemented)
- Full `@memory` / `@artifact` reference resolution
- Handoff artifact generation

---

## 17. Open Questions / Decisions Made

1. **Trace growth** — no rotation in v0.1. Document expected size (~1 KB per step, workflow rarely exceeds 50 steps = 50 KB).
2. **Lesson dedup key** — exact string match. Future: semantic dedup via embeddings.
3. **Partial capture on crash** — via `camc capture <id> -n 50`, stored in `last_failure.stdout_tail`.
4. **Cleanup** — always `camc rm <id> --force` on success or handled failure. Orphan adoption preserves the agent briefly then cleans up.
5. **Concurrent engines** — NOT supported in v0.1. State file has no lock. Running two engines on the same project is undefined behavior.
