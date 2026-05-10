# Skill: workflow_designer

Second node of the builtin Planner workflow. You take the
`task_statement` produced by `prompt_analyzer` and design a DAG of
nodes whose successful execution accomplishes the task.

The upstream `prompt_analyzer` envelope is in your input under
`upstream.understand`.

## What you must produce

A JSON envelope written to `agent_output.json`. Required `data` fields:

```json
{
  "workflow_goal": "<one-sentence concrete restatement of the user's objective — the persistent objective for the run>",
  "dag": [
    {
      "id": "...",
      "goal": "<this node's local objective — for non-trivial nodes, must name the part of workflow_goal it advances or proves>",
      "steps": ["...", "..."],
      "needs": [],
      "run": {"skill": "..."},
      "verify": {"criterion": "..."},
      "retry": 2,
      "output_schema": {"field": "string"}
    }
  ],
  "context": "<text to put in workflow.context — shared facts every node sees>"
}
```

## Workflow goal — the persistent objective for the run

Every workflow you design has a top-level `workflow_goal` field —
**you must produce one** as a single concise sentence that restates
the user's objective in concrete terms. It's what `analyzer.task_statement`
condenses to one outcome sentence. The runtime persists this as the
compiled workflow's top-level `goal:` (the existing v1.1 `Workflow.goal`).
Retry, review, and final audit are judged against this goal — not
just the last local error.

**Every non-trivial Node.goal must map back to workflow_goal.** A
non-trivial node is any skill node or any audit/reviewer node — not
boilerplate set-up tools. Phrase Node.goal so it names the *part* of
workflow_goal the node advances or proves: "extract the requirement
list workflow_goal will be measured against", "implement the artifact
satisfying workflow_goal", "audit one test class proving part of
workflow_goal", "independently confirm workflow_goal is met with
per-requirement evidence". A node-goal that's just a verb ("read
files", "run pytest") without naming the workflow-goal connection is
weak — the retry / reviewer machinery needs that connection to know
what evidence still counts.

The trivial set-up exception: a one-shot tool that does an obviously
mechanical thing (e.g., `chmod +x`, copying a fixture) doesn't need a
workflow-goal sentence. When in doubt, link the goal.

## Design rules

1. **Decompose deliberately.** Each node should be one cohesive unit of
   work — small enough that a single skill / tool can do it well, large
   enough that the verify can meaningfully check it.
2. **Dependency edges only via `needs`.** A node lists its upstream
   nodes by id; the runtime auto-injects upstream envelopes into its
   prompt. No `run.input` field — that doesn't exist.
3. **Pick existing skills.** If you don't know a skill exists, don't
   reference it. The runtime fails workflow load if a skill is missing.

   **Available repo skills** — the camflow shipped `skills/` directory
   commonly carries these (resolved automatically; treat the list as
   indicative, not exhaustive — projects can add their own under
   `<project>/skills/<name>/SKILL.md`):

   - `analyzer` — read source/spec/test artifacts and emit a
     structured requirements list with verbatim evidence.
   - `code_writer` — implement or modify code to satisfy a stated
     requirement set; iterate until a deterministic test command
     passes.
   - `reviewer` — independently confirm a diff covers a requirement
     list, citing concrete file:line evidence per requirement.
   - `evaluator` — the default agent-verify role (you don't reference
     this directly; the runtime invokes it when a node has no explicit
     `verify` block).

   Prefer these over inventing new skill names. If none of them fit a
   step, that's a sign the step is itself a custom skill — but don't
   reference custom names unless you know they exist on disk.

   **`run.tool:` is an escape hatch with hard criteria.** Default every
   node to `run: { skill: <name> }`. Use `run: { tool: <path> }` only
   when ALL FIVE of these hold for the node:

   1. Known command, no judgment about *what* to run.
   2. Inputs are fully determined by upstream + workflow.context.
   3. The script writes a structured envelope itself (no LLM
      interpretation of stdout/stderr needed).
   4. Idempotent and side-effect-bounded.
   5. Cost or speed actually matters here (in a loop, or hot path).

   If ANY criterion fails → skill. If you're not sure → skill.

   **Tool anti-patterns** (do NOT design these):
   - tool that wraps a one-liner doing JSON post-processing of upstream
   - tool that conditionally chooses among commands based on upstream
   - tool that reads files and tries to interpret them
   - chains of three+ tool nodes for what is really one pipeline

   When the work is "compile / test / lint / format with a known CLI",
   tool is correct. When the work is "look at the failing test and
   propose a fix", that's skill.
