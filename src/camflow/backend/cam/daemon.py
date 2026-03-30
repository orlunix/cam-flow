"""CAM backend — Coding Agent Manager daemon.

An external daemon process owns the workflow loop and controls
the Claude Code agent via subprocess calls.

Implements: spec/transition.md, spec/retry.md, spec/recovery.md
"""

import time

from camflow.backend.persistence import save_state, load_state, append_trace
from camflow.engine.state import init_state, apply_updates
from camflow.engine.transition import resolve_next
from camflow.engine.node_contract import validate_result
from camflow.engine.memory import init_memory, add_summary
from camflow.engine.retry import should_retry, apply_retry
from camflow.engine.error_classifier import classify_error
from camflow.backend.cam.supervisor import supervisor_step
from camflow.backend.cam.agent_caller import run_agent, parse_json
from camflow.backend.cam.prompt_compiler import compile_prompt

STATE_PATH = "data/state.json"
TRACE_PATH = "data/trace.log"


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

        append_trace(TRACE_PATH, {
            "step": step,
            "pc": node_id,
            "next_pc": next_pc,
            "status": result.get("status"),
            "summary": result.get("summary"),
            "reason": transition.get("reason"),
            "ts": time.time()
        })

        save_state(STATE_PATH, state)
        add_summary(memory, result.get("summary"))

        state["pc"] = next_pc
        state["status"] = transition["workflow_status"]

        if state["status"] != "running":
            break

        time.sleep(1)

    save_state(STATE_PATH, state)
    return state
