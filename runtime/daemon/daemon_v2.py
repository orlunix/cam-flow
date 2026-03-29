import subprocess
import json
import time

from runtime.daemon.state_store import init_state, apply_updates
from runtime.daemon.trace_store import init_trace, append_trace
from runtime.daemon.resolver_v2 import resolve_next


def run_agent(prompt):
    try:
        result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=120)
        return result.stdout
    except Exception as e:
        return json.dumps({"status": "fail", "error": str(e)})


def parse_json(output):
    try:
        start = output.index("{")
        end = output.rindex("}") + 1
        return json.loads(output[start:end])
    except Exception:
        return None


def compile_prompt(node_id, node, state):
    text = node.get("with", "")
    for k, v in state.items():
        text = text.replace(f"{{{{state.{k}}}}}", str(v))

    return f"Node: {node_id}\nTask:\n{text}\nReturn JSON"


def run_daemon(workflow):
    state = init_state()
    state["error"] = "reset assertion failed"

    trace = init_trace()
    step = 0

    while state["status"] == "running":
        step += 1
        node_id = state["pc"]
        node = workflow[node_id]

        prompt = compile_prompt(node_id, node, state)

        raw = run_agent(prompt)
        result = parse_json(raw)

        if not result:
            raw = run_agent(prompt)
            result = parse_json(raw)

        if not result:
            break

        apply_updates(state, result.get("state_updates", {}))

        transition = resolve_next(node_id, node, result, state)

        next_pc = transition["next_pc"]

        append_trace(trace, {
            "step": step,
            "pc": node_id,
            "next_pc": next_pc
        })

        state["pc"] = next_pc
        state["status"] = transition["workflow_status"]

        if state["status"] != "running":
            break

        time.sleep(1)

    return trace
