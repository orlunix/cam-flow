# camflow v1.1 spec

A self-hosting, prompt-driven workflow runner for multi-agent DAGs.
**One verb. Two classes. Strict contracts. Same interface as `camc`.**

---

## 0. Mental model

```
camc    run "<prompt>"  →  one agent runs alone, hopes for the best
camflow run "<prompt>"  →  a builtin Planner workflow compiles the prompt
                           into a DAG of high-quality, verified nodes,
                           then a Runtime executes that DAG
```

User-facing CLI is identical in shape: `<verb> "<prompt>"`. The difference is what happens after — camflow self-hosts a Planner that emits a `workflow.yaml`, which the same Runtime then executes node-by-node with retry + verify.

> **camflow uses camflow to compile camflow.** The Planner is not a special
> path — it's a builtin workflow that goes through the same `Workflow`/`Node`
> machinery as user workflows. Same retry. Same halt. Same trace. Same camc
> spawning. If the Planner's nodes fail or halt, you see exactly the same
> halt.json + trace.jsonl format.

```
user prompt                       (the only external input)
   ↓
Planner workflow                  (camflow/builtin/planner/workflow.yaml)
   ↓
workflow.yaml                     (intermediate representation; .camflow/run/workflow.yaml)
   ↓
Runtime executor                  (loads the IR; runs each node via camc)
   ↓
trace.jsonl + per-attempt outputs
```

Mapping to compiler vocabulary:

| compiler concept | camflow equivalent |
|---|---|
| Source code | user prompt |
| Compiler | Planner workflow |
| IR (intermediate representation) | `.camflow/run/workflow.yaml` |
| Linker / interpreter | Runtime executor |
| Build artifacts | `trace.jsonl`, `nodes/<id>/attempt-<n>/output.json` |

There is **no non-interactive bypass** —  every run goes through Planner. If the user wants to skip Planner, they can't. The only way `camflow` runs a workflow is by having Planner produce one (or, on `resume`, replaying a previously-produced one).

---

## 1. The DAG — Workflow and Node

camflow is two cooperating classes:

```python
class Workflow:
    # static (from YAML)
    name           : str
    goal           : str | None
    context        : str | None        # shared prompt injected into every node
    nodes          : list[Node]        # children, in declaration order

    # runtime (mutated during execute_dag)
    lifecycle      : "running" | "done" | "halted"
    step_n         : int               # trace event counter
    run_id         : str
    tag            : str               # camc crash-safety tag

    # I/O paths
    run_dir, trace_path, pid_path, project_root

    # behavior — schedules; does NOT itself run or verify
    def execute_dag()        -> "done" | "halted"
    def expr_ctx()           -> dict
    def trace(event, **f)    -> None
    def halt(node, reason, env) -> None
    def cleanup()            -> None
```

```python
class Node:
    # static (from YAML)
    id              : str
    goal            : str
    steps           : list[str]
    needs           : list[str]
    output_schema   : dict             # field → type
    retry_max       : int              # default 1 = no retry
    run_config      : {"skill": str} | {"tool": str}        # mutually exclusive
    verify_config   : None
                    | {"criterion": str}     # agent verify with override
                    | {"command": str, "timeout"?: int}
                    | {"human": str}         # NEW in v1.1

    # runtime (mutated by execute_attempt)
    lifecycle       : "waiting" | "running" | "done"
    result          : "success" | "fail" | None
    retry_count     : int
    output          : envelope | None
    history         : list[envelope]

    # behavior
    def run(workflow, attempt_n)         -> envelope
    def verify(workflow, envelope)       -> (ok, feedback)
    def execute_attempt(workflow, n)     -> envelope    # = run + verify + persist
    def is_ready(all_nodes)              -> bool
    def is_done()                        -> bool
```

### Why Workflow and Node don't share methods

Different layers, different semantics:

| | `run` does | `verify` does |
|---|---|---|
| Node | calls camc skill / tool — does the actual work | checks one envelope (schema + criterion / command / human) |
| Workflow | **doesn't exist** — Workflow has `execute_dag` (scheduler), not `run` | **doesn't exist** — Workflow's correctness = aggregate of children |

