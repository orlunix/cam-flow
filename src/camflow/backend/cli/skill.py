"""Skill prompt template generation for CLI backend.

Generates prompts for the coding agent to execute workflow nodes as skills.
"""

from camflow.engine.input_ref import resolve_refs


def build_skill_prompt(node_id, node, state):
    task = resolve_refs(node.get("with", ""), state)
    executor = node.get("do", "skill")

    return f"""You are executing workflow node '{node_id}'.

Executor: {executor}

Task:
{task}

After completing the task, report your result as:
- status: success / fail / wait
- summary: brief description of what you did
- output: any structured output (dict)
- state_updates: any state changes (dict)
"""
