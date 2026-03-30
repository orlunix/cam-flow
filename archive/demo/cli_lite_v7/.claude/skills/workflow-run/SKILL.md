---
name: workflow-run
description: Execute the formal cam-flow DSL from workflow.yaml
---

Use `template.md` as the execution contract.

Your job is to:

1. Read `workflow.yaml`
2. Read `.claude/state/workflow.json`
3. Read `.claude/state/memory.json`
4. Find the current node from `state.pc`
5. Parse the formal DSL fields for that node:
   - `do`
   - `with`
   - `next`
   - `transitions`
6. Execute the node according to `do`
7. Update workflow state
8. Continue until workflow reaches `done`

You MUST update state after each executed node.
You MUST NOT invent nodes or transitions.
