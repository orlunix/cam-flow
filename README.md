# cam-flow

Lightweight stateful workflow engine for agent execution.

Designed for workflows that are **not** well modeled as pure DAGs:

- loopable agent flows
- retry and recovery
- wait / human approval / resume
- structured handoff and checkpointing
- deterministic runtime control around `skill`, `cmd`, and `agent`

## Core idea

Workflow execution is modeled as a **stateful graph**:

- Nodes execute `skill`, `cmd`, or `agent`
- Transitions are controlled by a small DSL
- Runtime owns `pc`, `status`, `resume_pc`, `memory`, and `trace`
- Append-only trace, separate from logs and artifacts
- Handoff/checkpoint is a first-class pattern for resume and context compression

## Architecture

cam-flow is split into three layers: **spec → engine → backend**.

```
spec/  (.md)     ── what the workflow language IS
  │                  DSL, contracts, transition rules, policies
  ▼
engine/  (.py)   ── implements the spec
  │                  parser, validator, resolver, state, memory
  ▼
backend/         ── executes workflows (3 modes)
  cli/              pure Claude Coding Agent (agent drives loop)
  cam/              Coding Agent Manager (daemon drives agent)
  sdk/              script-driven (programmatic control)
```

Dependency: `backend/ → engine/ → spec/`

