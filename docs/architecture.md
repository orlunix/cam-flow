# cam-flow Architecture Reference

> **For strategy — how cam-flow works, and why — read
> [`strategy.md`](strategy.md) first.** This document is the
> code-level companion: every file, every module, every public
> function. When the two disagree, `strategy.md` wins.

Complete map of every file, every module, every public function in
cam-flow. Written for evaluation: each entry has Purpose, Why, Inputs /
Outputs, an Evaluation metric, and How to measure it.

Dependency direction:

```
cli_entry
    │
    ▼
backend/cam/*        ── depends on ──▶   engine/*         (pure logic)
    │                                         │
    └─▶ backend/persistence.py ◀──────────────┘
```

Runtime data flow:

```
workflow.yaml ──▶ engine.dsl ──▶ engine.Engine ──▶ agent_runner ──▶ camc ──▶ agent
                                       │                                        │
                                       ▼                                        ▼
                                state.json  ◀──── state_enricher ◀── node-result.json
                                       │
                                       ▼
                                 trace.log (append-only)
```

---

## Engine core (`src/camflow/engine/`)

Pure Python, no subprocess, no network. Exercised by unit tests.

### `engine/state.py`

- **Purpose.** Bootstrap the workflow state dict and merge
  `state_updates` into it.
- **Why it exists.** Without this, every caller has to agree on the
  initial shape (`pc`, `status`) and reimplement `dict.update()`
  semantics with the same None-safety. Centralizing means one place to
  evolve the invariants.

**Public functions.**

- `init_state() -> dict`
  - **What.** Returns the initial state: `{"pc": "start", "status":
    "running"}`.
  - **Inputs.** None.
  - **Outputs.** Fresh state dict.
  - **Evaluation metric.** Every resumed run begins with a well-formed
    state; absence of `KeyError` on `state["pc"]` in fresh runs.
  - **How to measure.** Percentage of engine starts that reach the
    first transition (vs. crash on missing key). Currently 100% in
    the 155-test suite.
- `apply_updates(state, updates) -> dict`
  - **What.** Merges an updates dict into state via `dict.update()`,
    no-op on empty / None.
  - **Inputs.** `state` (dict), `updates` (dict | None).
  - **Outputs.** The same state dict, mutated in place and returned.
  - **Evaluation metric.** Downstream nodes see the mutations written
    by the previous node.
  - **How to measure.** Integration test assertion: after node A
    writes `state_updates.error = "foo"`, node B's prompt contains
    `"foo"`.

### `engine/transition.py`

- **Purpose.** Given (node, node_result, state), decide the next
  `pc` and `workflow_status` deterministically.
- **Why it exists.** The runtime must never ask the model "what
  next" — that belongs to the DSL. Concentrating the algorithm here
  also makes it trivially replayable from a trace entry.

**Public functions.**

- `resolve_next(node_id, node, result, state) -> dict`
  - **What.** Returns `{workflow_status, next_pc, resume_pc,
    reason}`. Priority chain: `control.abort` → `control.wait` →
    `if: fail` match → `if: success` match → generic `output.*` /
    `state.*` conditions → `control.goto` → `next` field → default.
  - **Inputs.** current `node_id`, the node dict from
    workflow.yaml, the agent/cmd `result` dict, and the current
    `state` dict.
  - **Outputs.** Transition dict.
  - **Evaluation metric.** Given the same (node, result, state) it
    always produces the same transition.
  - **How to measure.** Unit tests in `test_transition.py` cover
    every branch; re-running with identical fixtures gives identical
    output (property test).

### `engine/dsl.py`

- **Purpose.** Load `workflow.yaml` and validate its structure.
- **Why it exists.** Errors in the DSL should stop at startup with a
  clear message, not surface as a `KeyError` mid-run.

**Public functions.**

- `load_workflow(path) -> dict`
  - **What.** `yaml.safe_load` a file.
  - **Inputs.** Path to YAML.
  - **Outputs.** Parsed workflow dict.
  - **Evaluation metric.** Successful parse rate for hand-written
    workflows.
  - **How to measure.** YAML syntax errors → caller sees them at
    startup, not at step N.
- `validate_node(node_id, node) -> (ok, errors)`
  - **What.** Checks a single node: required fields, known executor
    type, transitions have `if` + `goto`.
  - **Inputs.** `node_id` string, `node` dict.
  - **Outputs.** `(bool, list[str])`.
  - **Evaluation metric.** Catches malformed nodes before execution.
  - **How to measure.** Unit tests in `test_dsl.py` for each failure
    mode.
- `validate_workflow(workflow) -> (ok, errors)`
  - **What.** Per-node validation + cross-node reference check
    (dangling `next` / `goto`, missing `start`).
  - **Inputs.** workflow dict.
  - **Outputs.** `(bool, list[str])`.
  - **Evaluation metric.** No engine run proceeds with a dangling
    reference.
  - **How to measure.** `camflow --validate workflow.yaml` returns
    non-zero on dangling refs.

### `engine/input_ref.py`

- **Purpose.** Replace `{{state.xxx}}` placeholders in a string with
  values from the state dict.
- **Why it exists.** Declarative DSL values need to carry dynamic
  data; re-implementing substitution in every renderer would be
  error-prone.

**Public functions.**

- `resolve_refs(text, state) -> str`
  - **What.** Substring-replace every `{{state.<key>}}` occurrence
    with `str(state[key])`. Unknown keys leave the placeholder (so
    the caller can detect them).
  - **Inputs.** `text` (str | None), `state` (dict).
  - **Outputs.** Rendered string (`""` for None input).
  - **Evaluation metric.** After resolution, no `{{state.` remains
    for keys that exist.
  - **How to measure.** Grep resolved prompts for `{{state.`; any
    match is either a missing key (intentional) or a bug.

### `engine/state_enricher.py`

- **Purpose.** Merge node execution results into the six-section
  structured state so the next node sees accumulated context.
- **Why it exists.** Without structured enrichment, downstream nodes
  get a flat `state.error` blob and must parse it back into meaning.
  Structured state = predictable agent prompts = fewer wasted
  iterations.

