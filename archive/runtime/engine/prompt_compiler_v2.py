def compile_prompt(node_id, node, state):
    text = node.get("with", "")

    for k, v in state.items():
        text = text.replace(f"{{{{state.{k}}}}}", str(v))

    return f"""
You are executing exactly ONE workflow node.
You are NOT the workflow controller.

Node: {node_id}

Task:
{text}

Return ONLY JSON:
{{
  "status": "success|fail|wait",
  "summary": "...",
  "output": {{}},
  "state_updates": {{}},
  "control": {{"action":"continue","target":null,"reason":null}},
  "error": null
}}
"""