They share **data vocabulary** (`goal`, `lifecycle`, `result` as attribute names) but **not behavior**. Workflow is a composer; Node is an executor.

### DAG scheduling

`Workflow.execute_dag()` is a serial loop:

```
while workflow.lifecycle == "running":
    ready = [n for n in workflow.nodes if n.is_ready(all_nodes)]
    if not ready:
        if all done       → workflow.lifecycle = "done"; return "done"
        else              → deadlock; halt and return "halted"
    pick first ready by YAML declaration order
    envelope = node.execute_attempt(workflow, attempt_n)
    if envelope.success:                node done+success; continue
    if envelope.request_human:          workflow halt (skip retry)
    if node.retry_count < retry_max:    retry_count++; continue (loop picks same node next iter)
    else:                                workflow halt
```

No parallelism in v1.1. One node at a time, deterministic order.

---

## 2. State machines

### Workflow.lifecycle

```
Workflow.lifecycle ∈ { running, done, halted }
```

| value | meaning | exit code |
|---|---|---|
| `running` | at least one node hasn't reached `done` | — |
| `done` | all nodes are `done+success` | 0 |
| `halted` | a node ended `done+fail` (retry exhausted) OR `request_human=true` | 2 |

A `halted` workflow is recoverable via `camflow resume <run_dir>`.

### Node.lifecycle + Node.result

```
Node.lifecycle ∈ { waiting, running, done }
Node.result    ∈ { success, fail }   (only when lifecycle=done)
```

| state | meaning |
|---|---|
| `waiting` | not yet started, or some `needs` not yet `done+success` |
| `running` | currently executing (between-retry transitions stay `running`) |
| `done+success` | run + verify both passed |
| `done+fail` | retry exhausted, or run set `request_human=true` |

Internal counters:

```
Node.retry_count : int   # 0 on first attempt, ++ on each retry
Node.retry_max   : int   # from YAML, default 1 (no retry)
```

`Node.lifecycle` does **not** have a `halted` state — only Workflow halts.

---

## 3. Top-level workflow YAML

```yaml
workflow: <name>                  # display name
version: "1.1"

goal: |                           # optional, top-level intent (planner often fills this)
  ...

context: |                        # optional, shared prompt injected into every node
  Free-form text. Planner typically writes this section to encode:
  - the original user prompt (so every downstream node sees it)
  - run-constants (tree names, paths, tools available, conventions)
  - any inferred preconditions or constraints
  Becomes a `# Workflow Context` block above each node's `# Goal`.

nodes:
  - <Node>
  - ...
```

There is **no `inputs:` section, no `state:` section**. The only external input to a camflow run is the user prompt, which Planner consumes. After Planner is done, the workflow.yaml is fully self-contained — all its variability has been encoded into nodes' goals/steps and the `context` block.

### `context:` semantics

`context` is a literal string injected verbatim into every Node's run-prompt and verify-prompt, between the skill template and the node's `# Goal`. It is **not** templated, **not** mutable.

Use `context` for prompt-shared facts — including the original user prompt, which Planner writes here.

---

## 4. Node YAML schema

```yaml
- id: <string>                    # REQUIRED, unique within workflow
  goal: <string>                  # REQUIRED, one-line intent
  steps:                          # REQUIRED, list[string]
    - "step 1"                    # ← shared between run prompt + verify checklist
    - "step 2"
  needs: [<node_id>, ...]         # optional, default []

  run:                            # REQUIRED
    skill: <skill_name>           # OR tool: <path>  (mutually exclusive)
                                   # NOTE: no `input:` field. Upstream node outputs
                                   #       are auto-injected; no per-run user input.

  output_schema:                  # optional but recommended
    field_name: <type>            # type ∈ {string, integer, number, boolean, array}

  verify:                         # optional; default = agent + steps as criterion
    criterion: <override>         # OR command: <bash>  OR human: <prompt>
    timeout: <int>                # only with command, default 60

  retry: <int>                    # optional, default 1 (first attempt only, no retry)
```

### Required vs optional

