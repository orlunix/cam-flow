"""Quality validator for planner output.

The DSL validator in `engine/dsl.py` checks structural correctness
(required fields, known executor, dangling gotos). This module checks
PLAN QUALITY: things a good plan has that a technically-valid one
might lack.

Two severity levels:
  errors   — the plan is broken; engine cannot / should not run it
  warnings — the plan will run but could be improved

`validate_plan_quality(workflow)` returns `(errors, warnings)`.
"""

from __future__ import annotations

import re
from collections import deque


AGENT_REQUIRED_FIELDS = ("methodology", "escalation_max", "allowed_tools",
                         "max_retries", "verify")
VALID_METHODOLOGIES = {
    "rca", "simplify-first", "search-first",
    "working-backwards", "systematic-coverage",
}


# ---- helpers -------------------------------------------------------------


def _is_agent_node(node):
    do = (node or {}).get("do", "") or ""
    return do.startswith("agent ") or do.startswith("subagent ")


def _is_cmd_node(node):
    do = (node or {}).get("do", "") or ""
    return do.startswith("cmd ")


def _node_successors(node):
    """Return the list of node ids this node can reach in one step."""
    targets = []
    if "next" in node and node["next"]:
        targets.append(node["next"])
    for rule in (node.get("transitions") or []):
        goto = rule.get("goto")
        if goto:
            targets.append(goto)
    return targets


def _reachable_from(workflow, start):
    """BFS from `start`; return the set of reachable node ids."""
    seen = {start}
    queue = deque([start])
    while queue:
        nid = queue.popleft()
        node = workflow.get(nid)
        if not isinstance(node, dict):
            continue
        for t in _node_successors(node):
            if t not in seen:
                seen.add(t)
                queue.append(t)
    return seen


def _cycles_without_retry_budget(workflow):
    """Return a list of node ids that participate in a cycle and neither
    they nor any node in the cycle declare `max_retries`.

    Coarse detection: a cycle exists iff any back-edge visits an ancestor.
    Rather than fully enumerate cycles, we flag the simple "self-loop or
    two-node loop" cases which are the pattern planners realistically
    produce (fix → test → fix).
    """
    flagged = []
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        successors = _node_successors(node)
        # Self-loop
        if nid in successors:
            if "max_retries" not in node:
                flagged.append(nid)
            continue
        # Two-node loop: A → B → A where neither has max_retries
        for succ in successors:
            peer = workflow.get(succ)
            if not isinstance(peer, dict):
                continue
            if nid in _node_successors(peer):
                a_has = "max_retries" in node
                b_has = "max_retries" in peer
                if not a_has and not b_has:
                    if nid not in flagged:
                        flagged.append(nid)
    return flagged


