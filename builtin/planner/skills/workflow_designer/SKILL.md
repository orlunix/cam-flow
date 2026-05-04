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
  "dag": [
    {
      "id": "...",
      "goal": "...",
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
   `verify.command` for that gate. This is what makes the runtime's
   retry-with-feedback loop fire on missed requirements: a wrong
   implementation gets an exit-code rejection, not a self-approving
   agent verdict, and the next attempt sees the failure as
   `previous.feedback`. Falling back to `verify: agent` when a
   deterministic gate is available is a regression — the agent can
   convince itself its own output is fine.

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
5. **Retry sparingly.** Default 1 (no retry). High-stakes generative
   work (code, plans) → 2-3. Deterministic tools rarely need retry.
6. **`workflow.context`** is for facts shared across all nodes — put
   the user's original task there, plus run-constants like tool paths,
   conventions, codebase layout, anything every node should know.
7. **No `state:`, no `inputs:`, no `run.input`.** they don't exist.
   Anything constant goes in `workflow.context`. Per-run inputs flow
   through the user's original prompt only (already in context).

## Output `dag` shape

A list of node dicts. Each must have `id`, `goal`, `steps`, `run`. The
other fields are optional but recommended where applicable.

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
   project has). On failure the runtime auto-retries with
   `previous.feedback`. `retry: 2` or `3` is appropriate.

3. **One audit tool node per test class.** When the project has
   distinct test groupings (visible vs. invariant, unit vs.
   integration), give each its own `run.tool` node that emits a
   structured pass/fail envelope (`{passed: bool, tests_run: int,
   failed_tests: array}`). These are pure audit — they make the
   passing evidence concrete in the trace, separate from the
   implementer's self-report. Each should `needs: [implementer]` and
   use `verify.command` to gate on the envelope's `data.passed` field.

4. **`reviewer`** (skill: `reviewer`, `needs: [analyzer, implementer,
   <each audit node>]`) — independently confirm every requirement is
   satisfied, citing either a `file:line` range in the implementation
   or a passing test name from the audit envelopes.

This shape is not the only valid one — adapt it to what the actual
project has. But when these ingredients are present (a spec,
multi-class test suite, a deterministic test command), this is the
shape that produces inspectable per-attempt artifacts and surfaces
retry-with-feedback when the implementer misses a hidden requirement.

If the case has no test suite, drop the audit nodes and lean on
`verify: agent` for the implementer (with the spec's requirements as
explicit verify criteria). If the case is single-step (config tweak,
trivial rename), a 2-node analyzer+implementer DAG is fine.

## On retry

`previous.feedback` tells you what the verify agent rejected. Most
common: missing dependencies, orphan nodes, infeasible decomposition,
referenced a skill that doesn't exist, fell back to `verify: agent`
on a node where a deterministic test command was available.
