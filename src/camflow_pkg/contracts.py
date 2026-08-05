"""CamFlow v1.2 validation contracts, Python 3.6 stdlib only."""
from __future__ import print_function

import os
import re

TOP_KEYS = set(["workflow", "version", "goal", "context", "input_schema", "nodes"])
NODE_KEYS = set(["id", "goal", "steps", "needs", "run", "output_schema", "verify", "retry", "when"])
TYPES = set(["string", "integer", "number", "boolean", "array", "object"])
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
WHEN_PATH_RE = re.compile(r"^data\.([A-Za-z0-9_-]+)$")


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


def _validate_verify(value, prefix, errors):
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(prefix + ".verify: must be object")
        return
    forms = [key for key in ("criterion", "command", "human") if key in value]
    if len(forms) != 1 or set(value) - set(["criterion", "command", "human", "timeout"]):
        errors.append(prefix + ".verify: must specify exactly one of criterion, command, human")
        return
    form = forms[0]
    if not isinstance(value[form], str) or not value[form].strip():
        errors.append(prefix + ".verify.%s: required non-empty string" % form)
    if "timeout" in value:
        timeout = value["timeout"]
        if form != "command" or not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            errors.append(prefix + ".verify.timeout: requires command and positive integer")


def _validate_when(value, prefix, errors):
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != set(["node", "path", "equals"]):
        errors.append(prefix + ".when: must contain exactly node, path, equals")
        return
    for key in ("node", "path", "equals"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(prefix + ".when.%s: required non-empty string" % key)
    path = value.get("path")
    if isinstance(path, str) and path.strip() and not WHEN_PATH_RE.match(path):
        errors.append(prefix + ".when.path: must be data.<field>")


def validate_workflow(spec, root):
    errors = []
    if not isinstance(spec, dict): return ["workflow is not an object"]
    unknown = set(spec) - TOP_KEYS
    if unknown: errors.append("workflow: unknown keys %s" % sorted(unknown))
    if not isinstance(spec.get("workflow"), str) or not spec.get("workflow", "").strip(): errors.append("workflow.workflow: required string")
    if spec.get("version") != "1.2": errors.append('workflow.version: must be "1.2"')
    for key in ("goal", "context"):
        if key in spec and not isinstance(spec[key], str): errors.append("workflow.%s: must be string" % key)
    schema = spec.get("input_schema", {})
    if not isinstance(schema, dict): errors.append("workflow.input_schema: must be object"); schema = {}
    for key, kind in schema.items():
        if not isinstance(key, str) or not key or kind not in TYPES: errors.append("workflow.input_schema.%s: unsupported type" % key)
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes: return errors + ["workflow.nodes: required non-empty list"]
    ids = []
    graph = {}
    node_by_id = {}
    for index, node in enumerate(nodes):
        prefix = "nodes[%d]" % index
        if not isinstance(node, dict): errors.append(prefix + ": must be object"); continue
        extra = set(node) - NODE_KEYS
        if extra: errors.append(prefix + ": unknown keys %s" % sorted(extra))
        node_id = node.get("id")
        if not isinstance(node_id, str) or not ID_RE.match(node_id): errors.append(prefix + ".id: invalid")
        else:
            ids.append(node_id)
            graph[node_id] = node.get("needs", [])
            node_by_id[node_id] = node
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
        output = node.get("output_schema", {})
        if not isinstance(output, dict): errors.append(prefix + ".output_schema: must be object")
        else:
            for key, kind in output.items():
                if not isinstance(key, str) or not key or kind not in TYPES: errors.append(prefix + ".output_schema: invalid field")
        _validate_verify(node.get("verify"), prefix, errors)
        _validate_when(node.get("when"), prefix, errors)
    if len(ids) != len(set(ids)): errors.append("workflow.nodes: duplicate ids")
    known = set(ids)
    for node_id, needs in graph.items():
        for dep in needs:
            if dep not in known: errors.append("%s.needs: unknown node %r" % (node_id, dep))
    route_targets = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("when"), dict):
            continue
        prefix = "nodes[%d]" % index
        condition = node["when"]
        if set(condition) != set(["node", "path", "equals"]):
            continue
        source_id = condition.get("node")
        path = condition.get("path")
        expected = condition.get("equals")
        if source_id not in known:
            errors.append(prefix + ".when.node: unknown node %r" % source_id)
            continue
        needs = node.get("needs", [])
        if isinstance(needs, list) and source_id not in needs:
            errors.append(prefix + ".when.node: must also appear in needs")
        match = WHEN_PATH_RE.match(path) if isinstance(path, str) else None
        if match:
            field = match.group(1)
            source_schema = node_by_id[source_id].get("output_schema", {})
            if not isinstance(source_schema, dict) or source_schema.get(field) != "string":
                errors.append(prefix + ".when.path: source output_schema must declare %s: string" % field)
        if isinstance(path, str) and isinstance(expected, str):
            key = (source_id, path, expected)
            if key in route_targets:
                errors.append(prefix + ".when: duplicate route value %r also used by %s" % (expected, route_targets[key]))
            else:
                route_targets[key] = node.get("id", prefix)
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
    if "request_human" in envelope and not isinstance(envelope["request_human"], bool): return "envelope.request_human must be boolean"
    if "feedback" in envelope and envelope["feedback"] is not None and not isinstance(envelope["feedback"], str): return "envelope.feedback must be string or null"
    if envelope["status"] == "fail":
        error = envelope.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not error["code"] or not isinstance(error.get("message"), str) or not error["message"]: return "fail envelope needs error.code and error.message"
    for key, kind in (schema or {}).items():
        if key not in envelope.get("data", {}): return "schema missing field %s" % key
        if not type_matches(envelope["data"][key], kind): return "schema wrong type for %s" % key
    return None