**Public functions / constants.**

- `MAX_LESSONS = 10`, `MAX_COMPLETED = 20`, `MAX_FAILED_APPROACHES = 5`,
  `MAX_RESOLVED = 20`, `MAX_NEXT_STEPS = 10`,
  `TEST_OUTPUT_MAX_CHARS = 3000`.
- `init_structured_fields(state) -> dict`
  - **What.** Seeds the six sections (`iteration`, `active_task`,
    `completed`, `active_state`, `blocked`, `test_output`,
    `resolved`, `next_steps`, `lessons`, `failed_approaches`,
    `escalation_level`, `retry_counts`). Idempotent.
  - **Inputs.** state dict.
  - **Outputs.** Same dict with defaults.
  - **Evaluation metric.** After init, every section is a known
    shape (list or dict or None).
  - **How to measure.** `test_state_enricher.TestInit` asserts each
    field.
- `enrich_state(state, node_id, node_result, cmd_output=None) -> dict`
  - **What.** Increments `iteration`, merges `files_touched` into
    `key_files`, captures cmd stdout into `test_output`, updates
    `lessons` (dedup + FIFO prune), and records success
    (→ `completed`) or failure (→ `blocked` + `failed_approaches`).
    Pops managed keys (`new_lesson`, `files_touched`, `resolved`,
    `next_steps`, `active_task`, `detail`, `lines`) from
    state_updates so they don't leak as arbitrary state keys.
  - **Inputs.** state dict (mutated), `node_id`, `node_result`
    dict (status / summary / state_updates / output / error),
    optional `cmd_output` string for cmd node stdout.
  - **Outputs.** Same state dict, enriched.
  - **Evaluation metric.** Does the downstream agent make better
    decisions with enriched state vs raw state?
  - **How to measure.** A/B compare fix success rate over N runs
    (see `docs/evaluation.md` §Measurement Plan).

### `engine/retry.py`

- **Purpose.** Track whether a failed node may be retried and bump
  the counter.
- **Why it exists.** Every unit-of-execution needs a bounded retry
  budget or an infinite loop is one flaky network call away.

**Public functions.**

- `MAX_RETRY = 2` (module constant; engine may override via
  `EngineConfig.max_retries`).
- `should_retry(state, result) -> bool`
  - **What.** True iff `result.status == "fail"` and
    `state.retry < MAX_RETRY`.
  - **Inputs.** state dict, result dict.
  - **Outputs.** Boolean.
  - **Evaluation metric.** No node retries beyond the configured
    budget.
  - **How to measure.** Trace.log: count `attempt` field across
    consecutive entries for the same node; must not exceed
    `max_retries`.
- `apply_retry(state) -> dict`
  - **What.** Increments `state["retry"]`.
  - **Inputs.** state.
  - **Outputs.** Same dict.
  - **Evaluation metric.** Counter monotonically increases until
    reset.
  - **How to measure.** Unit test in `test_retry.py`.

### `engine/recovery.py`

- **Purpose.** Choose between "retry same node" vs "reroute to a
  recovery node" when a budget is exhausted.
- **Why it exists.** Flat retry is wasteful past the budget.
  Recovery lets the workflow fall through to a safe terminal path.

**Public functions.**

- `choose_recovery_action(state, error=None) -> dict`
  - **What.** If `retry < 2` → `{action: "retry", target: pc}`.
    Else → `{action: "reroute", target: state.recovery_node or
    "done"}`.
  - **Inputs.** state, optional classified error dict.
  - **Outputs.** `{action, target, reason}`.
  - **Evaluation metric.** Workflows don't spin forever on a
    hopeless node.
  - **How to measure.** For any `fail` outcome, verify the trace
    shows reroute within `max_retries + 1` attempts.

### `engine/error_classifier.py`

- **Purpose.** Categorize a failure into a typed error code and a
  retry mode.
- **Why it exists.** "Retry" alone is not enough — a PARSE_ERROR
  (transient) wants the same prompt again; a NODE_FAIL (task) wants
  a different prompt. Typing the error unlocks smarter retries.

**Public functions / constants.**

- `TRANSIENT_CODES = {"PARSE_ERROR", "AGENT_TIMEOUT", "AGENT_CRASH",
  "CAMC_ERROR"}`.
- `TASK_CODES = {"NODE_FAIL", "CMD_FAIL", "CMD_TIMEOUT",
  "CMD_NOT_FOUND", "CMD_ERROR"}`.
- `classify_error(raw_output, parse_ok, result=None) -> dict | None`
  - **What.** Agent-node classifier: parse failure → `PARSE_ERROR`;
    parsed but `status=fail` → `NODE_FAIL`; else None.
  - **Inputs.** raw stdout, boolean `parse_ok`, optional result dict.
  - **Outputs.** Error dict `{code, retryable, reason}` or None.
- `retry_mode(error) -> "transient" | "task" | "none"`
  - **What.** Maps error code to mode. Unknown → `"task"` (safer —
    retry with context rather than blind).
  - **Inputs.** Error dict (or None).
  - **Outputs.** One of the three strings.
  - **Evaluation metric.** Percentage of retries that use the right
    mode.
  - **How to measure.** Per-trace `retry_mode` field vs the actual
    recovery outcome (did the retry succeed or fail again?).

### `engine/memory.py`

- **Purpose.** Append-dedup-prune a lessons list.
- **Why it exists.** Lessons are the only part of state that is
  genuinely cross-workflow knowledge. They must deduplicate (agents
  repeat themselves) and never grow unbounded.

**Public functions / constants.**

- `MAX_LESSONS = 10`.
- `init_memory() -> dict`. Legacy (pre-enricher) shape:
  `{"summaries": [], "lessons": []}`.
- `add_summary(memory, text)`, `add_lesson(memory, lesson)` — legacy
  helpers retained for back-compat; not used by the enricher path.
