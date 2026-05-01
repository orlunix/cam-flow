# camflow v1.0 spec

A workflow runner for multi-agent DAGs.  
**Thin runtime. Strict contracts. No tricks.**

---

## 0. Mental model

```
Workflow = DAG of Nodes.
Node = one unit of work.
Each Node has a Run (designer) and a Verify (QA), sharing the same `steps`.
Run does the work. Verify checks the work.
Both look at the same goal + steps.
```

---

## 1. State machines

### Workflow

```
Workflow.state ∈ { running, done, halted }
```

| state | meaning |
|---|---|
| `running` | at least one node still hasn't reached `done` |
| `done` | all nodes are `done+success` → exit 0 |
| `halted` | a node ended `done+fail` (retry exhausted) OR a node set `request_human=true` → exit 2 |

No `failure` state. A halted workflow is recoverable via `camflow resume <run_dir>`.

### Node

```
Node.state ∈ { waiting, running, done }
Node.result ∈ { success, fail }   (only when state=done)
```

| state | meaning |
|---|---|
| `waiting` | not yet started OR not all needs are done+success |
| `running` | currently executing (includes between-retry transitions) |
| `done+success` | run + verify both passed |
| `done+fail` | retry exhausted, or run set request_human |

Internal counters (not exposed in YAML, set by runtime):

```
Node.retry_count: int   # 0 on first attempt
Node.retry_max:   int   # from YAML, default 1 (no retry)
```

---

## 2. Node YAML schema

```yaml
- id: <string>                    # REQUIRED, unique within workflow
  goal: <string>                  # REQUIRED, one-line intent
  steps:                          # REQUIRED, list[string]
    - "step 1"                    # ← shared between run prompt + verify checklist
    - "step 2"
    - "..."
  needs: [<node_id>, ...]         # optional, default []
  
  run:                            # REQUIRED
    skill: <skill_name>           # OR tool: <path>  (mutually exclusive)
    input:                        # optional, default {}
      key: "{{state.x}}"          # template strings
  
  output_schema:                  # optional but strongly recommended
    field_name: <type>            # type ∈ {string, integer, number, boolean, array}
  
  verify:                         # optional; default = agent + steps as criterion
    criterion: <override>         # OR command: <bash>  (mutually exclusive)
    timeout: <int>                # only with command, default 60
  
  retry: <int>                    # optional, default 1 (no retry — first attempt only)
```

### Required vs optional summary

| field | required | default |
|---|---|---|
| `id` | ✅ | — |
| `goal` | ✅ | — |
| `steps` | ✅ | — |
| `run` | ✅ | — |
| `run.skill` or `run.tool` | ✅ (one of) | — |
| `run.input` | ❌ | `{}` |
| `needs` | ❌ | `[]` |
| `output_schema` | ❌ | `{}` (no field-presence check) |
| `verify` | ❌ | implicit `agent + steps` |
| `retry` | ❌ | `1` |

### Mutual exclusion rules

- `run.skill` XOR `run.tool` (exactly one)
- `verify.criterion` XOR `verify.command` (at most one)

---

## 3. Top-level workflow YAML

```yaml
workflow: <name>
version: "1.0"
goal: |                           # optional, top-level intent
  ...
state:                            # optional, declarative schema for initial state
  field_name:
    type: <type>
    required: <bool>
    default: <value>              # optional
nodes:
  - <Node>
  - ...
```

The `state` section is documentation; runtime doesn't enforce it. Initial state is whatever the user passes via `--state foo.json`.

---

## 4. Envelope shape (node output)

Every Node attempt produces one envelope:

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
| `data` | when `status=success`, must satisfy output_schema | run |
| `error` | when `status=fail`, must have `{code, message}` | run, OR runtime (on verify-fail) |
| `feedback` | when verify ran | verify |
| `request_human` | optional, default `false` | run or verify |

### Status values

```
"success"   = run produced data per schema AND verify passed
"fail"      = run failed, or verify rejected, or run timed out
```

No `"halted"`, `"skipped"`, `"ok"`, `"done"`, `"completed"`, etc.
Runtime treats unknown status as fail with `error.code = "BAD_STATUS"`.

### Halt triggers

Workflow halts when **any one** of these:

1. A node ends `done+fail` (retry exhausted or no retry configured).
2. A node sets `request_human=true` in its envelope (skips retry).

Halt writes `<run_dir>/halt.json` and propagates downstream nodes to `done+fail` (so the trace is complete).

---

## 5. Execution model

### Per-attempt sequence

