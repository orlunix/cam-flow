import json
import subprocess


class RuleBackend:
    def __init__(self, agent_cmd="claude"):
        self.agent_cmd = agent_cmd

    def build_prompt(self, node_id, node, rendered_with):
        return f"""
You are executing ONE cam-flow node.
You are NOT the workflow controller.

Node: {node_id}
Executor: {node.get('do')}

Task:
{rendered_with}

Return ONLY JSON in this format:
{{
  "status": "success|fail",
  "summary": "...",
  "output": {{}},
  "memory_updates": {{}},
  "control": {{"action": "continue|wait|fail", "target": null, "reason": null}},
  "error": null
}}
"""

    def call_agent(self, prompt):
        # simple CLI call (can be replaced later)
        result = subprocess.run(
            [self.agent_cmd, "-p", prompt],
            capture_output=True,
            text=True,
        )
        return result.stdout

    def extract_json(self, text):
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception:
            return None

    def execute_node(self, node_id, node, rendered_with):
        prompt = self.build_prompt(node_id, node, rendered_with)
        raw = self.call_agent(prompt)

        parsed = self.extract_json(raw)
        if not parsed:
            return {
                "status": "fail",
                "summary": "failed to parse agent output",
                "output": {},
                "memory_updates": {},
                "control": {"action": "fail", "target": None, "reason": "parse_error"},
                "error": {"code": "PARSE_ERROR"}
            }

        return parsed
