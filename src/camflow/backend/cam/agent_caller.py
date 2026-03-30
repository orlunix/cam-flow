"""Agent caller — subprocess interface to Claude CLI.

Used by CAM backend to invoke the coding agent for single-node execution.
"""

import json
import subprocess


def run_agent(prompt, agent_cmd="claude", timeout=120):
    try:
        result = subprocess.run(
            [agent_cmd, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
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
