---
name: healthy
description: Monitor workflow execution
---

1. Read .claude/state/workflow.json
2. Check current node
3. Detect:
   - repeated node
   - no progress
   - high retry count
4. If healthy:
   return status = ok
5. If stuck:
   return status = stuck
   suggest next action