_STATE_REF_RE = re.compile(r"\{\{\s*state\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _state_refs(text):
    if not isinstance(text, str):
        return set()
    return set(_STATE_REF_RE.findall(text))


def _topological_precedence(workflow):
    """Build a map {node_id -> set of node_ids that can precede it} via
    forward reachability inversion. Conservative: a producer node need
    only be reachable-to the consumer via some path."""
    # Reverse adjacency
    rev = {nid: set() for nid in workflow}
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        for succ in _node_successors(node):
            if succ in rev:
                rev[succ].add(nid)

    precedence = {}
    for nid in workflow:
        seen = set()
        stack = list(rev.get(nid, ()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(rev.get(cur, ()))
        precedence[nid] = seen
    return precedence


_TEMPLATE_REF = re.compile(r"\{\{[^}]*\}\}")


def _strip_consumer_refs(text):
    """Remove `{{...}}` template blocks. Those are consumers, not producers."""
    if not isinstance(text, str):
        return ""
    return _TEMPLATE_REF.sub(" ", text)


def _producer_candidates(workflow):
    """Map {state_key -> set of node_ids that plausibly produce it}.

    A cmd node is assumed to produce `last_cmd_output` and `last_cmd_stderr`
    (the enricher does this). Other keys are assumed to come from agent
    nodes whose `with` text mentions writing them via `state_updates.<key>`
    or bare `state.<key>` outside of any `{{...}}` template (templates are
    consumer references, not productions).
    """
    producers = {}
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if _is_cmd_node(node):
            for key in ("last_cmd_output", "last_cmd_stderr"):
                producers.setdefault(key, set()).add(nid)
        with_text = _strip_consumer_refs(node.get("with") or "")
        for pattern in (r"state_updates\.([A-Za-z_][A-Za-z0-9_]*)",
                         r"state\.([A-Za-z_][A-Za-z0-9_]*)"):
            for key in re.findall(pattern, with_text):
                producers.setdefault(key, set()).add(nid)
    return producers


# ---- public API ----------------------------------------------------------


def validate_plan_quality(workflow):
    """Return (errors, warnings) lists of strings.

    Errors block execution. Warnings are advisory and the CLI asks the
    user whether to proceed.
    """
    errors = []
    warnings = []

    if not isinstance(workflow, dict):
        return ["plan is not a mapping (expected dict of nodes)"], []

    if not workflow:
        return ["plan has no nodes"], []

    # Entry = first declared node (engine uses this since the "start"
    # fix a few commits ago).
    entry = next(iter(workflow))

    # 1. Dangling goto / next targets
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            errors.append(f"node '{nid}': not a mapping")
            continue
        for succ in _node_successors(node):
            if succ not in workflow:
                errors.append(f"node '{nid}': references missing node '{succ}'")

    # 2. Reachability
    reachable = _reachable_from(workflow, entry)
    orphans = [nid for nid in workflow if nid not in reachable]
    for nid in orphans:
        errors.append(f"orphan node: '{nid}' is unreachable from entry '{entry}'")

    # 3. Cycles without retry budget
    cycles = _cycles_without_retry_budget(workflow)
    for nid in cycles:
        errors.append(
            f"cycle involving '{nid}' has no max_retries — would loop forever"
        )

    # 4. Agent nodes missing plan-priority fields
    for nid, node in workflow.items():
        if not isinstance(node, dict) or not _is_agent_node(node):
            continue
        for field in AGENT_REQUIRED_FIELDS:
            if field not in node:
                warnings.append(
                    f"agent node '{nid}' missing recommended field `{field}`"
                )
        meth = node.get("methodology")
        if meth is not None and meth not in VALID_METHODOLOGIES:
            warnings.append(
                f"agent node '{nid}': methodology '{meth}' is not one of "
                f"{sorted(VALID_METHODOLOGIES)}"
            )

    # 5. {{state.x}} refs should have a producer somewhere upstream
    producers = _producer_candidates(workflow)
    precedence = _topological_precedence(workflow)
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        refs = _state_refs(node.get("with", "")) | _state_refs(node.get("verify", ""))
        for key in refs:
            producing_nodes = producers.get(key, set())
            if not producing_nodes:
                warnings.append(
                    f"node '{nid}': references `state.{key}` but no node "
                    f"seems to produce it"
                )
                continue
            # producer must be in this node's precedence set (or same node)
            valid_producers = producing_nodes & (precedence.get(nid, set()) | {nid})
            if not valid_producers:
                warnings.append(
                    f"node '{nid}': references `state.{key}` but no upstream "
                    f"node produces it (producers: {sorted(producing_nodes)})"
                )

    return errors, warnings


def format_report(errors, warnings):
    """Human-readable report for CLI output."""
    lines = []
    if errors:
        lines.append("ERRORS (block execution):")
        for e in errors:
            lines.append(f"  - {e}")
    if warnings:
        lines.append("WARNINGS:")
        for w in warnings:
            lines.append(f"  - {w}")
    if not errors and not warnings:
        lines.append("Plan validation: OK")
    return "\n".join(lines)
