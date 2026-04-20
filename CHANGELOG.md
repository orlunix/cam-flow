# Changelog

All notable changes to cam-flow. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
dates are ISO-8601.

## [Unreleased]

### Added (2026-04-19, strategy doc + docs cleanup)

- **`docs/strategy.md`** — NEW, the single source of truth for how
  cam-flow works. Eight sections: agent management strategy (every
  node spawns a new camc agent, engine owns the lifecycle), context
  management strategy (stateless execution, state.json is the only
  bridge), node execution strategy (preflight → execute → verify),
  DSL reference (every `do:` form + every node field), plan/execute
  boundary (plan decides, engine executes, engine never modifies
  the plan), preflight principle (cheap check before expensive op —
  universal, not hardware-specific), inline → agent → skill
  promotion (when to extract), and CLI reference (every shipped +
  planned subcommand).
- **`docs/architecture.md`** trimmed to a code-level companion.
  Strategy-duplicating sections replaced by pointers to strategy.md:
    * "DSL v2: inline → agent → skill promotion" (full block
      moved to strategy § 7)
    * "User-facing lifecycle" (conceptual overview moved; code
      diagrams + module-ownership table retained)
    * "Plan vs Runtime boundary" (conceptual split moved; per-module
      ownership table + test pointer retained)
  Top of architecture.md now opens with a banner pointing readers
  at strategy.md first.
- **`docs/cam-phase-plan.md`** — banner added marking it HISTORICAL.
  Superseded by strategy.md; retained only for the reasoning chain
  (why file-first completion detection, why dual-signal polling,
  etc.) that isn't captured elsewhere.
- **`docs/roadmap.md`** — new SHIPPED row for the strategy doc +
  docs cleanup.

### Removed (2026-04-19, `agent claude` sentinel)

- **`agent claude` no longer a special anonymous sentinel.** DSL v2
  treats the name `claude` identically to every other agent name:
  `load_agent_definition("claude")` looks for
  `~/.claude/agents/claude.md` and returns None if absent (so the
  node runs anonymously with the default agent). The behavior of
  existing `do: agent claude` nodes is preserved whenever that file
  doesn't exist — they still run anonymously — but the special-case
  hard-coded `if name == "claude": return None` branches in
  `node_runner.py::_resolve_agent_def` and
  `engine.py::_resolve_agent_def` are gone.
- **Planner prompt now emits inline prompts instead of `agent claude`.**
  `PLANNING_RULES` in `prompt_template.py` explicitly instructs the
  LLM: *"Do NOT emit `agent claude` — that was a legacy anonymous
  sentinel and is no longer accepted; use an inline prompt instead."*
  All three few-shot examples in `planner/examples.py` rewritten to
  use inline prompts (`do: |` block scalars) and `shell` for
  deterministic commands.
