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
   prompt. No `run.input` field — that doesn't exist in v1.1.
3. **Pick existing skills.** If you don't know a skill exists, don't
   reference it. The runtime fails workflow load if a skill is missing.

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

   **Don't worry about plan-level approval.** The Planner's own
   `render_yaml` node already has a `verify: human` gate by design —
   the user always reviews + approves the compiled workflow.yaml
   before it starts executing. That's the interactive contract; you
   don't need to add anything for it. Your job is only the *in-flow*
   case described above.
5. **Retry sparingly.** Default 1 (no retry). High-stakes generative
   work (code, plans) → 2-3. Deterministic tools rarely need retry.
6. **`workflow.context`** is for facts shared across all nodes — put
   the user's original task there, plus run-constants like tool paths,
   conventions, codebase layout, anything every node should know.
7. **No `state:`, no `inputs:`, no `run.input`.** v1.1 doesn't have them.
   Anything constant goes in `workflow.context`. Per-run inputs flow
   through the user's original prompt only (already in context).

## Output `dag` shape

A list of node dicts. Each must have `id`, `goal`, `steps`, `run`. The
other fields are optional but recommended where applicable.

## On retry

`previous.feedback` tells you what the verify agent rejected. Most
common: missing dependencies, orphan nodes, infeasible decomposition,
referenced a skill that doesn't exist.
