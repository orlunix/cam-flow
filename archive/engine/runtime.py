class RuntimeState:
    def __init__(self, workflow_id):
        self.workflow_id = workflow_id
        self.status = "ready"
        self.pc = None
        self.resume_pc = None
        self.step = 0
        self.memory = {}
        self.last_output = None


def run(runtime, workflow):
    if runtime.status == "ready":
        runtime.status = "running"

    while runtime.status == "running":
        node = workflow["nodes"][runtime.pc]
        runtime.step += 1

        # placeholder execution
        result = {
            "status": "success",
            "summary": f"executed {runtime.pc}",
            "output": {},
            "memory_updates": {},
            "control": {"action": "continue", "target": None},
        }

        # TODO: resolve transition using spec
        runtime.pc = node.get("next", None)

        if runtime.pc is None:
            runtime.status = "done"
