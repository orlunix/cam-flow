"""Prompt compiler for CAM backend.

Builds a bounded single-node execution prompt for the agent.
Loads the prompt template from node-prompt.md.
"""

from camflow.engine.input_ref import resolve_refs


def compile_prompt(node_id, node, state, memory=None):
    text = resolve_refs(node.get("with", ""), state)

    if memory and memory.get("summaries"):
        text += "\nContext Summary:\n" + "\n".join(memory["summaries"][-3:])

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
