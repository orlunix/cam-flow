"""Python 3.6 standalone CLI for CamFlow v1.2."""
from __future__ import print_function

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys

from camflow_pkg import __build__, __version__
from camflow_pkg.contracts import validate_input, validate_workflow
from camflow_pkg.engine import _ensure_embedded_assets, _ensure_embedded_skills, _trace, execute, recover
from camflow_pkg.yaml_lite import YamlError, dumps, loads


def _require_python36():
    if sys.version_info < (3, 6):
        sys.stderr.write("ERROR: camflow requires Python 3.6 or newer\n")
        raise SystemExit(1)


def _read_json(path):
    try:
        with open(path, "r") as handle:
            value = json.load(handle)
    except (IOError, ValueError) as exc:
        raise ValueError("cannot read input.json: %s" % exc)
    if not isinstance(value, dict):
        raise ValueError("input.json: top level must be an object")
    return value


def _load_workflow(path):
    root = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "r") as handle:
            spec = loads(handle.read())
    except (IOError, YamlError) as exc:
        raise ValueError("cannot read workflow.yaml: %s" % exc)
    errors = validate_workflow(spec, root)
    if errors:
        raise ValueError("workflow validation failed:\n  " + "\n  ".join(errors))
    return spec, root


def _case_slug(value, fallback):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or fallback).strip("._-")
    return slug or "case"