| field | required | default |
|---|---|---|
| `id` | ✅ | — |
| `goal` | ✅ | — |
| `steps` | ✅ | — |
| `run` | ✅ | — |
| `run.skill` or `run.tool` | ✅ (one of) | — |
| `needs` | ❌ | `[]` |
| `output_schema` | ❌ | `{}` (no field-presence check) |
| `verify` | ❌ | implicit `agent + steps` |
| `retry` | ❌ | `1` |

### Mutual exclusion rules

- `run.skill` XOR `run.tool` (exactly one)
- `verify.criterion` XOR `verify.command` XOR `verify.human` (at most one)

### No `run.input` field in v1.1

In v1.0 a node could declare `run.input: { key: "{{state.x}}" }` to template per-run user input into its prompt. v1.1 removes this:

* User-supplied per-run state doesn't exist (no `inputs:`/`state:` mechanism).
* Cross-node data flow is automatic: every `needs` node's output is injected into the consumer's prompt as `# Upstream Outputs` (see §8).
* Templating still exists, but only inside `verify.command` (see §7).

---

## 5. Envelope shape (Node output)

Every `Node.execute_attempt` produces one envelope:

```json
{
  "status":         "success" | "fail",
  "data":           { ... },
  "error":          null | { "code": "...", "message": "..." },
  "feedback":       null | "...",
  "request_human":  false
}
```

### Field rules

| field | when required | who writes |
|---|---|---|
| `status` | always | run agent / tool |
| `data` | when `status=success`, must satisfy `output_schema` | run |
| `error` | when `status=fail`, must have `{code, message}` | run, OR runtime (on verify-fail) |
| `feedback` | when verify ran | verify (runtime, agent, command, or human) |
| `request_human` | optional, default `false` | run or verify |

### Status values

```
"success"   = run produced data per schema AND verify passed
"fail"      = run failed, OR verify rejected, OR run timed out
```

No `"halted"`, `"skipped"`, `"ok"`, `"done"`, `"completed"`, etc.
Runtime treats unknown status as fail with `error.code = "BAD_STATUS"`.

### Halt triggers

Workflow halts when **any** of:

1. A Node ends `done+fail` (retry exhausted or no retry configured).
2. A Node sets `request_human=true` in its envelope (skips retry).

Halt writes `<run_dir>/halt.json` and propagates downstream nodes to `done+fail`.

---

## 6. Per-attempt sequence

`Node.execute_attempt(workflow, attempt_n) -> envelope`:

```
1. node.lifecycle = "running"
2. Build attempt input by collecting:
     - upstream outputs (one entry per `needs` id, full envelope)
     - on retry (n>1), `previous` = last attempt's envelope
   Write attempt-N/input.json
3. Call node.run(workflow, attempt_n) → returns envelope
     - skill: spawn camc agent with prompt (see §8)
     - tool:  subprocess; stdin=input.json, stdout=envelope JSON
4. If envelope.status == "fail" → return envelope (caller decides retry/halt)
5. If envelope.status == "success":
     a. auto_schema_check(envelope, output_schema)
        if fails → status="fail", error=VERIFY_FAIL, return
     b. node.verify(workflow, envelope) → (ok, feedback)
        - {command}: bash exit-code
        - {human}: stdin Q&A (see §9)
        - {criterion} or default: spawn evaluator agent
        if not ok → status="fail", error=VERIFY_FAIL, feedback=<reason>, return
6. node.lifecycle="done", node.result="success"
7. Persist envelope → attempt-N/output.json
8. Return envelope
```

### Retry feedback channel

On retry, runtime auto-injects `previous` into next attempt's input:

```json
// attempt-2/input.json
{
  "upstream": { ... },                   // same as attempt-1
  "previous": {                          // auto-injected on retry
    "status": "fail",
    "data": {...},
    "error": {...},
    "feedback": "<verify's reason — could be from agent, command, OR human>"
  }
}
```

The agent reads `input.previous.feedback` to know what went wrong. Particularly important when the previous attempt was rejected by a `verify=human` — the human's complaint becomes the next attempt's feedback.

