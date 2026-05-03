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
    run_config      : {"skill": str} | {"tool": str}    # XOR; skill is default (see §10)
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
| Node | runs a skill (camc-spawned agent) — or a tool (shell script) for narrow mechanical cases — does the actual work | checks one envelope (schema + criterion / command / human) |
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

  run:                            # REQUIRED — exactly one of:
    skill: <skill_name>           # default and strongly preferred (see §10)
    # OR
    tool: <path>                  # narrow escape hatch — only if ALL 5 criteria
                                   # in §10 hold. Stdin=input.json; stdout=envelope.
                                   # NOTE: no `input:` field; upstream is auto-injected.

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

### Phase rules

- **Run phase**: every node has exactly **one** of:
  - `run: { skill: <name> }` — **default and strongly preferred** (see §10).
  - `run: { tool: <path> }` — narrow escape hatch with hard criteria (see §10).
- **Verify phase**: at most one of `verify.criterion` / `verify.command` /
  `verify.human`. `verify.command` is the canonical deterministic gate.

`run.skill XOR run.tool` (exactly one). The two are mutually exclusive.

> **Why two run options?** Real workflows have two kinds of work:
> *integrative* (read code, interpret output, decide next step) and
> *mechanical* (run pytest, format file). Skills do the first well but
> are slow + expensive for the second. Tool is the escape hatch for
> the second — see §10 for the hard criteria gating its use.

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
     - skill: spawn camc agent with the assembled prompt (see §8); agent
       writes envelope to agent_output.json and exits
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

For `run.tool:`: the prompt is N/A. The tool subprocess receives
`input.json` (containing `upstream` + `previous`) on stdin and is
required to write the envelope JSON to stdout.

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

Human approval has **two opt-in mechanisms**, both off by default:

**1. Plan-level approval (review the compiled workflow.yaml).**
Triggered by the `-i` / `--interactive` flag on `camflow run`. When set,
the runtime patches Planner's `render_yaml` node at startup to use
`verify: { human: ... }` — after Planner finishes designing, the user
sees the compiled workflow.yaml and must type `approve` before the
runtime executes it (or describe a change to drive a Planner revision).
**Default (no `-i`)**: Planner's `render_yaml` uses its declared
agent-criterion verify and the runtime executes whatever passes that —
fire-and-forget.

