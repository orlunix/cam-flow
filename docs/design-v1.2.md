# camflow spec v1.2 draft

A tiny prompt-call-verify-trace runner for agent/tool workflows.

> Status: implemented source of truth
> Replaces the v1.1 assumption that every fresh run must start from a prompt and mandatory Planner compilation.
> Preserves the v1.1 `Workflow` / `Node` execution model, binary envelope, strict skill registry, retry, resume, `run --from`, trace, and per-attempt artifacts.

---

## 0. Design position

camflow is not an agent framework.

camflow does not provide agent intelligence. It executes checked-in workflows, calls external agents or deterministic tools, validates outputs, and preserves audit artifacts.

```text
camflow = prompt-call-verify-trace runner

Claude / Codex / tools = workers
camflow = runner + verifier + recorder
```

The runtime must stay small, deterministic, auditable, and recoverable.

camflow should optimize for:

```text
thin      — minimal runtime semantics
stable    — deterministic execution and durable artifacts
effective — useful evidence/report output for engineering workflows
```

camflow should not grow into:

```text
general agent framework
LangGraph clone
autonomous replanning system
mutable state machine
server platform
plugin ecosystem
unbounded dynamic workflow engine
```

---

## 1. Major changes from v1.1

### 1.1 Planner is no longer mandatory

v1.1:

```text
camflow run "<prompt>"
  → Planner
  → workflow.yaml
  → Runtime
```

v1.2:

```text
camflow run workflow.yaml --input input.json
  → Runtime
```

Planner becomes optional:

```text
camflow plan "<prompt>" --out workflow.yaml
```

The primary runtime entrypoint is now a checked-in or user-supplied `workflow.yaml`.

### 1.2 `input.json` is added

A run may now have one external structured input file.

```bash
camflow run workflows/core_hang.yaml --input cases/bug_001.json
```

The input is read-only run context. It is injected into every node attempt but does not create a mutable global state system.

### 1.3 `replan` is removed

The runtime does not support:

```text
on_halt: replan
max_replans
auto-replan
in-place workflow mutation
agent-created nodes
```

If a user wants a new plan, they should run `camflow plan` again or edit a workflow manually.

### 1.4 `batch` is added

Batch is an outer map runner:

```bash
camflow batch workflows/core_hang.yaml --inputs cases/*.json --out runs/batch_001
```

Each input file creates one independent camflow run.

### 1.5 Restricted `when` routing

v1.2 keeps the static `needs` DAG and adds one deliberately small branch
primitive:

```yaml
when:
  node: test_or_dut
  path: data.route
  equals: lsu_debug
```

`when` may inspect one declared string field from one direct dependency. It
cannot evaluate expressions, mutate the graph, jump, loop, or read mutable
global state. Route groups are exhaustive at runtime: exactly one target must
match, otherwise the workflow halts with `unmatched_route`.

---

## 2. Mental model

```text
workflow.yaml                 checked-in workflow definition
input.json                    one run-specific case/context file
Runtime                       executes nodes in deterministic needs order
Node                          calls one skill/executor, verifies output, persists artifacts
trace.jsonl                   event stream
nodes/<id>/attempt-<n>/       input/prompt/output/verify artifacts
report/evidence artifacts     optional workflow-produced outputs
```

Primary flow:

```text
workflow.yaml + input.json
   ↓
Runtime executor
   ↓
node attempt directories
   ↓
trace.jsonl
   ↓
final outputs / report / halt
```

Optional planning flow:

```text
user prompt
   ↓
camflow plan
   ↓
workflow.yaml
```

---

## 3. CLI

### 3.1 Run a workflow

```bash
camflow run <workflow.yaml> --input <input.json>
```

Optional:

```bash
camflow run <workflow.yaml> --input <input.json> --run-dir <dir>
camflow run <workflow.yaml> --input <input.json> --steps N
```

Behavior:

```text
1. Load workflow.yaml.
2. Validate workflow schema.
3. Load input.json.
4. Validate input.json against workflow.input_schema if present.
5. Require a new or empty run directory.
6. Copy workflow.yaml, input.json, skills, and validators into it.
7. Record workflow/input SHA-256 in run.json.
8. Execute the workflow.
```

Exit codes:

```text
0 = workflow completed successfully
1 = invocation / validation error
2 = workflow halted
```