4. **Verify everything non-trivial.** Default is `verify: agent` (steps
   as checklist). Use `verify.command` for things you can check with
   bash exit code.

   **Prefer `verify.command` whenever a deterministic test command
   exists.** If the task has any pass/fail gate that a shell command
   can evaluate (`pytest tests/`, `make test`, `cargo test`, `npm
   test`, a custom shell script that exits non-zero on failure), the
   generative node that produces the artifact under test should use
   `verify.command` for that gate. The point is **deterministic
   gating** — a wrong implementation gets an exit-code rejection, not
   a self-approving agent verdict. If verification fails, the runtime
   supports retry-with-feedback as a bounded recovery path, but the
   goal is still first-pass correctness; retry is the safety net,
   not the feature. Falling back to `verify: agent` when a
   deterministic gate is available is a regression — the agent can
   convince itself its own output is fine.

   **Portable command rule.** `verify.command` must use only POSIX shell
   builtins, common Linux/coreutils commands, `python3` (stdlib only), or
   task-specific commands that are already known to exist in the target
   environment. Do not assume optional host tools (for example `jq`,
   `yq`, `qsub`, `smake`, `p4`, custom CLIs) exist just because they are
   convenient. For JSON checks, prefer parsing `agent_output.json` with
   Python stdlib. If a command is not common Linux/coreutils, either:

   - make the workflow check it with `command -v <cmd>` before use and
     fail clearly when missing, or
   - replace the shell dependency with Python stdlib logic inside the
     `verify.command`, or
   - move the behavior into an existing declared `run.tool` wrapper
     script that lives in the project/package and emits a CamFlow
     envelope, or
   - use a `run.skill` node to perform the operation when the wrapper
     does not already exist.

   Do not reference a script as `run.tool` unless it exists at workflow
   load time and is executable. The runtime validates `run.tool` paths
   before the workflow starts; a script created later by another node
   cannot satisfy that load-time check. For reusable packaged workflows,
   put wrapper scripts under the package's declared tools directory.

   The Planner's own render_yaml verification runs a deterministic
   command-availability check on every non-general command head in the
   compiled workflow's `verify.command` snippets. If you emit `jq`,
   `qsub`, `smake`, `p4`, a custom CLI, or any other non-general command
   and it is not available on the target PATH, render_yaml will fail and
   retry with that feedback.

   **`verify.command` runs from the node's attempt directory, NOT the
   project root.** Specifically the cwd is
   `<run-dir>/nodes/<id>/attempt-N/`, several levels deep under
   `<project>/.camflow/run/`. So a naive `bash scripts/run_all_tests.sh`
   or `pytest tests/` fails with exit 127 / "no such file or
   directory" — the test command never runs at all and the runtime
   reports retry exhaustion that has nothing to do with code quality.

   The reliable cross-project pattern is a walk-up to a project-root
   marker, then run from there:

   ```yaml
   verify:
     command: |
       P=$(pwd)
       while [ ! -f "$P/<marker>" ] && [ "$P" != "/" ]; do P=$(dirname "$P"); done
       [ "$P" = "/" ] && { echo "ERROR: no <marker> ancestor"; exit 2; }
       cd "$P" && <your test command>
   ```

   Pick `<marker>` as the most specific single file that uniquely
   identifies the project root for this case. In rough priority:

   1. A spec/readme file the user named in the prompt
      (`SPEC.md`, `README.md`, `CONTRIBUTING.md`).
   2. A language manifest (`pyproject.toml`, `setup.cfg`,
      `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`,
      `pom.xml`, `build.gradle`).
   3. `.git` as a last resort (loose — fails inside a submodule or
      worktree).

   Avoid markers that appear in multiple ancestors (e.g. plain
   `__init__.py`, `index.js`, `main.go`). The `[ "$P" = "/" ] && exit 2`
   guard is mandatory — without it a missing marker silently cds to
   `/` and the test runs against your filesystem instead of the
   project. Failing loudly is the right behavior; the runtime then
   reports exit 2 in `previous.feedback` and the next attempt can
   correct the marker.

   If the deterministic test command is itself a path-bearing script
   (e.g. `scripts/run_all_tests.sh`), using the script's *directory
   anchor* as the marker is also fine — pick a parent file unique to
   that project layout (the spec file, manifest, or the script's own
   name if guaranteed unique).

   **`verify: human` on USER nodes is OPT-IN.** This is about the
   workflow YOU are designing — the user's compiled workflow.yaml.
   Only insert `verify: human` on a user-workflow node when the user's
   prompt explicitly asks for *in-flow* review on that specific step
   ("show me the patch before applying it", "let me sanity-check the
   regex"). Default is no human gating mid-flow. Adding human approval
   the user didn't ask for is a regression — it stalls the workflow.

   When the user did ask for in-flow review, attach `verify: { human: ... }`
   to the relevant node (the one whose output the user wants to gate
   on), not every node.

   **Don't worry about plan-level approval.** Whether the user gets to
   review the compiled workflow.yaml before execution is controlled by
   the runtime's `-i` / `--interactive` CLI flag, NOT by you. If the
   user wants plan approval, they invoked `camflow run -i "<prompt>"`
   and the runtime patches Planner's `render_yaml` accordingly. You
   should never put `verify: human` on the `render_yaml` node yourself,
   and you should not assume plan approval will or won't happen.
5. **Retry sparingly — it's a safety net, not a feature.** Design
   for first-pass correctness; retry is bounded recovery if
   verification fails, not part of the expected path. Default 1
   (no retry). High-stakes generative work (code, plans) → 2-3 as
   recovery budget. Deterministic tools rarely need retry — if a tool
   audit fails twice, that's evidence the implementation is wrong,
   not something to loop on. Excessive retry churn (looping on the
   same failure mode) is a Planner bug, not a feature.
6. **`workflow.context`** is for facts shared across all nodes — put
   the user's original task there, plus run-constants like tool paths,
   conventions, codebase layout, anything every node should know.
7. **No `state:`, no `inputs:`, no `run.input`.** they don't exist.
   Anything constant goes in `workflow.context`. Per-run inputs flow
   through the user's original prompt only (already in context).

## Output `dag` shape

A list of node dicts. Each must have `id`, `goal`, `steps`, `run`. The
other fields are optional but recommended where applicable.

## `output_schema` field types — strict allow-list

Every value in a node's `output_schema` map MUST be one of these five
type names exactly:

- **`string`** — text
- **`integer`** — whole number (NOT `int`)
- **`number`** — int or float
- **`boolean`** — true/false (NOT `bool`)
- **`array`** — list (NOT `array of <X>`, NOT `list`)

Anything else is a workflow-load error. **Forbidden** patterns the
runtime will reject (or silently ignore, which is worse — the field
stops being type-checked):

- `bool` — write `boolean`
- `int` — write `integer`
- `float` — write `number`
- `list` — write `array`
- `array of string`, `array of {...}`, `string[]` — just write `array`;
  array element types are NOT part of v1.1 schema. Document the
  element shape in the node's `goal` / `steps` instead.
- Inline object schemas like `{id: string, count: integer}` — also not
  v1.1. Promote to top-level fields if you need typed access, or use
  `array` and document the per-element shape in the goal.
- Schema as a string: `"object"`, `"any"`, `"json"` — not v1.1 types.

Right vs wrong:

```yaml
# RIGHT
output_schema:
  passed: boolean
  tests_run: integer
  failed_tests: array

# WRONG (Planner-load failure or silent skip)
output_schema:
  passed: bool                 # use boolean
  tests_run: int               # use integer
  failed_tests: array of string  # element type not allowed; use array
  results: { id: string, n: int }  # nested schema not allowed
```

This applies to every node you produce, including audit tool nodes
and reviewer nodes — same five type names everywhere.

## Audit-node mandatory check

**If `upstream.understand.data.deterministic_test_scripts` is
non-empty, you MUST include one `run.tool` audit node per script
listed there.** Each entry has the shape
`{path: <script_path>, envelope_data_fields: [<field>, ...]}` —
use both fields when designing the audit node. Per audit node:

- `run: { tool: <script_path> }` (the `path` comes verbatim from
  the analyzer's entry).
- `needs: [<implementer-class-node>]`.
- `output_schema` MUST match the script's **actual** envelope —
  declare ONLY the fields listed in this script's
  `envelope_data_fields` (mapped to the right v1.1 type:
  `passed: boolean`, `tests_run: integer`, `output: string`,
  `failed_tests: array`, etc.). **Two scripts with different
  `envelope_data_fields` get different schemas — don't generate a
  uniform schema across audit nodes.** The runtime accepts
  envelopes with extra `data` fields; it REJECTS envelopes missing
  declared fields, so under-declaring is safe and over-declaring
  halts the node.
  Minimum-viable audit schema if a script emits at least
  `{passed, tests_run, output}`:
  ```yaml
  output_schema:
    passed: boolean
    tests_run: integer
    output: string
  ```
  Add `failed_tests: array` ONLY for scripts whose
  `envelope_data_fields` includes `"failed_tests"`. If the analyzer
  surfaced a `path` but no `envelope_data_fields` summary (or just
  a string path under an older shape), default to the minimum
  schema.
- `verify.command` should parse `agent_output.json` with Python stdlib,
  not `jq` or other optional host tools. Use this portable pass/fail
  gate:
  ```yaml
  verify:
    command: 'python3 -c ''import json,sys; data=json.load(open("agent_output.json")).get("data", {}); sys.exit(0 if data.get("passed") is True else 1)'''
  ```
- Listed in the reviewer node's `needs` so the reviewer can cite the
  envelopes as per-class evidence.

**Why this is mandatory and not optional:** the implementer's
`verify.command` is a node-local quality gate — it tells you "the
implementer's attempt passed all tests" but it does NOT produce a
structured envelope a downstream reviewer or operator can cite as
independent evidence. Audit tool nodes produce
`agent_output.json` envelopes that live in `nodes/<id>/attempt-N/`
and become first-class citation targets. Skipping them when valid
envelope-emitting scripts exist is a regression — you lose the
per-class structured evidence trail and force the reviewer to do
its citation work from prose-level inference.

**If `deterministic_test_scripts` is empty** (the analyzer didn't
find any envelope-emitting runners), do NOT invent invalid
`run.tool` nodes. Two valid alternatives:

1. **Skill-based audit node** — a node with `run: { skill: ... }` if
   you have a registered skill that runs tests and emits the
   envelope (none of the shipped repo skills do this directly today;
   reach for project-specific custom skills if available).
2. **Lean on implementer.verify.command** — it's still a real
   deterministic gate, just without the structured per-class
   evidence. Reviewer must then cite tests by name from prose
   inference. Acceptable but weaker.

Don't fabricate a `run.tool` node pointing at a script that emits
raw test output (e.g. `pytest -q` returning to stdout). The runtime
will reject the envelope as `TOOL_BAD_OUTPUT` and the audit node
will halt instead of producing evidence.

## Common shape: implement code per spec

When the task is "implement / modify code to satisfy a written spec
plus a deterministic test command" (whether the language is Python,
Go, Rust, JS, or anything else), the standard DAG shape is:

1. **`analyzer`** — read the spec/source/test artifacts named in
   `upstream.understand` (especially `test_files`); emit a structured
   requirement list with verbatim evidence per requirement.

2. **`implementer`** (skill: `code_writer`, `needs: [analyzer]`) —
   implement against `upstream.analyzer.data.requirements`. Crucially,
   `verify.command` runs the FULL deterministic test invocation
   (visible + invariant / hidden / property tests, whichever the
   project has). The implementer should aim to satisfy every
   requirement first try; configure `retry: 2` or `3` as a bounded
   safety net so a verification failure can recover via
   `previous.feedback`, but do not design steps assuming retry will
   fire — retry is a recovery mechanism, not part of the happy path.

3. **One audit tool node per test class.** When the project has
   distinct test groupings (visible vs. invariant, unit vs.
   integration), give each its own `run.tool` node that emits a
   structured pass/fail envelope (`output_schema: passed: boolean,
   tests_run: integer, failed_tests: array`). These are pure audit —
   they make the passing evidence concrete in the trace, separate from
   the implementer's self-report. Each should `needs: [implementer]`
   and use `verify.command` to gate on the envelope's `data.passed`
   field. **Do not skip these nodes** when the project has separable
   test groups — they are the per-class evidence the reviewer cites.
   `retry: 1` is correct here: if a deterministic audit fails
   repeatedly, that's evidence the implementation is bad, not
   something to loop on.

4. **`reviewer`** (skill: `reviewer`, `needs: [analyzer, implementer,
   <each audit node>]`) — independently confirm every requirement from
   `upstream.analyzer.data.requirements` is satisfied, with one
   evidence citation per requirement: either a `file:line` range in
   the implementation or a passing test name from an upstream audit
   envelope. Approve only when every requirement has concrete
   evidence; reject with specific issues that name the missing
   requirement.

### Verbatim template (generic — adapt names/paths to your project)

Emit the `dag` along these lines for any "implement code per spec
plus deterministic test groups" task. Replace `<spec-marker>`,
`<test-cmd>`, `<visible-test-cmd>`, `<invariant-test-cmd>`,
`<run_visible.sh>`, `<run_invariants.sh>` with the project's actual
markers and commands; the structure should stay the same.

```yaml
- id: analyzer
  goal: "Extract every requirement in the spec plus the test files it references, with verbatim evidence per requirement."
  steps:
    - "Read the spec verbatim."
    - "Read each test file the spec references; verify which test covers which requirement."
    - "Emit a structured requirement list with one entry per req."
  run:
    skill: analyzer
  output_schema:
    requirements: array
    test_paths_referenced: array
  retry: 2

- id: implementer
  goal: "Implement the artifact satisfying every requirement listed by analyzer."
  needs: [analyzer]
  steps:
    - "Read upstream.analyzer.data.requirements as the ground-truth req list."
    - "Implement the artifact covering ALL listed requirements."
    - "Iterate until the deterministic test command exits 0."
  run:
    skill: code_writer
  output_schema:
    files_changed: array
    summary: string
  verify:
    command: |
      P=$(pwd)
      while [ ! -f "$P/<spec-marker>" ] && [ "$P" != "/" ]; do P=$(dirname "$P"); done
      [ "$P" = "/" ] && { echo "ERROR: no <spec-marker> ancestor"; exit 2; }
      cd "$P" && <test-cmd>
    timeout: 60
  retry: 3

- id: test_runner
  goal: "Audit the visible test suite and emit a structured pass/fail envelope."
  needs: [implementer]
  steps:
    - "Run the visible test command from the project root."
    - "Capture pass/fail and a count of tests run."
    - "Emit envelope with data.passed and data.tests_run."
  run:
    tool: scripts/<run_visible.sh>
  # Minimum-viable schema. If you've read the script and confirmed it
  # also emits failed_tests, add `failed_tests: array` here too —
  # otherwise omit (declared-but-missing fields halt the node).
  output_schema:
    passed: boolean
    tests_run: integer
    output: string
  verify:
    command: 'python3 -c ''import json,sys; data=json.load(open("agent_output.json")).get("data", {}); sys.exit(0 if data.get("passed") is True else 1)'''
  retry: 1

- id: invariant_checker
  goal: "Audit the invariant/hidden tests and emit a structured pass/fail envelope listing any failing tests."
  needs: [implementer]
  steps:
    - "Run the invariant/hidden test command from the project root."
    - "Capture pass/fail and any failed test names."
    - "Emit envelope with data.passed, data.tests_run, data.failed_tests."
  run:
    tool: scripts/<run_invariants.sh>
  # This schema includes failed_tests because run_invariants.sh
  # explicitly emits it. Match each script's ACTUAL envelope —
  # don't assume both audit scripts share a shape.
  output_schema:
    passed: boolean
    tests_run: integer
    failed_tests: array
    output: string
  verify:
    command: 'python3 -c ''import json,sys; data=json.load(open("agent_output.json")).get("data", {}); sys.exit(0 if data.get("passed") is True else 1)'''
  retry: 1

- id: reviewer
  goal: "Independently confirm the artifact satisfies every requirement; cite concrete evidence per req."
  needs: [analyzer, implementer, test_runner, invariant_checker]
  steps:
    - "Read upstream.analyzer.data.requirements (ground-truth req list)."
    - "Read the implementation."
    - "For each requirement, cite a specific file:line range OR a passing test name from upstream.test_runner / upstream.invariant_checker."
    - "Approve only if every requirement has concrete evidence; reject otherwise with specific issues that name the missing requirement."
  run:
    skill: reviewer
  output_schema:
    approved: boolean
    issues: array
  retry: 2
```

This shape is not the only valid one — adapt it to what the actual
project has. But when these ingredients are present (a spec,
multi-class test suite, a deterministic test command), this is the
shape that produces inspectable per-attempt artifacts and supports
retry-with-feedback as a recovery path if verification fails. The
goal is first-pass success: bounded retry is configured as a safety
net, not as a feature to be exercised.

If the case has no test suite, drop the audit nodes and lean on
`verify: agent` for the implementer (with the spec's requirements as
explicit verify criteria). If the case is single-step (config tweak,
trivial rename), a 2-node analyzer+implementer DAG is fine.

## On retry

`previous.feedback` tells you what the verify agent rejected. Most
common: missing dependencies, orphan nodes, infeasible decomposition,
referenced a skill that doesn't exist, fell back to `verify: agent`
on a node where a deterministic test command was available.

## On replan (`# Replan Context` block in the prompt)

If your prompt includes a `# Replan Context` section, you are being
re-invoked because a prior compiled workflow halted. The same
Workflow.goal still applies — do NOT silently drop requirements.
Read the context to understand:

- which node halted, and the halt kind (`halt` = retry exhausted /
  request_human; `breakpoint` = `--steps` debug stop);
- the prior DAG (it's quoted under `## Prior compiled workflow.yaml`);
- the recent trace events around the halt.

Decide whether the halt was:

- **local**: the failing node's plan was wrong (bad verify command,
  missing upstream data, wrong skill, wrong retry budget). Most of
  the prior DAG should remain — change just the failing node's
  plan, copy the rest. This is by far the common case.
- **structural**: the DAG itself was wrong (missing decomposition
  step, wrong audit shape, deterministic gate skipped, evidence trail
  too thin for the reviewer to confirm Workflow.goal). Re-design the
  affected slice; preserve nodes upstream of the halt that were
  already producing valid envelopes.

Don't re-design for the sake of it — if the prior plan was nearly
right, the new revision should look mostly like the prior one with
the targeted fix. The runtime records every revision under
`dag_revisions/<NNNN>/` so a downstream replay tool can compare
revisions; gratuitous rewrites make replay harder.
