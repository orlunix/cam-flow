# camflow — project memory for future Claude sessions

This is a from-scratch rewrite (started 2026-04-28) of an earlier camflow
that grew to ~15k lines and got over-engineered. The earlier version is
on the `archive/phase-abc-2026-04` branch. **Do not resurrect** any of
the deleted modules (planner/, evolution/, registry/, steward/, etc.)
without explicit user consent — they were intentionally cut.

## What it is

**camflow = camc + flow.** A multi-agent workflow runner that drives agents through `camc`.

Two foundational principles — both load-bearing:

1. **Multi-agent system.** Three primitives, strictly distinct:
   - **Workflow** = a multi-agent DAG (the artifact). Lives in user space.
   - **Agent** = a single `camc`-spawned execution unit. Autonomous *internally*
     (multi-step, multi-skill, may use Read/Write/Bash) but **one camc run** —
     agents do NOT kick off workflows.
   - **Skill** = a capability template (`SKILL.md` prompt + tool grants).
     Agents USE skills.

   Built-in agent definitions live in `agents/<name>.md`. Built-in skill
   templates live in `skills/<name>/SKILL.md`. Roles like Planner / Evaluator /
   Worker / Orchestrator are AGENTS — `agents/planner.md` (autonomous,
   uses skill_searcher + workflow_designer internally) — NOT
   `skills/planner/SKILL.md` (one prompt) and NOT
   `workflows/planner/workflow.yaml` (sub-DAG). Both alternatives are wrong.

2. **Every LLM invocation goes through `camc run`.** `claude -p` and direct
   Anthropic SDK calls are **forbidden** — if any path in the runtime wants to
   call an LLM another way, that's a bug. See "Core infrastructure" below.

DAG dependencies for modeling, serial runner for execution. Retry is the only
backwards mechanism. Every node output is the same envelope.

> "DAG for dependency modeling, serial runner for execution — agents through camc."

The full language spec is in [`docs/spec.md`](docs/spec.md) (v0.6).
The architectural intent is in [`docs/harness-design.md`](docs/harness-design.md).
**Read these before changing the runner.**

## Core infrastructure: camc (default), cam (multi-machine)

**This is load-bearing. Read carefully.**

The CLIs `camc` and `cam` are the foundational infrastructure of this project.
**All agent spawning must go through them**, not by bypassing to `claude -p`
or the Anthropic SDK.

**Default: `camc`.** Reach for `cam` only when multi-machine orchestration
is explicitly needed.

- `camc` — standalone per-machine client. `camc run` to start an agent, `list/status/logs/stop/kill/send/key/capture/apply`. **The default path for the runner.**
- `cam` — full Coding Agent Manager with server mode (`serve`, `release`, `sync`, `machines.json`). Use only when crossing machines.

Why this is load-bearing:
- camc/cam owns telemetry, lifecycle, heartbeat, orphan handling. Bypassing it loses all of that.
- The Orchestrator (future LLM agent) will drive flows via the same surface, so unifying spawn/stop/inspect there means one mental model.
- camc is local-first / simple-first; cam adds server overhead that's not needed single-machine.

**Current state:** `skill.X` is fully wired through `camc run` (no `claude -p` anywhere). Agent lifecycle: spawn via camc → wait for `<workspace>/agent_output.json` → schema check → if mismatch, `camc send` feedback (inner self-correction loop, capped by `CAMFLOW_SKILL_INNER_RETRIES`, default 3) → `camc kill` to clean up. `agent.X` (autonomous, tool-using) is the v0.8 next step on the same camc surface.

`claude -p` is **forbidden** in the runtime. All LLM invocations go through camc. When parsing camc output, use `camc --json` for machine-callable JSON (note: `camc run` itself is text-only — parse the agent ID via the `Starting <tool> agent <id>` regex).

## Status: v0.7

| Layer | What runs |
|---|---|
| Runtime | `src/runner/runtime.py` — DAG + retry + verify + when + skip propagation, ~810 lines |
| Mocks | `mock:` field on a node returns canned envelope (testing) |
| Tools | `uses: tool.X` runs `tools/X.sh`, stdin = input JSON, stdout = envelope JSON |
| Skills | `uses: skill.X` spawns a one-shot agent via `camc run` (workspace = node attempt dir; agent writes envelope to `agent_output.json`; runner self-corrects on schema mismatch via `camc send`; cleans up via `camc kill`) |
| Agents | `uses: agent.X` → returns `NOT_IMPLEMENTED` (v0.8 — autonomous tool-using agents) |
| CLI | `camflow <workflow> --state <state.json>` via pyproject `[project.scripts]` entry |
| Halt | `status: halted` envelope OR retry-exhausted → workflow_halted (exit 2), `halt.json` sidecar |
| Tests | 38 pytest tests, 0.2 s, all mock+tool path; agent-demo + plan-demo manual (LLM cost) |

