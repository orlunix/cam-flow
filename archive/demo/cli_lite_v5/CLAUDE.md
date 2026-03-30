# CLI Lite DSL Execution

You are executing a workflow defined in `workflow.yaml`.

## Responsibilities

1. Read `workflow.yaml`
2. Maintain `.claude/state/workflow.json`
3. Execute nodes step-by-step
4. Follow DSL transitions strictly

## Rules

- Do NOT stop early
- Always continue until `done`
- Do not invent nodes
- Update state after each step

## Loop

Monitoring is done externally via:

/loop 2m /healthy