- `add_lesson_deduped(lessons, lesson, max_lessons=MAX_LESSONS) -> list`
  - **What.** Appends `lesson` if not already present (exact string
    match after whitespace strip); FIFO-drops oldest when over cap.
    Returns the same list object.
  - **Inputs.** mutable list, lesson string (None / empty / "  " are
    no-ops).
  - **Outputs.** The same list.
  - **Evaluation metric.** No duplicate entries; length ≤
    `max_lessons`.
  - **How to measure.** Unit test; invariant check at every engine
    step: `len(set(state.lessons)) == len(state.lessons)`.
- `prune_lessons(lessons, max_lessons=MAX_LESSONS)` — standalone
  prune for callers that mutated the list directly.

### `engine/node_contract.py`

- **Purpose.** Validate that a node result follows the contract all
  backends produce.
- **Why it exists.** Without this, a malformed agent JSON would
  sneak past and crash the transition resolver.

**Public functions / constants.**

- `REQUIRED_KEYS = ["status", "summary", "output", "state_updates",
  "control", "error"]`.
- `VALID_STATUS = {"success", "fail", "wait", "abort"}`.
- `VALID_ACTIONS = {"continue", "goto", "wait", "fail", "abort", None}`.
- `validate_result(result) -> (ok, error_message)`
  - **What.** Checks type, required keys, valid status and
    control.action, and that `output` / `state_updates` are dicts.
  - **Inputs.** Result dict.
  - **Outputs.** `(bool, str | None)`.
  - **Evaluation metric.** Zero uncaught malformed results pass to
    the transition resolver.
  - **How to measure.** `test_node_contract.py` covers all failure
    modes; integration test counts malformed-result events in
    trace.log.

---

## CAM backend (`src/camflow/backend/cam/`)

The production backend. Spawns camc agents, runs cmd nodes as
subprocesses, orchestrates the main loop.

### `backend/cam/engine.py`

- **Purpose.** Main execution loop: pick node, run it, enrich state,
  resolve transition, write trace, save state, repeat.
- **Why it exists.** Without a loop owner, nothing drives the
  workflow. The engine also owns agent lifecycle — explicit shutdown
  rather than relying on `--auto-exit`.

**Public surface.**

- `class EngineConfig(dataclass)`
  - Fields: `poll_interval` (5 s), `node_timeout` (600 s),
    `workflow_timeout` (3600 s), `max_retries` (3),
    `max_node_executions` (10), `dry_run` (False),
    `force_restart` (False), `state_filename` / `trace_filename`.
  - **Evaluation metric.** Changing any field doesn't require code
    edits — `camflow --poll-interval 2 ...` works today.
- `class Engine`
  - `__init__(workflow_path, project_dir, config=None)` — load
    paths.
  - `run() -> dict | int` — main loop; returns final state, or an
    int exit code in dry-run mode.
  - `dry_run() -> int` — static walk of the happy path.
  - Private methods handle orphan recovery, per-step execution,
    result-and-transition bookkeeping, trace writing, progress.
  - **Evaluation metric.** End-to-end workflow completion rate on
    the calculator demo (target: 100% on a fresh tree).
  - **How to measure.** Run `examples/cam/` repeatedly; ratio of
    `status == "done"` outcomes to total runs.
- `run(workflow_path, project_dir, max_steps=None, dry_run=False,
  config=None) -> dict`
  - **What.** Legacy function wrapper around `Engine.run()`.
  - **Evaluation metric.** Back-compat: old callers don't break.

### `backend/cam/agent_runner.py`

- **Purpose.** Launch a camc agent, wait for its `node-result.json`,
  clean up. The engine owns the lifecycle; camc's `--auto-exit` is
  NOT used.
