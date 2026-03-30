class SDKBackend:
    def __init__(self, client):
        self.client = client

    def build_input(self, rendered_with, attachments=None):
        return {
            "prompt": rendered_with,
            "attachments": attachments or []
        }

    def execute_node(self, node_id, node, rendered_with, attachments=None):
        request = self.build_input(rendered_with, attachments)

        # placeholder for SDK call
        response = self.client.query(request["prompt"])

        # TODO: normalize response properly
        return {
            "status": "success",
            "summary": "sdk execution completed",
            "output": {"raw": response},
            "memory_updates": {},
            "control": {"action": "continue", "target": None, "reason": None},
            "error": None
        }
