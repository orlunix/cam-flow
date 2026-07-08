"""Static v1.2 workflow engine, Python 3.6 and stdlib only."""
from __future__ import print_function

import json
import os
import shlex
import subprocess
import time

from camflow_pkg.contracts import validate_envelope


def _write(path, value):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(value)
    os.rename(temporary, path)


def _ensure_embedded_skills(root):
    for name, content in _EMBEDDED_SKILLS.items():
        target = os.path.join(root, "skills", name, "SKILL.md")
        if not os.path.isfile(target):
            _write(target, content)


def _ensure_embedded_assets(root):
    for relative, content in _EMBEDDED_ASSETS.items():
        pieces = relative.replace("\\", "/").split("/")
        if not pieces or pieces[0] != "builtin" or any(part in ("", ".", "..") for part in pieces):
            continue
        target = os.path.join(root, *pieces)
        if not os.path.isfile(target):
            _write(target, content)


def _trace(run_dir, event, **fields):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    record.update(fields)
    with open(os.path.join(run_dir, "trace.jsonl"), "a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _prompt(spec, node, attempt_input, output_path):
    skill_path = os.path.join(spec["_root"], "skills", node["run"]["skill"], "SKILL.md")
    with open(skill_path, "r") as handle:
        skill = handle.read().strip()
    sections = [skill]
    if spec.get("goal"):
        sections.append("# Workflow Goal\n" + spec["goal"])
    if spec.get("context"):
        sections.append("# Workflow Context\n" + spec["context"])
    if "run_input" in attempt_input:
        sections.append("# Workflow Input\n```json\n" + json.dumps(attempt_input["run_input"], indent=2) + "\n```")
    sections.append("# Goal\n" + node["goal"])
    sections.append("# Steps\n" + "\n".join("%d. %s" % (i + 1, step) for i, step in enumerate(node["steps"])))
    if attempt_input.get("upstream"):
        sections.append("# Upstream Outputs\n```json\n" + json.dumps(attempt_input["upstream"], indent=2) + "\n```")
    if attempt_input.get("previous"):
        sections.append("# Previous Attempt\n```json\n" + json.dumps(attempt_input["previous"], indent=2) + "\n```")
    schema = node.get("output_schema") or {}
    sections.append("# Output\nWrite exactly one JSON envelope to %s. Required data schema: %s" % (output_path, json.dumps(schema, sort_keys=True)))
    return "\n\n".join(sections) + "\n"


def _read_output(output_path):
    if not os.path.isfile(output_path):
        return None
    try:
        with open(output_path, "r") as handle:
            return json.load(handle)
    except (ValueError, IOError) as exc:
        return {"status": "fail", "data": {}, "error": {"code": "BAD_OUTPUT", "message": str(exc)}, "feedback": None, "request_human": False}


def _invoke(root, attempt_dir, node, prompt):
    output_path = os.path.join(attempt_dir, "agent_output.json")
    command = os.environ.get("CAMFLOW_EXECUTOR")
    if command:
        code = subprocess.call(shlex.split(command), cwd=attempt_dir, env=os.environ.copy())
        if code:
            return {"status": "fail", "data": {}, "error": {"code": "EXECUTOR_FAILED", "message": "executor exited %d" % code}, "feedback": None, "request_human": False}
        result = _read_output(output_path)
        if result is not None:
            return result
    else:
        camc = os.environ.get("CAMC_BIN", "camc")
        name = "camflow-%s-%d" % (node["id"], int(time.time() * 1000))
        try:
            code = subprocess.call([camc, "run", "--auto-exit", "--name", name, "--path", root, prompt], cwd=root)
        except OSError as exc:
            return {"status": "fail", "data": {}, "error": {"code": "EXECUTOR_UNAVAILABLE", "message": str(exc)}, "feedback": None, "request_human": False}
        if code:
            return {"status": "fail", "data": {}, "error": {"code": "EXECUTOR_FAILED", "message": "camc exited %d" % code}, "feedback": None, "request_human": False}
        try:
            timeout = max(1, int(os.environ.get("CAMFLOW_AGENT_TIMEOUT", "600")))
        except ValueError:
            timeout = 600
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = _read_output(output_path)
            if result is not None:
                return result
            time.sleep(1)
    return {"status": "fail", "data": {}, "error": {"code": "MISSING_OUTPUT", "message": "agent_output.json was not written"}, "feedback": None, "request_human": False}


def _agent_verify(root, node, envelope, attempt_dir, criterion):
    skill_path = os.path.join(root, "skills", "evaluator", "SKILL.md")
    if not os.path.isfile(skill_path):
        return False, "default evaluator skill is missing: skills/evaluator/SKILL.md"
    verify_dir = os.path.join(attempt_dir, "verify")
    if not os.path.isdir(verify_dir):
        os.makedirs(verify_dir)
    _write(os.path.join(verify_dir, "envelope.json"), json.dumps(envelope, indent=2, sort_keys=True))
    with open(skill_path, "r") as handle:
        skill = handle.read().strip()
    output_path = os.path.abspath(os.path.join(verify_dir, "agent_output.json"))
    prompt = skill + "\n\n# Envelope produced by run\n```json\n" + json.dumps(envelope, indent=2) + "\n```\n\n# Criterion\n" + criterion + "\n\n# Output\nWrite a success envelope to " + output_path + " with data.approved as a boolean and data.reasoning as a string."
    verifier = _invoke(root, verify_dir, {"id": node["id"] + "-verify"}, prompt)
    if verifier.get("status") != "success":
        return False, "agent verifier failed: " + str(verifier.get("error"))
    data = verifier.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
        return False, "agent verifier must return data.approved boolean"
    if not data["approved"]:
        return False, str(data.get("reasoning") or verifier.get("feedback") or "agent verifier rejected output")
    return True, "agent verifier approved"


def _verify(root, node, envelope, attempt_dir):
    problem = validate_envelope(envelope, node.get("output_schema") or {})
    if problem:
        return False, problem
    verify = node.get("verify") or {}
    command = verify.get("command")
    if command:
        try:
            code = subprocess.call(command, shell=True, cwd=attempt_dir, timeout=verify.get("timeout"))
        except subprocess.TimeoutExpired:
            return False, "verify command timed out"
        except OSError as exc:
            return False, str(exc)
        if code:
            return False, "verify command exited %d" % code
        return True, "verify command passed"
    if verify.get("human"):
        return False, "human verification requested: " + verify["human"]
    if os.environ.get("CAMFLOW_EXECUTOR"):
        return True, "mock executor contract check"
    return _agent_verify(root, node, envelope, attempt_dir, verify.get("criterion") or "Check every workflow step is satisfied with concrete evidence.")


def recover(spec, run_dir):
    """Rebuild successful-node state and complete attempt history from disk."""
    state = {}
    histories = {}
    for node in spec["nodes"]:
        node_id = node["id"]
        base = os.path.join(run_dir, "nodes", node_id)
        history = []
        if os.path.isdir(base):
            names = sorted(os.listdir(base), key=lambda value: int(value.split("-", 1)[1]) if value.startswith("attempt-") and value.split("-", 1)[1].isdigit() else -1)
            for name in names:
                path = os.path.join(base, name, "output.json")
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, "r") as handle:
                        history.append(json.load(handle))
                except (IOError, ValueError):
                    continue
        histories[node_id] = history
        if history and history[-1].get("status") == "success":
            state[node_id] = history[-1]
    return state, histories