**2. In-flow approval (review a specific user-workflow node's
output mid-execution).** Planner inserts `verify: { human: ... }` on a
user-workflow node only when the user's *prompt itself* explicitly
asked for in-flow review on that step ("show me the patch before
applying", "let me sanity-check the regex"). Inserting it on nodes
the user didn't ask about is a UX regression — it stalls the workflow.

In both cases, the runtime mechanic is identical. The question is
*who decides to insert it*:

| context | default | how to opt-in |
|---|---|---|
| Plan-level (`render_yaml`) | absent — fire-and-forget | `camflow run -i "<prompt>"` |
| User-workflow node | absent | user mentions in-flow review in the prompt; Planner's `workflow_designer` skill detects it |

Note: `-i` flag controls only the plan-level gate. It does NOT cause
the Planner to sprinkle `verify: human` across user-workflow nodes.

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

## 10. Skills — registry, layout, lifecycle

A **skill** is a directory containing a `SKILL.md` markdown file. The
file is opaque from the runtime's perspective: it gets injected
verbatim as the first section of every node prompt that references the
skill. SKILL.md content gives the agent its identity, conventions,
output contract, and recovery rules.

### Layout

```
<dir>/skills/<skill_name>/SKILL.md          ← the only required file
                          │
                          └─ optionally accompanied by examples,
                             reference data, or sub-skill files
                             that the SKILL.md itself references
```

### Resolution order

`run.skill: <name>` is searched in this order at workflow-load:

| order | path | who owns |
|---|---|---|
| 1 | `<project>/skills/<name>/SKILL.md` | the project (workflow author) |
| 2 | `<camflow_repo>/skills/<name>/SKILL.md` | shipped with camflow |
| 3 | `<camflow_repo>/builtin/<name>/skills/<name>/SKILL.md` | builtin-workflow-private (only when the running workflow IS the builtin, e.g. Planner) |

The first match wins. The Planner's project_root is overridden to its
own builtin directory so its private skills (`prompt_analyzer`,
`workflow_designer`, `yaml_writer`) don't pollute the global namespace.

### Strict registry (load-time enforcement)

* **Workflow load fails if any referenced skill is missing.** No dynamic
  creation, no fallback, no fuzzy matching. If Planner emits
  `skill: my_new_idea` and `skills/my_new_idea/SKILL.md` doesn't exist,
  the run never starts — fail-fast with a clear error.
* This is a doctrine choice (rule #6): skills are checked-in artifacts,
  not LLM-generated at runtime. Planner has to design within the
  available skill set; it cannot invent skills mid-run.

### Adding / managing skills

To make a new skill `foo` available:

```
mkdir -p skills/foo
cat > skills/foo/SKILL.md <<'EOF'
# Skill: foo
You are <identity>. Your job is <X>. ...
EOF
```

That's it. The next workflow that references `skill: foo` resolves it.
There's no manifest, no register-call, no tooling. The directory's
existence + presence of `SKILL.md` is the registration.

To remove a skill: delete the directory. Workflows that referenced it
will fail to load (which is the desired behavior — silent drift is
worse than a hard error).

To version a skill: just edit `SKILL.md`. The old version is in git
history. There's no in-tree multi-version mechanism (don't add one
without RFC).

### What SKILL.md should contain

There is no schema; this is a recommendation:

* **Identity** — "You are X. Your job is Y."
* **Conventions** — coding style, output format, idioms expected.
* **Process** — numbered steps for the typical case (mirrors the node's
  own `steps:` checklist; redundancy is fine).
* **Output contract** — the envelope shape, what `data` fields the
  agent must produce. Runtime also injects this automatically, but
  having it in SKILL.md helps the agent self-correct without re-reading
  the auto-injection.
* **On retry** — what to do when `previous.feedback` is present.

### Worked example: `skills/code_writer/SKILL.md`

A complete, realistic skill — given a function spec from upstream,
emit Python code + pytest tests on disk. This is canonical *skill*
territory (judgment, integration, retry-aware) — it could not be a
tool node, since the script would have nothing to do beyond echoing.

```markdown
# Skill: code_writer

You are a Python code writer. Given a function specification — passed
in as upstream output from a design node — you produce the function
plus pytest tests on disk.

## Conventions

- Append, don't overwrite. If `util.py` or `test_util.py` already
  exist (created by earlier nodes in the DAG), append to them; other
  functions and tests must keep working.
- Standard library only. No external dependencies.
- Type hints + docstrings on every function.
- Tests use plain `def test_xxx():` (pytest), importing from `util`.

## Inputs you'll see

The runtime auto-injects these into your prompt:

- `# Workflow Context` — project-wide conventions, output paths.
- `# Upstream Outputs` — typically `upstream.design.data` carries the
  function signature, behavior description, and edge cases to cover.
- `# Steps` — this node's checklist, in order.

There is no `run.input:` in v1.1; everything you need is above.

## Process

1. Read upstream's spec under `upstream.<id>.data`.
2. Append the function to `<output_path>/util.py` (create if missing).
3. Append the tests to `<output_path>/test_util.py` (create if missing).
4. Optionally run pytest locally to sanity-check; the node's
   `verify.command` will run it authoritatively.
5. Write the envelope JSON and stop.

## Output contract

Match the node's `output_schema` exactly. Typical fields:

- `summary` (string) — one sentence on what was added.
- `func_name` (string) — the function name written.
- `lines_added` (integer) — number of lines appended.

On failure: `status = "fail"`, `error.code` ∈
{`FILE_WRITE`, `AMBIGUOUS_SPEC`, `CONFLICT_WITH_EXISTING`}, plus an
explanatory `error.message`.

## On retry

If `previous.feedback` is present in the input, read it carefully.
Common reasons the previous attempt was rejected:

- code didn't pass `verify.command` (pytest)
- overwrote existing functions instead of appending
- signature drifted from `upstream.design.data.signature`
- missing edge-case tests called out in the design

Address the specific feedback this attempt — do not re-emit the same
code unchanged.
```

This skill is exercised by the worked workflow in Appendix A.

### Tools available to skill agents

A skill agent runs as `claude` spawned by camc, which by default grants
Bash / Edit / Read / Write / Glob / Grep / WebFetch / TodoWrite. There
is no per-skill tool-grant mechanism in camflow itself — that would
require a runtime extension and an RFC. If you need a non-standard tool
set, that's currently out of scope.

### When to use `run.tool:` (hard rules)

`run.tool: <path>` runs a shell script directly as the node's executor:
stdin = `input.json` (with `upstream` + `previous`), stdout = envelope
JSON. Workflow-load resolves it to `<project>/<path>` and fails if the
file isn't `-x`.

**Tool nodes are an escape hatch, not a coequal alternative to skills.**
Use tool **only if ALL FIVE of these hold**:

1. **Known command, no judgment.** The work is "run *this exact*
   command" — pytest, prettier, terraform plan, make. There is no
   decision to make about *what* to run.
2. **Inputs are fully determined.** Everything the script needs is
   already in `input.json` (i.e., from upstream envelopes); no extra
   context-reading, no "look around the project to figure it out".
3. **Output is structured by the script itself.** The script writes a
   well-formed envelope. No LLM is needed to *interpret* command output
   into `data` fields.
4. **Idempotent and side-effect-bounded.** Re-running it is safe; the
   side effects are confined to a known location (a build dir, a
   formatter overwriting files, etc.).
5. **Cost or speed actually matters.** This step runs often enough, or
   in a loop, that the LLM startup overhead (10–30s + tokens per spawn)
   is meaningful.

**If ANY of those don't hold → use a skill.** In particular, use a
skill when:

* The node needs to **read multiple files and reason** about them.
* The node needs to **handle cases** ("try X, if it fails apply Y").
* The node needs to **interpret stdout/stderr** into structured data.
* The node needs to **integrate `workflow.context`** into its action
  (e.g., "respect the conventions stated in context").
* The output requires **synthesis** of more than what the script
  literally prints.
* You're unsure. **Default to skill.**

A skill agent can always shell out via Bash, so anything a tool can
do, a skill can do — slower and more expensive, but also more
adaptable. The Planner's job is to pick the right one per node. See
the workflow_designer SKILL.md for the operational rules it follows.

### When NOT to use a tool node

Anti-patterns — these are tool misuse and will be flagged by the
verify-agent reviewing Planner's output:

* "Tool that wraps a Python one-liner that imports json and post-processes
  upstream output." → that's interpretation, use a skill.
* "Tool that conditionally chooses between two commands based on
  upstream data." → that's judgment, use a skill.
* "Tool that runs `git status` and tries to decide if the working tree
  is clean." → that's reading + reasoning, use a skill.
* Stacking three tool nodes in a row to chain shell pipelines. → if it's
  truly mechanical, write one tool that does the whole pipeline; if any
  step needs interpretation, use a skill for the chain.

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
camflow run "<prompt>"           # fire-and-forget: Planner compiles, runtime executes
camflow run -i "<prompt>"        # interactive: pause for plan approval before execution
camflow resume <run_dir>         # resume a halted run
```

| command | behavior | exit codes |
|---|---|---|
| `run "<prompt>"` | mandatory prompt; runs Planner → executes generated workflow.yaml | 0 done / 1 invocation error / 2 halted |
| `run -i "<prompt>"` | same, plus a plan-approval gate after Planner finishes (see §9) | 0 / 1 / 2 |
| `run` (no prompt) | print error and exit | 1 |
| `resume <run_dir> [--feedback "<text>"]` | resume a halted run; one more attempt for the halted node, optional human feedback. See §13 for full semantics. | 0 / 1 / 2 |

`-i` / `--interactive` patches Planner's `render_yaml` node at startup
to require human approval of the compiled `workflow.yaml`. Without
`-i`, Planner finishes and runtime executes the result with no pause.

Inspecting in-progress: `cat .camflow/run/trace.jsonl` (no `camflow status` subcommand).

There is no `camflow exec workflow.yaml`, no `--validate`, no `--inputs`. The only way to run a workflow is via `camflow run "<prompt>"` — Planner is mandatory, prompt is mandatory.

---

## 13. Resume

After a workflow halts, the user can fix what went wrong and continue
where they left off via `camflow resume <run_dir>`. Resume is the
manual recovery counterpart to retry — retry happens automatically
within a node (bounded by `retry_max`); resume is the human-driven
escape hatch at workflow level after retries are exhausted.

### Pre-conditions

```bash
camflow resume <run_dir> [--feedback "<text>"]
```

* `<run_dir>/halt.json` MUST exist. If absent → CLI prints
  `ERROR: not halted` and exits 1.
* `<run_dir>/workflow.yaml` MUST exist (the compiled IR is intact).
* `<run_dir>` may be the user's main run dir (`./.camflow/run/`) OR
  a Planner sub-run dir (`./.camflow/run/planner/`) when it was
  Planner that halted. The same code path handles both.

### What gets restored from disk

The runtime walks `<run_dir>/nodes/` and rebuilds per-node state by
replaying attempt directories:

| Node had on disk | Post-restore state |
|---|---|
| `attempt-N/output.json` files | `history` = list of envelopes, `output` = last envelope, `retry_count` = N − 1, `lifecycle = "done"`, `result` = `success`/`fail` per last envelope |
| no attempt directory | default fresh state (`lifecycle = "waiting"`, `retry_count = 0`) — these are downstream nodes that were marked done+fail by halt propagation but never executed |

This restoration is precise: a workflow that was halfway through a
DAG has its successful upstream nodes preserved as `done+success`
(they don't re-run), and its yet-to-run downstream nodes left as
`waiting` (they'll execute as the DAG progresses).

### Where execution restarts

The single halted node — identified by `halt.json`'s `halted_node`
field — is reset:

```
node.lifecycle = "waiting"
node.result    = None
node.retry_max = max(node.retry_max + 1, node.retry_count + 1)
                       └────────┬────────┘
                       grants exactly one more attempt
```

`retry_count` is **not** reset; it stays at the count from the
original run. Combined with the bumped `retry_max`, this means resume
gives **one fresh attempt**, not a full retry budget. If that
attempt also fails (for the same or different reason), the workflow
halts again — and the user can `resume` again, granting one more.

The runtime then re-enters `Workflow.execute_dag()`. The DAG
scheduler picks up the now-ready halted node, runs it, and proceeds
forward — any downstream nodes become ready in turn if it succeeds.

### Feedback channel (`--feedback "<text>"`)

When the user wants to *steer* the next attempt (not just retry it
identically), `--feedback` injects guidance:

```bash
camflow resume .camflow/run --feedback \
  "the test was looking at the wrong file; check src/auth.py instead"
```

Mechanics — uses the same channel as agent-rejected retries:

```
1. Splice text into halted node's last envelope on disk:
       node.history[-1].feedback = "<text>"

2. On the next attempt, runtime auto-injects `previous` into the
   input dict (per §6 retry feedback channel):
       attempt-(N+1)/input.json
         {
           "upstream": {...},
           "previous": {
             "status": "fail",
             "data":   {...},
             "error":  {...},
             "feedback": "the test was looking at the wrong file..."
           }
         }

3. Skill agent reads input.previous.feedback as the actionable
   directive; SKILL.md should already document the "On retry" path
   that consults previous.feedback.
```

So `--feedback` is the user's hand-typed equivalent of the verify
agent's reject reasoning. Same field, same plumbing.

### Halt scenarios and what resume does to each

| Halt cause | Resume behavior |
|---|---|
| Verify-agent rejected; retries exhausted | One more attempt; user can pass `--feedback` to redirect |
| `verify.command` failed; retries exhausted | One more attempt; if the issue is transient, plain resume; if it's substantive, fix the cause + `--feedback` |
| `verify.human` rejected and retry budget consumed | One more attempt; user can be more decisive on next try (or pass `--feedback` describing what to do differently) |
| Node returned `request_human=true` | One more attempt; the human is expected to have resolved the underlying issue (e.g. unstuck a stale credential, fixed a file) before resuming |
| Planner halted (couldn't compile valid yaml) | Resume `<run_dir>/planner/`; one more shot at the failing Planner node, optionally with `--feedback` steering the regeneration |

### `-i` flag and resume

`-i` / `--interactive` is a `run`-time flag only. Once a workflow.yaml
has been compiled (and possibly approved via `-i` on the original
`run`), the *same* compiled yaml is reused on every resume — plan
approval does NOT re-trigger. The user already approved the plan; now
they're just unsticking individual nodes.

### What resume cannot do

* Cannot resume a workflow that completed successfully (no halt.json).
* Cannot un-run a node that already succeeded — `success` is permanent
  within a run.
* Cannot reorder, insert, or delete nodes — the DAG is fixed once
  Planner has compiled. If the user wants a fundamentally different
  plan, they should start a fresh `camflow run`.
* Cannot re-prompt the Planner mid-run for a different design;
  Planner is upstream of execution, not interactive with it.

### Trace event on resume

```jsonl
{"step": N, "ts": "...", "event": "workflow_resumed",
 "node": "<halted_id>", "retry_count": <count>, "feedback_len": <len>}
```

The trace's continuity (step counter monotonic across original-run +
resume) makes resumed runs distinguishable from fresh ones in
post-mortem analysis without extra tooling.

---

## 14. The builtin Planner workflow

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
  that decomposes the task into a DAG of nodes (skills by default,
  tools only for narrow mechanical cases — see §10), each with
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
      - "Pick a skill from the strict registry. Use a tool ONLY if all 5 §10 criteria hold; otherwise skill."
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

## 15. Reserved / forbidden in v1.1

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

## 16. Doctrine (rules for changing this spec)

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
13. **Skill is the default run executor.** `run.tool:` is allowed only when ALL FIVE hard criteria in §10 hold (known command + fully-determined inputs + script-structured output + idempotent + cost matters). When in doubt, use skill. Adding a new run-phase executor type beyond skill / tool requires RFC.
14. **Verify phase carries the deterministic gate.** `verify.command` (bash exit code) is the canonical place for "did the test pass / did the file parse / did the patch apply" checks. Verify-side commands are unconstrained — they're already deterministic by design. The hard rules in #13 are about RUN-phase tools, not verify.
15. **`verify: human` is opt-in via two distinct mechanisms.** Plan-level approval is opted into via the `-i` / `--interactive` CLI flag (runtime patches Planner's `render_yaml`); in-flow node approval is opted into via the user's prompt language (Planner's `workflow_designer` detects requests like "show me X before doing Y"). Both default to off — `camflow run "<prompt>"` is fire-and-forget. Adding human gates the user didn't request is a UX regression.

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
      - "Apply upstream.propose_fix.data.patch"
      - "Run python -m pytest"
      - "Capture pass/fail"
    run: { tool: scripts/apply_and_test.sh }    # OK to use tool here:
                                                 # known command, no judgment,
                                                 # script writes envelope itself
    output_schema:
      passed: boolean
      tests_run: integer
    verify:
      command: "test $(jq -r .data.passed agent_output.json) = 'true'"
    retry: 1
```

> Note `run_tests` legitimately uses `run.tool:` — it's a known
> command (apply patch + pytest), inputs are fully determined by
> upstream, the script structures its own output, and the operation is
> idempotent. All five §10 criteria hold. Compare to `diagnose` and
> `propose_fix` which need code-reading and synthesis — those must be
> skills.

The user reviewed `render_yaml`'s output, typed `approve`. Runtime now executes this 3-node workflow:

1. `diagnose` → camc spawns analyzer agent → reads context (which carries the original prompt) → produces root_cause + affected_location → schema check passes → default verify-agent approves → done+success.
2. `propose_fix` → camc spawns code_writer agent → reads upstream's diagnose output (auto-injected) → produces patch → schema check passes → verify-agent (with criterion override) approves → done+success.
3. `run_tests` → tool runs `scripts/apply_and_test.sh` with patch from upstream → produces `passed: true` → verify=command checks bash → exit 0 → done+success.

Workflow lifecycle: running → done. Exit 0.

---

## End of spec.
