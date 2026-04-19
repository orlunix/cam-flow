# Changelog

All notable changes to cam-flow. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
dates are ISO-8601.

## [Unreleased]

### Added (2026-04-19, split lifecycle skills)
- **`camflow-creator` skill** (`skills/camflow-creator/SKILL.md` +
  `~/.claude/skills/camflow-creator/SKILL.md`) — SETUP skill for
  both CLI and CAM modes. Steps 0–6: mode select, requirements
  interview, env investigation, `camflow plan` call, mandatory plan
  review with user, write project files (CLAUDE.md + workflow.yaml +
  `.camflow/`), launch. CAM mode launches engine via `nohup` then
  EXITS. CLI mode writes initial `.claude/state/workflow.json` and
  tells the user to run `camflow-runner` via `/loop`. Same 10 hard
  interaction rules as the deprecated `cam-flow` skill.
- **`camflow-runner` skill** (`skills/camflow-runner/SKILL.md` +
  `~/.claude/skills/camflow-runner/SKILL.md`) — CLI-mode per-tick
  executor. Supersedes the older `workflow-run` skill. Same file
  layout and contract (`.claude/state/workflow.json`,
  `.claude/state/trace.log`, single-node execution per invocation)
  but upgraded to:
    * honor `verify` fields (post-agent gate, same contract as CAM)
    * consume `methodology` hints
    * cap retries at node-level `max_retries`
    * maintain the six-section state shape (`completed`, `blocked`,
      `test_output`, `lessons`, `failed_approaches`, etc.) for
      migration parity between CLI and CAM modes
  Designed to be called by `/loop camflow-runner` — runs one node
  then exits; `/loop` calls again for the next tick.

### Deprecated (2026-04-19)
- **`cam-flow` skill** — description now marks it DEPRECATED and
  points users to `camflow-creator` (setup) + `camflow-runner`
  (CLI execution). The skill file is retained for reference and
  backward compatibility; it will stop being advertised to the
  harness in a future release.

### Added (2026-04-19, lifecycle skills)
- **`cam-flow` skill** (`skills/cam-flow/SKILL.md`, also installed to
  `~/.claude/skills/cam-flow/SKILL.md`) — the definitive user
  interface for setting up and launching cam-flow workflows. Seven
  steps: mode select → requirements interview → env investigation →
  `camflow plan` → mandatory user review → write project files →
  launch engine and EXIT. Post-execution reporting (Step 7) is a
  separate skill invocation. The SETUP agent does not stay alive
  while the engine grinds — no Claude-session cost during a 90-min
  formal-verify run. Triggers on "create a workflow", "set up a
  flow", "automate this", "run a pipeline", "cam-flow", "/flow".
- **`camflow` skill** (`skills/camflow/SKILL.md`) — alternative
  babysit-style lifecycle: a single agent stays alive across plan +
  execute + report, monitoring `state.json` every 30 s, pausing on
  L3+ escalation, verify mismatch, or engine silence. Heavier than
  `cam-flow` but right for workflows where every node needs human
  eyes.
- **`docs/architecture.md` — new "User-facing lifecycle" section**
  with an ASCII diagram showing the three-phase separation
  (setup agent → engine process → report agent).

### Added (2026-04-19, planner)
- **`camflow plan "<request>"` CLI.** Natural-language request →
  validated workflow.yaml in one strong-model call. New
  `src/camflow/planner/` package:
  - `planner.py` — `generate_workflow()`, context collection,
    YAML extraction, ASCII graph rendering.
  - `prompt_template.py` — planner prompt with planning rules and
    verify-condition cookbook.
  - `examples.py` — 3 few-shot workflows (calculator, build+lint+
    smoke, P4 investigation) showing the methodology / escalation /
    allowed_tools / max_retries / verify conventions.
  - `validator.py` — `validate_plan_quality()` returns (errors,
    warnings): empty plan, dangling goto, orphans, cycles without
    `max_retries`, agent nodes missing recommended fields, unknown
    methodology labels, `{{state.x}}` refs without a producer.
  - `llm.py` — pluggable LLM backend: tries anthropic SDK first
    (ANTHROPIC_API_KEY + `anthropic` package), falls back to
    `claude -p` via subprocess, raises `LLMUnavailable` if neither
    works.
  - `cli_entry/plan.py` — the subcommand with `--claude-md`,
    `--skills-dir`, `--output`, `--force` flags.
