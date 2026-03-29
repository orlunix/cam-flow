---
name: healthy
description: Monitor DSL workflow execution
---

1. Read `.claude/state/workflow.json`
2. Identify current node
3. Check for issues:
   - same node repeating
   - no progress
   - repeated failures
4. If healthy:
   return status = ok
5. If stuck:
   return status = stuck
   suggest recovery action
