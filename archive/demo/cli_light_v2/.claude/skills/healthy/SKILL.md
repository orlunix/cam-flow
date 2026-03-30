---
name: healthy
description: Monitor workflow progress and detect issues
---

Check workflow health:

1. Read `.claude/state/workflow.json`
2. Determine current step and progress
3. Detect issues:
   - repeated steps
   - no progress
   - repeated failures

If healthy:
- Return: status = ok

If not healthy:
- Return: status = stuck
- Suggest next action