---

## 7. Templates

In v1.1, templating is dramatically reduced from v1.0. Only one place uses them:

### Allowed location

- `verify.command` string body (bash interpolation).

### Namespaces

| namespace | meaning | example |
|---|---|---|
| `nodes.<id>.output.X` | upstream node's data | `{{nodes.diagnose.output.data.cause}}` |
| `output.X` | the current envelope being verified (verify.command only) | `{{output.data.passed}}` |

`output.X` is exposed only inside `verify.command`. Outside that (e.g. in any future template-bearing field), only `nodes.X.output` is visible.

### Removed in v1.1

- `{{state.X}}` — there's no state/inputs concept anymore.
- `{{inputs.X}}` — same.
- `.latest`, `.attempts[N]`, `retry.*` — never were in v1.0; not coming back.

### Expression operators (verify.command only)

`==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, attribute chain, `[index]`.

---

## 8. Run prompt structure (auto-built by runtime)

```
[skill template / SKILL.md content]    ← from skills/<name>/SKILL.md

# Workflow Context                     ← only if workflow.context is non-blank
<workflow.context>
                                        (Planner usually writes the original
                                         user prompt + run-constants here.)

# Goal
<node.goal>

# Steps (you MUST do these in order)
1. <node.steps[0]>
2. <node.steps[1]>
...

# Upstream Outputs                     ← only if node.needs is non-empty
## <upstream_node_id_1>
<JSON of that node's envelope>

## <upstream_node_id_2>
<JSON of that node's envelope>
...

# Note: previous attempt failed       ← only on retry (attempt_n > 1)
Inputs include `previous` with the last attempt's envelope. Read
`previous.feedback` to know what went wrong; address it this time.

# Output
Write a single JSON envelope to `agent_output.json` in cwd:
{
  "status": "success" | "fail",
  "data": {...matching output_schema},
  "error": null | {"code": "...", "message": "..."},
  "feedback": null,
  "request_human": false
}

## data shape (required when status=success)
<output_schema rendered as field: type list>

## Rules
- status MUST be "success" or "fail" (not "ok"/"done"/etc).
- success → data contains all schema fields with correct types.
- fail → error MUST have non-empty code + message.
- request_human=true to escalate (skips retry, halts workflow).
- Don't print to stdout; only write the file. Don't use markdown fences.
```

For `tool`: prompt N/A. Tool reads `input.json` (containing `upstream` + `previous`) from stdin, writes envelope to stdout.

---

## 9. Verify types

Three mutually-exclusive verify configurations, plus a default:

### Default: `verify=agent` (criterion = node.steps as checklist)

If `verify` is omitted, runtime spawns an evaluator agent that reads node.steps as the implicit checklist. Evaluator's data shape is fixed:

```json
{
  "approved": true | false,
  "step_results": [
    {"step": 1, "passed": <bool>, "reasoning": "..."},
    ...
  ],
  "reasoning": "<one sentence overall>"
}
```

`step_results` length = `len(node.steps)`. On reject, the per-step reasoning is concatenated into the feedback string for the next retry.

### `verify: { criterion: <text> }`

Same as default-agent but with an override criterion text. The evaluator considers steps + criterion together.

### `verify: { command: <bash>, timeout?: <int> }`

```bash
bash -c <command-template-after-rendering>
```

Run in the attempt directory. Exit 0 → approved. Non-zero → rejected; feedback = stderr/stdout snippet (≤300 chars). Default timeout 60s.

### `verify: { human: <prompt-text-shown-to-user> }`

Runtime prints to stdout:

```
─── Human verify required: <node.id> ───
<envelope.data rendered as JSON, indented>

<the human prompt-text>

