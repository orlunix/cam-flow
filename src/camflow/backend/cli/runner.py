"""CLI backend — pure Claude Coding Agent execution.

The Coding Agent drives the workflow loop directly inside its session.
No external daemon needed.
"""

from camflow.engine.dsl import load_workflow
from camflow.engine.transition import resolve_next
from camflow.engine.input_ref import resolve_refs
from camflow.engine.state import init_state, apply_updates
from camflow.backend.persistence import save_state, load_state, append_trace

STATE_PATH = ".claude/state/workflow.json"
TRACE_PATH = ".claude/state/trace.log"


def load_or_init_state():
    return load_state(STATE_PATH) or init_state()


def step(workflow, state, result):
    """Advance the workflow by one step given a node result."""
    node_id = state["pc"]
    node = workflow[node_id]

    apply_updates(state, result.get("state_updates", {}))

    transition = resolve_next(node_id, node, result, state)

    state["pc"] = transition["next_pc"]
    state["status"] = transition["workflow_status"]

    append_trace(TRACE_PATH, {
        "pc": node_id,
        "next_pc": transition["next_pc"],
        "status": result.get("status"),
        "reason": transition.get("reason"),
    })

    save_state(STATE_PATH, state)
    return state


def get_current_task(workflow, state):
    """Get the current node's resolved task prompt."""
    node_id = state["pc"]
    node = workflow[node_id]
    return {
        "node_id": node_id,
        "do": node.get("do", ""),
        "task": resolve_refs(node.get("with", ""), state),
    }
