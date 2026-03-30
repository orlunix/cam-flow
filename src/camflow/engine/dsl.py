"""DSL parser and validator.

Implements: spec/dsl.md
"""

import yaml

NODE_FIELDS = {"do", "with", "next", "transitions", "set"}
EXECUTOR_TYPES = {"skill", "cmd", "agent"}


def load_workflow(path):
    """Load a workflow definition from YAML."""
    with open(path) as f:
        return yaml.safe_load(f)


def validate_node(node_id, node):
    errors = []

    if not isinstance(node, dict):
        return False, [f"node '{node_id}' is not a dict"]

    unknown = set(node.keys()) - NODE_FIELDS
    if unknown:
        errors.append(f"node '{node_id}': unknown fields {unknown}")

    if "do" not in node:
        errors.append(f"node '{node_id}': missing required field 'do'")
    else:
        executor = node["do"].split()[0] if isinstance(node["do"], str) else None
        if executor not in EXECUTOR_TYPES:
            errors.append(f"node '{node_id}': invalid executor type '{executor}'")

    transitions = node.get("transitions")
    if transitions is not None:
        if not isinstance(transitions, list):
            errors.append(f"node '{node_id}': transitions must be a list")
        else:
            for i, rule in enumerate(transitions):
                if "if" not in rule or "goto" not in rule:
                    errors.append(f"node '{node_id}': transition[{i}] must have 'if' and 'goto'")

    return len(errors) == 0, errors


def validate_workflow(workflow):
    errors = []

    if not isinstance(workflow, dict):
        return False, ["workflow is not a dict"]

    if "start" not in workflow:
        errors.append("workflow must have a 'start' node")

    for node_id, node in workflow.items():
        valid, node_errors = validate_node(node_id, node)
        errors.extend(node_errors)

        if isinstance(node, dict):
            next_target = node.get("next")
            if next_target and next_target not in workflow:
                errors.append(f"node '{node_id}': next target '{next_target}' does not exist")

            for rule in (node.get("transitions") or []):
                goto = rule.get("goto")
                if goto and goto not in workflow:
                    errors.append(f"node '{node_id}': goto target '{goto}' does not exist")

    return len(errors) == 0, errors