Type 'approve' to accept, or describe what to change:
> _
```

Reads one line from stdin:

* If line equals `"approve"` (case-insensitive, whitespace-trimmed) → approved → node done+success.
* Anything else → rejected; that line becomes the feedback for retry.
* EOF / no-TTY (stdin not connected to a terminal) → reject with feedback `"no TTY available for human verify"`. The next attempt fails the same way unless the user runs `camflow resume` from a TTY.

The retry mechanic is identical to other verify types: rejection → `previous.feedback` on next attempt → run agent regenerates with that feedback. retry_max applies normally.

---

## 10. Skill / tool registry (strict)

### Skill resolution

`run.skill: <name>` resolves to `<project>/skills/<name>/SKILL.md` OR `<repo>/skills/<name>/SKILL.md`.

**Workflow load fails if any referenced skill is missing.** No dynamic creation, no fallback.

### Tool resolution

`run.tool: <path>` resolves to `<project>/<path>`. Workflow load fails if file doesn't exist or isn't executable (`-x`).

---

## 11. Run dir layout

```
<project>/.camflow/
├── run/                              # current run (always here, single)
│   ├── workflow.yaml                 # IR — what Planner produced (or what's resuming)
│   ├── prompt.txt                    # the original user prompt
│   ├── trace.jsonl                   # event stream
│   ├── runner.pid                    # while running; deleted on exit
│   ├── halt.json                     # only if halted
│   ├── planner/                      # Planner's own run dir (recursive: same shape)
│   │   ├── workflow.yaml             # = camflow/builtin/planner/workflow.yaml
│   │   ├── trace.jsonl
│   │   └── nodes/...
│   └── nodes/<node_id>/              # nodes of the user-facing workflow
│       └── attempt-<n>/
│           ├── input.json            # upstream + previous (no user inputs)
│           ├── prompt.txt            # full prompt sent to camc (skill nodes)
│           ├── agent_output.json     # what the agent wrote
│           ├── output.json           # runtime-validated envelope
│           ├── agent.id              # camc agent ID (debug)
│           └── verify-<n>/           # if verify=agent, sub-directory
│               ├── prompt.txt
│               ├── agent_output.json
│               └── output.json
│
└── archives/                         # past runs (auto-archived on next run start)
    ├── <timestamp>-success/
    ├── <timestamp>-halted/
    └── ...
```

`runner.pid` lets `kill $(cat .camflow/run/runner.pid)` stop a running workflow.

The Planner sub-directory makes Planner's own execution **fully inspectable as just another camflow run** — same trace.jsonl, same per-attempt outputs, same halt.json on Planner failure.

---

## 12. CLI

```
camflow run "<prompt>"        # mandatory; spawns Planner workflow + executes its output
camflow resume <run_dir>      # resume a halted run
```

| command | behavior | exit codes |
|---|---|---|
| `run "<prompt>"` | mandatory prompt; runs Planner → executes generated workflow.yaml | 0 done / 1 invocation error / 2 halted |
| `run` (no prompt) | print error and exit | 1 |
| `resume <run_dir>` | resume from `<run_dir>/halt.json` state | 0 / 1 / 2 |

Inspecting in-progress: `cat .camflow/run/trace.jsonl` (no `camflow status` subcommand).

There is no `camflow exec workflow.yaml`, no `--validate`, no `--inputs`. The only way to run a workflow is via `camflow run "<prompt>"` — Planner is mandatory, prompt is mandatory.

---

## 13. The builtin Planner workflow

Lives at `camflow/builtin/planner/`:

```
camflow/builtin/planner/
├── workflow.yaml
└── skills/
    ├── prompt_analyzer/SKILL.md
    ├── workflow_designer/SKILL.md
    └── yaml_writer/SKILL.md
```

A reference shape (subject to evolution as we improve quality):

```yaml
workflow: planner
version: "1.1"

context: |
  You are a multi-step planner. Given a user prompt, produce a workflow.yaml
  that decomposes the task into a DAG of skill-or-tool nodes, each with
  explicit goals, steps, dependencies, and verification.
  
  Output format MUST conform to camflow v1.1 spec (no inputs:, no run.input,
  context for shared facts, verify for each non-trivial node).

