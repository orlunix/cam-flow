"""CamFlow v1.2 validation contracts, Python 3.6 stdlib only."""
from __future__ import print_function

import os
import re

TOP_KEYS = set(["workflow", "version", "goal", "context", "input_schema", "nodes"])
NODE_KEYS = set(["id", "goal", "steps", "needs", "run", "output_schema", "verify", "retry"])
TYPES = set(["string", "integer", "number", "boolean", "array", "object"])
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def type_matches(value, kind):
    if kind == "string": return isinstance(value, str)
    if kind == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean": return isinstance(value, bool)
    if kind == "array": return isinstance(value, list)
    if kind == "object": return isinstance(value, dict)
    return False


def validate_input(data, schema):
    errors = []
    if not isinstance(data, dict): return ["input.json: top level must be an object"]
    for key, kind in (schema or {}).items():
        if key not in data: errors.append("input.json: missing required field %r" % key)
        elif not type_matches(data[key], kind): errors.append("input.json.%s: expected %s" % (key, kind))
    return errors


def validate_workflow(spec, root):
    errors = []
    if not isinstance(spec, dict): return ["workflow is not an object"]
    unknown = set(spec) - TOP_KEYS
    if unknown: errors.append("workflow: unknown keys %s" % sorted(unknown))
    if not isinstance(spec.get("workflow"), str): errors.append("workflow.workflow: required string")
    if spec.get("version") != "1.2": errors.append('workflow.version: must be "1.2"')
    schema = spec.get("input_schema", {})
    if not isinstance(schema, dict): errors.append("workflow.input_schema: must be object"); schema = {}
    for key, kind in schema.items():
        if not isinstance(key, str) or not key or kind not in TYPES: errors.append("workflow.input_schema.%s: unsupported type" % key)
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes: return errors + ["workflow.nodes: required non-empty list"]
    ids = []
    graph = {}
    for index, node in enumerate(nodes):
        prefix = "nodes[%d]" % index
        if not isinstance(node, dict): errors.append(prefix + ": must be object"); continue
        extra = set(node) - NODE_KEYS
        if extra: errors.append(prefix + ": unknown keys %s" % sorted(extra))
        node_id = node.get("id")
        if not isinstance(node_id, str) or not ID_RE.match(node_id): errors.append(prefix + ".id: invalid")
        else: ids.append(node_id); graph[node_id] = node.get("needs", [])
        if not isinstance(node.get("goal"), str) or not node.get("goal", "").strip(): errors.append(prefix + ".goal: required string")
        steps = node.get("steps")
        if not isinstance(steps, list) or not steps or any(not isinstance(x, str) or not x.strip() for x in steps): errors.append(prefix + ".steps: required string list")
        needs = node.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(x, str) for x in needs): errors.append(prefix + ".needs: must be list")
        run = node.get("run")
        if not isinstance(run, dict) or set(run) != set(["skill"]): errors.append(prefix + ".run: must contain only skill")
        elif not isinstance(run.get("skill"), str) or not run["skill"].strip(): errors.append(prefix + ".run.skill: required string")
        elif not os.path.isfile(os.path.join(root, "skills", run["skill"], "SKILL.md")): errors.append(prefix + ".run.skill: local SKILL.md missing")
        retry = node.get("retry", 1)
        if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0: errors.append(prefix + ".retry: non-negative integer")
        for key, kind in (node.get("output_schema") or {}).items():
            if not isinstance(key, str) or kind not in TYPES: errors.append(prefix + ".output_schema: invalid field")
    if len(ids) != len(set(ids)): errors.append("workflow.nodes: duplicate ids")
    known = set(ids)
    for node_id, needs in graph.items():
        for dep in needs:
            if dep not in known: errors.append("%s.needs: unknown node %r" % (node_id, dep))
    visiting = set(); visited = set()
    def visit(node_id):
        if node_id in visiting: errors.append("workflow.nodes: cycle through %r" % node_id); return
        if node_id in visited: return
        visiting.add(node_id)
        for dep in graph.get(node_id, []):
            if dep in graph: visit(dep)
        visiting.remove(node_id); visited.add(node_id)
    for node_id in graph: visit(node_id)
    return errors


def validate_envelope(envelope, schema):
    if not isinstance(envelope, dict): return "envelope must be object"
    if envelope.get("status") not in ("success", "fail"): return "invalid envelope status"
    if not isinstance(envelope.get("data", {}), dict): return "envelope.data must be object"
    if envelope["status"] == "fail":
        error = envelope.get("error")
        if not isinstance(error, dict) or not error.get("code") or not error.get("message"): return "fail envelope needs error.code and error.message"
    for key, kind in (schema or {}).items():
        if key not in envelope.get("data", {}): return "schema missing field %s" % key
        if not type_matches(envelope["data"][key], kind): return "schema wrong type for %s" % key
    return None