- **spec/** contains only `.md` files. It defines the language, contracts, and rules — no code.
- **engine/** contains `.py` implementations of the spec. Every engine module references which spec it implements.
- **backend/** contains three execution modes. All share the same spec and engine. Backends that invoke Claude CLI (cam, cli) may include `.md` prompt templates.

## Workflow DSL

Defined in `spec/dsl.md`. Workflows are written in YAML.

```yaml
start:
  do: skill analyze
  with: |
    Analyze error: {{state.error}}
  next: fix

fix:
  do: skill fix
  with: |
    Propose a fix for: {{state.error}}
  next: test

test:
  do: skill test
  with: |
    Validate the fix
  transitions:
    - if: success
      goto: done
    - if: fail
      goto: fix

done:
  do: skill summarize
  with: |
    Summarize the final solution
```

### DSL fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `do` | string | yes | Executor type: `skill <name>`, `cmd <command>`, or `agent <name>` |
| `with` | string | no | Task prompt. Supports `{{state.x}}` templates and `@ref` context references |
| `next` | string | no | Explicit next node ID |
| `transitions` | list | no | Conditional routing: `[{if: condition, goto: target}]` |
| `set` | dict | no | State/memory updates |

## Node output contract

Defined in `spec/node-contract.md`. Every node execution must return this schema, regardless of backend:

```json
{
  "status": "success|fail|wait|abort",
  "summary": "...",
  "output": {},
  "state_updates": {},
  "control": {"action": "continue|goto|wait|fail|abort", "target": null, "reason": null},
  "error": null
}
```

- Node fail ≠ workflow fail. A failed node triggers transition resolution.
- `state_updates` and `output` must always be dicts.
- `control.action = null` or `continue` means "proceed via DSL transitions".

## Transition resolution

Defined in `spec/transition.md`. Deterministic priority chain — first match wins:

1. `control.action = abort` → workflow aborted
2. `control.action = wait` → workflow waiting, pc stays
3. DSL `if: fail` rule → goto target
4. DSL condition rules (`output.*`, `state.*`) → evaluated top-to-bottom
5. `control.action = goto` → go to target
6. Explicit `next` → go to that node
7. Default → `done` (if success) or `failed` (if fail)

## Input references

Defined in `spec/input-ref.md`. Two forms in the `with` field:

- `{{state.x}}` — template variable, substituted inline before execution
- `@memory.x`, `@artifact://path` — context reference, resolved into attachments (future)

## Policies

All policies are spec-level definitions (not backend-specific):

- **Retry** (`spec/retry.md`): Max 2 retries per node. Only retryable errors trigger retry.
- **Recovery** (`spec/recovery.md`): When retry budget exhausted → reroute to recovery node.
- **Error classification** (`spec/error-classifier.md`): `PARSE_ERROR` (can't parse JSON) and `NODE_FAIL` (node returned fail). Both retryable.
- **Memory** (`spec/memory.md`): Small mutable working store. Summaries + lessons. Not a log.
- **Supervisor** (`spec/supervisor.md`): Rule-based health monitoring by CAM. No AI in v0.1.
- **Webhook** (`spec/webhook.md`): Event ingress for resume/approval. Cannot mutate trace directly.

## State

Defined in `spec/state.md`:

```json
{"pc": "start", "status": "running"}
```

- `pc`: program counter — current node ID
- `status`: `running`, `done`, `failed`, `waiting`, `aborted`
- `state_updates` from node output are merged via `dict.update()`
- Persisted after each step

## Three backends

All backends share the same spec and engine. They differ in who owns the execution loop.

| Backend | Loop owner | How it works | Best for |
|---------|-----------|--------------|----------|
| **cli** | Coding Agent itself | Agent reads workflow, executes nodes as skills, advances state directly | Interactive dev with Claude Code |
| **cam** | External daemon (CAM) | Daemon picks node → compiles prompt → calls `claude -p` → parses result → resolves transition | Automated pipelines, CI/CD |
| **sdk** | Your script | You control the loop programmatically via Python API | Production integrations |

### CLI backend

The coding agent (e.g. Claude Code) drives everything inside its own session. No external process.

### CAM backend (Coding Agent Manager)

An external daemon process controls the workflow. It calls the agent via `claude -p` for each node, enforcing single-node bounded execution. Includes:
- Prompt compiler with `.md` template
- Subprocess agent caller
- Rule-based supervisor for health monitoring and recovery

### SDK backend

Fully programmatic. You instantiate a client, call `execute_node()`, and handle the loop yourself. Placeholder — not yet implemented.

## Design decisions

### Chosen

- Stateful graph workflow (not pure DAG)
- Small DSL: `do`, `with`, `next`, `transitions`, `set`
- Executor types: `skill`, `cmd`, `agent`
- Three-layer architecture: spec (.md) → engine (.py) → backend
- Three backends sharing one spec: cli, cam, sdk
- Append-only trace
- Memory / trace / artifact separation
- Explicit handoff/checkpoint node pattern
- Deterministic supervisor (rule-based, no AI in v0.1)
- Agent executes one bounded node at a time (cam backend)

### Not in v0.1

- Parallel execution
- BPMN / visual-first modeling
- Distributed workers
- Complex expression language
- LLM-based supervisor reasoning
- Full `@reference` resolution (`@memory`, `@artifact`)
- Handoff artifact generation
- Webhook implementation

## Repository layout

```
cam-flow/
  src/camflow/
    spec/                      Specification (.md only)
      dsl.md                     DSL schema
      node-contract.md           node output contract
      transition.md              transition resolution rules
      state.md                   state schema
      memory.md                  memory policy
      input-ref.md               template & context references
      retry.md                   retry policy
      recovery.md                recovery policy
      error-classifier.md        error classification
      webhook.md                 webhook events
      supervisor.md              supervisor loop

    engine/                    Engine (.py — implements spec)
      dsl.py                     parser + validator
      node_contract.py           result validator
      transition.py              transition resolver
      state.py                   state init / update
      memory.py                  memory store
      input_ref.py               reference resolution
      retry.py                   retry logic
      recovery.py                recovery logic
      error_classifier.py        error classification

    backend/                   Backends
      base.py                    abstract Backend interface
      persistence.py             shared state/trace I/O

      cli/                     Backend: pure Coding Agent
        runner.py                  workflow runner (agent drives loop)
        skill.py                   skill prompt generation

      cam/                     Backend: Coding Agent Manager
        daemon.py                  daemon main loop
        prompt_compiler.py         node prompt builder
        agent_caller.py            subprocess → claude CLI
        supervisor.py              health check / recovery
        node-prompt.md             prompt template for agent

      sdk/                     Backend: script-driven
        executor.py                SDK execution interface
        client.py                  API client (placeholder)

    cli_entry/
      main.py                  CLI entry point

  examples/
    cam/                       CAM backend example
    sdk/                       SDK backend example
  tests/
  archive/                     historical iterations
```

## Long-term direction

cam-flow is intended to become the orchestration kernel for CAM-style agent systems:

- DAG remains useful for simple dependency scheduling
- cam-flow handles loopable, resumable, human-in-the-loop agent execution
- Three backends cover the spectrum from interactive development to production automation

## License

MIT