### 3.2 Plan a workflow

```bash
camflow plan "<prompt>" --out workflow.yaml
```

Behavior:

```text
1. Invoke optional Planner.
2. Generate workflow.yaml.
3. Validate generated workflow.
4. Write workflow.yaml.
5. Do not execute it.
```

Planner is a workflow-authoring helper, not the runtime’s mandatory path.

### 3.3 Batch run

```bash
camflow batch <workflow.yaml> --inputs <glob> --out <dir>
```

Example:

```bash
camflow batch workflows/core_hang.yaml \
  --inputs cases/*.json \
  --out runs/core_hang_batch \
  --continue-on-fail
```

Behavior:

```text
1. Expand input files.
2. For each input file:
   - create one independent run directory
   - run the same workflow.yaml with that input
   - preserve that run's trace/artifacts independently
3. Write batch summary.
```

Batch is not a workflow-internal loop.

Each input is a separate run.

### 3.4 Resume

```bash
camflow resume <run_dir>
camflow resume <run_dir> --feedback "<text>"
camflow resume <run_dir> --steps N
```

Resume semantics remain the same as v1.1:

```text
resume = give the halted node one more attempt
```

Resume does not change the workflow graph.

### 3.5 Run from node

```bash
camflow run --from <node_id> --run-dir <run_dir>
camflow run --from <node_id> --run-dir <run_dir> --feedback "<text>"
```

Behavior:

```text
1. Reuse the existing workflow.yaml and input.json in run_dir.
2. Verify both files against run.json before replay.
3. Preserve upstream successful/skipped nodes.
4. Reset the target node and all downstream descendants.
5. Re-execute from target.
```

`run --from` does not modify the workflow graph.

---

## 4. Workflow YAML schema

### 4.1 Top-level schema

```yaml
workflow: <name>
version: "1.2"

goal: |
  Optional high-level workflow goal.

context: |
  Optional shared prompt context injected into every node.

input_schema:
  field_name: <type>

nodes:
  - <Node>
```

Allowed top-level keys:

```text
workflow
version
goal
context
input_schema
nodes
```

Unknown top-level keys are validation errors.

### 4.2 Input schema

`input_schema` is optional but recommended.

```yaml
input_schema:
  case_id: string
  test_name: string
  seed: string
  failure_type: string
  sim_log: string
  trace_log: string
  wave_path: string
  rtl_commit: string
```

Supported scalar types:

```text
string
integer
number
boolean
array
object
```

v1.2 schema checking is shallow:

```text
required field exists
field has expected top-level type
```

No nested JSON Schema support in v1.2.

### 4.3 Node schema

```yaml
- id: <string>
  goal: <string>
  steps:
    - "step 1"
    - "step 2"
  needs: [<node_id>, ...]

  run:
    skill: <skill_name>

  output_schema:
    field_name: <type>

  verify:
    criterion: <text>
    # OR
    command: <bash>
    timeout: <int>
    # OR
    human: <prompt>

  retry: <int>

  when:
    node: <direct dependency id>
    path: data.<declared string field>
    equals: <literal string>
```

Allowed node keys:

```text
id
goal
steps
needs
run
output_schema
verify
retry
when
```

Unknown node keys are validation errors.

This means the following are invalid in v1.2:

```yaml
next: foo
goto: foo
route: foo
routes: {}
on_success: foo
on_fail: foo
```

### 4.4 Required fields

| field           | required | default              |
| --------------- | -------: | -------------------- |
| `id`            |      yes | —                    |
| `goal`          |      yes | —                    |
| `steps`         |      yes | —                    |
| `run`           |      yes | —                    |
| `run.skill`     |      yes | —                    |
| `needs`         |       no | `[]`                 |
| `output_schema` |       no | `{}`                 |
| `verify`        |       no | default agent verify |
| `retry`         |       no | `1`                  |
| `when`          |       no | always run           |

---

## 5. Input semantics

### 5.1 `input.json` is read-only

`input.json` represents run-specific context.

Example:

```json
{
  "case_id": "rv_rand_001_seed_12345",
  "test_name": "rv_rand_001",
  "seed": "12345",
  "failure_type": "hang",
  "sim_log": "/regress/run1/sim.log",
  "trace_log": "/regress/run1/trace.log",
  "wave_path": "/regress/run1/wave.fsdb",
  "rtl_commit": "abc123"
}
```