## Done

- ✅ Workflow language v0.6 (spec.md). Nodes have id / goal / needs / when / uses / input / output_schema / verify / retry. No `next` / no `goto`.
- ✅ Expression evaluator (App. A): `==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, attribute chains, `[n]` subscript, YAML-style `true`/`false`/`null` literals. Implemented via `ast` whitelist walk, **not** `eval()`.
- ✅ Template renderer (App. B): `{{state.x}}`, `{{nodes.X.latest.output.*}}`, `{{nodes.X.attempts[n].output.*}}` (1-indexed), `{{retry.feedback}}`. Trailing `?` opts into "missing → empty string". **Strict mode**: missing field on existing namespace raises `ExprError` (use `?` to opt out).
- ✅ Retry (simplified): retries **only the current node**. No `target` field. Triggers on `success` + `until` false, OR on node failure. Retry exhausted → halt (not fail).
- ✅ Halt: envelope `status: halted` halts the workflow; retry exhausted halts; node failure with no retry halts. `halt.json` sidecar written at run root. Exit codes: 0 success, 2 halted, 1 failed.
- ✅ State is **read-only** after workflow start. The old DSL `set:` is gone. Cross-node data flow uses `nodes.X.latest.output` references.
- ✅ Run dir: `<project>/.camflow/runs/<run_id>/`. Structure per spec App. C: workflow.yaml snapshot, state.json, trace.jsonl, nodes/<id>/attempt-<n>/{output.json, workspace/{input.json, prompt.txt, response.txt, raw_stdout.txt}}, plus halt.json on halt and runner.pid while running.
- ✅ Workspace dir per attempt: tools `cwd` is `workspace/` + `CAMFLOW_WORKSPACE` env var; skills' prompt + inputs live there too. Future `agent.X` via camc will also `cwd` here.
- ✅ Auto-schema: runner automatically validates `data` against `output_schema` after success. User no longer writes `{type: schema}` in verify; `verify:` is for additional checks (rule, future file/command/agent).
- ✅ `camflow` CLI installed via `pip install -e .`. Entry point `runner.runtime:main`.
- ✅ Planner = 1-node workflow (`uses: skill.planner`, template at `prompts/planner.md`). `camflow plan "<goal>" [--run]` is the user-facing convenience.

## Roadmap

### v0.8 — agent.X (autonomous, tool-using) via camc

`skill.X` already runs through camc (one-shot, runner-driven turn loop with self-correction). v0.8 adds `agent.X`: long-running autonomous agents that decide their own tool use and only emit a final envelope when they consider themselves done.

Implementation will reuse the camc adapter (`_camc_run` / `_camc_send` / `_camc_kill` / `_wait_for_output` already in runtime.py) — the difference is the prompt template (autonomy + tool-grant) and the wait loop (might be longer; might watch for camc `state=idle` after work, not just `agent_output.json` appearing).

`cam` (the multi-machine variant) is reserved for when workflows need to spawn agents on remote nodes — not part of v0.8.

### v0.9 — Orchestrator + extra CLI subcommands

The Orchestrator is a separate LLM agent that **drives the runner via the
camflow CLI**, not by importing the runtime. Two functions:
1. Exception handler — runner emits an event, orchestrator decides retry/abort/escalate.
2. Human-in-the-loop — handles questions / approvals.

This means CLI must grow: `camflow status` / `stop` / `resume` / `inspect-state` / `inspect-trace` / `send-control` / `abort`. All must be machine-callable: stable arg shapes, JSON output, exit codes that distinguish success / user-action-needed / hard-fail.

### Later — Planner

A separate LLM agent that takes a natural-language goal and **generates a
workflow.yaml**. Lives outside the runtime. Will eventually own the
`prompts/` directory (the 4 templates: planner / orchestrator / evaluator
/ worker, currently empty).

## Architecture

The 4-role design from `docs/harness-design.md`:

```
Planner (LLM)        — natural language → workflow.yaml         [Later]
Runner (Python)      — executes workflow.yaml deterministically [Done]
Worker (LLM)         — runs inside skill.X / agent.X nodes      [Done for skill, v0.8 for agent]
Evaluator (LLM)      — judges quality at evaluator-type nodes   [Later — built-in checks for now]
Orchestrator (LLM)   — exception handler + human-in-loop        [v0.9, drives runner via CLI]
```

LLMs are **off the dispatch path**. The runner is pure Python and decides
nothing semantically — it follows the DAG, runs nodes, persists outputs,
and lets the LLMs do reasoning inside nodes. This keeps the engine
deterministic and easy to reason about under failure.

## Project layout

```
camflow/
├── README.md
├── LICENSE
├── pyproject.toml
├── docs/
│   ├── harness-design.md       # design intent, true north
│   └── spec.md                 # v0.6 workflow language spec
├── src/runner/                 # the Python package (named `runner`, not `camflow`, to avoid project-name collision)
│   ├── __init__.py
│   └── runtime.py              # the DAG runner, single file for now
├── prompts/                    # 4 prompt templates, empty until v0.9+
├── examples/
│   ├── echo-retry/             # all-mock smoke
│   ├── retry-demo/             # tools, test self-retries 3x via tester.sh CAMFLOW_ATTEMPT
│   ├── code-review/            # tools, fan-out/fan-in/when-branching
│   ├── halt-demo/              # tools, retry exhaustion → halt (exit 2 + halt.json)
│   └── agent-demo/             # real Claude (skill.X), 3 nodes, ~$0.35 per run
└── tests/test_runtime.py       # 29 tests, pure mock+tool, no LLM
```

## Conventions / decisions

- **`runner/` not `camflow/`**: the package directory holds Python code; `camflow` is the project (and CLI binary) name. Avoid the redundancy.
- **Single file until 5+ files**: `runner/runtime.py` is one ~800-line file. Don't split it preemptively. When it gets to 1500+ lines, then split (likely candidates: `expr.py`, `template.py`, `verify.py`).
- **Strict expression mode**: `{{state.missing}}` raises; use `{{state.missing?}}` to opt into empty-string fallback.
- **One-shot skills via `camc run`**: each skill node spawns a camc-managed Claude agent in the per-attempt workspace. Prompt is compiled deterministically (workflow.goal + node.goal + input + schema instruction + delivery protocol). Agent writes the envelope to `agent_output.json`; runner self-corrects on schema mismatch via `camc send`; cleanup via `camc kill`. Skill templates load from `prompts/<name>.md`.
- **Persisted artifacts**: every attempt writes prompt.txt + response.txt + output.json. Highly debuggable. Don't remove.
- **No CI yet**: tests run locally. Don't add CI/.github until the project has stabilized.

## DO / DON'T for future Claude sessions

**DO**
- Read `docs/spec.md` before changing runtime semantics.
- Read this file (CLAUDE.md) before adding new files or features.
- Run the example workflows after any runtime change to catch regressions:
  ```bash
  pytest tests/ -q
  camflow examples/echo-retry/workflow.yaml --state examples/echo-retry/state.json
  camflow examples/retry-demo/workflow.yaml --state examples/retry-demo/state.json
  camflow examples/code-review/workflow.yaml --state examples/code-review/state.json
  camflow examples/halt-demo/workflow.yaml --state examples/halt-demo/state.json   # expect exit 2
  # agent-demo / plan --run only when explicitly asked — burns LLM credits
  ```
- When adding a new tool script, use `python3 -c '...'` for output, not bash heredoc with f-strings — the bash/Python escaping is a footgun (we hit this on `code-review` first run).

**DON'T**
- **Don't model a role as a single skill OR as a sub-workflow.** Planner /
  Evaluator / Worker / Orchestrator are AGENTS — `agents/<name>.md`
  (autonomous Claude Code session, one camc spawn, multi-step internally). A
  single SKILL.md is too small (one prompt). A sub-workflow (DAG) is too
  big (agents don't kick off workflows). The right shape is one autonomous
  agent that internally uses multiple skills. See `multi_agent_system.md`.
- Don't add features that aren't in the spec.md or roadmap.
- Don't predict-build the Orchestrator / Planner — wait until they're needed.
- Don't restore deleted concepts: methodology routing, escalation levels, error_classifier, result_reader, brainstorm, evolution. They were cut on purpose.
- Don't add `agent.X` in a hurry — the goal of v0.8 is keeping it ~half the lines of the old agent_runner.py. Plan it before coding.
- Don't bypass camc. The runtime starts agents via `camc run` only — never `claude -p`, never the Anthropic SDK. Reach for `cam` only when crossing machines.
- Don't push `force` to remote (origin/main has 17 leftover Phase-A commits from before the reset; needs explicit user authorization to force-push).

## How to run

```bash
# Install (editable mode)
pip install -e .

# Run a workflow
camflow <workflow.yaml> --state <state.json>

# Validate without running
camflow <workflow.yaml> --validate

# Tests
pip install -e '.[test]'
pytest tests/ -q
```

## Pointers

- Workflow language spec: [`docs/spec.md`](docs/spec.md) (v0.6, 472 lines)
- Architecture / true-north: [`docs/harness-design.md`](docs/harness-design.md) (511 lines)
- Memory (this conversation's persistent notes): `~/.claude/projects/-home-hren--openclaw-workspace-camflow/memory/`
- Old codebase (do not import from): `archive/phase-abc-2026-04` branch
