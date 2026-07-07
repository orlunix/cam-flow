"""CamFlow v1.2 public contract: static workflows, read-only input, batch."""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import runtime as rt
from .assets import _resolve_skill_path

TOP = {"workflow", "version", "goal", "context", "input_schema", "nodes"}
NODE = {"id", "goal", "steps", "needs", "run", "output_schema", "verify", "retry"}
TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def _matches(value: Any, kind: str) -> bool:
    if kind == "string": return isinstance(value, str)
    if kind == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean": return isinstance(value, bool)
    if kind == "array": return isinstance(value, list)
    if kind == "object": return isinstance(value, dict)
    return False


def project_root(workflow_path: Path) -> Path:
    for candidate in (workflow_path.parent, *workflow_path.parents):
        if (candidate / "skills").is_dir():
            return candidate
    return workflow_path.parent


def validate_workflow(spec: Any, root: Path) -> list[str]:
    if not isinstance(spec, dict): return ["workflow is not an object"]
    errors: list[str] = []
    unknown = set(spec) - TOP
    if unknown: errors.append(f"workflow: unknown keys {sorted(unknown)}")
    if not isinstance(spec.get("workflow"), str): errors.append("workflow.workflow: required string")
    if spec.get("version") != "1.2": errors.append('workflow.version: must be "1.2"')
    for key in ("goal", "context"):
        if key in spec and not isinstance(spec[key], str): errors.append(f"workflow.{key}: must be a string")
    input_schema = spec.get("input_schema", {})
    if not isinstance(input_schema, dict):
        errors.append("workflow.input_schema: must be an object")
        input_schema = {}
    for key, kind in input_schema.items():
        if not isinstance(key, str) or not key or kind not in TYPES:
            errors.append(f"workflow.input_schema.{key}: unsupported type {kind!r}")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes: return errors + ["workflow.nodes: required non-empty list"]
    ids: list[str] = []
    for index, node in enumerate(nodes):
        p = f"nodes[{index}]"
        if not isinstance(node, dict): errors.append(f"{p}: must be an object"); continue
        bad = set(node) - NODE
        if bad: errors.append(f"{p}: unknown keys {sorted(bad)}")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not rt._NODE_ID_RE.match(node_id): errors.append(f"{p}.id: invalid")
        else: ids.append(node_id)
        if not isinstance(node.get("goal"), str) or not node.get("goal", "").strip(): errors.append(f"{p}.goal: required non-empty string")
        steps = node.get("steps")
        if not isinstance(steps, list) or not steps or any(not isinstance(x, str) or not x.strip() for x in steps): errors.append(f"{p}.steps: required non-empty string list")
        needs = node.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(x, str) for x in needs): errors.append(f"{p}.needs: must be a string list")
        retry = node.get("retry", 1)
        if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0: errors.append(f"{p}.retry: must be a non-negative integer")
        run = node.get("run")
        if not isinstance(run, dict) or set(run) != {"skill"}: errors.append(f"{p}.run: must contain exactly skill")
        elif not isinstance(run["skill"], str) or not run["skill"].strip(): errors.append(f"{p}.run.skill: required string")
        elif not (root / "skills" / run["skill"] / "SKILL.md").is_file(): errors.append(f"{p}.run.skill: {run['skill']!r} not found in local skills/")
        output = node.get("output_schema", {})
        if not isinstance(output, dict): errors.append(f"{p}.output_schema: must be an object")
        else:
            for key, kind in output.items():
                if not isinstance(key, str) or not key or kind not in TYPES: errors.append(f"{p}.output_schema.{key}: unsupported type {kind!r}")
        verify = node.get("verify")
        if verify is not None:
            forms = [key for key in ("criterion", "command", "human") if isinstance(verify, dict) and key in verify]
            if not isinstance(verify, dict) or len(forms) != 1 or set(verify) - {"criterion", "command", "human", "timeout"}: errors.append(f"{p}.verify: must specify exactly one form")
            elif any(not isinstance(verify[key], str) or not verify[key].strip() for key in forms): errors.append(f"{p}.verify: form must be non-empty string")
            elif "timeout" in verify and ("command" not in verify or not isinstance(verify["timeout"], int) or isinstance(verify["timeout"], bool) or verify["timeout"] < 1): errors.append(f"{p}.verify.timeout: requires command and positive integer")
    if len(ids) != len(set(ids)): errors.append("workflow.nodes: duplicate ids")
    known = set(ids)
    graph = {}
    for node in nodes:
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            graph[node["id"]] = node.get("needs", [])
            for dep in node.get("needs", []) if isinstance(node.get("needs", []), list) else []:
                if dep not in known: errors.append(f"{node['id']}.needs: unknown node {dep!r}")
    visiting, visited = set(), set()
    def visit(node_id: str) -> None:
        if node_id in visiting: errors.append(f"workflow.nodes: cycle through {node_id!r}"); return
        if node_id in visited: return
        visiting.add(node_id)
        for dep in graph.get(node_id, []):
            if dep in graph: visit(dep)
        visiting.remove(node_id); visited.add(node_id)
    for node_id in graph: visit(node_id)
    return errors