The runtime copies it to:

```text
<run_dir>/input.json
```

### 5.2 Attempt input shape

Each node attempt receives an `input.json` in its attempt directory:

```json
{
  "run_input": {
    "...": "contents of top-level input.json"
  },
  "upstream": {
    "upstream_node_id": {
      "status": "success",
      "data": {},
      "error": null,
      "feedback": null,
      "request_human": false
    }
  },
  "previous": {
    "status": "fail",
    "data": {},
    "error": {
      "code": "VERIFY_FAIL",
      "message": "..."
    },
    "feedback": "...",
    "request_human": false
  },
  "dag_revision": 1
}
```

Rules:

```text
run_input is always present when --input is used
upstream is present only when node.needs is non-empty
previous is present only on retry/resume attempts
dag_revision is internal runtime metadata
```

### 5.3 Prompt injection

The runtime injects input into every run prompt:

```text
# Workflow Input
<JSON contents of run_input>
```

This section appears before `# Goal`.

Recommended order:

```text
[skill template / SKILL.md]
# Workflow Goal
# Workflow Context
# Workflow Input
# Goal
# Steps
# Upstream Outputs
# Note: previous attempt failed
# Output
```

### 5.4 No `run.input`

Nodes cannot declare:

```yaml
run:
  input:
    sim_log: "{{input.sim_log}}"
```

Reason:

```text
input.json is already injected as read-only run context
cross-node data flow remains automatic through needs/upstream
camflow v1.2 does not introduce a state/template language
```

### 5.5 No `{{input.X}}` templates

`input.json` is not a template namespace in v1.2.

The agent sees the input in the prompt and in attempt `input.json`.

`verify.command` templating may continue to use existing supported namespaces, but v1.2 should not add general `{{input.*}}` templating unless explicitly approved later.

---

## 6. DAG execution

camflow v1.2 keeps the existing static `needs` DAG.

A node is ready when:

```text
all nodes in needs are completed as success or skipped
```

Execution is serial and deterministic:

```text
while workflow is running:
    ready = nodes whose needs are satisfied
    if no ready nodes:
        if all done:
            workflow done
        else:
            workflow halted due to deadlock
    pick first ready node by YAML declaration order
    if when does not match:
        persist skip.json and node_skipped
        continue
    execute one attempt
    if success:
        mark node done+success
    elif request_human:
        halt workflow
    elif retry budget remains:
        retry same node
    else:
        halt workflow
```

No parallelism in v1.2.

Restricted, deterministic `when` routing is supported.

No `next`.

No `goto`.

No runtime graph mutation.

---

## 7. `needs` semantics

`needs` is the directed edge relation. It establishes both scheduling order
and the only node-output data flow.

Example:

```yaml
nodes:
  - id: extract_signature
    ...

  - id: analyze_lsu
    needs: [extract_signature]
    ...

  - id: analyze_ifu
    needs: [extract_signature]
    ...

  - id: judge_verdict
    needs: [analyze_lsu, analyze_ifu]
    ...
```

Without `when`, after `extract_signature` succeeds both `analyze_lsu` and
`analyze_ifu` become ready.

Because v1.2 is serial, the runtime runs them one at a time in YAML declaration order.

`needs` alone does not mean:

```text
exclusive branch
route
goto
next step override
skip other ready nodes
```

Exclusive branches add one `when` object to each candidate node. Example:

```yaml
- id: test_or_dut
  output_schema:
    route: string

- id: lsu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: lsu_debug}

- id: ifu_debug
  needs: [test_or_dut]
  when: {node: test_or_dut, path: data.route, equals: ifu_debug}
```

The non-selected node is persisted as `skipped`. A downstream join may depend
on both candidates; skipped dependencies count as complete, while only
successful dependencies are included in its `upstream` input.

---

## 8. Envelope shape

Every node attempt produces one envelope:

```json
{
  "status": "success",
  "data": {},
  "error": null,
  "feedback": null,
  "request_human": false
}
```

Status values:

```text
success
fail
```

No third status.

`skipped` is a scheduler-owned branch state in `skip.json`; agents cannot emit
it as an envelope status.

Invalid statuses are runtime errors.

Rules:

```text
success → data must satisfy output_schema
fail → error must contain code and message
request_human=true → halt workflow and skip retry
feedback → passed into next attempt as previous.feedback
```

---

## 9. Run phase

v1.2 keeps one primary node executor:

```yaml
run:
  skill: <skill_name>
```

A skill is a checked-in directory:

```text
skills/<skill_name>/SKILL.md
```

The runtime loads `SKILL.md` and injects it into the node prompt.

The runtime must fail workflow validation if a referenced skill does not exist.

No dynamic skill creation.

No fuzzy matching.

No fallback skill.

---

## 10. Agent adapter boundary

The runtime should not call Claude, Codex, or any LLM directly from scattered code paths.

All agent invocation goes through one adapter interface.

Conceptual interface:

```python
class AgentExecutor:
    def run(self, node, prompt, attempt_dir, context) -> dict:
        ...
```

Possible implementations:

```text
CamcExecutor
ClaudeCodeExecutor
CodexExecutor
MockExecutor
```

The runtime only expects:

```text
executor returns an envelope
executor writes raw artifacts into attempt_dir
```

This keeps camflow independent of any one agent system.

The camc adapter does not use auto-exit. Once an attempt output exists, it
performs and records this durability sequence:

```text
camc archive -> camc --json status -> camc stop -> camc rm
```

The attempt directory retains `agent.id`, `agent.json`,
`camc-archive/*.tar.gz`, and `camc-lifecycle.json`. If archive fails, the
workflow halts and the camc record remains available for inspection/retry.

The supervisor, runtime, and CAMC have separate ownership boundaries:

```text
supervisor skill  chooses workflow/input and handles done or halted
camflow runtime   schedules nodes and enforces child lifecycle
camc              owns agent processes, sessions, tags, and archives
```

The supervisor may choose a short top-level `workflow` name, but it does not
manually spawn individual node agents. Camflow persists a run-level flow ID in
`run.json`, names agents `cf-<flow-label>-<node-label>-<attempt-hash>`, and
passes both `cf-<flow-label>` and `cf-<flow-id>` as CAMC tags. The same rule
applies to retry attempts and evaluator agents.

---

## 11. Verify phase

Verify remains one of:

```yaml
verify:
  criterion: <text>
```

or:

```yaml
verify:
  command: <bash>
  timeout: 60
```

or:

```yaml
verify:
  human: <prompt>
```

If `verify` is omitted, default agent verify is used.

`verify.command` is the preferred deterministic gate.

Examples:

```yaml
verify:
  command: python3 validators/check_evidence.py agent_output.json
```

```yaml
verify:
  command: python3 validators/check_verdict.py agent_output.json
```

---

## 12. Evidence-first workflow convention

v1.2 adds an explicit convention for engineering/debug workflows.

If a workflow produces claims, hypotheses, or verdicts, those outputs should be evidence-backed.

Recommended node output fields:

```yaml
output_schema:
  symptoms: array
  evidence: array
  hypotheses: array
  verdict: object
```

Recommended evidence object:

```json
{
  "id": "E001",
  "claim": "LSU transaction id=7 has no response before timeout",
  "artifact": "sim.log",
  "location": "cycle 184120-250000",
  "signal_or_event": "lsu_req_valid id=7",
  "supports": ["H001"],
  "weakens": [],
  "confidence": "high"
}
```

Recommended hypothesis object:

```json
{
  "id": "H001",
  "module": "LSU",
  "root_cause": "Outstanding load transaction did not complete",
  "supporting_evidence": ["E001"],
  "contradictions": [],
  "confidence": "medium"
}
```

Recommended verdict object:

```json
{
  "type": "LIKELY_RTL_BUG",
  "suspect_module": "LSU",
  "confidence": "medium",
  "primary_evidence": ["E001"],
  "why_not_tb": "TB response legality not fully checked",
  "next_action": "Run bus protocol checker for transaction id=7"
}
```

Validator rules should enforce:

```text
claim is non-empty
artifact is non-empty
location is non-empty
evidence ids are unique
hypotheses reference existing evidence ids
verdict references existing evidence ids
final report must not introduce unsupported claims
```

This evidence convention is not a general camflow runtime requirement for every workflow, but it is strongly recommended for RTL/debug workflows.

---

## 13. Run directory layout

