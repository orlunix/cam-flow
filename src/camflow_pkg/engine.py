"""Static v1.2 workflow engine, Python 3.6 and stdlib only."""
from __future__ import print_function

import json
import hashlib
import os
import re
import shlex
import subprocess
import time

from camflow_pkg import _EMBEDDED_ASSETS, _EMBEDDED_SKILLS
from camflow_pkg.contracts import validate_envelope


def _compact_slug(value, limit, fallback):
    """Return a short CAMC-safe label, retaining a hash when truncated."""
    text = str(value or "")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not slug:
        return fallback
    if len(slug) <= limit:
        return slug
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:4]
    head = slug[:max(1, limit - len(digest) - 1)].rstrip("-")
    return head + "-" + digest


def _flow_identity(spec, run_dir, supplied=None):
    """Build or restore the short identity shared by every agent in a run."""
    stored = supplied if isinstance(supplied, dict) else None
    if stored is None:
        try:
            with open(os.path.join(run_dir, "run.json"), "r") as handle:
                candidate = json.load(handle).get("flow")
            if isinstance(candidate, dict):
                stored = candidate
        except (IOError, OSError, ValueError):
            stored = None
    fallback_id = hashlib.sha256(
        os.path.abspath(run_dir).encode("utf-8")
    ).hexdigest()[:8]
    flow_id = str((stored or {}).get("id") or "").lower()
    if not re.match(r"^[0-9a-f]{8}$", flow_id):
        flow_id = fallback_id
    name = str((stored or {}).get("name") or spec.get("workflow") or "flow")
    label = _compact_slug((stored or {}).get("label") or name, 12, flow_id)
    tags = ["cf-" + label]
    id_tag = "cf-" + flow_id
    if id_tag not in tags:
        tags.append(id_tag)
    return {"id": flow_id, "name": name, "label": label, "tags": tags}


def _camc_agent_name(flow, node_id, attempt_dir):
    node_label = _compact_slug(node_id, 18, "node")
    token = hashlib.sha256(
        os.path.abspath(attempt_dir).encode("utf-8")
    ).hexdigest()[:8]
    return "cf-%s-%s-%s" % (flow["label"], node_label, token)


def _write(path, value):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(value)
    os.replace(temporary, path)


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


def _failure(code, message, request_human=False):
    return {
        "status": "fail",
        "data": {},
        "error": {"code": code, "message": message},
        "feedback": None,
        "request_human": request_human,
    }