def load_spec(path: Path) -> tuple[dict, Path]:
    try: spec = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc: raise ValueError(f"cannot load workflow: {exc}") from exc
    root = project_root(path)
    errors = validate_workflow(spec, root)
    if errors: raise ValueError("workflow validation failed:\n  " + "\n  ".join(errors))
    return spec, root


def load_input(path: str | None, schema: Any) -> tuple[dict | None, str | None]:
    if path is None:
        if schema:
            raise ValueError("workflow declares input_schema; --input is required")
        return None, None
    try: text = Path(path).read_text(); data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc: raise ValueError(f"cannot load input.json: {exc}") from exc
    if not isinstance(data, dict): raise ValueError("input.json: top level must be an object")
    for field, kind in (schema or {}).items():
        if field not in data: raise ValueError(f"input.json: missing required field {field!r}")
        if not _matches(data[field], kind): raise ValueError(f"input.json.{field}: expected {kind}")
    return data, text


def execute(spec: dict, run_dir: Path, root: Path, run_input: dict | None, input_text: str | None, steps: int | None) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    if input_text is not None: (run_dir / "input.json").write_text(input_text)
    workflow = rt.Workflow(spec, run_dir, project_root=root)
    workflow.v12_mode = True
    workflow.run_input = run_input
    workflow.trace("workflow_started", run_id=workflow.run_id, camflow_name=workflow.camflow_name, workflow_source={"type": "checked_in", "planner_invoked": False})
    try: return workflow.execute_dag(max_attempts=steps)
    finally: workflow.cleanup()


def cmd_run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="camflow run", description="Run a checked-in v1.2 workflow.")
    p.add_argument("workflow", nargs="?"); p.add_argument("--input"); p.add_argument("--run-dir"); p.add_argument("--steps", type=int); p.add_argument("--from", dest="from_node"); p.add_argument("--feedback", default="")
    a = p.parse_args(argv)
    if a.steps is not None and a.steps < 1: p.error("--steps must be >= 1")
    if a.from_node:
        if a.workflow or a.input: p.error("--from reuses run-dir workflow and input")
        if not a.run_dir: p.error("--from requires --run-dir")
        return rt._do_rerun(Path(a.run_dir), a.from_node, a.feedback, a.steps)
    if not a.workflow: p.error("workflow.yaml is required")
    try: spec, root = load_spec(Path(a.workflow).resolve()); data, text = load_input(a.input, spec.get("input_schema"))
    except ValueError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1
    run_dir = Path(a.run_dir).resolve() if a.run_dir else rt.default_run_dir(root)
    return rt._result_to_exit(execute(spec, run_dir, root, data, text, a.steps))


def case_slug(case_id: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id or fallback).strip("._-")
    return slug or "case"


def cmd_batch(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="camflow batch"); p.add_argument("workflow"); p.add_argument("--inputs", required=True); p.add_argument("--out", required=True); p.add_argument("--continue-on-fail", action="store_true")
    a = p.parse_args(argv)
    try: spec, root = load_spec(Path(a.workflow).resolve())
    except ValueError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1
    paths = [Path(x).resolve() for x in sorted(glob.glob(a.inputs))]
    if not paths: print("ERROR: --inputs matched no files", file=sys.stderr); return 1
    batch = Path(a.out).resolve(); runs = batch / "runs"; runs.mkdir(parents=True, exist_ok=True)
    meta = {"workflow": str(Path(a.workflow).resolve()), "inputs": [str(x) for x in paths], "started_at": rt.utcnow_iso(), "continue_on_fail": a.continue_on_fail}; (batch / "batch.json").write_text(json.dumps(meta, indent=2) + "\n")
    summary, code = [], 0
    for path in paths:
        try: data, text = load_input(str(path), spec.get("input_schema"))
        except ValueError as exc:
            summary.append({"case_id": path.stem, "status": "error", "exit_code": 1, "error": str(exc)}); code = 1
            if not a.continue_on_fail: break
            continue
        raw_case_id = str((data or {}).get("case_id") or path.stem)
        run_name = case_slug(raw_case_id, path.stem)
        candidate = run_name
        suffix = 2
        while (runs / candidate).exists():
            candidate = f"{run_name}-{suffix}"
            suffix += 1
        result = execute(spec, runs / candidate, root, data, text, None)
        exit_code = rt._result_to_exit(result)
        summary.append({"case_id": raw_case_id, "run_name": candidate, "status": result, "exit_code": exit_code, "run_dir": str(runs / candidate)})
        code = code or exit_code
        if exit_code and not a.continue_on_fail: break
    meta["completed_at"] = rt.utcnow_iso(); (batch / "batch.json").write_text(json.dumps(meta, indent=2) + "\n"); (batch / "summary.json").write_text(json.dumps(summary, indent=2) + "\n"); (batch / "summary.md").write_text("# CamFlow batch summary\n\n" + "\n".join(f"- {x['case_id']}: {x['status']} ({x['exit_code']})" for x in summary) + "\n")
    return code