```
1. Set node.state = "running"
2. Render run.input (templates) → write to attempt-N/input.json
3. Spawn run executor (skill or tool)
   - skill: camc agent with skill prompt
   - tool:  shell out to <tool_path>
4. Receive run envelope from executor
5. If run.status == "fail":
     → goto retry decision (step 8)
6. If run.status == "success":
     a. Auto-schema check: verify data has all output_schema fields with right types
        if fails → status="fail", error=VERIFY_FAIL, goto retry
     b. If verify present (or default agent), run it:
          - command: bash exit code
          - agent:   spawn evaluator with steps as checklist
        if verify fails → status="fail", error=VERIFY_FAIL, feedback=<reason>, goto retry
7. node.state = "done", node.result = "success" → done.
8. Retry decision:
     if request_human → workflow halt (skip retry)
     elif retry_count < retry_max → retry_count++, goto 1
     else → node.state="done", node.result="fail" → workflow halt
```

### Retry feedback channel

On retry, runtime auto-injects `previous` into next attempt's input:

```json
// attempt-2/input.json
{
  "<user input fields>": ...,
  "previous": {                           // auto-injected
    "status": "fail",
    "data": {...},
    "error": {...},
    "feedback": "<verify's reason>"
  }
}
```

Workflow author doesn't write `previous` — runtime adds it. Agent reads `input.previous.feedback` to know what went wrong last time.

---

## 6. Run prompt structure (auto-built by runtime)

```
[skill template / SKILL.md content]    ← from skills/<name>/SKILL.md

# Goal
<node.goal>

# Steps (you MUST do these in order)
1. <node.steps[0]>
2. <node.steps[1]>
...

# Inputs
<JSON of rendered input + auto-injected `previous` if retry>

# Output schema
data must contain:
  <output_schema rendered as type list>

# Delivery protocol
Write a single JSON envelope to `agent_output.json` in cwd:
{
  "status": "success" | "fail",
  "data": {...matching schema above},
  "error": null | {"code": "...", "message": "..."},
  "request_human": false
}
- status MUST be "success" or "fail" (not "ok"/"done"/etc).
- success → data contains all schema fields with correct types.
- fail → error MUST have non-empty code + message.
- request_human=true to escalate (skips retry, halts workflow).
- Don't print to stdout; only write the file. Don't use markdown fences.
```

For `tool`: prompt → not applicable. Tool reads `input.json` from stdin and writes envelope to stdout.

---

## 7. Verify prompt structure (when verify=agent)

```
You are evaluating whether the previous node `<node.id>` did its job.

# Goal (same as run's)
<node.goal>

# Steps that should have been done (your checklist)
1. <step 1>
2. <step 2>
...

# What run produced (envelope)
<JSON of run's envelope>

# Your job
For EACH step above, decide if it was done correctly.
Approve = true ONLY if ALL steps pass.

# Output
Write to agent_output.json:
{
  "status": "success",
  "data": {
    "approved": <bool>,
    "step_results": [
      {"step": 1, "passed": <bool>, "reasoning": "..."},
      {"step": 2, "passed": <bool>, "reasoning": "..."},
      ...
    ],
    "reasoning": "<one sentence overall>"
  }
}
```

Verify-agent's data shape is **fixed by runtime**, not by user output_schema. Number of `step_results` = `len(node.steps)`.

When verify=command: prompt N/A; runtime runs `bash -c <cmd>` in attempt dir, gates on exit code.

---

## 8. Templates

### Allowed in YAML

- `input:` values (`run.input`)
- `verify.command` string

### Namespaces

| namespace | meaning | example |
|---|---|---|
| `state.X` | initial state (read-only) | `{{state.bug_report}}` |
| `nodes.<id>.output.data.X` | upstream node's data | `{{nodes.diagnose.output.data.cause}}` |
| `nodes.<id>.output.feedback` | upstream node's verify feedback | `{{nodes.review.output.feedback}}` |