Each run writes:

```text
<run_dir>/
├── workflow.yaml
├── workflow.json
├── input.json                         # only when supplied
├── run.json                           # workflow/input SHA-256
├── trace.jsonl
├── halt.json
├── skills/                            # required skill snapshot
├── validators/                        # optional validator snapshot
├── nodes/
│   └── <node_id>/
│       ├── skip.json                  # only for an unselected branch
│       └── attempt-<n>/
│           ├── input.json
│           ├── prompt.txt
│           ├── agent_output.json
│           ├── output.json
│           ├── verify.json
│           ├── agent.id
│           ├── agent.json
│           ├── camc-lifecycle.json
│           ├── camc-archive/*.tar.gz
│           └── verify/                # default agent verifier artifacts
├── evidence.json
├── symptoms.json
├── hypotheses.json
├── verdict.json
└── report.md
```

The workflow does not have to produce every summary artifact, but RTL/debug workflows should.

`agent.id`, `agent.json`, `camc-lifecycle.json`, and `camc-archive/` are
present for real camc-backed attempts; mock executor tests do not create them.

`trace.jsonl` remains the authoritative event stream.

---

## 14. Batch directory layout

Batch writes:

```text
<batch_dir>/
├── batch.json
├── summary.json
├── summary.md
├── runs/
│   ├── <case_id_1>/
│   │   ├── workflow.yaml
│   │   ├── input.json
│   │   ├── trace.jsonl
│   │   └── ...
│   ├── <case_id_2>/
│   │   ├── workflow.yaml
│   │   ├── input.json
│   │   ├── trace.jsonl
│   │   └── ...
│   └── ...
```

`batch.json` records:

```json
{
  "workflow": "workflows/core_hang.yaml",
  "inputs": ["cases/bug_001.json", "cases/bug_002.json"],
  "started_at": "...",
  "completed_at": "...",
  "continue_on_fail": true
}
```

`summary.json` records one entry per run:

```json
{
  "case_id": "bug_001",
  "status": "done",
  "exit_code": 0,
  "run_dir": "runs/bug_001",
  "verdict": {
    "type": "LIKELY_RTL_BUG",
    "suspect_module": "LSU",
    "confidence": "medium"
  }
}
```

Batch failure behavior:

```text
default: stop on first invocation error
--continue-on-fail: continue to next input when one run halts or fails
```

A halted individual run is not automatically resumed by batch.

---

## 15. Planner

Planner is optional in v1.2.

Command:

```bash
camflow plan "<prompt>" --out workflow.yaml
```

Planner output must be validated as normal workflow YAML.

Planner must not be invoked automatically by `camflow run workflow.yaml --input input.json`.

Optional convenience command:

```bash
camflow run-prompt "<prompt>" --input input.json
```

If implemented, `run-prompt` is defined as:

```text
plan prompt into temporary workflow.yaml
then run that workflow with input.json
```

`run-prompt` is convenience only, not the canonical path.

---

## 16. Removed features

v1.2 removes or rejects:

```text
replan
auto-replan
on_halt: replan
max_replans
goto
next
routes
run.input
state:
inputs:
mutable global state
agent-created nodes
in-place workflow mutation
unbounded loops
```

Some of these may appear in old archives or experiments, but they are not part of v1.2.

---

## 17. Restricted `when`

Current shape:

```yaml
- id: debug_hang
  needs: [classify_failure]
  when:
    node: classify_failure
    path: data.failure_class
    equals: hang
  run:
    skill: hang_debugger
```

Hard constraints:

```text
when can only reference a direct dependency listed in needs
path is exactly data.<field>
the source output_schema must declare that field as string
equals is a non-empty literal string
duplicate values in one route group are validation errors
exactly one target in a route group must match
when cannot mutate graph
when cannot loop
false means node_skipped trace event
skipped nodes must have precise resume/run-from semantics
```

The persisted source envelope, `route_selected`/`node_skipped` trace event,
and `skip.json` are sufficient to replay and audit the decision without
asking an agent to route again.

---

## 18. Doctrine

### Runtime does

```text
load workflow
validate workflow
load input
validate input
build prompts
call agent/tool executor
parse envelope
validate schema
run verifier
persist artifacts
retry
resume
run from node
batch over input files
write trace
```

### Runtime does not

