"""Python 3.6 standalone CLI for CamFlow v1.2."""
from __future__ import print_function

import argparse
import glob
import json
import os
import re
import shutil
import sys

from camflow_pkg.contracts import validate_input, validate_workflow
from camflow_pkg.engine import _ensure_embedded_assets, _ensure_embedded_skills, execute
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
    return value


def _load_workflow(path):
    root = os.path.dirname(os.path.abspath(path))
    _ensure_embedded_skills(root)
    _ensure_embedded_assets(root)
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


def cmd_run(args):
    try:
        spec, root = _load_workflow(args.workflow)
        schema = spec.get("input_schema") or {}
        if schema and not args.input:
            raise ValueError("workflow declares input_schema; --input is required")
        input_data = _read_json(args.input) if args.input else None
        errors = validate_input(input_data, schema) if input_data is not None else []
        if errors:
            raise ValueError("input validation failed:\n  " + "\n  ".join(errors))
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    run_dir = os.path.abspath(args.out or os.path.join(root, ".camflow", "run"))
    result = execute(spec, root, run_dir, input_data, args.steps)
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
    if not os.path.isdir(runs): os.makedirs(runs)
    summary = []; result_code = 0
    for path in paths:
        try:
            data = _read_json(path)
            errors = validate_input(data, spec.get("input_schema") or {})
            if errors: raise ValueError("; ".join(errors))
        except ValueError as exc:
            summary.append({"case_id": os.path.splitext(os.path.basename(path))[0], "status": "error", "error": str(exc)})
            result_code = 1
            if not args.continue_on_fail: break
            continue
        base = _case_slug(str(data.get("case_id", "")), os.path.splitext(os.path.basename(path))[0])
        name = base; index = 2
        while os.path.exists(os.path.join(runs, name)):
            name = "%s-%d" % (base, index); index += 1
        status = execute(spec, root, os.path.join(runs, name), data, None)
        summary.append({"case_id": data.get("case_id", base), "run_name": name, "status": status, "run_dir": os.path.join(runs, name)})
        if status != "done":
            result_code = 2
            if not args.continue_on_fail: break
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
    if not os.path.isdir(output): os.makedirs(output)
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
    source = os.path.abspath(args.source_dir); target = os.path.abspath(args.out)
    workflow = os.path.join(source, "workflow.yaml"); template = os.path.join(source, "input.template.json")
    if not os.path.isfile(workflow): sys.stderr.write("ERROR: missing workflow.yaml\n"); return 1
    if not os.path.isfile(template): sys.stderr.write("ERROR: missing input.template.json\n"); return 1
    if os.path.exists(target) and os.listdir(target): sys.stderr.write("ERROR: output directory is not empty: %s\n" % target); return 1
    try: spec, _root = _load_workflow(workflow)
    except ValueError as exc: sys.stderr.write("ERROR: %s\n" % exc); return 1
    if not os.path.isdir(target): os.makedirs(target)
    shutil.copy2(workflow, os.path.join(target, "workflow.yaml")); shutil.copy2(template, os.path.join(target, "input.template.json"))
    for node in spec["nodes"]:
        name = node["run"]["skill"]; src = os.path.join(source, "skills", name, "SKILL.md")
        if not os.path.isfile(src): sys.stderr.write("ERROR: missing skill file: skills/%s/SKILL.md\n" % name); return 1
        dst = os.path.join(target, "skills", name)
        if not os.path.isdir(dst): os.makedirs(dst)
        shutil.copy2(src, os.path.join(dst, "SKILL.md"))
    builtin = os.path.join(source, "builtin")
    if os.path.isdir(builtin): shutil.copytree(builtin, os.path.join(target, "builtin"))
    if os.path.isdir(os.path.join(source, "validators")): shutil.copytree(os.path.join(source, "validators"), os.path.join(target, "validators"))
    with open(os.path.join(target, "package_manifest.json"), "w") as handle: json.dump({"package_schema": "simple-v1", "name": os.path.basename(target), "created_by": "camflow pack", "entry": "workflow.yaml", "input_template": "input.template.json", "skills_dir": "skills"}, handle, indent=2); handle.write("\n")
    return 0


def main(argv=None):
    _require_python36()
    parser = argparse.ArgumentParser(prog="camflow", description="CamFlow v1.2 standalone workflow runner")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run"); run.add_argument("workflow"); run.add_argument("--input"); run.add_argument("--out"); run.add_argument("--steps", type=int); run.set_defaults(func=cmd_run)
    batch = sub.add_parser("batch"); batch.add_argument("workflow"); batch.add_argument("--inputs", required=True); batch.add_argument("--out", required=True); batch.add_argument("--continue-on-fail", action="store_true"); batch.set_defaults(func=cmd_batch)
    plan = sub.add_parser("plan"); plan.add_argument("prompt"); plan.add_argument("--out", required=True); plan.set_defaults(func=cmd_plan)
    pack = sub.add_parser("pack"); pack.add_argument("source_dir"); pack.add_argument("--out", required=True); pack.set_defaults(func=cmd_pack)
    sub.add_parser("version")
    args = parser.parse_args(argv)
    if args.command == "version": print("camflow v%s build=%s" % (__version__, __build__ or "unknown")); return 0
    if not args.command: parser.print_help(); return 1
    return args.func(args)