nodes:
  - id: understand
    goal: "Parse user prompt into a structured task statement"
    steps:
      - "Extract the explicit goal stated by the user"
      - "Identify any constraints, preconditions, success criteria"
      - "Note ambiguities that may need user clarification later"
    run: { skill: prompt_analyzer }
    output_schema:
      task_statement: string
      constraints: array
      ambiguities: array
    verify:
      criterion: "Task statement covers everything stated in the prompt"
    retry: 2

  - id: design_dag
    goal: "Design a DAG of nodes whose successful execution accomplishes the task"
    needs: [understand]
    steps:
      - "Pick available skills (or tools) that map to natural decomposition steps"
      - "Order them by dependency"
      - "Decide retry/verify for each — high-stakes work gets verify=agent or verify=command"
      - "Identify shared facts that should go in workflow.context"
    run: { skill: workflow_designer }
    output_schema:
      dag: array         # list of {id, goal, steps, needs, run, verify, retry}
      context: string
    verify:
      criterion: "DAG covers all task steps; needs are consistent; no orphans/cycles"
    retry: 3

  - id: render_yaml
    goal: "Emit a syntactically valid + user-approved workflow.yaml"
    needs: [design_dag]
    steps:
      - "Serialize the DAG and context to YAML following spec v1.1"
      - "Include the original user prompt in workflow.context"
      - "Verify the YAML loads, validates, and reflects the design"
    run: { skill: yaml_writer }
    output_schema:
      yaml_text: string
    verify:
      human: |
        Review the proposed workflow.yaml below.
        Type 'approve' to accept and run it.
        Otherwise, describe what to change.
    retry: 5
```

When the user runs `camflow run "<prompt>"`:

1. Runtime starts the Planner workflow with the user prompt copied into Planner's `workflow.context` (so Planner's nodes can read it like any other node reads context).
2. Planner runs: understand → design_dag → render_yaml.
3. `render_yaml`'s verify=human shows the user the candidate workflow.yaml. User types `approve` or feedback.
4. On reject, retry kicks in: `yaml_writer` regenerates with `previous.feedback` from the user. Up to retry=5 cycles.
5. On approve, Planner workflow ends `done+success`. Runtime extracts `nodes.render_yaml.output.data.yaml_text`, writes it to `<run_dir>/workflow.yaml`, and starts a new `execute_dag()` on it.
6. That second execution has its own trace, halts, retries — fully indistinguishable from any other camflow workflow.

Planner failure → camflow halts with the Planner's halt.json. User sees what Planner couldn't do, can `camflow resume` to retry from the halt point.

---

## 14. Reserved / forbidden in v1.1

| Cut | Why |
|---|---|
| `Run` class | Folded into `Workflow`. Workflow IS the runtime instance. |
| `state:` / `inputs:` YAML section | Per-run inputs don't exist. Prompt is the input; Planner compiles. |
| `--state` / `--inputs` CLI | Same as above. |
| `{{state.X}}` / `{{inputs.X}}` templates | Same as above. |
| `state.X.default` | Workflow author doesn't pre-fill values. Anything constant goes in `workflow.context`. |
| `run.input:` field | Upstream outputs are auto-injected; no per-node user input. |
| `camflow exec workflow.yaml` | No bypass of Planner. |
| `camflow --validate` | No bypass of Planner. |
| `agent.X` autonomous executor | Multi-step work goes into multi-node DAG. |
| `when:` conditional skip | Branches are explicit DAG paths. No skipped status. |
| `output_schema` rule type | Use `verify.command` for value checks. |
| `?` optional template marker | Strict mode: missing field is ExprError. |
| `retry.until`, `retry.feedback` template | Retry is internal counter; feedback auto-injected as `input.previous`. |
| `nodes.X.attempts[N]` template subscript | Only `output` (latest) is exposed. |
| Skip propagation, `node.lifecycle=skipped` | Halt is the only "node didn't succeed" path. |
| `metrics`, `artifacts` envelope fields | Camc archives cost; writing files is not envelope's job. |
| Multiple concurrent runs per project | One run at a time. Archives hold history. |
| `camflow status / trace / stop` subcommands | `cat trace.jsonl` / `kill $(cat runner.pid)`. |
| `Workflow.run()` / `Workflow.verify()` | Workflow doesn't run or verify. It schedules. |
| Workflow-as-Node (sub-workflow at user level) | The Planner-bootstraps-Runtime relationship is the only nesting. Agents do not kick off workflows. |

---

## 15. Doctrine (rules for changing this spec)

1. **Two classes only: `Workflow` and `Node`.** No third runtime class.
2. **Workflow doesn't run or verify.** It only schedules.
3. **Status is always 2 values: `success` and `fail`.** No third, ever.
4. **Halt is workflow-level only.** Nodes don't have a halted state.
5. **`steps` is the design+QA contract.** Don't introduce a separate "verify criterion list".
6. **Skills must pre-exist.** No dynamic creation. Workflow load fails on unresolved skill.
7. **Retry is a counter, not an expression.** No `retry.until`, no `retry.feedback`.
8. **Template has 1 namespace** (`nodes.<id>.output`), used in 1 place (`verify.command`). Plus `output.X` inside verify.command for the envelope under verification.
9. **Every LLM invocation goes through `camc_lib.run_and_collect`.** No `claude -p`, no SDK.
10. **Runtime contract for envelope is enforced via prompt injection** — every agent gets the explicit shape, schema, and rules.
11. **`camflow run` always invokes Planner.** No bypass. The CLI shape mirrors `camc run`.
12. **Adding a verify type requires RFC.** Currently: agent (default), command, human.
13. **Adding an executor type requires RFC.** Currently: skill, tool. (No `human` executor — human-in-loop is a verify type, not a run type.)

Breaking any of these without explicit reason is a regression.

---

## Appendix A: complete example trace

User runs:

```bash
camflow run "Fix the TypeError on line 87 of foo.py: 'NoneType' has no attribute 'split'"
```

Runtime creates `.camflow/run/`, writes `prompt.txt` with the prompt, starts the **Planner workflow** with that prompt copied into Planner's `context`.

Planner produces (rendered after yaml_writer's render + user approval):

```yaml
workflow: bug_fix
version: "1.1"

