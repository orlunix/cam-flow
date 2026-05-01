"""Workflow YAML loading + structural validation.

Public entry points:
- `load_workflow(path)` — read YAML file → dict.
- `validate_workflow(wf, project_root=None) -> [errors]` — checks IDs,
  needs, retry shape, cycles, and (if project_root given) that every
  `uses: skill.X` and `uses: agent.X` resolves to an installed
  SKILL.md / AGENT.md.
- `parse_workflow_yaml(text, project_root=None) -> dict` — combined
  parse+validate, raises `WorkflowParseError` on any problem. Strips
  ```yaml code fences. Used by Planner output validation.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# Skill/agent resolution lives in `executors`, but importing it at
# module load creates a dependency cycle (executors -> Run-typed args).
# Late-import inside validate_workflow keeps parse.py decoupled.


def load_workflow(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class WorkflowParseError(Exception):
    """Raised when text → workflow dict conversion or validation fails."""


_FENCE_RE = re.compile(r"^```(?:yaml|yml)?\s*\n?|\n?```\s*$",
                       re.IGNORECASE | re.MULTILINE)


def validate_workflow(wf: dict, project_root: Path | None = None) -> list[str]:
    """Return list of validation error strings. Empty list = OK.

    With project_root, also checks `uses: skill.X` / `uses: agent.X`
    references resolve. Without it (unit tests), only structure is checked.
    """
    errors = []
    if not isinstance(wf, dict):
        return ["workflow is not a dict"]
    nodes = wf.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["workflow.nodes is missing or empty"]

    ids = [n.get("id") for n in nodes]
    if any(not i for i in ids):
        errors.append("a node is missing 'id'")
    if len(ids) != len(set(ids)):
        errors.append("duplicate node ids")
    id_set = set(ids)

    # Late import to avoid cycle: skill/agent resolvers live in runtime
    # (will move to executors.py when that file is extracted).
    if project_root is not None:
        from .runtime import _resolve_skill_md_path, _resolve_agent_md_path
    else:
        _resolve_skill_md_path = _resolve_agent_md_path = None  # type: ignore

    for n in nodes:
        nid = n.get("id", "<?>")
        if "uses" not in n and "mock" not in n:
            errors.append(f"{nid}: must have 'uses' or 'mock'")
        for dep in n.get("needs", []) or []:
            if dep not in id_set:
                errors.append(f"{nid}.needs: unknown node '{dep}'")
        retry = n.get("retry")
        if retry:
            if "until" not in retry or "max_attempts" not in retry:
                errors.append(f"{nid}.retry: requires until / max_attempts")
            if "target" in retry:
                errors.append(
                    f"{nid}.retry.target: unsupported in v0.6 — retry only "
                    f"re-runs the current node; remove the target field"
                )
        uses = n.get("uses", "")
        if project_root and uses.startswith("skill."):
            skill_name = uses[len("skill."):]
            if _resolve_skill_md_path(skill_name, project_root) is None:
                errors.append(
                    f"{nid}.uses: skill.{skill_name} not found "
                    f"(not in built-ins, not in <project>/.claude/skills, "
                    f"not in skillm library)"
                )
        if project_root and uses.startswith("agent."):
            agent_name = uses[len("agent."):]
            if _resolve_agent_md_path(agent_name, project_root) is None:
                errors.append(
                    f"{nid}.uses: agent.{agent_name} not found "
                    f"(no AGENT.md in built-ins or <project>/.claude/agents)"
                )

    # cycle detection on `needs` graph
    needs_map = {n["id"]: list(n.get("needs", []) or []) for n in nodes if "id" in n}
    visited, stack = set(), set()

    def dfs(u):
        if u in stack:
            errors.append(f"cycle through node '{u}'")
            return
        if u in visited:
            return
        stack.add(u)
        for v in needs_map.get(u, []):
            if v in id_set:
                dfs(v)
        stack.discard(u)
        visited.add(u)

    for nid in id_set:
        dfs(nid)
    return errors


def parse_workflow_yaml(text: str, project_root: Path | None = None) -> dict:
    """Parse a YAML string into a workflow dict and validate it.

    Strips optional ```yaml fences. Raises WorkflowParseError on:
      - empty / whitespace-only input
      - invalid YAML
      - non-dict top level
      - any validate_workflow error (incl. skill.X existence if
        project_root given).
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:yaml|yml)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise WorkflowParseError("empty input")
    try:
        wf = yaml.safe_load(cleaned)
    except yaml.YAMLError as e:
        raise WorkflowParseError(f"invalid YAML: {e}") from e
    if not isinstance(wf, dict):
        raise WorkflowParseError(
            f"top level is not a dict (got {type(wf).__name__})"
        )
    errors = validate_workflow(wf, project_root=project_root)
    if errors:
        raise WorkflowParseError(
            "validation failed:\n  " + "\n  ".join(errors)
        )
    return wf