- **Test-fixture migration.** Every `{"do": "agent claude"}` dict
  fixture in unit / integration / resume / error_injection tests
  migrated to either an inline prompt (where it models the
  "anonymous task" case) or `agent placeholder` (where it models
  a generic agent-node-with-persona that's not under test). 14
  test files touched; behavior preserved.
- **New test: `test_dispatch_agent_claude_resolves_when_file_exists`**
  demonstrates that if someone DID write `~/.claude/agents/claude.md`,
  the DSL treats it like any other agent — nothing about the name is
  magic. Sister test
  `test_dispatch_agent_claude_tries_loader` pins
  `CAMFLOW_AGENTS_DIR` to an empty dir and confirms the anonymous
  fall-through still works. 344 unit tests passing (+1 net).

### Added (2026-04-19, resume + CLI wrapper)

- **`camflow resume <workflow.yaml>` subcommand**
  (`src/camflow/cli_entry/resume.py`). Pick up a stopped, failed,
  aborted, or done workflow without restarting from scratch. Behavior:
    * `failed` / `aborted` / `engine_error` → auto-flip status back
      to `running`, retry the failed node. Resets `retry_counts` and
      `node_execution_count` for that pc only (other nodes keep their
      counts; loop guard remembers the rest of the workflow).
    * `done` → refuses without `--from <node_id>`; explicit re-run
      from a chosen node only.
    * `waiting` → refuses without `--retry` (waiting on an external
      event is intentional; don't break it silently).
    * `running` → no edits, hand off to engine (auto-resume case).
  Flags:
    * `--from <node>` jump pc, also clears `state.blocked` and
      `state.last_failure` (you're choosing a new starting point, not
      retrying the same broken node).
    * `--retry` force-flip non-running statuses.
    * `--dry-run` apply state edits and print what would happen but
      don't spawn the Engine — useful for verifying the resume plan
      before a long-running workflow takes off.
  Preserves `completed`, `lessons`, `failed_approaches`, `trace.log`
  unchanged. The state-mutation logic is in `_prepare_state` (pure
  function, fully unit-tested).
- **`bin/camflow` wrapper script** — symlink-friendly Bash wrapper
  that resolves the repo root via `readlink -f` and exec's
  `camflow.cli_entry.main`. Lets users put a `camflow` symlink on
  PATH (e.g. `~/bin/camflow → <repo>/bin/camflow`) and run
  `camflow plan ...`, `camflow resume ...`, etc. without the
  `PYTHONPATH=… python3 -m …` boilerplate.
- **Tests** — `tests/unit/test_resume.py` (23 tests) covers the
  pure `_prepare_state` matrix (each terminal status × each flag
  combination), retry-budget reset semantics, the CLI wrapper
  end-to-end with a stubbed Engine, and argparse wiring. 343 unit
  tests passing.

### Added (2026-04-19, planner scouts + promotion guideline)

- **`src/camflow/planner/scouts.py`** — read-only environment + skill
  discovery for the Planner. Two scout types:
    * `run_skill_scout(query)` — `skillm search` first; falls back to
      walking `~/.claude/skills/` and `skills/`. Returns at most 5
      candidates with the first 50 lines (1500 chars) of each
      SKILL.md. Bounded JSON / plain-text output parsing.
    * `run_env_scout(checks)` — `which <tool>` + best-effort
      `--version` per tool; `path:<abs>` probes with `os.path.is*`.
  Hard guarantees: READ-ONLY, BOUNDED, 30 s per-subprocess timeout,
  GRACEFUL (missing skillm / missing tools never raise — always
  return a structured warning entry). `default_scout_fn(type, query)`
  dispatcher for callers that want a single callable.
- **`camflow scout` CLI subcommand** (`src/camflow/cli_entry/scout.py`)
  — thin wrapper over scouts.py for Option B. Emits JSON to stdout:
    ```
    camflow scout --type skill --query "RTL trace analysis"
    camflow scout --type env --query vcs --query smake --query p4
    camflow scout --type env --query path:/home/x/rtl
    ```
  `--pretty` toggles indented output. Wired into `cli_entry/main.py`.
- **`camflow plan --scout-report <file>`** flag — accepts a JSON
  report file (or `-` for stdin) produced by `camflow scout`. Repeat
  the flag for multiple reports. The reports flow into
  `build_planner_prompt(scout_reports=...)` and are rendered as a
  `## Scout reports (read-only, already ran)` section in the planner
  prompt. Hard cap at `MAX_SCOUT_REPORTS = 3` entries.
- **Planner prompt scout doc block** — new "Scouts (read-only context
  probes)" section in `PLANNING_RULES` tells the Planner LLM how to
  interpret the scout reports section, and what to do when scouts
  return no candidates (fall back to agents → inline; add
  `preflight:` for missing tools).
- **camflow-manager Phase 3.0 SCOUT** — the manager skill now
  documents the scout-then-plan pattern: spawn scouts via Bash
  before calling the Planner, save JSON reports, pipe them in via
  `--scout-report`. Capped at 3 scout calls per planning session.
- **Inline → agent → skill promotion guideline** — new section in
  `docs/architecture.md` ("DSL v2: inline → agent → skill promotion")
  with an explicit decision table and ordered promotion rules:
  inline if one-off, promote to agent for reused personas,
  promote to skill for reused procedures. The Planner uses the same
  rule (skill-first when a scout-confirmed skill matches).
- **Tests (28 new, 320 total passing)**:
    * `tests/unit/test_scouts.py` (17 tests) — skillm JSON / plain
      text / nonzero exit / timeout / missing skillm / fallback
      walk / candidate cap / env tool present / missing / path
      probe / unknown spec / cap / version probe failure /
      dispatcher.
    * `tests/unit/test_scout_cli.py` (6 tests) — JSON output, pretty
      indent, multi-query env, missing-query error, unknown type
      rejected.
    * `tests/unit/test_planner.py` (5 new) — scout reports rendered
      for skill + env shapes, capped at 3, planning rules describe
      scouts, no section without reports.

### Added (2026-04-19, DSL v2)

- **`shell <command>` node type.** Preferred spelling for shell
  commands. `cmd <command>` still accepted as a deprecated alias —
  existing workflows continue to work.
- **`agent <name>` node type.** References a sub-agent definition at
  `~/.claude/agents/<name>.md`. New module
  `src/camflow/backend/cam/agent_loader.py` parses the frontmatter
  (name, description, model, tools, skills) and body (system prompt),
  and `build_prompt` injects a persona block + role line. Legacy
  `agent claude` remains accepted as the anonymous default.
- **`skill <name>` node type.** Invokes an installed skill via an
  agent session with an "Invoke the skill named X" preamble.
- **Inline prompt node type.** Any `do` value that doesn't start with
  `shell`/`cmd`/`agent`/`subagent`/`skill` is treated as a free-text
  prompt to the default agent — `do: "Fix the bug in calculator.py"`.
- **`classify_do(do) -> (kind, body)`** helper in
  `src/camflow/engine/dsl.py` — single source of truth for DSL
  parsing. Used by node_runner and engine.py's inlined dispatcher.
- **`preflight: <shell>` node field** for two-layer validation.
  Engine runs preflight BEFORE every node body; on non-zero exit the
  body is skipped and the node fails immediately as
  `PREFLIGHT_FAIL`. Timeout, error, and template-ref substitution
  paths all covered. Pairs with the existing post-agent `verify:`
  gate — preflight answers "can I even start?", verify answers
  "did I succeed?".
- **`model:` node field** — per-node model override hook. Validated
  in DSL; plumbed through `agent_def` for future hard enforcement
  (camc has no `--model` flag today).
- **Planner DSL v2 rewrite.** `src/camflow/planner/prompt_template.py`
  now describes the new node types, embeds the preflight and
  OUTCOME-not-OUTPUT rules, carries a verify + preflight cookbook,
  and ingests an **Agent catalog** block built from
  `agent_loader.list_available_agents()`.
- **Domain rule packs.** `DOMAIN_PACKS` dict with `hardware`,
  `software`, `deployment`, `research` entries; `--domain` flag on
  `camflow plan` selects one; `generate_workflow(domain=)` threads
  it through. Unknown domains fall through silently.
- **Tests.** `tests/unit/test_agent_loader.py` (8 tests),
  `tests/unit/test_node_runner_dispatch.py` (8 tests),
  `tests/unit/test_preflight.py` (9 tests), plus DSL-v2 additions to
  `test_dsl.py` and `test_planner.py`. Full suite: 292 passing.

### Changed (2026-04-19)

- **`planner/validator.py::_is_cmd_node`** now matches both
  `shell ` and `cmd ` prefixes (DSL v2 canonical + legacy alias).
- **`NODE_FIELDS`** extended with `preflight` and `model`.
- **`build_prompt` / `build_retry_prompt`** take optional
  `agent_def=` and `inline_task=` kwargs. Backwards-compatible —
  default None preserves prior behavior.

### Deprecated (2026-04-19)

- **`cmd <command>`** in DSL — still accepted, but `shell <command>`
  is the canonical spelling. Existing workflows don't need migration.
- **`EXECUTOR_TYPES`** set export from `engine/dsl.py` — kept as an
  alias of `EXECUTOR_KEYWORDS` for importers, but DSL v2 no longer
  uses it for validation (inline prompts have no keyword).

### Added (2026-04-19, final skill architecture)
- **`camflow-manager` skill** (`skills/camflow-manager/SKILL.md` +
  `~/.claude/skills/camflow-manager/SKILL.md`) — **the sole
  user-facing skill** for cam-flow. Full 8-phase lifecycle: GATHER
  (requirements interview) → COLLECT (resources: skillm list,
  agents, tools, CLAUDE.md, env) → PLAN (call `camflow plan` as an
  internal tool) → REVIEW (dependency check, per-node Q&A, explicit
  user approval required) → SETUP (write workflow.yaml + CLAUDE.md +
  `.camflow/config.yaml` + CLI state seed) → CONFIRM → KICKOFF
  (CLI → `/loop camflow-runner`; CAM → `nohup` engine then EXIT) →
  POST (separate invocation: state check, `camflow evolve report`,
  REPORT.md). Standing rule: "when uncertain about ANYTHING, ASK
  THE USER." 11 hard interaction rules including "exit after CAM
  kickoff — do NOT poll."
- **Four-component architecture** now explicit: **Manager**
  (user-facing camflow-manager skill) + **Planner** (`camflow plan`
  CLI, called by manager) + **Engine** (Python process for CAM
  mode) + **Runner** (camflow-runner skill for CLI mode). Users
  only interact with Manager; the other three are internal tools.

### Changed (2026-04-19)
- **`camflow-runner` state path.** Primary state file is now
  `.camflow/state.json` (co-located with CAM mode), with
  `.claude/state/workflow.json` kept as a legacy fallback for
  pre-0.4 projects. Runner reads-through and writes back to the
  same path it loaded from — no mid-flight migration.
- **`camflow-runner` description** clarified as an **internal tool**
  used by camflow-manager in CLI mode; users drive it via
  `/loop camflow-runner` rather than calling it directly.

### Deprecated (2026-04-19)
- **`camflow-creator` skill** — superseded by `camflow-manager`,
  which covers the full lifecycle (gather + collect + plan +
  review + setup + kickoff + post) rather than just setup.
  File kept for historical reference; description marks it
  DEPRECATED.
- **`camflow` skill** (no hyphen — the babysit variant) — superseded
  by the clean split between camflow-manager (project management)
  and camflow-runner (execution). The old skill combined the two
  concerns. File kept for historical reference.
- **`cam-flow` skill** — description updated to point at
  camflow-manager rather than the now-deprecated camflow-creator
  (earlier deprecation pointed the wrong way).

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
