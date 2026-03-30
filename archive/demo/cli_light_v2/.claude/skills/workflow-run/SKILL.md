---
name: workflow-run
description: Execute the workflow step-by-step
---

Run the workflow defined in CLAUDE.md.

Steps:
1. Read current state from `.claude/state/workflow.json` (if exists)
2. Determine current step
3. Execute the step
4. Update state file
5. Decide next step

Repeat until workflow is complete.