- **`allowed_tools` passthrough from node to `start_agent`.** Engine
  now reads `node.allowed_tools` and passes it as a kwarg to
  `start_agent`. camc hard-enforcement pending a `--allowed-tools`
  flag on `camc run`; soft prompt-level constraint already rendered
  by `prompt_builder._render_tool_scope`.

### Added (2026-04-18, post-0.2.0 batch)
- **§5.1 Context positioning (HQ.1).** `prompt_builder.build_prompt`
  reordered so the fenced CONTEXT block precedes the role line and
  task body. Stanford "Lost in the Middle" fix — LLMs attend least to
  mid-window content; CONTEXT now sits where attention is highest.
- **§5.2 Observation masking (HQ.2).** `state_enricher` archives the
  previous `test_output` to a bounded `test_history` list (cap 10,
  FIFO) before overwriting with the latest round. Prevents long
  fix→test loops from bloating the prompt while preserving a
  trajectory summary the agent sees in CONTEXT.
- **§5.3 Per-node tool scoping (HQ.3).** Optional `allowed_tools`
  field in the node DSL renders a soft prompt-level constraint
  ("Tools you may use: ..."). `start_agent` accepts the parameter
  for API readiness; hard enforcement (`camc run --allowed-tools`)
  pending camc CLI support.
- **§4.1 Methodology router.** New `engine/methodology_router.py`.
  Keyword-based routing from a node's `do`/`with` text to one of 5
  methodologies: rca / simplify-first / search-first /
  working-backwards / systematic-coverage. Hint injected into the
  prompt; label logged to trace.
- **§4.2 Failure escalation ladder.** New `engine/escalation.py`.
  Maps `state.retry_counts[node_id]` to L0..L4 with distinct
  intervention prompts (Normal / Rethink / Deep Dive / Diagnostic /
  Escalate). Resets on success or node change.
- **§6.1 Git checkpoint (local mode).** New `engine/checkpoint.py`.
  After each successful agent node, engine auto-commits via
  `git init` + `git add -A` + `git commit --allow-empty` with a
  `camflow: <node> iter <N> — <summary>` message. Best-effort;
  workflow proceeds if git is unavailable.
- **Trace evaluation fields wired through engine.** `_finish_step`
  now passes `prompt_tokens`, `tools_available`,
  `context_position="first"`, `methodology`, `escalation_level` to
  every trace entry. `approx_token_count(text)` helper added to
  `tracer.py` for dependency-free token estimation.
- **3 hermes-CCC skill ports** (rough first drafts under `skills/`):
  `systematic-debugging` (10-phase RCA loop), `task-router` (triage
  for start/analyze nodes), `task-decomposition` (break complex node
  into disjoint slices). Attribution to NousResearch Hermes Agent
  preserved in metadata.
- **Trace rollup + `camflow evolve report` CLI.** New
  `src/camflow/evolution/rollup.py` (`rollup_trace`, `rollup_all`,
  `print_report`) and `src/camflow/cli_entry/evolve.py`. Emits
  per-node and per-methodology statistics from one or many trace.log
  files. `cli_entry/main.py` reworked to dispatch:
  `camflow <workflow>` (default) | `camflow evolve report <dir>`.
- **`docs/architecture.md`** — complete module + public function
  reference with per-component evaluation metrics.
- **`docs/evaluation.md`** — metrics table, trace-field additions,
  A/B experiment harness design, measurement plan.
- **`docs/ideas-backlog.md`** — long-memory record of all 31 ideas
  across 7 categories. Each: what / why / source / status.
