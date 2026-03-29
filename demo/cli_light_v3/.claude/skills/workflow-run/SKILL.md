---
name: workflow-run
description: Execute DSL workflow from workflow.yaml
---

1. Load `workflow.yaml`
2. Load `.claude/state/workflow.json`
3. Identify current node (pc)
4. Execute task defined in DSL
5. Update state file
6. Follow DSL transitions
7. Repeat until done