def cmd_plan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="camflow plan")
    parser.add_argument("prompt")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    fields = {
        key: value.strip('"')
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", args.prompt)
    }
    required = ["case_id", "sim_log", "trace_log"]
    missing = [key for key in required if not fields.get(key)]
    if missing:
        print("ERROR: cannot generate real input.json.\nMissing required fields:\n" + "\n".join(f"  - {key}" for key in missing), file=sys.stderr)
        return 1
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"ERROR: plan output directory is not empty: {out}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    input_schema = {key: "string" for key in fields}
    workflow = {
        "workflow": "generated_debug_plan", "version": "1.2", "goal": args.prompt,
        "input_schema": input_schema,
        "nodes": [{"id": "investigate", "goal": "Inspect the supplied case and produce evidence-backed findings.", "steps": ["Read Workflow Input.", "Inspect referenced logs.", "Record concrete evidence and next actions."], "run": {"skill": "investigator"}, "output_schema": {"evidence": "array"}, "retry": 1}],
    }
    (out / "workflow.yaml").write_text(yaml.safe_dump(workflow, sort_keys=False))
    (out / "input.json").write_text(json.dumps(fields, indent=2) + "\n")
    (out / "input.template.json").write_text(json.dumps(input_schema, indent=2) + "\n")
    skill = out / "skills" / "investigator"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# Investigator\n\nInspect the workflow input and report only evidence-backed findings.\n")
    (out / "README.md").write_text("# Generated CamFlow plan\n\nEdit workflow.yaml, input.json, skills/, and validators/ before running.\n")
    manifest = {"generated_by": "camflow plan", "runnable": True, "input_fields": sorted(fields), "workflow": "workflow.yaml"}
    (out / "plan_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0

def cmd_pack(argv: list[str]) -> int:
    """Create a reusable directory bundle; never archive, install, or lock it."""
    parser = argparse.ArgumentParser(prog="camflow pack")
    parser.add_argument("source_dir")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    source = Path(args.source_dir).resolve()
    target = Path(args.out).resolve()
    if target.exists() and any(target.iterdir()):
        print(f"ERROR: output directory is not empty: {target}", file=sys.stderr)
        return 1
    workflow = source / "workflow.yaml"
    template = source / "input.template.json"
    if not workflow.is_file():
        print("ERROR: missing workflow.yaml", file=sys.stderr)
        return 1
    if not template.is_file():
        print("ERROR: missing input.template.json", file=sys.stderr)
        return 1
    try:
        spec, _root = load_spec(workflow)
        template_data = json.loads(template.read_text())
        if not isinstance(template_data, dict):
            raise ValueError("input.template.json: top level must be an object")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(workflow, target / "workflow.yaml")
    shutil.copy2(template, target / "input.template.json")
    if (source / "README.md").is_file():
        shutil.copy2(source / "README.md", target / "README.md")
    skill_names = []
    for node in spec["nodes"]:
        name = node["run"]["skill"]
        if name not in skill_names:
            skill_names.append(name)
    for name in skill_names:
        src = source / "skills" / name / "SKILL.md"
        if not src.is_file():
            shutil.rmtree(target, ignore_errors=True)
            print(f"ERROR: missing skill file: skills/{name}/SKILL.md", file=sys.stderr)
            return 1
        dst = target / "skills" / name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst / "SKILL.md")
    if (source / "validators").is_dir():
        shutil.copytree(source / "validators", target / "validators", dirs_exist_ok=True)
    manifest = {
        "package_schema": "simple-v1",
        "name": target.name,
        "created_by": "camflow pack",
        "entry": "workflow.yaml",
        "input_template": "input.template.json",
        "skills_dir": "skills",
    }
    (target / "package_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0