- **Why it exists.** The agent execution model is "stateless
  function call": engine starts an agent, agent writes a result
  file, engine reads it and stops the agent. Without engine-owned
  lifecycle, idle detection flakiness (camc bug #10) silently wedges
  workflows.

**Public functions.**

- `PROMPT_FILE = ".camflow/node-prompt.txt"`.
- `RESULT_FILE = ".camflow/node-result.json"`.
- `start_agent(node_id, prompt, project_dir) -> agent_id`
  - **What.** Writes prompt to `.camflow/node-prompt.txt`, runs
    `camc run --name camflow-<node> --path <dir>` with a short
    instruction to read the file, parses the agent ID from camc
    stdout, kicks Enter to submit the pasted prompt.
  - **Inputs.** `node_id` (used for agent naming), full prompt
    string, project dir.
  - **Outputs.** Agent ID string (raises `RuntimeError` on launch
    failure).
  - **Evaluation metric.** Launch success rate (target: >= 99%).
  - **How to measure.** Trace.log: count
    `completion_signal == "launch_failed"`; divide by total agent
    nodes.
- `finalize_agent(agent_id, completion_signal, project_dir,
  cleanup=True) -> result_dict`
  - **What.** Reads `.camflow/node-result.json`. If missing,
    synthesizes a typed fail result (AGENT_TIMEOUT, AGENT_CRASH)
    based on the completion signal. Always (when `cleanup=True`)
    runs `camc stop` then `camc rm --kill` — engine owns shutdown.
  - **Inputs.** agent ID, completion signal string, project dir.
  - **Outputs.** Normalized result dict.
  - **Evaluation metric.** Zero leftover `camflow-*` agents in
    `camc list` after a run.
  - **How to measure.** `camc --json list | grep camflow` after the
    engine exits; expected count is 0.
- `run_agent(node_id, prompt, project_dir, timeout=600,
  poll_interval=5) -> (result, agent_id, completion_signal)`
  - **What.** Convenience that combines `start_agent` +
    `_wait_for_result` + `finalize_agent`.
  - **Evaluation metric.** Mean agent completion time
    (file-appeared latency).
  - **How to measure.** Trace.log: median `duration_ms` for
    `exec_mode == "camc"` rows.

(Private helpers: `_parse_agent_id`, `_get_agent_status`,
`_capture_screen`, `_stop_agent`, `_rm_agent`, `_cleanup_agent`,
`_send_key`, `_kick_prompt`, `_wait_for_result`.)

### `backend/cam/cmd_runner.py`

- **Purpose.** Run a shell command, capture stdout/stderr tails.
- **Why it exists.** Tests, builds, lint — anything that doesn't
  need an LLM. Directly `subprocess.run` instead of wasting tokens.

**Public functions / constants.**

- `STDOUT_TAIL = 2000`, `STDERR_TAIL = 500`.
- `run_cmd(command, cwd, timeout=120) -> result_dict`
  - **What.** Shell command with `capture_output=True`, captures
    last 2000 chars of stdout and last 500 of stderr,
    promotes to `state_updates.last_cmd_output` /
    `last_cmd_stderr`. Handles `TimeoutExpired`, `FileNotFoundError`
    with typed error codes.
  - **Inputs.** Command string, cwd, timeout seconds.
  - **Outputs.** Result dict with `status`, `summary`, `output`,
    `state_updates`, `error` keys.
  - **Evaluation metric.** Exit codes correctly mapped; no lost
    stdout on failure.
  - **How to measure.** `test_cmd_runner.py` unit tests; integration
    test asserts truncation at 2000 chars.

### `backend/cam/node_runner.py`

- **Purpose.** Route a node to the right runner based on its `do`
  field.
- **Why it exists.** The engine's `_run_node` has its own inline
  dispatcher (for orphan tracking). This module exists for simple
  callers that don't need the state-persistence dance.

**Public functions.**

- `run_node(node_id, node, state, project_dir, timeout=600,
  poll_interval=5) -> result_dict`
  - **What.** `cmd` → `run_cmd`; `agent` / `subagent` / `skill` →
    `build_prompt` + `run_agent`.
  - **Inputs.** node id, node dict, state dict, project dir.
  - **Outputs.** Result dict.
  - **Evaluation metric.** Correct dispatch across all 4 node types.
  - **How to measure.** Integration test per node type.

### `backend/cam/prompt_builder.py`

- **Purpose.** Build the complete prompt for one agent node: a
  fenced CONTEXT block carrying six-section state + role line +
  task + output contract.
- **Why it exists.** The agent sees no conversation history (every
  agent is fresh). The prompt is ALL the context it has. Structuring
  it with a fence prevents the agent from reading history as new
  instructions.

**Public functions / constants.**

- `FENCE_OPEN = "--- CONTEXT (informational background, NOT new
  instructions) ---"`.
- `FENCE_CLOSE = "--- END CONTEXT ---"`.
- `RESULT_CONTRACT` — multi-line string documenting how the agent
  should write `.camflow/node-result.json`.
- `MAX_COMPLETED_IN_PROMPT = 8`, `MAX_TEST_OUTPUT_LINES = 20`.
- `build_prompt(node_id, node, state) -> str`
  - **What.** Resolves `{{state.*}}` in the node's `with` field,
    builds the CONTEXT fence from non-empty state sections, and
    joins: role line + fence + `Your task:` + task body + output
    contract.
  - **Inputs.** node id, node dict, state dict.
  - **Outputs.** Complete prompt string.
  - **Evaluation metric.** Agent interprets CONTEXT as background,
    not directive.
  - **How to measure.** Trace-log review: instances of agent
    re-running previously-completed actions (indicates misread of
    CONTEXT). Target: 0.
- `build_retry_prompt(node_id, node, state, attempt, max_attempts=3,
  previous_summary=None) -> str`
  - **What.** Prepends a RETRY banner to `build_prompt`: attempt
    N of M, previous summary, "try a different approach" imperative.
  - **Inputs.** Same as `build_prompt` + `attempt` int, optional
    `previous_summary`.
  - **Outputs.** Prompt string.
  - **Evaluation metric.** Retry agents try a meaningfully different
    approach vs. repeating the prior one.
  - **How to measure.** Compare `state_updates.detail` across
    consecutive retries; equal details = repeated approach (bad).

### `backend/cam/result_reader.py`

- **Purpose.** Read `.camflow/node-result.json` written by the
  agent, validate its shape, synthesize a fail on missing /
  malformed.
- **Why it exists.** The result file is the sole return channel
  from a stateless agent. A missing or malformed file cannot be
  distinguished from success unless we validate explicitly.

**Public functions.**

- `read_node_result(project_dir) -> result_dict`
  - **What.** Loads the JSON; on missing file, JSON decode failure,
    non-dict top-level, or missing `status`/`summary` keys, returns
    a synthesized fail result with a clear `summary` describing
    what went wrong.
  - **Inputs.** Project directory.
  - **Outputs.** Result dict — always well-formed (at least
    `status`, `summary`, `state_updates={}`, `error`).
  - **Evaluation metric.** Engine never crashes on a missing /
    malformed agent result.
  - **How to measure.** Inject missing / malformed files in
    integration tests; engine always continues.
- `clear_node_result(project_dir) -> None`
  - **What.** Removes the result file if it exists (pre-run
    hygiene).

### `backend/cam/tracer.py`

- **Purpose.** Build a trace entry — a full, immutable snapshot of
  one engine step.
- **Why it exists.** Without a rich trace, evaluation (did this
  component help?) is impossible. Every step's before/after state
  is recorded so we can replay or A/B-test offline.

**Public functions.**

- `build_trace_entry(step, node_id, node, input_state, node_result,
  output_state, transition, ts_start, ts_end, attempt=1,
  is_retry=False, retry_mode=None, agent_id=None, exec_mode="cmd",
  completion_signal=None, lesson_added=None, event=None, ...) -> dict`
  - **What.** Computes `duration_ms`, ISO-formats timestamps,
    deep-copies the three dicts (input_state, node_result,
    output_state) so post-hoc mutation can't corrupt the record.
  - **Inputs.** All the raw per-step data.
  - **Outputs.** A single trace entry dict suitable for
    `append_trace_atomic`.
  - **Evaluation metric.** Trace is self-describing and replayable.
  - **How to measure.** Run offline analysis
    (`cam evolve report`) over a set of traces; compute metrics
    from fields alone, no extra instrumentation needed.

Planned additions (for evaluation, not yet wired): `prompt_tokens`,
`context_tokens`, `task_tokens`, `tools_available`, `tools_used`,
`context_position`, `enricher_enabled`, `fenced`, `methodology`,
`escalation_level`. See `docs/evaluation.md` §Data Collection.

### `backend/cam/orphan_handler.py`

- **Purpose.** Recover state when the engine crashes mid-node and
  an agent is still alive in tmux.
- **Why it exists.** Naive resume would spawn a duplicate agent for
  the same node, wasting work and producing conflicting writes.

**Public functions / constants.**

- `ACTION_NO_ORPHAN`, `ACTION_WAIT`, `ACTION_ADOPT_RESULT`,
  `ACTION_TREAT_AS_CRASH`.
- `decide_orphan_action(state, project_dir) -> (action, agent_id)`
  - **What.** Combines `state.current_agent_id`, camc status, and
    the result file on disk to decide: no orphan / wait / adopt
    existing result / treat as crash.
  - **Inputs.** state dict, project dir.
  - **Outputs.** Action constant + agent id (or None).
  - **Evaluation metric.** Resume after SIGKILL does not create
    duplicate agents.
  - **How to measure.** Integration test:
    `test_resume_orphan_adopt_result`.
- `handle_orphan(action, agent_id, project_dir, timeout,
  poll_interval) -> (result, completion_signal)`
  - **What.** Executes the decided action: resume polling (WAIT),
    read the existing result (ADOPT_RESULT), synthesize a crash
    result (TREAT_AS_CRASH).
  - **Outputs.** result + signal, as from a fresh run.

### `backend/cam/progress.py`

- **Purpose.** Emit progress to stdout and `.camflow/progress.json`.
- **Why it exists.** Operators need visibility during long runs;
  external tools may poll the JSON for dashboards.

**Public functions.**

- `write_progress(project_dir, step, pc, node_exec_count, attempt,
  max_retries, node_started_at, workflow_started_at) -> None`
  - **What.** Atomic write of `.camflow/progress.json` via temp +
    rename.
- `format_progress_line(step, pc, node_exec, attempt, max_retries,
  exec_mode, elapsed) -> str`
  - **What.** Returns a single-line human-readable progress string
    for stdout.

---

## Persistence (`src/camflow/backend/persistence.py`)

Shared across backends; fsync'd writes; JSONL append-only trace.

- **Purpose.** Crash-safe state + trace I/O.
- **Why it exists.** A non-atomic write during SIGKILL can leave
  `state.json` truncated; the engine would then fail to resume.

**Public functions.**

- `save_state_atomic(path, state) -> None`
  - **What.** Write to `path.tmp.<pid>`, fsync, `os.rename` (atomic
    on POSIX), fsync parent dir. Removes the tmp file on any
    exception (e.g., non-JSON-serializable payload) so the previous
    `state.json` stays intact.
  - **Evaluation metric.** No partial / corrupt state files ever
    observed.
  - **How to measure.** Kill engine randomly during a run; restart;
    assert `load_state` succeeds.
- `append_trace_atomic(path, entry) -> None`
  - **What.** Open `O_APPEND`, write JSON line, flush + fsync.
  - **Evaluation metric.** Trace line count equals the number of
    steps the engine thought it ran.
  - **How to measure.** `wc -l trace.log` vs. the engine's final
    step counter.
- `load_state(path, default=None) -> dict | default`
  - **What.** Load JSON, return `default` on `FileNotFoundError`.
- `load_trace(path) -> list[dict]`
  - **What.** Parse JSONL; skip trailing malformed line on `JSONDecodeError`.
  - **Evaluation metric.** Always returns a valid list even after a
    crash mid-write.
- `save_state(path, state)` / `append_trace(path, entry)` —
  back-compat non-atomic versions. Prefer the `_atomic` variants.

---

## CLI entry (`src/camflow/cli_entry/main.py`)

- **Purpose.** Command-line interface: `camflow workflow.yaml`.
- **Why it exists.** Operators need a way to run workflows without
  writing Python.

**Public functions.**

- `main() -> None`
  - **What.** argparse: workflow path + `--project-dir`, `--validate`,
    `--dry-run`, `--force-restart`, `--poll-interval`, `--node-timeout`,
    `--workflow-timeout`, `--max-retries`, `--max-node-executions`,
    `--no-steward`. Dispatches to `evolve` / `plan` / `plan-tool` /
    `scout` / `resume` / `status` / `stop` / `watchdog` / `ctl` /
    `chat` / `steward` / `run` subcommands; the default positional
    path is "run a workflow." Validates the workflow, then runs the
    engine. Exits 0 on `status == "done"`, 1 otherwise.
  - **Evaluation metric.** The CLI is the one-line quickstart — does
    `camflow examples/cam/workflow.yaml` work out of the box?
  - **How to measure.** Runbook check: fresh clone → `pip install -e .`
    → `camflow …` completes a demo.

---

## Steward (`src/camflow/steward/`)

Phase A (2026-04-26). Project-scoped LLM agent that owns chat and
project memory. See `strategy.md` §9.

| Module | What it does |
|--------|-------------|
| `spawn.py` | Boot-pack assembly + ``camc run`` for a fresh Steward; pointer file at ``.camflow/steward.json``; `is_steward_alive` liveness probe via `camc status`; `_camc_status` / `_default_camc_runner` are the leaf subprocess wrappers blocked in tests |
| `events.py` | Engine→Steward push channel. `emit(project_dir, event_type, **fields)` mirrors to `steward-events.jsonl`, locates the Steward via the pointer file, sends `[CAMFLOW EVENT] {json}` via `camc send`, and writes a paired `kind=event_emitted` trace entry. Convenience wrappers: `emit_flow_started/terminal`, `emit_node_started/done/failed`, `emit_engine_resumed`. Event types are a closed `EVENT_TYPES` frozenset documented in §7.3 of the design doc and cross-referenced with `tracer.EVENT_KINDS` |

The Steward has no Python module of its own — it's a Claude Code
session running inside camc. The `steward/` package owns the Python
side of "spawn it / talk to it / mirror events to disk." The
mutating `ctl` verb support, autonomy config, and compaction handoff
land in Phase B.

---

## Registry (`src/camflow/registry/`)

Phase A. Project-scoped agent registry — the snapshot view of "who
exists?" complementing trace.log's "what happened?" view.

| Module | What it does |
|--------|-------------|
| `agents.py` | `agents.json` reader/writer. Atomic write via `save_state_atomic`. Public API: `register_agent`, `update_agent_status`, `get_agent`, `list_agents` (filter by role / status), `set_current_steward`, `get_current_steward`, `append_flow_to_steward` (idempotent flow_id append). Engine is the SOLE writer; CLI commands and the Steward read but never write |
| `hooks.py` | Lifecycle hooks that update registry + write a paired trace event in lockstep. `on_agent_spawned`, `on_agent_finalized`, `on_agent_killed`, `on_agent_handoff_archived`. Trace events use `build_event_entry` so registry status flips and audit entries can never drift |

Roles: `worker` (per-node, flow-scoped), `planner` (one-shot per yaml
generation), `steward` (project-scoped, persistent). Statuses:
`alive`, `completed`, `failed`, `killed`, `handoff_archived`.

---

## Planner agent (`src/camflow/planner/`)

Phase A made the Planner agent-based — a camc-spawned Claude Code
session that explores, drafts, self-validates, and writes the final
yaml. The single-shot LLM Planner is preserved for one release cycle
behind `--legacy`.

| Module | What it does |
|--------|-------------|
| `agent_planner.py` | `generate_workflow_via_agent(request, project_dir, ...)` — spawn `planner-<id>` via camc, register it, poll `.camflow/workflow.yaml` until it appears, run final DSL + plan-quality validation, mark agent completed, `camc rm` it. Boot pack `_BOOT_TEMPLATE` instructs the agent to read `plan-request.txt`, draft → `camflow plan-tool validate` → iterate → `plan-tool write`. Failure modes (camc unreachable, agent dies before writing, timeout, invalid yaml, DSL or plan-quality failure) raise `PlannerAgentError`. All `camc` shell-outs are dependency-injected for tests |
| `planner.py` | Pre-Phase-A single-shot LLM Planner. Used when `camflow plan --legacy` is passed |
| `validator.py` | `validate_plan_quality(workflow)` — heuristic checks (verify on long nodes, OUTCOME-not-OUTPUT, methodology routing). Used both by the legacy Planner post-hoc and by `camflow plan-tool validate` (which the agent Planner calls every loop iteration) |
| `prompt_template.py` | Legacy Planner prompt assembly |
| `examples.py` | Legacy Planner few-shot examples |
| `scouts.py` | Legacy Planner scout integration |
| `llm.py` | Legacy Planner LLM call wrapper |

---

## CLI entry — Phase A + B subcommand modules

| Module | Subcommand | What it does |
|--------|-----------|-------------|
| `cli_entry/ctl.py` | `camflow ctl <verb>` | Dispatcher for the Steward's verb whitelist. Resolution: per-verb override → preset table (`autonomy.PRESET_TABLE`) → verb's spec default. Three levels: `autonomous` runs inline; `confirm` queues to `.camflow/control-pending.jsonl`; `block` refuses |
| `cli_entry/ctl_read.py` | `read-state`, `read-trace`, `read-events`, `read-rationale`, `read-registry` | Five autonomous read-only verbs (Phase A). Local file I/O — even a stuck engine can still answer "what do we know?" |
| `cli_entry/ctl_mutate.py` | `pause`, `resume`, `kill-worker`, `spawn`, `skip` | Phase B mutating verbs. All five queue uniformly to `.camflow/control.jsonl` (autonomous) or `.camflow/control-pending.jsonl` (confirm); engine drains via `control_drain` on each tick |
| `cli_entry/ctl_steward.py` | `summarize "<text>"`, `archive-summary [--label]` | Phase B Steward memory verbs. Autonomous, file-only (write to `steward/summary.md`, fold into `steward/archive.md`). The Steward calls these in response to `checkpoint_now` / `flow_idle` events |
| `cli_entry/chat.py` | `camflow chat "<msg>"` / `--history` / `--pending` | One-shot send via `camc send`; `--history` tails `steward-events.jsonl`; Phase B `--pending` interactively reviews confirm-queue entries with `[y/N/never]`, applies timeout-deny, and persists `never` as a `block` override |
| `cli_entry/steward.py` | `camflow steward {status,kill,restart}` | Lifecycle commands. `kill` flips registry status to `killed`, clears the pointer, runs `camc rm --kill`. `restart` is kill + spawn fresh |
| `cli_entry/plan_tool.py` | `camflow plan-tool {validate,write}` | Internal tools the Planner agent shells out to during its self-validate loop. `validate` prints `{ok, errors[], warnings[]}` JSON and exits 0/1. `write` atomic-writes yaml from stdin, sandboxed to `.camflow/` |

---

## Filesystem layout — `camflow.paths`

Phase B introduced `src/camflow/paths.py` as the single source of
truth for per-agent directory layout. Every file path under
`.camflow/` is computed via a helper here; nothing else does string
concatenation. Helpers cover:

  * Project root — `state_path`, `trace_path`, `workflow_path`,
    `plan_rationale_path`, `node_result_path`, `camflow_dir`.
  * Steward (one per project, persistent) — `steward_dir`,
    `steward_prompt_path`, `steward_context_path`,
    `steward_summary_path`, `steward_archive_path`,
    `steward_inbox_path`, `steward_session_log_path`,
    `steward_archive_subdir(old_id, ts)` (Phase B handoff).
  * Flow — `flow_dir`, `flow_summary_path`.
  * Planner (one per workflow generation, plus replans) —
    `planner_dir(replan_n=...)`, `planner_prompt_path`,
    `planner_request_path`, `planner_draft_path`,
    `planner_warnings_path`, `planner_session_log_path`,
    `planner_context_path`.
  * Worker (per-node, per-attempt) — `node_dir`, `node_prompt_path`,
    `node_context_path`, `node_inputs_dir`,
    `node_winning_result_path`, `attempt_dir`,
    `attempt_agent_id_path`, `attempt_progress_path`,
    `attempt_result_path`, `attempt_session_log_path`.
  * Counters — `latest_attempt_n(project, flow, node)`,
    `next_attempt_n(...)` (used by reboot logic).

See `strategy.md` §11 for the full layout diagram and rationale.

---

## Phase B modules

| Module | What it does |
|--------|-------------|
| `backend/cam/control_drain.py` | Reads `.camflow/control.jsonl` at the top of every engine tick and applies each command to engine state: `pause` flips status to `waiting`; `resume` to `running`; `kill-worker` runs `_cleanup_agent` on `state.current_agent_id` (subject to a 30s per-`(flow_id, node_id, agent_id)` cooldown that lives in this module's process-local state); `spawn` overrides `state.pc`; `skip` sets `state.skip_current` for `_execute_step` to consume. Each command emits a paired `kind=control_resolution` trace entry so the audit trail shows both the user's intent (`control_command` from ctl) and the engine's action |
| `steward/autonomy.py` | Loader / resolver for `.camflow/steward-config.yaml`. Three presets (`PRESET_CAUTIOUS`, `PRESET_DEFAULT`, `PRESET_BOLD`) plus a `LEVEL_BLOCK` sentinel for "user said never". `effective_autonomy(verb, config, default=...)` resolves: per-verb override → preset table → caller-supplied default (the verb's spec autonomy). `set_override(verb, level)` persists a single change atomically |
| `steward/handoff.py` | `handoff_steward(project_dir, reason=...)` archives the live `steward/` directory under `steward/archive/<old-id>-<ts>/`, folds `summary.md` into `archive.md` with a "Pre-handoff summary" section, resets `summary.md` to a clean post-handoff slate, kills the old camc agent (best-effort), marks the old record `handoff_archived` in `agents.json` with `successor_id` + `archived_dir` + `handoff_reason`, and spawns a fresh Steward via `spawn_steward` so the carried-over archive sits next to the new boot pack |
| `steward/events.py` (extended) | Phase B event wrappers: `emit_node_retry` (rate-limited engine-side, 30s coalesce window per `(flow_id, node_id)`), `emit_escalation_level_change`, `emit_verify_failed`, `emit_heartbeat_stale_worker`, `emit_checkpoint_now`, `emit_flow_idle`. Plus `RETRY_COALESCE_WINDOW` (30s) and `reset_retry_coalesce_for_tests()` |

---

## User-facing lifecycle (camflow-manager skill)

> Strategy overview lives in [`strategy.md` § 1](strategy.md#1-agent-management-strategy)
> and the camflow-manager `skills/camflow-manager/SKILL.md` file itself.
> This section is the code-level/diagram companion — what runs where,
> which Python module owns which phase.

Four components, one user-facing entry point:

```
                  ┌─────────────────────────────────┐
                  │  USER                           │
                  └──────────────┬──────────────────┘
                                 │  talks only to manager
                                 ▼
                  ┌─────────────────────────────────┐
                  │  camflow-manager (skill)        │   ← sole user interface
                  │  project manager                │
                  │                                 │
                  │  GATHER → COLLECT → PLAN →      │
                  │  REVIEW → SETUP → CONFIRM →     │
                  │  KICKOFF → (EXIT) → POST        │
                  └──┬──────────────┬───────────┬───┘
          calls once │   launches   │  hands    │
                     │              │   off to  │
                     ▼              ▼           ▼
         ┌──────────────────┐ ┌──────────┐ ┌──────────────────┐
         │ Planner          │ │ Engine   │ │ camflow-runner   │
         │ (camflow plan    │ │ (Python  │ │ (skill)          │
         │  CLI)            │ │  proc,   │ │                  │
         │                  │ │  CAM)    │ │  CLI per-tick    │
         │  architect       │ │          │ │  exec — /loop    │
         │  (1 LLM call,    │ │  const-  │ │  calls it each   │
         │  no exec)        │ │  ruction │ │  node            │
         │                  │ │  crew    │ │                  │
         └──────────────────┘ └──────────┘ └──────────────────┘
```

Responsibility split:

| Role      | Component        | Type            | When active         |
|-----------|------------------|-----------------|---------------------|
| Manager   | camflow-manager  | Skill (Claude)  | Setup + POST only   |
| Architect | `camflow plan`   | CLI (LLM call)  | Once, during PLAN   |
| Builder   | Engine           | Python process  | CAM mode, unattended|
| Worker    | camflow-runner   | Skill (Claude)  | CLI mode, per /loop |

The manager is the ONLY thing the user talks to. Everything else is
an internal tool the manager calls or hands off to. In CAM mode the
manager exits right after kickoff — no Claude session cost during a
90-minute formal-verify run. In CLI mode the manager seeds state and
the user drives `/loop camflow-runner` themselves.

### Detail: SETUP → EXECUTION → REPORT

The `camflow-manager` skill at `skills/camflow-manager/SKILL.md`
separates SETUP (an interactive Claude Code agent session) from
EXECUTION (a separate Python engine process that spawns sub-agents
on its own, for CAM mode) from REPORT (a later, separate
invocation):

```
 ┌─────────────────────────────────────┐
 │ SETUP AGENT (camflow-manager skill) │
 │                                     │
 │  1. GATHER  — interview user        │
 │  2. COLLECT — resources catalog     │
 │  3. PLAN    — camflow plan CLI      │
 │  4. REVIEW  — user approval         │
 │  5. SETUP   — write project files   │
 │  6. CONFIRM — final go/no-go        │
 │  7. KICKOFF:                        │
 │       CAM → launch engine → EXIT    │
 │       CLI → seed state, hand off    │
 │              to /loop camflow-runner│
 └──────────────┬──────────────────────┘
                │  nohup python3 -m camflow.cli_entry.main
                │        workflow.yaml &       (CAM only)
                ▼
 ┌─────────────────────────────────────┐
 │ ENGINE PROCESS (no Claude session)  │
 │                                     │
 │  per node:                          │
 │    cmd → subprocess.run             │
 │    agent → camc run → sub-agent     │
 │      writes node-result.json        │
 │      engine.stop+rm                 │
 │    verify → run verify cmd          │
 │    enrich_state → update state.json │
 │    append trace.log                 │
 │  exit: done / failed / interrupted  │
 └──────────────┬──────────────────────┘
                │  user comes back later
                ▼
 ┌─────────────────────────────────────┐
 │ REPORT AGENT                        │
 │   (camflow-manager Phase 8 POST,    │
 │    new invocation — no persistent   │
 │    agent running)                   │
 │                                     │
 │  8. read state.json + trace.log     │
 │     camflow evolve report           │
 │     show REPORT.md                  │
 │     summarize findings              │
 └─────────────────────────────────────┘
```

The SETUP agent does not stay alive. It sets up and launches, then
exits. This is what makes long-running workflows tractable — no
Claude session is consuming tokens while the engine grinds through
90 minutes of tree setup and formal verification.

The ENGINE owns the full execution. It spawns its own sub-agents via
`camc run`, reads their `node-result.json`, and cleans them up. See
the Plan vs Runtime boundary section for which fields the plan
specifies and which the runtime defaults.

The REPORT phase is a NEW invocation of the skill. There's no
persistent agent waiting. The user (or a future agent session) asks
"how did it go?" and the skill runs its Step 7 procedure against the
persisted `.camflow/state.json` and `.camflow/trace.log`.

Earlier skill variants (`cam-flow`, `camflow` babysit, `camflow-
creator`) are now DEPRECATED — their responsibilities fold cleanly
into camflow-manager (lifecycle) + camflow-runner (CLI execution)
+ Planner + Engine. The deprecated skill files are kept for
reference but should not be triggered for new work.

---

## DSL v2: inline → agent → skill promotion

> Moved to [`strategy.md` § 7](strategy.md#7-inline--agent--skill-promotion).
> That's the source of truth for when to keep an inline prompt vs.
> promote to an agent definition or a skill.

---

## Plan vs Runtime boundary

> The conceptual split — plan decides, engine executes, engine never
> modifies the plan — lives in [`strategy.md` § 5](strategy.md#5-plan--execute-boundary).
> This section is the code-level companion: which Python module owns
> each gap, which config key supplies each default, which test pins
> the semantic.

cam-flow splits decisions across two sources:

- **Plan** (`workflow.yaml`): what the workflow author declared. The
  plan is authoritative when it speaks.
- **Runtime** (engine + prompt_builder): heuristics and defaults that
  apply only when the plan is silent.

```
  ┌─────────────────────────────┐
  │  camflow plan "<request>"   │    (planner package — NEW)
  │   ├─ collect CLAUDE.md      │
  │   ├─ discover skills/       │
  │   ├─ assemble env info      │
  │   ├─ build prompt + few-    │
  │   │   shot examples         │
  │   ├─ LLM call (anthropic    │
  │   │   SDK → claude CLI)     │
  │   ├─ parse YAML             │
  │   ├─ validate DSL           │
  │   └─ validate plan quality  │
  └────────────┬────────────────┘
               │ writes
               ▼
     ┌──────────────────────┐
     │   workflow.yaml      │  ← author-owned (plan)
     │   methodology:       │
     │   escalation_max:    │
     │   max_retries:       │
     │   allowed_tools:     │
     │   verify:            │
     │   timeout:           │
     └──────────┬───────────┘
                │ loaded by
                ▼
     ┌──────────────────────┐
     │  Engine (runtime)    │
     │                      │
     │  Plan-silent defaults│ ←  engine/methodology_router.py
     │  fill in when fields │    engine/escalation.py (max_level=4)
     │  aren't declared     │    EngineConfig.max_retries=3
     │                      │
     │  Plan-specified      │ ←  node.methodology (label)
     │  fields always win   │    node.escalation_max
     │                      │    node.max_retries
     │                      │    node.allowed_tools
     │                      │    node.verify (runs cmd after success)
     │                      │    node.timeout
     └──────────────────────┘
```

Concretely, per-node fields recognized by the engine:

| Field | Plan wins over | Default when absent |
|-------|----------------|---------------------|
| `methodology: "rca"` | keyword routing in `select_methodology` | keyword match on `do` + `with` |
| `escalation_max: 2` | uncapped L0..L4 ladder | `max_level=4` (ladder reaches L4 on enough retries) |
| `max_retries: 5` | `EngineConfig.max_retries` | 3 (engine config default) |
| `allowed_tools: [...]` | (no runtime heuristic) | full default tool set |
| `verify: "cmd"` | agent's self-reported status | no verification — agent's word is final |
| `timeout: 120` | `EngineConfig.node_timeout` | 600 s |

A good mnemonic: the keyword methodology router is a "second opinion"
the runtime offers; the plan's explicit `methodology:` is a
"first-party instruction." Similarly, an agent's `status: "success"`
is a self-report; `verify:` is an external proof. Plan-level fields
let the author demand the proof when the stakes warrant it.

See `tests/unit/test_plan_priority.py` for the 14 tests that pin
these semantics.

---

## What isn't here

- `backend/cam_lite/`, `backend/sdk/` — placeholders, no runtime yet.
- `backend/base.py` — abstract `Backend` class retained for the SDK
  phase; not used by CAM backend today.
- `engine/recovery.py`, `engine/retry.py` — wired into CAM engine but
  public surface is minimal (see above).
- Example workflows and tests live in `examples/` and `tests/`
  respectively; not documented here because they illustrate rather
  than define architecture.

---

## Per-component evaluation summary

See `docs/evaluation.md` for the full metrics table and measurement
plan. Every entry above references a "How to measure" that maps to
a trace-log query.
