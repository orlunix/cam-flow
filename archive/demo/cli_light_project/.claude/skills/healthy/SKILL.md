---
name: healthy
description: Check workflow progress and detect issues

disable-model-invocation: true
---

Check workflow health:

- Is progress being made?
- Is the same step repeating?
- Are errors repeating?

If stuck:
- Suggest next corrective action
- Propose alternative step

Output:
- status
- recommendation