No `.latest`, no `.attempts[N]`, no `retry.*`, no `output.*` outside templates. (Runtime injects `previous` into the next attempt's input automatically — no template syntax needed.)

### Expression operators (only inside `verify.command` or future asserts)

`==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, attribute chain, `[index]`.

---

## 9. Skill / tool registry (strict)

### Skill resolution

`run.skill: <name>` resolves to `<project>/skills/<name>/SKILL.md` OR `<repo>/skills/<name>/SKILL.md`.

**Workflow load fails if any referenced skill is missing.** No dynamic creation, no fallback.

### Tool resolution

`run.tool: <path>` resolves to `<project>/<path>`. Workflow load fails if file doesn't exist or isn't executable (`-x`).

---

## 10. Run dir layout

```
<project>/.camflow/
├── run/                              # current run (always here, single)
│   ├── workflow.yaml                 # snapshot of what's running
│   ├── state.json                    # initial state
│   ├── trace.jsonl                   # event stream
│   ├── runner.pid                    # while running; deleted on exit
│   ├── halt.json                     # only if halted
│   └── nodes/<node_id>/
│       └── attempt-<n>/              # 1, 2, 3 ... per retry
│           ├── input.json            # runtime-rendered input (with `previous` on retries)
│           ├── prompt.txt            # for skill: full prompt sent to camc agent
│           ├── agent_output.json     # for skill/agent: what they wrote
│           ├── output.json           # runtime-validated envelope
│           ├── agent.id              # camc agent ID (debug)
│           └── verify-<n>/           # if verify=agent, sub-directory per check
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

---

## 11. CLI

```
camflow <workflow.yaml> [--state STATE_FILE] [--validate]
camflow plan "<goal>" [--out FILE] [--run --state FILE]
camflow resume <run_dir> [--feedback TEXT]
```

| command | exit codes |
|---|---|
| `<workflow.yaml>` | 0 success / 1 failure / 2 halted |
| `plan` | 0 / 2 |
| `resume` | 0 / 1 / 2 |

Inspecting state: `cat .camflow/run/trace.jsonl` (no `camflow status` subcommand).

---

## 12. Reserved / forbidden in v1.0

These were considered and **explicitly cut**. Don't add back without RFC:

| Cut | Why |
|---|---|
| `agent.X` autonomous executor | Multi-step work goes into multi-node DAG, not one autonomous node. |
| `when:` conditional skip | Branches must be explicit DAG paths. No skipped status. |
| `output_schema` rule type | Use `verify.command` for value checks. |
| `verify.rule` (expression assert) | Replaced by `verify.command` (more general). |
| `?` optional template marker | Strict mode: missing field is ExprError. |
| `retry.until`, `retry.feedback` template | Retry is internal counter; feedback is auto-injected as `input.previous`. |
| `nodes.X.attempts[N]` template subscript | Only `latest` (now: `output`) is exposed. |
| Skip propagation, `node.state=skipped` | Halt is the only "node didn't succeed" path. |
| `metrics`, `artifacts` envelope fields | Camc archives cost; writing files is not envelope's responsibility. |
| Multiple `--run-dir` per project | One run at a time. Archives hold history. |
| `camflow status / trace / stop` subcommands | `cat trace.jsonl` / `kill $(cat runner.pid)`. |

---

## 13. Doctrine (rules for changing this spec)

1. **Status is always 2 values: `success` and `fail`.** No 3rd, ever.
2. **Halt is workflow-level only.** Nodes don't have a halted state.
3. **Steps is the design+QA contract.** Don't introduce a separate "verify criterion list".
4. **Skills must pre-exist.** No dynamic creation. Workflow load fails on unresolved skill.
5. **Retry is a counter, not an expression.** No `retry.until`, no `retry.feedback`.
6. **Templates have 2 namespaces** (`state`, `nodes.<id>.output`). No `retry.*`, no `output.*` outside `verify.command`.
7. **Every LLM invocation goes through `camc_lib.run_and_collect`.** No `claude -p`, no SDK, no other path.
8. **Runtime contract for envelope is enforced via prompt injection** — every agent gets the explicit shape, schema, and rules.
9. **Adding a verify type requires RFC.** Currently only `agent` (default) and `command`.
10. **Adding an executor type requires RFC.** Currently only `skill` and `tool`.

Breaking any of these without explicit reason is a regression.

---

## Appendix A: complete example

```yaml
workflow: bug_fix
version: "1.0"

state:
  bug_report:
    type: string
    required: true

nodes:
  - id: diagnose
    goal: "Identify the bug's root cause from the report"
    steps:
      - "Parse the error message and stack trace"
      - "Identify the file and line where the error originates"
      - "Identify the variable / expression that's None / wrong"
      - "Write a one-sentence root cause"
    run:
      skill: analyzer
      input:
        bug: "{{state.bug_report}}"
    output_schema:
      root_cause: string
      affected_location: string
      confidence: number
    retry: 2
  
  - id: propose_fix
    goal: "Write a minimal patch that addresses the diagnosed root cause"
    steps:
      - "Read diagnose's root_cause and affected_location"
      - "Produce a unified-diff patch touching only the affected lines"
      - "Provide a one-sentence explanation"
    needs: [diagnose]
    run:
      skill: code_writer
      input:
        bug: "{{state.bug_report}}"
        cause: "{{nodes.diagnose.output.data.root_cause}}"
        location: "{{nodes.diagnose.output.data.affected_location}}"
    output_schema:
      patch: string
      explanation: string
    verify:
      criterion: "patch directly addresses root_cause + minimal change"
    retry: 3
  
  - id: run_tests
    goal: "Apply the patch and run pytest"
    steps:
      - "Apply propose_fix.patch to the codebase"
      - "Run python -m pytest"
      - "Capture pass/fail result"
    needs: [propose_fix]
    run:
      tool: scripts/apply_and_test.sh
      input:
        patch: "{{nodes.propose_fix.output.data.patch}}"
    output_schema:
      passed: boolean
      tests_run: integer
    verify:
      command: "test $(jq -r .data.passed agent_output.json) = 'true'"
    retry: 1
```

3 nodes, 1 success path. If diagnose can't pinpoint root cause → fail → retry (2 more times) → halt. If propose_fix's patch doesn't pass verify-agent → fail → retry (3 more times) with `previous.feedback` to learn → halt. If pytest doesn't pass → fail → retry once → halt.

---

## End of spec.
