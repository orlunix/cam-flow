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
   Fall back to `tool` references for shell scripts when applicable.
4. **Verify everything non-trivial.** Default is `verify: agent` (steps
   as checklist). Use `verify.command` for things you can check with
   bash exit code. Use `verify.human` only at human-decision points.
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
