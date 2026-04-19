# Changelog

All notable changes to cam-flow. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
dates are ISO-8601.

## [Unreleased]

### Added
- `docs/architecture.md` — complete module + public function
  reference with per-component evaluation metrics.
- `docs/evaluation.md` — metrics table, trace-field additions,
  A/B experiment harness design, measurement plan.
- `CHANGELOG.md` — this file.
- Token-counting and evaluation fields in `tracer.build_trace_entry`:
  `prompt_tokens`, `context_tokens`, `task_tokens`,
  `tools_available`, `tools_used`, `context_position`,
  `enricher_enabled`, `fenced`, `methodology`, `escalation_level`.
  All default to None / 0 / "middle" / True / "none" / 0 so existing
  tests and callers are unaffected.

### Planned (see `docs/roadmap.md` for the full timeline)
- §5.1 HQ.1 — Context positioning: reorder prompt so CONTEXT leads,
  task comes last (Stanford "Lost in the Middle" fix).
- §5.2 HQ.2 — Observation masking: keep only the latest round's full
  test output; summarize prior rounds in a bounded `test_history`.
- §5.3 HQ.3 — Per-node tool scoping via `allowed_tools` in the node
  DSL, passed through to `camc run --allowed-tools`.
- §5.4 HQ.4 — Multi-layer verification template: fix → lint →
  typecheck → test, each gating the next.
- §6 Checkpoint system — git commit after each successful agent
  node; local / branch / remote modes; `camflow history` +
  `camflow restore <sha>`.
- §4 Exception Handler — methodology router + escalation ladder
  (L0..L4).

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