def _copy_tree(source, target):
    if not os.path.isdir(source):
        return
    if os.path.isdir(target):
        for parent, _dirs, names in os.walk(source):
            relative = os.path.relpath(parent, source)
            destination = target if relative == "." else os.path.join(target, relative)
            if not os.path.isdir(destination):
                os.makedirs(destination)
            for name in names:
                shutil.copy2(os.path.join(parent, name), os.path.join(destination, name))
    else:
        shutil.copytree(source, target)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(65536)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _snapshot_run(spec, source_root, workflow_path, run_dir, input_path):
    """Copy supplied runnable artifacts so resume never needs the source package."""
    if not os.path.isdir(run_dir):
        os.makedirs(run_dir)
    shutil.copy2(workflow_path, os.path.join(run_dir, "workflow.yaml"))
    if input_path:
        shutil.copy2(input_path, os.path.join(run_dir, "input.json"))
    names = []
    for node in spec["nodes"]:
        name = node["run"]["skill"]
        if name not in names:
            names.append(name)
        verify = node.get("verify") or {}
        if not os.environ.get("CAMFLOW_EXECUTOR") and (not verify or "criterion" in verify) and "evaluator" not in names:
            names.append("evaluator")
    for name in names:
        source = os.path.join(source_root, "skills", name)
        if not os.path.isdir(source):
            raise ValueError("missing skill directory: skills/%s" % name)
        _copy_tree(source, os.path.join(run_dir, "skills", name))
    _copy_tree(os.path.join(source_root, "validators"), os.path.join(run_dir, "validators"))
    workflow_snapshot = os.path.join(run_dir, "workflow.yaml")
    input_snapshot = os.path.join(run_dir, "input.json")
    manifest = {
        "schema": "camflow-run/1",
        "workflow_sha256": _sha256(workflow_snapshot),
        "input_sha256": _sha256(input_snapshot) if os.path.isfile(input_snapshot) else None,
    }
    with open(os.path.join(run_dir, "run.json"), "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _validate_input(data, spec):
    errors = validate_input(data, spec.get("input_schema") or {})
    if errors:
        raise ValueError("input validation failed:\n  " + "\n  ".join(errors))


def _fresh_run(workflow_path, input_path, run_dir, steps):
    try:
        if os.path.exists(run_dir) and (not os.path.isdir(run_dir) or os.listdir(run_dir)):
            raise ValueError("run directory is not empty; use resume or run --from: %s" % run_dir)
        spec, root = _load_workflow(workflow_path)
        schema = spec.get("input_schema") or {}
        if schema and not input_path:
            raise ValueError("workflow declares input_schema; --input is required")
        data = _read_json(input_path) if input_path else None
        if data is not None:
            _validate_input(data, spec)
        _snapshot_run(spec, root, workflow_path, run_dir, input_path)
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    result = execute(spec, run_dir, run_dir, data, steps)
    print("result: %s" % result)
    return 0 if result == "done" else 2


def _load_run(run_dir):
    workflow = os.path.join(run_dir, "workflow.yaml")
    if not os.path.isfile(workflow):
        raise ValueError("run directory missing workflow.yaml")
    spec, root = _load_workflow(workflow)
    input_path = os.path.join(run_dir, "input.json")
    schema = spec.get("input_schema") or {}
    if schema and not os.path.isfile(input_path):
        raise ValueError("run directory missing input.json")
    data = _read_json(input_path) if os.path.isfile(input_path) else None
    if data is not None:
        _validate_input(data, spec)
    manifest_path = os.path.join(run_dir, "run.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r") as handle:
                manifest = json.load(handle)
        except (IOError, ValueError) as exc:
            raise ValueError("invalid run.json: %s" % exc)
        if manifest.get("schema") != "camflow-run/1":
            raise ValueError("unsupported run.json schema")
        if manifest.get("workflow_sha256") != _sha256(workflow):
            raise ValueError("workflow.yaml differs from the recorded run snapshot")
        expected_input = manifest.get("input_sha256")
        actual_input = _sha256(input_path) if os.path.isfile(input_path) else None
        if expected_input != actual_input:
            raise ValueError("input.json differs from the recorded run snapshot")
    return spec, root, data


def _feedback_envelope(feedback):
    return {"status": "fail", "data": {}, "error": {"code": "OPERATOR_FEEDBACK", "message": feedback}, "feedback": feedback, "request_human": False}


def cmd_run(args):
    if args.from_node:
        if args.workflow or args.input:
            sys.stderr.write("ERROR: --from reuses workflow.yaml and input.json from --run-dir\n")
            return 1
        if not args.run_dir:
            sys.stderr.write("ERROR: --from requires --run-dir\n")
            return 1
        return _run_from(args.run_dir, args.from_node, args.feedback, args.steps)
    if not args.workflow:
        sys.stderr.write("ERROR: workflow.yaml is required\n")
        return 1
    run_dir = os.path.abspath(args.run_dir or args.out or os.path.join(os.path.dirname(os.path.abspath(args.workflow)), ".camflow", "run"))
    return _fresh_run(args.workflow, args.input, run_dir, args.steps)


def cmd_resume(args):
    run_dir = os.path.abspath(args.run_dir)
    halt = os.path.join(run_dir, "halt.json")
    if not os.path.isfile(halt):
        sys.stderr.write("ERROR: not halted (no halt.json)\n")
        return 1
    try:
        with open(halt, "r") as handle:
            halt_info = json.load(handle)
        spec, root, data = _load_run(run_dir)
        state, histories = recover(spec, run_dir)
        node_id = halt_info.get("halted_node")
        if not node_id or node_id not in dict((node["id"], node) for node in spec["nodes"]):
            raise ValueError("halt.json has no resumable halted_node")
        if node_id in state and halt_info.get("reason") == "step_limit":
            node_id = None
        if args.feedback and node_id:
            history = histories.get(node_id, [])
            if history:
                history[-1] = dict(history[-1])
                history[-1]["feedback"] = args.feedback
            else:
                histories[node_id] = [_feedback_envelope(args.feedback)]
        os.unlink(halt)
    except (IOError, ValueError) as exc:
        sys.stderr.write("ERROR: cannot resume: %s\n" % exc)
        return 1
    _trace(run_dir, "workflow_resumed", node=node_id or "next")
    result = execute(spec, root, run_dir, data, args.steps, state, histories, node_id)
    print("result: %s" % result)
    return 0 if result == "done" else 2


def _downstream(spec, node_id):
    selected = set([node_id])
    changed = True
    while changed:
        changed = False
        for node in spec["nodes"]:
            if node["id"] not in selected and any(dep in selected for dep in node.get("needs", [])):
                selected.add(node["id"])
                changed = True
    return selected


def _run_from(run_dir, node_id, feedback, steps):
    run_dir = os.path.abspath(run_dir)
    try:
        spec, root, data = _load_run(run_dir)
        nodes = set(node["id"] for node in spec["nodes"])
        if node_id not in nodes:
            raise ValueError("unknown node %r" % node_id)
        for reset in _downstream(spec, node_id):
            path = os.path.join(run_dir, "nodes", reset)
            if os.path.isdir(path):
                shutil.rmtree(path)
        halt = os.path.join(run_dir, "halt.json")
        if os.path.isfile(halt):
            os.unlink(halt)
        state, histories = recover(spec, run_dir)
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    previous = dict((node_id, _feedback_envelope(feedback))) if feedback else None
    _trace(run_dir, "workflow_rerun_from", node=node_id)
    result = execute(spec, root, run_dir, data, steps, state, histories, None, previous)
    print("result: %s" % result)
    return 0 if result == "done" else 2


def cmd_batch(args):
    try:
        spec, root = _load_workflow(args.workflow)
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    paths = sorted(glob.glob(args.inputs))
    if not paths:
        sys.stderr.write("ERROR: --inputs matched no files\n")
        return 1
    output = os.path.abspath(args.out)
    runs = os.path.join(output, "runs")
    if not os.path.isdir(runs):
        os.makedirs(runs)
    summary = []
    result_code = 0
    for path in paths:
        try:
            data = _read_json(path)
            _validate_input(data, spec)
        except ValueError as exc:
            summary.append({"case_id": os.path.splitext(os.path.basename(path))[0], "status": "error", "error": str(exc)})
            result_code = 1
            if not args.continue_on_fail:
                break
            continue
        base = _case_slug(str(data.get("case_id", "")), os.path.splitext(os.path.basename(path))[0])
        name = base
        index = 2
        while os.path.exists(os.path.join(runs, name)):
            name = "%s-%d" % (base, index)
            index += 1
        status = _fresh_run(args.workflow, path, os.path.join(runs, name), None)
        summary.append({"case_id": data.get("case_id", base), "run_name": name, "status": "done" if status == 0 else "halted", "run_dir": os.path.join(runs, name)})
        if status:
            result_code = status
            if not args.continue_on_fail:
                break
    with open(os.path.join(output, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return result_code


def cmd_plan(args):
    values = dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", args.prompt))
    required = ["case_id", "sim_log", "trace_log"]
    missing = [key for key in required if not values.get(key)]
    if missing:
        sys.stderr.write("ERROR: cannot generate real input.json.\nMissing required fields:\n" + "\n".join("  - " + key for key in missing) + "\n")
        return 1
    output = os.path.abspath(args.out)
    if os.path.exists(output) and os.listdir(output):
        sys.stderr.write("ERROR: plan output directory is not empty: %s\n" % output)
        return 1
    if not os.path.isdir(output):
        os.makedirs(output)
    _ensure_embedded_skills(output)
    _ensure_embedded_assets(output)
    schema = dict((key, "string") for key in values)
    spec = {"workflow": "generated_debug_plan", "version": "1.2", "goal": args.prompt, "input_schema": schema, "nodes": [{"id": "investigate", "goal": "Inspect supplied evidence and emit evidence-backed findings.", "steps": ["Read Workflow Input.", "Inspect supplied logs.", "Write evidence-backed findings."], "run": {"skill": "analyzer"}, "output_schema": {"evidence": "array"}, "retry": 1}]}
    with open(os.path.join(output, "workflow.yaml"), "w") as handle: handle.write(dumps(spec) + "\n")
    with open(os.path.join(output, "input.json"), "w") as handle: json.dump(values, handle, indent=2, sort_keys=True); handle.write("\n")
    with open(os.path.join(output, "input.template.json"), "w") as handle: json.dump(schema, handle, indent=2, sort_keys=True); handle.write("\n")
    with open(os.path.join(output, "plan_manifest.json"), "w") as handle: json.dump({"generated_by": "camflow plan", "runnable": True, "workflow": "workflow.yaml"}, handle, indent=2); handle.write("\n")
    return 0


def cmd_pack(args):
    source = os.path.abspath(args.source_dir)
    target = os.path.abspath(args.out)
    workflow = os.path.join(source, "workflow.yaml")
    template = os.path.join(source, "input.template.json")
    if not os.path.isfile(workflow): sys.stderr.write("ERROR: missing workflow.yaml\n"); return 1
    if os.path.exists(target) and os.listdir(target): sys.stderr.write("ERROR: output directory is not empty: %s\n" % target); return 1
    try: spec, _root = _load_workflow(workflow)
    except ValueError as exc: sys.stderr.write("ERROR: %s\n" % exc); return 1
    if not os.path.isdir(target): os.makedirs(target)
    shutil.copy2(workflow, os.path.join(target, "workflow.yaml"))
    if os.path.isfile(template):
        shutil.copy2(template, os.path.join(target, "input.template.json"))
    else:
        with open(os.path.join(target, "input.template.json"), "w") as handle:
            json.dump(spec.get("input_schema") or {}, handle, indent=2, sort_keys=True)
            handle.write("\n")
    for node in spec["nodes"]:
        name = node["run"]["skill"]
        src = os.path.join(source, "skills", name, "SKILL.md")
        if not os.path.isfile(src): sys.stderr.write("ERROR: missing skill file: skills/%s/SKILL.md\n" % name); return 1
        dst = os.path.join(target, "skills", name)
        if not os.path.isdir(dst): os.makedirs(dst)
        shutil.copy2(src, os.path.join(dst, "SKILL.md"))
    _copy_tree(os.path.join(source, "builtin"), os.path.join(target, "builtin"))
    _copy_tree(os.path.join(source, "validators"), os.path.join(target, "validators"))
    with open(os.path.join(target, "package_manifest.json"), "w") as handle: json.dump({"package_schema": "simple-v1", "name": os.path.basename(target), "created_by": "camflow pack", "entry": "workflow.yaml", "input_template": "input.template.json", "skills_dir": "skills"}, handle, indent=2); handle.write("\n")
    return 0


def main(argv=None):
    _require_python36()
    parser = argparse.ArgumentParser(prog="camflow", description="CamFlow v1.2 standalone workflow runner")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run")
    run.add_argument("workflow", nargs="?")
    run.add_argument("--input")
    run.add_argument("--out")
    run.add_argument("--run-dir")
    run.add_argument("--steps", type=int)
    run.add_argument("--from", dest="from_node")
    run.add_argument("--feedback", default="")
    run.set_defaults(func=cmd_run)
    resume = sub.add_parser("resume")
    resume.add_argument("run_dir")
    resume.add_argument("--feedback", default="")
    resume.add_argument("--steps", type=int)
    resume.set_defaults(func=cmd_resume)
    batch = sub.add_parser("batch")
    batch.add_argument("workflow")
    batch.add_argument("--inputs", required=True)
    batch.add_argument("--out", required=True)
    batch.add_argument("--continue-on-fail", action="store_true")
    batch.set_defaults(func=cmd_batch)
    plan = sub.add_parser("plan")
    plan.add_argument("prompt")
    plan.add_argument("--out", required=True)
    plan.set_defaults(func=cmd_plan)
    pack = sub.add_parser("pack")
    pack.add_argument("source_dir")
    pack.add_argument("--out", required=True)
    pack.set_defaults(func=cmd_pack)
    sub.add_parser("version")
    args = parser.parse_args(argv)
    if args.command == "version": print("camflow v%s build=%s" % (__version__, __build__ or "unknown")); return 0
    if not args.command: parser.print_help(); return 1
    return args.func(args)
