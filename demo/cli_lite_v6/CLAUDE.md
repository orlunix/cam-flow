# CLI Lite Runtime Rules

You are executing a workflow defined in `workflow.yaml`.

## Responsibilities

1. Parse the workflow DSL
2. Maintain `.claude/state/workflow.json`
3. Execute nodes step-by-step
4. Follow transitions strictly

## Execution model

- Always read current state first
- Identify current node (pc)
- Execute its task
- Decide next node using `next` or `transitions`
- Update state after each step

## Rules

- Do NOT invent nodes
- Do NOT skip transitions
- Do NOT stop early
- Always continue until `done`

## Supervisor integration

The `/healthy` skill will:
- analyze execution state
- provide suggestions

You should incorporate suggestions when necessary.

## Loop

Monitoring is external:

/loop 2m /healthy
