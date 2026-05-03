# camflow — project memory for future Claude sessions

This is a from-scratch rewrite. Earlier overgrown versions live on
`archive/phase-abc-2026-04` — **do not resurrect** any of those
deleted modules (planner/, evolution/, registry/, steward/,
error_classifier/, brainstorm/, ...) without explicit user consent.
They were cut on purpose.

## What it is

**camflow = camc + flow.** A self-hosting, prompt-driven multi-agent
workflow runner.

```
camc    run "<prompt>"   →  one agent, hopes for the best
camflow run "<prompt>"   →  Planner compiles prompt → DAG → Runtime
                            executes each node with retry + verify
```

User-facing CLI mirrors `camc run`: one mandatory prompt, two verbs
(`run`, `resume`). Everything else is internal.

## Three load-bearing principles

1. **Self-hosting via the Planner builtin workflow.**
   `camflow run` invokes the **Planner workflow** at
   `builtin/planner/workflow.yaml`, which compiles the user prompt
   into a `workflow.yaml`. The runtime then executes that. The Planner
   is itself a normal camflow workflow — it goes through the same
   Workflow/Node/retry/halt/trace machinery as any user workflow.
   Every fresh-prompt run goes through Planner; `resume` and
   `run --from <node>` re-use the existing compiled workflow.yaml.

2. **Two classes only: `Workflow` and `Node`.**
   - `Workflow` — runtime instance, the scheduler. Has `lifecycle ∈
     {running, done, halted}`. **Doesn't run, doesn't verify.** Its
     only behavior is `execute_dag()` (pick ready node, delegate,
     decide retry/halt).
   - `Node` — atomic execution unit. Has `lifecycle ∈ {waiting,
     running, done}` and `result ∈ {success, fail}`. Methods: `run()`
     (skill or tool), `verify()` (criterion / command / human),
     `execute_attempt()` (run + verify + persist).

3. **Every LLM invocation goes through `camc_lib.run_and_collect`.**
   `claude -p` and direct Anthropic SDK calls are forbidden. If any
   path in the runtime calls an LLM another way, that's a bug.

## Spec is source of truth

[`docs/spec-v1.1.md`](docs/spec-v1.1.md) is the canonical spec.
Doctrine rules at the bottom — any change that contradicts those
needs an explicit RFC. Read it before changing runtime semantics.

Highlights of the v1.1 spec to keep top-of-mind:

- **Prompt is the only external input.** No `--state`, no `--inputs`,
  no `state:` / `inputs:` YAML, no `{{state.X}}` templates, no
  `run.input:` field. Cross-node data is auto-injected as
  `# Upstream Outputs`. Templating exists only inside `verify.command`
  with the single namespace `{{nodes.<id>.output.X}}`.
- **`workflow.context`** — shared prompt block injected into every
  node. Planner writes the user's original prompt + run-constants here.
- **Skill is the default run executor.** `run.tool:` is allowed only
  when **all five** §10 criteria hold (known command + fully-determined
  inputs + script-structured output + idempotent + cost matters). When
  in doubt, use skill.
- **`verify.command`** — bash exit code as the deterministic gate.
  Verify-side commands are unconstrained — use them freely for
  pass/fail checks.
- **`verify.human`** is opt-in via **two distinct mechanisms**, both
  off by default:
  1. **Plan-level approval** — opted into via the `-i` / `--interactive`
     CLI flag. `camflow run -i "<prompt>"` patches Planner's
     `render_yaml` at startup to require human approval of the
     compiled workflow.yaml before runtime executes it. Without `-i`,
     fire-and-forget — Planner finishes and runtime executes.
  2. **In-flow node approval** — opted into via the user's prompt
     language. Planner's `workflow_designer` detects requests like
     "show me X before doing Y" and inserts `verify: human` on the
     relevant user-workflow node.

  `camflow run "<prompt>"` (no `-i`, prompt has no in-flow ask) =
  zero human gates. That's the default.