```text
invent nodes
auto-replan
mutate workflow graph
interpret RTL domain
own long-term memory
host a server
manage queues
parallelize internally
provide general state programming
```

### Core rule

If a feature makes camflow look like a general agent platform, it probably does not belong in v1.2.

If a feature improves prompt-call-verify-trace reliability without adding broad semantics, it may belong.

---

## 19. Minimal example

### workflow.yaml

```yaml
workflow: core_hang_debug
version: "1.2"

goal: |
  Triage a RISC-V core hang and produce evidence-backed verdict.

context: |
  Every claim must cite concrete evidence.
  Do not assign RTL/TB/RM blame without evidence.

input_schema:
  case_id: string
  test_name: string
  seed: string
  failure_type: string
  sim_log: string
  trace_log: string
  wave_path: string
  rtl_commit: string

nodes:
  - id: extract_last_retire
    goal: Extract the last retired instruction and no-retire window.
    steps:
      - Read Workflow Input.
      - Inspect trace_log.
      - Emit symptoms and evidence for last retire and timeout window.
    run:
      skill: retire_extractor
    output_schema:
      symptoms: array
      evidence: array
    verify:
      command: python3 validators/check_evidence.py agent_output.json
    retry: 1

  - id: analyze_lsu
    needs: [extract_last_retire]
    goal: Determine whether LSU or memory system is a plausible primary cause.
    steps:
      - Read upstream no-retire evidence.
      - Inspect sim_log or available LSU summaries.
      - Emit hypotheses and evidence.
    run:
      skill: lsu_debugger
    output_schema:
      hypotheses: array
      evidence: array
    verify:
      command: python3 validators/check_hypotheses.py agent_output.json
    retry: 1

  - id: write_report
    needs: [extract_last_retire, analyze_lsu]
    goal: Write an evidence-backed debug report.
    steps:
      - Summarize symptoms.
      - Summarize hypotheses.
      - State verdict and confidence.
      - List next actions.
    run:
      skill: report_writer
    output_schema:
      report_path: string
      verdict: object
    verify:
      command: python3 validators/check_report_claims.py agent_output.json
    retry: 1
```

### input.json

```json
{
  "case_id": "rv_rand_001_seed_12345",
  "test_name": "rv_rand_001",
  "seed": "12345",
  "failure_type": "hang",
  "sim_log": "/regress/run1/sim.log",
  "trace_log": "/regress/run1/trace.log",
  "wave_path": "/regress/run1/wave.fsdb",
  "rtl_commit": "abc123"
}
```

### command

```bash
camflow run workflows/core_hang.yaml --input cases/rv_rand_001.json
```

---

## Appendix: Simple plan / pack / run interface

CamFlow v1.2 uses three narrow user-facing actions:

```text
plan = generate editable artifacts
pack = clean-copy reusable authoring artifacts
run  = execute a workflow with a real input
```

`camflow plan "<intent>" --out DIR` creates ordinary editable files. When it
has the required case data it writes `workflow.yaml`, a real `input.json`, an
`input.template.json`, local `skills/`, `README.md`, and `plan_manifest.json`.
When required input is unavailable it fails explicitly; placeholders belong
only in `input.template.json`.

`camflow pack SOURCE_DIR --out PACKAGE_DIR` creates a simple reusable
directory bundle. It copies only `workflow.yaml`, `input.template.json`, local
referenced `skills/*/SKILL.md`, optional `validators/`, optional `README.md`,
and `package_manifest.json`. It excludes per-run input and outputs, including
`input.json`, trace files, node attempts, reports, logs, waveforms, virtual
environments, and build directories. It is not an archive format, registry,
installer, lockfile, or package-specific runtime.

```bash
camflow plan "debug hang case_id=bug_001 sim_log=/runs/sim.log trace_log=/runs/trace.log" --out .camflow/plan/core_hang
camflow pack .camflow/plan/core_hang --out packages/core_hang
camflow run packages/core_hang/workflow.yaml --input cases/bug_001.json --out runs/bug_001
camflow batch packages/core_hang/workflow.yaml --inputs "cases/*.json" --out runs/core_hang_batch
```

`run` and `batch` execute exactly the supplied workflow and real inputs. They
never implicitly plan, pack, install, generate missing skills, or fall back to
unrelated host skills.
