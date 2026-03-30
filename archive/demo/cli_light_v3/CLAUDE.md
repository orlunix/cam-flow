# CLI Light DSL Execution

You are executing a workflow defined in `workflow.yaml`.

## Responsibilities

1. Read `workflow.yaml`
2. Parse nodes, tasks, and transitions
3. Maintain state in `.claude/state/workflow.json`
4. Execute current node
5. Update state
6. Determine next node

## State format

{
  "pc": "start",
  "status": "running"
}

## Rules

- Always follow DSL transitions
- Do not invent new nodes
- Continue until `done`
- If stuck, try alternative reasoning
- If repeated failure, escalate approach

## Loop behavior

You may be periodically checked by `/healthy`.
You must keep state consistent.