- **Retry is a counter, not an expression.** No `retry.until`. On
  retry, runtime auto-injects `previous` (last attempt's envelope)
  into the next attempt's input.
- **Status is binary: `success` or `fail`.** No third value, ever.
- **Halt is workflow-level only.** Nodes don't halt; they end
  done+success or done+fail.
- **Strict skill registry.** Workflow load fails if any referenced
  skill is missing. No dynamic creation.
- **Resume = retry the halted node once.** `camflow resume <run_dir>`
  restores Workflow + Node state from disk, resets the halted node to
  `waiting`, bumps `retry_max += 1` (one more attempt), and re-enters
  `execute_dag`. Optional `--feedback "<text>"` splices into the
  halted node's last-envelope `feedback` field — surfaces as
  `previous.feedback` on the next attempt, same channel as
  agent-rejected retries. See spec §13 for full semantics.
- **`--steps N` is the debug breakpoint.** Both `run` and `resume`
  accept `--steps N` (N ≥ 1) — halt cleanly after N node-attempts.
  The halt is `kind="breakpoint"` (not `"halt"`); downstream nodes
  stay pristine; resume picks up without resetting state. Built on
  the existing halt+resume infrastructure, no new lifecycle states.
  See spec §14.
- **`camflow run --from <node_id>`** — re-execute a specific node
  and all its downstream descendants (their inputs depend on it, so
  they must re-run too). Upstream stays as-is. Operates on
  `./.camflow/run/` by default; pass `--run-dir <path>` to point
  elsewhere. Mutex with prompt — pass either prompt OR `--from`,
  never both. Works on halted OR completed runs. Common use: "I
  changed SKILL.md for node X, redo from X." See spec §14.

## Project layout

```
camflow/
├── README.md
├── LICENSE
├── pyproject.toml                     # entry: camflow = "runner_v2.runtime:main"
├── docs/
│   ├── spec.md / spec-v1.0.md         # historical
│   └── spec-v1.1.md                   # ← source of truth, read this first
├── src/runner_v2/                     # the v1.1 runtime — only path
│   ├── camc_lib.py                    # camc subprocess wrapper
│   └── runtime.py                     # Workflow + Node + execute_dag + CLI
├── builtin/planner/                   # the self-hosting Planner workflow
│   ├── workflow.yaml                  # understand → design_dag → render_yaml
│   └── skills/
│       ├── prompt_analyzer/SKILL.md
│       ├── workflow_designer/SKILL.md
│       └── yaml_writer/SKILL.md
├── skills/                            # global skill registry
│   ├── analyzer/, evaluator/, reviewer/   # tracked
│   └── code_writer/                   # untracked (v1.0 content; v1.1 update pending)
├── examples/
│   ├── README.md
│   └── bug-fix-compiled/              # reference workflow.yaml shape
├── examples-v1.0-archive/             # the 6 old hand-authored examples
└── tests/test_v2.py                   # full test suite, no LLM cost
```

## How to run

```bash
# install (editable)
pip install -e .

# run a workflow from a prompt
camflow run "<your task description>"

# resume after a halt
camflow resume .camflow/run

# inspect mid-run
cat .camflow/run/trace.jsonl
cat .camflow/run/halt.json     # only if halted

# stop a run
kill $(cat .camflow/run/runner.pid)

# tests
pip install -e '.[test]'
pytest tests/test_v2.py -q
```

## Run dir layout

Every `camflow run` writes to `<project>/.camflow/run/`:

```
.camflow/run/
├── prompt.txt                         # the original user prompt
├── workflow.yaml                      # compiled IR (from Planner)
├── trace.jsonl                        # event stream
├── runner.pid                         # while running
├── halt.json                          # only if halted
├── planner/                           # Planner's own sub-run dir
│   ├── workflow.yaml                  # = builtin/planner/workflow.yaml
│   ├── trace.jsonl
│   └── nodes/{understand,design_dag,render_yaml}/attempt-N/
└── nodes/<user_node_id>/attempt-N/
    ├── input.json                     # upstream + previous (auto-injected)
    ├── prompt.txt                     # full prompt sent to camc (skill nodes)
    ├── agent_output.json              # what the agent wrote
    ├── output.json                    # validated envelope
    └── verify-N/                      # only when verify=agent
        ├── prompt.txt
        ├── agent_output.json
        └── output.json
```

The Planner sub-dir mirrors the same shape — Planner's own execution
is fully inspectable as just another camflow run.

## Conventions / decisions

- **`runner_v2/` not `camflow/`** as the package dir — `camflow` is
  the project name + CLI, `runner_v2` is the Python package.
- **Single-file runtime** — `runner_v2/runtime.py` is intentionally
  one file (~1300 lines). Don't split preemptively. If it crosses
  ~1800 lines, candidates are `expr.py` + `template.py` + `verify.py`.
- **Strict expressions**: `{{nodes.missing}}` raises `ExprError`. No
  `?` opt-out marker.
- **Persisted artifacts**: every attempt writes prompt.txt + input.json
  + output.json + agent_output.json. Highly debuggable. Don't remove.
- **No CI yet**: tests run locally. Don't add `.github/` until project
  has stabilized.

## DO / DON'T

**DO**
- Read [`docs/spec-v1.1.md`](docs/spec-v1.1.md) before changing
  runtime semantics.
- Read this file (CLAUDE.md) before adding new files or features.
- Run `pytest tests/test_v2.py -q` after any runtime change. Fast,
  no LLM cost.
- A real `camflow run "<small task>"` smoke test is the right way to
  verify the Planner chain end-to-end — but it does spend LLM credits.
  Only run when explicitly asked.

**DON'T**
- **Don't bring back `state:` / `inputs:` / `--state` / `{{state.X}}`
  / `run.input:`.** All cut in v1.1 on purpose. Per-run input = the
  user prompt; Planner compiles it; that's the only path.
- **Don't bypass the Planner for fresh prompts.** No
  `camflow exec workflow.yaml`, no `--validate`, no positional yaml
  argument. Every fresh-prompt `camflow run "<prompt>"` goes through
  Planner. (`camflow resume` and `camflow run --from <node>` re-use
  the existing compiled workflow.yaml on disk — they don't re-invoke
  Planner, and that's correct.)
- **Don't insert `verify: human` anywhere by default.** Both flavors
  are opt-in (CLI `-i` for plan-level; user-prompt language for
  in-flow node-level). `camflow run "<prompt>"` should run end-to-end
  with zero pauses unless the user asked for one.
- **Don't let user workflows kick off other workflows.** Nodes do
  ONE camc spawn each (a skill agent or a tool), and they don't
  recursively start `camflow run`. The **Planner builtin is the sole
  system-level exception** to this rule — `camflow run` itself is
  implemented by running the Planner workflow whose output (a
  workflow.yaml) the runtime then executes. That recursion lives in
  the runtime CLI dispatch, NOT inside any user node. If you find
  yourself wanting a node to spawn its own DAG, the right shape is
  more nodes in the parent DAG; the Planner can be invoked again to
  redesign if needed.
- **Don't bypass camc.** Runtime starts agents via `camc run` only —
  never `claude -p`, never the Anthropic SDK. Reach for `cam` only
  when crossing machines.
- **Don't loosen the `run.tool:` 5-criterion gate.** If you find
  yourself wanting tool but not all 5 hold, it's a skill.
- **Don't push `--force` to remote** (origin has leftover Phase-A
  commits that need explicit user authorization to force-push).

## Pointers

- Workflow + runtime spec: [`docs/spec-v1.1.md`](docs/spec-v1.1.md)
- Memory (per-conversation notes):
  `~/.claude/projects/-home-hren--openclaw-workspace-camflow/memory/`
- Old codebase (do not import from): `archive/phase-abc-2026-04` branch