- **`docs/roadmap.md`** — major rewrite: added §1 Design Principles
  (7 from Pachaar's "Anatomy of an Agent Harness"), §5 Harness
  Quality Improvements, §6 Checkpoint System, §3.2 Agent lifecycle
  + submission (SHIPPED hotfix). Week-by-week timeline.
- **`CHANGELOG.md`** — this file.
- Token-counting + evaluation fields in `tracer.build_trace_entry`:
  `prompt_tokens`, `context_tokens`, `task_tokens`,
  `tools_available`, `tools_used`, `context_position`,
  `enricher_enabled`, `fenced`, `methodology`, `escalation_level`.
  All default to values preserving current behavior.

### Fixed (2026-04-18 — bugs found and squashed today)
- **Agent prompts never submitted** — `camc run "<prompt>"` pastes the
  prompt into the Claude Code TUI input box but does NOT submit it.
  Every fix agent previously sat at `❯ <prompt>` for the full
  node_timeout, then got killed; calculator.py was never modified.
  Fix: new `_kick_prompt(agent_id)` polls the screen for the TUI
  prompt char (`❯`/`›`/`>`) and sends Enter once visible. Fallback
  Enter after 30 s. Idempotent.
- **`--auto-exit` unreliable (camc bug #10).** Idle detection is
  flaky — agents complete the task and write `node-result.json` but
  never voluntarily exit, leaving the engine waiting on a signal
  that never fires. Fix: removed `--auto-exit` from `camc run`. The
  engine now OWNS the agent lifecycle — file-appeared is the
  primary (and only trusted) completion signal; explicit `camc stop`
  + `camc rm --kill` on every termination path.
- **`camc rm --force` no longer recognized.** camc CLI renamed the
  flag to `--kill`. `agent_runner._rm_agent` updated.
- **`cli_entry/main.py` import error.** Was importing the deleted
  `camflow.backend.cam.daemon` module (removed when CAM engine was
  refactored to the `Engine` class). Rewritten to use `Engine` +
  `EngineConfig` with proper CLI flags (`--poll-interval`,
  `--node-timeout`, `--workflow-timeout`, `--max-retries`,
  `--max-node-executions`, `--dry-run`, `--force-restart`,
  `--project-dir`).
- **Agent cleanup leaks (6 dead `camflow-fix` agents observed after
  the calculator demo).** Hardened with four defenses:
  1. **PATH resolution.** `agent_runner.CAMC_BIN` resolved via
     `shutil.which("camc")` at module import; warning to stderr if
     absent. Catches PATH-stripped service-manager launches.
  2. **try/finally in `Engine.run`.** Whatever happens — success,
     failure, signal, uncaught exception — the finally block runs
     `_cleanup_on_exit`.
  3. **Belt-and-suspenders sweep.** New
     `cleanup_all_camflow_agents()` lists the camc registry, finds
     every `camflow-*` name, and removes them. Called from
     `_cleanup_on_exit` after the per-current-agent cleanup.
  4. **Kill-before-start.** New
     `kill_existing_camflow_agents(except_id=None)` invoked
     immediately before each `start_agent` call, so even if a
     previous engine instance leaked, accumulation is bounded at 0.
  Verified live: `camc --json list | grep camflow` returns 0
  leftover agents after a calculator-demo run.

### Changed (2026-04-18)
- Engine `_apply_result_and_transition` no longer maintains a
  `state.last_failure` field; that role is now distributed across
  `state.blocked` + `state.failed_approaches` + `state.test_output`
  via `state_enricher`. `_maybe_capture_lesson` removed —
  `state_enricher` handles lesson dedup + prune as part of the
  result merge.
- `cli_entry/main.py` CLI shape changed from a single positional
  workflow argument to a positional dispatcher: first arg `evolve`
  → `camflow evolve …` subcommand; otherwise treated as the
  workflow path (preserves backward compatibility with existing
  scripts).
- **Engine entry node.** `engine.state.init_state(first_node="start")`
  now accepts the entry node name; `Engine._load_or_init_state()`
  uses the first YAML-declared node as `pc`. `validate_workflow` no
  longer hard-requires a literal `start` node (caught the RV32 ECC
  production workflow where `setup-tree` is first).
- **Plan vs Runtime boundary.** Node-level config now wins over
  keyword-based runtime routing:
  - `methodology: "<label>"` — picks `rca` / `simplify-first` /
    `search-first` / `working-backwards` / `systematic-coverage`
    explicitly; overrides the keyword router.
  - `escalation_max: N` — caps the escalation ladder at Ln so
    non-critical nodes never get promoted past a polite "rethink."
  - `max_retries: N` — per-node retry budget; overrides
    `EngineConfig.max_retries`.
  - `verify: "<shell cmd>"` — after an agent reports success, the
    engine runs this cmd; non-zero exit downgrades the result to
    `status=fail` with `error.code=VERIFY_FAIL` so the transition
    machinery sees a failure.
  `NODE_FIELDS` in `engine/dsl.py` now accepts `methodology`,
  `verify`, `escalation_max`, `max_retries`, `allowed_tools`,
  `timeout`. Full suite: 232 passing (was 218).

### Planned (see `docs/roadmap.md` for the full timeline)
- §5.4 HQ.4 — Multi-layer verification template: fix → lint →
  typecheck → test, each gating the next.
- §6.2–6.4 Checkpoint branch / remote modes + `camflow history` +
  `camflow restore <sha>`.
- §3.3 camc session-ID tracking (camc-side fix).
- §4.3 PreCompact State Preservation — DESCOPED (stateless model
  means no long-running sessions to protect).
- §3.6 RTL test-hex artifact references.
- §7 Skill evolution Phase 2+ (mutation, A/B testing).

## [0.2.0] — 2026-04-18

### Added
- Stateless node execution model with six-section structured state
  schema (`active_task`, `completed`, `active_state`, `blocked`,
  `test_output`, `resolved`, `next_steps`, `lessons`,
  `failed_approaches`, `escalation_level`, `retry_counts`).
- `src/camflow/engine/state_enricher.py` — merges each node_result
  into the six-section state with lessons dedup + FIFO prune,
  completed append + cap, failed_approaches per-node purge on
  success, cmd stdout capture into test_output.
- `src/camflow/backend/cam/prompt_builder.py` — fenced CONTEXT
  block (`--- CONTEXT (informational background, NOT new
  instructions) ---` / `--- END CONTEXT ---`) wraps the state so
  agents don't read history as a new directive.
- `src/camflow/backend/cam/engine.py` — `Engine` class +
  `EngineConfig` dataclass: retry with error classification,
  signal handlers, orphan recovery, per-node and workflow-wide
  timeouts, loop detection, dry-run, progress reporting.
- `src/camflow/backend/cam/agent_runner.py` — `start_agent` /
  `finalize_agent` split so `current_agent_id` persists between
  launch and wait. `_kick_prompt` sends Enter after camc pastes
  the prompt (TUI doesn't auto-submit).
- `src/camflow/backend/cam/tracer.py` — full replay-format trace
  entries with ts_start/end, duration_ms, deep-copied input/output
  state, agent_id, exec_mode, completion_signal, lesson_added,
  event.
- `src/camflow/backend/cam/orphan_handler.py` — on engine resume,
  detects whether an agent is still alive (WAIT), completed
  already (ADOPT_RESULT), or died (TREAT_AS_CRASH).
- `src/camflow/backend/cam/cmd_runner.py` — cmd subprocess
  execution with stdout (2000 char) / stderr (500 char) tails
  promoted to state.
- `src/camflow/backend/cam/progress.py` — stdout progress line +
  `.camflow/progress.json` for external monitoring.
- `src/camflow/engine/error_classifier.py::retry_mode(error)` —
  returns "transient" / "task" / "none" to drive context-aware
  retry vs. blind retry.
- `src/camflow/backend/persistence.py::save_state_atomic` /
  `append_trace_atomic` — crash-safe writes (temp + rename +
  fsync + parent dir fsync).
- `src/camflow/engine/memory.py::add_lesson_deduped` with exact-
  string dedup + FIFO prune (max 10).
- `examples/cam/CLAUDE.md` — canonical per-project agent template
  documenting how to read the CONTEXT block and write the output
  contract.
- `docs/roadmap.md` — complete strategic roadmap: design
  principles, current state, critical gaps, harness quality
  improvements, checkpoint system, timeline, open questions.
- `docs/cam-phase-plan.md` — detailed CAM phase implementation
  plan that this release delivers.
- Test suite: 155 tests passing. Unit tests for every module in
  `engine/` and `backend/cam/`; integration tests for stateless
  loop, retry context, lessons flow, dry-run, cmd-only
  end-to-end; error injection for missing result file, loop
  detection, workflow timeout; resume tests for clean / orphan /
  done / missing-node scenarios.

### Fixed
- `agent_runner`: removed `--auto-exit` from `camc run` — camc's
  idle detection is unreliable (bug #10); agents did the work but
  never voluntarily exited. The engine now owns the agent
  lifecycle: file-appeared is the primary (and only trusted)
  completion signal; explicit `camc stop` + `camc rm --kill` on
  every termination path.
- `agent_runner._kick_prompt`: `camc run "<prompt>"` pastes the
  prompt into the Claude Code TUI but does NOT submit it. Every
  fix agent previously sat at `❯ <prompt>` for the full
  node_timeout. Now the engine polls for the TUI prompt char and
  sends Enter to submit.
- `agent_runner`: `camc rm --force` → `camc rm --kill` (camc CLI
  flag was renamed in a recent release).
- `cli_entry/main.py`: was importing the deleted
  `camflow.backend.cam.daemon` module. Rewritten to use
  `Engine` + `EngineConfig` with proper CLI flags
  (`--poll-interval`, `--node-timeout`, `--workflow-timeout`,
  `--max-retries`, `--max-node-executions`, `--dry-run`,
  `--force-restart`).

### Changed
- State schema: free-form `state.error` / `state.last_failure`
  replaced with the six-section structured schema. `last_failure`
  eliminated — its role is now split across `blocked`,
  `failed_approaches`, and `test_output`.
- `engine/transition.py`: added `if: success` shortcut wired to
  match the symmetric `if: fail` branch; cmd nodes now also get
  `{{state.x}}` template substitution on the command string.
- Prompt format: no longer interleaves `Previous lessons` /
  `Last failure` as free-form blocks; rendered inside the fenced
  CONTEXT block with consistent section headers.

### Removed
- `--auto-exit` flag from `camc run` invocations.
- `state.last_failure` field (superseded by `state.blocked` +
  `state.failed_approaches`).
- `src/camflow/backend/cam/daemon.py` (old non-class
  implementation, superseded by `Engine` class).
- `_maybe_capture_lesson` in engine (superseded by
  `state_enricher.enrich_state` doing dedup + prune as part of
  the result merge).

## [0.1.0] — 2026-04-05 (CLI Phase, pre-tag)

### Added
- YAML DSL with 4 node types: `cmd`, `agent`, `skill`, `subagent`.
- `/workflow-run` skill + `/loop` driver for CLI-phase execution
  inside a Claude Code session.
- `.claude/state/workflow.json` state file, JSONL
  `.claude/state/trace.log`.
- Template substitution `{{state.xxx}}` at prompt-build time.
- Calculator demo: 4 bugs fixed over 4 loops, all 11 tests pass
  (validated in the handoff materials, not this repo's test
  suite).
- Experiments documenting skill invocation via `Skill()` tool,
  subagent isolation, lessons accumulation. Full details in
  `camflow-handoff/docs/camflow-cli-research-handoff.md`.

---

## How to read this

- **Unreleased** is what's on `main` right now but not yet tagged.
- Releases are ISO-dated and have a short label.
- "Added / Fixed / Changed / Removed / Planned" follows the
  Keep-a-Changelog convention.
- Each bullet links back to a code path or a roadmap section so
  the change can be traced to intent.
