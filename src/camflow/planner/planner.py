"""camflow planner — natural-language request → workflow.yaml.

One strong-model call. Assembles project context (CLAUDE.md, skills
list, environment info), builds the planner prompt, calls the LLM,
parses the response into a workflow dict, and runs quality validation.

Public entry points:

    generate_workflow(user_request, ...)     — programmatic use
    collect_env_info()                         — CLI helper
    discover_skills(skills_dir)               — CLI helper
    ascii_graph(workflow)                      — CLI helper
    extract_yaml_block(response)               — parse LLM output
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
from pathlib import Path

import yaml

from camflow.engine.dsl import validate_workflow as validate_dsl
from camflow.planner.llm import default_llm_call
from camflow.planner.prompt_template import build_planner_prompt
from camflow.planner.validator import validate_plan_quality


# ---- context collection --------------------------------------------------


def collect_env_info():
    """Gather useful environment facts for the planner prompt."""
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
    }
    # Tools commonly referenced in workflows
    for tool in ("git", "pytest", "ruff", "mypy", "p4", "smake", "claude", "camc"):
        path = shutil.which(tool)
        if path:
            info[f"tool:{tool}"] = path
    # Scratch paths
    for p in ("/home/scratch.hren_gpu_1", "/home/scratch.hren_gpu_2",
              "/home/scratch.hren_gpu_3"):
        if os.path.isdir(p):
            info.setdefault("scratch_paths", []).append(p)
    return info


def discover_skills(skills_dir):
    """Return a list of (name, short_description) tuples for skills/ SKILL.md files.

    Parses the YAML frontmatter of each SKILL.md to extract `name` and
    `description`. Silently skips anything that doesn't look like a skill.
    """
    if not skills_dir or not os.path.isdir(skills_dir):
        return []
    skills = []
    for entry in sorted(os.listdir(skills_dir)):
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        meta = _read_frontmatter(skill_md)
        name = meta.get("name") or entry
        desc = meta.get("description") or ""
        skills.append((name, _truncate(desc, 200)))
    return skills


def _read_frontmatter(md_path):
    """Extract the YAML frontmatter block from a markdown file."""
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _truncate(text, n):
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."


def _read_text_safe(path):
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


# ---- LLM response parsing ------------------------------------------------


def extract_yaml_block(response):
    """Pull a YAML workflow out of an LLM response.

    Handles three common shapes:
      1. Pure YAML (best-case — we instructed the model to do this)
      2. YAML fenced with ```yaml ... ```
      3. YAML fenced with ``` ... ```

    Returns the YAML text. Raises ValueError if nothing looks like YAML.
    """
    if not isinstance(response, str):
        raise ValueError("LLM response is not a string")

    stripped = response.strip()
    if not stripped:
        raise ValueError("LLM response is empty")

    # Fenced blocks — grab the first one
    fence_patterns = [
        r"```yaml\s*\n(.*?)\n```",
        r"```ya?ml\s*\n(.*?)\n```",
        r"```\s*\n(.*?)\n```",
    ]
    for pat in fence_patterns:
        m = re.search(pat, stripped, re.DOTALL)
        if m:
            return m.group(1).strip()

    # No fences — try the raw response as YAML
    return stripped


# ---- main entry point ----------------------------------------------------


def generate_workflow(
    user_request,
    claude_md_path=None,
    skills_dir=None,
    env_info=None,
    llm_call=None,
):
    """Generate a workflow.yaml for `user_request`.

    Args:
        user_request: natural-language task description.
        claude_md_path: optional path to CLAUDE.md for domain context.
        skills_dir: optional path to skills/ directory.
        env_info: optional dict to override/augment collected env info.
        llm_call: optional callable(prompt) -> response_text; defaults
                  to `default_llm_call` (tries anthropic SDK, falls
                  back to `claude -p`).

    Returns:
        dict — the parsed workflow (also runs the DSL-level validator
        and raises ValueError if the generated YAML is structurally
        broken; quality warnings are returned separately by the CLI).
    """
    if llm_call is None:
        llm_call = default_llm_call

    claude_md = _read_text_safe(claude_md_path)
    skills_list = discover_skills(skills_dir) if skills_dir else []
    effective_env = collect_env_info()
    if env_info:
        effective_env.update(env_info)

    prompt = build_planner_prompt(
        user_request,
        skills_list=skills_list,
        env_info=effective_env,
        claude_md=claude_md,
    )

    response = llm_call(prompt)
    yaml_text = extract_yaml_block(response)

    try:
        workflow = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"LLM produced invalid YAML: {exc}") from exc

    if not isinstance(workflow, dict):
        raise ValueError(
            f"LLM produced a non-mapping YAML value (got {type(workflow).__name__})"
        )

    ok, errors = validate_dsl(workflow)
    if not ok:
        raise ValueError(
            "Generated workflow failed DSL validation:\n  - " + "\n  - ".join(errors)
        )

    return workflow


# ---- ASCII graph for CLI output -----------------------------------------


def ascii_graph(workflow):
    """Render the workflow as a simple ASCII-DAG summary.

    Not a beautiful graphviz diagram — just enough to eyeball the shape:

        start (cmd: pytest)
          │fail → fix
          │success → done
        fix (agent claude, methodology=rca, verify=...)
          → test
        test (cmd: pytest)
          │fail → fix
          │success → done
        done (cmd)
    """
    if not isinstance(workflow, dict) or not workflow:
        return "(empty workflow)"

    lines = []
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            lines.append(f"{nid} (invalid)")
            continue
        do = node.get("do", "?")
        meta_parts = []
        if "methodology" in node:
            meta_parts.append(f"methodology={node['methodology']}")
        if "verify" in node:
            verify = node["verify"]
            if len(verify) > 40:
                verify = verify[:37] + "..."
            meta_parts.append(f"verify=`{verify}`")
        meta = f", {', '.join(meta_parts)}" if meta_parts else ""
        lines.append(f"{nid} ({do}{meta})")

        transitions = node.get("transitions") or []
        for rule in transitions:
            cond = rule.get("if", "?")
            goto = rule.get("goto", "?")
            lines.append(f"  │{cond} → {goto}")
        if "next" in node:
            lines.append(f"  → {node['next']}")
    return "\n".join(lines)
