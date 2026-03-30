import subprocess
import json
import time

from runtime.daemon.persistence import save_state, load_state, append_trace
from runtime.daemon.state_store import init_state, apply_updates
from runtime.daemon.resolver_v2 import resolve_next
from runtime.daemon.validator import validate_result
from runtime.daemon.retry_policy import should_retry, apply_retry
from runtime.daemon.error_classifier import classify_error
from runtime.daemon.supervisor_v3 import supervisor_step
from runtime.daemon.memory_store import init_memory, add_summary

STATE_PATH = "data/state.json"
TRACE_PATH = "data/trace.log"


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
        return json.loads(output[start:end]), True
    except Exception:
        return None, False


def compile_prompt(node_id, node, state, memory):
    text = node.get("with", "")

    for k, v in state.items():
        text = text.replace(f"{{{{state.{k}}}}}", str(v))

    if memory["summaries"]:
        text += "\nContext Summary:\n" + "\n".join(memory["summaries"][-3:])

    return f"Node: {node_id}\n{text}\nReturn JSON"


def run_daemon(workflow):
    state = load_state(STATE_PATH) or init_state()
    memory = init_memory()

    if "pc" not in state:
        state["pc"] = "start"
        state["status"] = "running"

    step = 0

    while state.get("status") == "running":
        step += 1
        node_id = state["pc"]
        node = workflow[node_id]

        prompt = compile_prompt(node_id, node, state, memory)

        raw = run_agent(prompt)
        result, parse_ok = parse_json(raw)

        error = classify_error(raw, parse_ok, result)

        if error and error["retryable"]:
            if should_retry(state, result or {}):
                apply_retry(state)
                continue
            else:
                state["status"] = "failed"

        if not result:
            state["status"] = "failed"

        valid, _ = validate_result(result or {})
        if not valid:
            state["status"] = "failed"

        if state.get("status") == "failed":
            state = supervisor_step(state, error)
            continue

        apply_updates(state, result.get("state_updates", {}))

        transition = resolve_next(node_id, node, result, state)
        next_pc = transition["next_pc"]

        trace_entry = {
            "step": step,
            "pc": node_id,
            "next_pc": next_pc,
            "status": result.get("status"),
            "summary": result.get("summary"),
            "reason": transition.get("reason"),
            "ts": time.time()
        }

        append_trace(TRACE_PATH, trace_entry)
        save_state(STATE_PATH, state)

        add_summary(memory, result.get("summary"))

        state["pc"] = next_pc
        state["status"] = transition["workflow_status"]

        if state["status"] != "running":
            break

        time.sleep(1)

    save_state(STATE_PATH, state)
    return state
