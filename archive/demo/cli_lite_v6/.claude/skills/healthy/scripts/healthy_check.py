import json

state = json.load(open(".claude/state/workflow.json"))
pc = state.get("pc")
history = state.get("history", [])
retry = state.get("retry", 0)

status = "ok"
reason = "normal"
suggestion = "continue"

if retry > 3:
    status = "stuck"
    reason = "retry high"
    suggestion = "change approach"

if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
    status = "loop"
    reason = "repeated node"
    suggestion = "change transition"

print(json.dumps({
    "status": status,
    "reason": reason,
    "suggestion": suggestion
}))