def _camc_call(command, cwd, timeout):
    """Run one bounded camc command and retain enough evidence for audit."""
    try:
        process = subprocess.run(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        return {
            "returncode": process.returncode,
            "stdout": process.stdout or "",
            "stderr": process.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        error = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {"returncode": None, "stdout": output, "stderr": error, "error": "timed out"}
    except OSError as exc:
        return {"returncode": None, "stdout": "", "stderr": "", "error": str(exc)}


def _camc_agent_id(result):
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    match = re.search(r"\bStarting\s+\S+\s+agent\s+([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def _camc_finalize(camc, agent_id, attempt_dir):
    """Durably archive a camc agent, then stop and remove its live record."""
    archive_dir = os.path.join(attempt_dir, "camc-archive")
    archive = _camc_call(
        [camc, "archive", agent_id, "--output", archive_dir],
        attempt_dir, 45,
    )
    lifecycle = {"agent_id": agent_id, "archive": archive}
    try:
        archive_files = sorted(
            name for name in os.listdir(archive_dir) if name.endswith(".tar.gz")
        )
    except OSError:
        archive_files = []
    lifecycle["archive_files"] = archive_files
    if archive.get("returncode") != 0 or not archive_files:
        status = _camc_call([camc, "--json", "status", agent_id], attempt_dir, 10)
        lifecycle["status"] = status
        _write(os.path.join(attempt_dir, "camc-lifecycle.json"), json.dumps(lifecycle, indent=2, sort_keys=True))
        return "CAMC_ARCHIVE_FAILED", "camc archive failed; agent was kept for inspection: %s" % (
            archive.get("stderr") or archive.get("error") or "archive file was not created"
        )

    status = _camc_call([camc, "--json", "status", agent_id], attempt_dir, 10)
    lifecycle["status"] = status
    if status.get("returncode") == 0:
        try:
            agent = json.loads(status.get("stdout") or "")
            if isinstance(agent, dict):
                _write(os.path.join(attempt_dir, "agent.json"), json.dumps(agent, indent=2, sort_keys=True))
        except ValueError:
            pass

    stop = _camc_call([camc, "stop", agent_id], attempt_dir, 15)
    remove = _camc_call([camc, "rm", agent_id], attempt_dir, 15)
    lifecycle["stop"] = stop
    lifecycle["rm"] = remove
    _write(os.path.join(attempt_dir, "camc-lifecycle.json"), json.dumps(lifecycle, indent=2, sort_keys=True))
    if remove.get("returncode") != 0:
        return "CAMC_CLEANUP_FAILED", "camc rm failed after archive: %s" % (
            remove.get("stderr") or remove.get("error") or "unknown error"
        )
    return None


def _invoke(root, attempt_dir, node, prompt, flow=None):
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
        flow = _flow_identity({"workflow": "flow"}, attempt_dir, flow)
        name = _camc_agent_name(flow, node["id"], attempt_dir)
        command = [camc, "run"]
        tool = os.environ.get("CAMFLOW_AGENT_TOOL")
        if tool:
            command.extend(["--tool", tool])
        command.extend(["--name", name])
        for tag in flow["tags"]:
            command.extend(["--tag", tag])
        command.extend(["--path", root, prompt])
        try:
            launch_timeout = max(1, int(os.environ.get("CAMFLOW_CAMC_RUN_TIMEOUT", "180")))
        except ValueError:
            launch_timeout = 180
        launch = _camc_call(command, root, launch_timeout)
        agent_id = _camc_agent_id(launch)
        if launch.get("returncode") != 0:
            return _failure(
                "EXECUTOR_FAILED",
                launch.get("stderr") or launch.get("error") or "camc run failed",
            )
        if not agent_id:
            return _failure("EXECUTOR_FAILED", "camc run did not report an agent id")
        _write(os.path.join(attempt_dir, "agent.id"), agent_id + "\n")
        try:
            timeout = max(1, int(os.environ.get("CAMFLOW_AGENT_TIMEOUT", "600")))
        except ValueError:
            timeout = 600
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = _read_output(output_path)
            if result is not None:
                cleanup_problem = _camc_finalize(camc, agent_id, attempt_dir)
                if cleanup_problem:
                    return _failure(cleanup_problem[0], cleanup_problem[1], request_human=True)
                return result
            time.sleep(1)
        cleanup_problem = _camc_finalize(camc, agent_id, attempt_dir)
        if cleanup_problem:
            return _failure(cleanup_problem[0], cleanup_problem[1], request_human=True)
    return {"status": "fail", "data": {}, "error": {"code": "MISSING_OUTPUT", "message": "agent_output.json was not written"}, "feedback": None, "request_human": False}


def _agent_verify(root, node, envelope, attempt_dir, criterion, flow=None):
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
    verifier = _invoke(
        root, verify_dir, {"id": node["id"] + "-verify"}, prompt,
        flow=flow,
    )
    if verifier.get("status") != "success":
        return False, "agent verifier failed: " + str(verifier.get("error"))
    data = verifier.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
        return False, "agent verifier must return data.approved boolean"
    if not data["approved"]:
        return False, str(data.get("reasoning") or verifier.get("feedback") or "agent verifier rejected output")
    return True, "agent verifier approved"


def _verify(root, node, envelope, attempt_dir, flow=None):
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
    return _agent_verify(
        root, node, envelope, attempt_dir,
        verify.get("criterion") or "Check every workflow step is satisfied with concrete evidence.",
        flow=flow,
    )


def _dependency_done(result):
    return result.get("status") in ("success", "skipped")


def _condition_value(state, condition):
    value = state.get(condition["node"], {})
    for part in condition["path"].split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _condition_matches(state, condition):
    return _condition_value(state, condition) == condition["equals"]


def _route_problem(nodes, state):
    groups = {}
    for node in nodes:
        condition = node.get("when")
        if not condition:
            continue
        key = (condition["node"], condition["path"])
        groups.setdefault(key, []).append((node["id"], condition["equals"]))
    for key, branches in groups.items():
        source_id, path = key
        source = state.get(source_id, {})
        if source.get("status") != "success":
            continue
        actual = _condition_value(state, {"node": source_id, "path": path})
        matches = [node_id for node_id, expected in branches if expected == actual]
        if len(matches) != 1:
            return {
                "node": source_id,
                "path": path,
                "actual": actual,
                "expected": sorted(set(expected for _node_id, expected in branches)),
            }
    return None


def recover(spec, run_dir):
    """Rebuild completed-node state and attempt history from disk."""
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
        else:
            skip_path = os.path.join(base, "skip.json")
            if os.path.isfile(skip_path):
                try:
                    with open(skip_path, "r") as handle:
                        skipped = json.load(handle)
                    if skipped.get("status") == "skipped":
                        state[node_id] = skipped
                except (IOError, ValueError):
                    pass
    return state, histories


def execute(spec, root, run_dir, run_input, max_steps=None, state=None, histories=None, resume_node=None, previous=None, flow=None):
    spec = dict(spec)
    spec["_root"] = root
    if not os.path.isdir(run_dir):
        os.makedirs(run_dir)
    _write(os.path.join(run_dir, "workflow.json"), json.dumps(spec, indent=2, sort_keys=True))
    input_snapshot = os.path.join(run_dir, "input.json")
    if run_input is not None and not os.path.isfile(input_snapshot):
        _write(input_snapshot, json.dumps(run_input, indent=2, sort_keys=True))
    state = dict(state or {})
    flow = _flow_identity(spec, run_dir, flow)
    histories = dict(histories or {})
    previous = previous or {}
    _trace(run_dir, "workflow_started", workflow=spec["workflow"])
    nodes = spec["nodes"]
    attempts = 0
    while True:
        route_problem = _route_problem(nodes, state)
        if route_problem:
            halt = {"reason": "unmatched_route", "route": route_problem}
            _write(os.path.join(run_dir, "halt.json"), json.dumps(halt, indent=2, sort_keys=True))
            _trace(run_dir, "workflow_halted", reason="unmatched_route", route=route_problem)
            return "halted"
        ready = []
        for node in nodes:
            node_id = node["id"]
            if node_id in state:
                continue
            if all(_dependency_done(state.get(dep, {})) for dep in node.get("needs", [])):
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
        condition = node.get("when")
        if condition and not _condition_matches(state, condition):
            skipped = {
                "status": "skipped",
                "data": {},
                "skip": {
                    "node": condition["node"],
                    "path": condition["path"],
                    "actual": _condition_value(state, condition),
                    "expected": condition["equals"],
                },
            }
            state[node_id] = skipped
            _write(os.path.join(run_dir, "nodes", node_id, "skip.json"), json.dumps(skipped, indent=2, sort_keys=True))
            _trace(run_dir, "node_skipped", node=node_id, route=skipped["skip"])
            continue
        if condition:
            _trace(
                run_dir,
                "route_selected",
                node=condition["node"],
                path=condition["path"],
                value=_condition_value(state, condition),
                target=node_id,
            )
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
            upstream = dict((dep, state[dep]) for dep in node.get("needs", []) if state[dep].get("status") == "success")
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
            envelope = _invoke(root, attempt_dir, node, prompt, flow=flow)
            good, reason = _verify(root, node, envelope, attempt_dir, flow=flow)
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
