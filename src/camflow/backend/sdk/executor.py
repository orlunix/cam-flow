"""SDK backend — script-driven execution.

Fully programmatic. The caller controls the workflow loop
and invokes nodes through a structured API client.
"""

from camflow.backend.base import Backend


class SDKBackend(Backend):
    def __init__(self, client):
        self.client = client

    def execute_node(self, node_id, node, state, attachments=None):
        from camflow.engine.input_ref import resolve_refs

        rendered_with = resolve_refs(node.get("with", ""), state)

        # placeholder for SDK call
        response = self.client.query(rendered_with)

        # TODO: normalize response properly
        return {
            "status": "success",
            "summary": "sdk execution completed",
            "output": {"raw": response},
            "state_updates": {},
            "control": {"action": "continue", "target": None, "reason": None},
            "error": None
        }