context: |
  Original task (from user): Fix the TypeError on line 87 of foo.py:
  'NoneType' has no attribute 'split'.
  
  Codebase: src/ (Python). Tests: tests/ (pytest only).

nodes:
  - id: diagnose
    goal: "Identify the bug's root cause from the report"
    steps:
      - "Read foo.py around line 87 and understand context"
      - "Identify the variable that is None when 'split' is called"
      - "Determine why that variable is None at that point"
      - "Write a one-sentence root cause"
    run: { skill: analyzer }
    output_schema:
      root_cause: string
      affected_location: string
    retry: 2

  - id: propose_fix
    goal: "Write a minimal patch addressing the root cause"
    needs: [diagnose]
    steps:
      - "Read diagnose's root_cause and affected_location"
      - "Produce a unified-diff patch touching only the affected lines"
      - "Provide a one-sentence explanation"
    run: { skill: code_writer }
    output_schema:
      patch: string
      explanation: string
    verify:
      criterion: "patch directly addresses root_cause + minimal change"
    retry: 3

  - id: run_tests
    goal: "Apply the patch and run pytest"
    needs: [propose_fix]
    steps:
      - "Apply propose_fix.patch"
      - "Run python -m pytest"
      - "Capture pass/fail"
    run: { tool: scripts/apply_and_test.sh }
    output_schema:
      passed: boolean
      tests_run: integer
    verify:
      command: "test $(jq -r .data.passed agent_output.json) = 'true'"
    retry: 1
```

The user reviewed `render_yaml`'s output, typed `approve`. Runtime now executes this 3-node workflow:

1. `diagnose` → camc spawns analyzer agent → reads context (which carries the original prompt) → produces root_cause + affected_location → schema check passes → default verify-agent approves → done+success.
2. `propose_fix` → camc spawns code_writer agent → reads upstream's diagnose output (auto-injected) → produces patch → schema check passes → verify-agent (with criterion override) approves → done+success.
3. `run_tests` → tool runs `scripts/apply_and_test.sh` with patch from upstream → produces `passed: true` → verify=command checks bash → exit 0 → done+success.

Workflow lifecycle: running → done. Exit 0.

---

## End of spec.
