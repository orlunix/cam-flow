import subprocess
import json
import time


def run_agent(prompt):
    """Call Claude CLI (requires `claude` installed and authenticated)"""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120
        )
        return result.stdout
    except Exception as e:
        return json.dumps({"status": "fail", "error": str(e)})


def parse_json(output):
    try:
        start = output.index("{")
        end = output.rindex("}") + 1
        return json.loads(output[start:end])
    except Exception:
        return None


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


def resolve_next(node, result):
    return node.get("next", "done")


def run_daemon(workflow):
    state = {
        "pc": "start",
        "status": "running",
        "error": "reset assertion failed"
    }

    step = 0

    while state["status"] == "running":
        step += 1
        node_id = state["pc"]
        node = workflow[node_id]

        print(f"\n[STEP {step}] {node_id}")

        prompt = compile_prompt(node_id, node, state)

        raw = run_agent(prompt)
        result = parse_json(raw)

        if not result:
            print("[ERROR] Failed to parse agent output")
            break

        print("[RESULT]", result.get("summary"))

        # apply state updates
        state.update(result.get("state_updates", {}))

        next_pc = resolve_next(node, result)

        print(f"[TRANSITION] {node_id} → {next_pc}")

        state["pc"] = next_pc

        if next_pc == "done":
            state["status"] = "done"

        time.sleep(1)

    print("\nWorkflow finished.")