def execute(spec, root, run_dir, run_input, max_steps=None, state=None, histories=None, resume_node=None, previous=None):
    spec = dict(spec)
    spec["_root"] = root
    if not os.path.isdir(run_dir):
        os.makedirs(run_dir)
    _write(os.path.join(run_dir, "workflow.json"), json.dumps(spec, indent=2, sort_keys=True))
    if run_input is not None:
        _write(os.path.join(run_dir, "input.json"), json.dumps(run_input, indent=2, sort_keys=True))
    state = dict(state or {})
    histories = dict(histories or {})
    previous = previous or {}
    _trace(run_dir, "workflow_started", workflow=spec["workflow"])
    nodes = spec["nodes"]
    attempts = 0
    while True:
        ready = []
        for node in nodes:
            node_id = node["id"]
            if node_id in state:
                continue
            if all(state.get(dep, {}).get("status") == "success" for dep in node.get("needs", [])):
                ready.append(node)
        if not ready:
            if len(state) == len(nodes):
                _trace(run_dir, "workflow_completed", status="success")
                return "done"
            _write(os.path.join(run_dir, "halt.json"), json.dumps({"reason": "deadlock", "nodes": sorted(state)}, indent=2))
            _trace(run_dir, "workflow_halted", reason="deadlock")
            return "halted"
        node = ready[0]
        node_id = node["id"]
        history = list(histories.get(node_id, []))
        allowed = node.get("retry", 1) + 1 - len(history)
        if node_id == resume_node:
            allowed = max(1, allowed)
        if allowed < 1:
            _write(os.path.join(run_dir, "halt.json"), json.dumps({"halted_node": node_id, "reason": "retry_exhausted", "envelope": history[-1] if history else {}}, indent=2))
            return "halted"
        for unused in range(allowed):
            attempt = len(history) + 1
            attempts += 1
            attempt_dir = os.path.join(run_dir, "nodes", node_id, "attempt-%d" % attempt)
            if not os.path.isdir(attempt_dir):
                os.makedirs(attempt_dir)
            upstream = dict((dep, state[dep]) for dep in node.get("needs", []))
            attempt_input = {}
            if run_input is not None:
                attempt_input["run_input"] = run_input
            if upstream:
                attempt_input["upstream"] = upstream
            if history:
                attempt_input["previous"] = history[-1]
            elif node_id in previous:
                attempt_input["previous"] = previous[node_id]
            _write(os.path.join(attempt_dir, "input.json"), json.dumps(attempt_input, indent=2, sort_keys=True))
            output_path = os.path.abspath(os.path.join(attempt_dir, "agent_output.json"))
            prompt = _prompt(spec, node, attempt_input, output_path)
            _write(os.path.join(attempt_dir, "prompt.txt"), prompt)
            _trace(run_dir, "node_started", node=node_id, attempt=attempt)
            envelope = _invoke(root, attempt_dir, node, prompt)
            good, reason = _verify(root, node, envelope, attempt_dir)
            _write(os.path.join(attempt_dir, "verify.json"), json.dumps({"passed": good, "reason": reason}, indent=2, sort_keys=True))
            if not good:
                envelope["status"] = "fail"
                envelope["error"] = {"code": "VERIFY_FAIL", "message": reason}
                envelope["feedback"] = reason
            _write(os.path.join(attempt_dir, "output.json"), json.dumps(envelope, indent=2, sort_keys=True))
            history.append(envelope)
            histories[node_id] = history
            if envelope.get("request_human"):
                _write(os.path.join(run_dir, "halt.json"), json.dumps({"halted_node": node_id, "reason": "request_human", "envelope": envelope}, indent=2))
                _trace(run_dir, "workflow_halted", node=node_id, reason="request_human")
                return "halted"
            if envelope.get("status") == "success":
                state[node_id] = envelope
                _trace(run_dir, "node_completed", node=node_id, attempt=attempt)
                break
            _trace(run_dir, "node_failed", node=node_id, attempt=attempt)
        else:
            _write(os.path.join(run_dir, "halt.json"), json.dumps({"halted_node": node_id, "reason": "retry_exhausted", "envelope": history[-1]}, indent=2))
            _trace(run_dir, "workflow_halted", node=node_id, reason="retry_exhausted")
            return "halted"
        if max_steps is not None and attempts >= max_steps:
            _write(os.path.join(run_dir, "halt.json"), json.dumps({"halted_node": node_id, "reason": "step_limit"}, indent=2))
            _trace(run_dir, "workflow_halted", node=node_id, reason="step_limit")
            return "halted"
