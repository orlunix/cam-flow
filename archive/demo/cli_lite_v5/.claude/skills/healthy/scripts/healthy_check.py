import json

with open(".claude/state/workflow.json") as f:
    state = json.load(f)

pc = state.get("pc")
retry = state.get("retry", 0)
history = state.get("history", [])

status = "ok"
reason = "normal"
suggestion = "continue"

if retry > 3:
    status = "stuck"
    reason = "high retry"
    suggestion = "change strategy"

# simple loop detection
if len(history) >= 3:
    if history[-1] == history[-2] == history[-3]:
        status = "loop"
        reason = "same node repeated"
        suggestion = "try different transition"

print(json.dumps({
    "status": status,
    "reason": reason,
    "suggestion": suggestion
}))
