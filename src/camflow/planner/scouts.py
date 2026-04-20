"""Planner scouts — read-only environment + skill discovery.

The Planner is a pure decision-maker (one LLM call, in/out a workflow).
Scouts do the legwork: search the skill catalog, probe the environment,
report back. The Planner reads scout reports the same way it reads
CLAUDE.md — as additional context, not as a tool the LLM calls
mid-generation.

Two scout types:

  - skill-scout — `skillm search <query>` + read each match's SKILL.md
    summary. Returns a list of {name, description, fit_hint} dicts.
  - env-scout   — `which <tool>` + best-effort `<tool> --version` for
    each requested tool. Returns a dict keyed by tool name.

Hard guarantees (so the Planner can always trust the report shape):

  - READ-ONLY. Scouts never modify state. They only run search /
    which / version probes.
  - BOUNDED. skill-scout returns at most `max_candidates` (default 5).
    env-scout caps the requested tool list at `max_checks` (default 12).
  - TIMED OUT. Each underlying subprocess is hard-limited; the wall
    clock for one scout call is bounded by `timeout` (default 30 s).
  - GRACEFUL. Missing `skillm` or missing tools → empty / "not found"
    entries, never an exception. The Planner can call scouts
    unconditionally without a try/except.

Used by the camflow-manager skill (Option B): the Creator agent
invokes `camflow scout` (a CLI front-end on this module) before
calling `camflow plan`, then passes the JSON reports through to the
planner via `--scout-report`. When direct API access is available
later (Option A), the same functions can back a `tools=` definition
on the LLM call.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional


# Limits — kept conservative on purpose. Token cost on the planner
# side scales linearly with the number of skills the prompt enumerates.
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_MAX_ENV_CHECKS = 12
DEFAULT_SCOUT_TIMEOUT = 30
SKILL_SUMMARY_LINE_CAP = 50    # only the first N lines of each SKILL.md
SKILL_SUMMARY_CHAR_CAP = 1500  # hard char cap on the summary excerpt


# ---- skill-scout ---------------------------------------------------------


def run_skill_scout(
    query: str,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    timeout: int = DEFAULT_SCOUT_TIMEOUT,
    skillm_bin: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
) -> dict:
    """Search the skill catalog for matches, then read each SKILL.md.

    Args:
        query: free-text capability description, e.g.
            "RTL signal trace analysis for Verilog".
        max_candidates: cap on how many matches to evaluate.
        timeout: per-subprocess hard timeout (seconds).
        skillm_bin: override the `skillm` binary path. Default: PATH.
        skill_dirs: optional list of skill directories to fall back to
            when `skillm` is unavailable. Each must contain
            `<name>/SKILL.md` files.

    Returns:
        {
          "query":       str,                 # echoed back
          "tool":        "skillm" | "fallback" | "unavailable",
          "candidates":  [
            {"name": str, "description": str, "summary": str, "path": str},
            ...
          ],
          "warnings":    [str, ...],
        }
        Always returns a dict — never raises.
    """
    report: dict = {
        "query": query,
        "tool": "unavailable",
        "candidates": [],
        "warnings": [],
    }

    bin_path = skillm_bin or shutil.which("skillm")
    if bin_path:
        report["tool"] = "skillm"
        candidates = _skillm_search(bin_path, query, timeout, report["warnings"])
    else:
        report["tool"] = "fallback"
        candidates = _fallback_skill_search(
            query, skill_dirs or _default_skill_dirs(), report["warnings"],
        )

    # Hard cap before we read the (potentially many) SKILL.md files.
    candidates = candidates[:max_candidates]

    enriched = []
    for c in candidates:
        summary = _read_skill_summary(c.get("path"))
        enriched.append(
            {
                "name": c.get("name", ""),
                "description": c.get("description", "") or summary[:200],
                "summary": summary,
                "path": c.get("path", ""),
            }
        )
    report["candidates"] = enriched

    if not enriched and not report["warnings"]:
        report["warnings"].append(f"no skills matched query: {query!r}")
    return report


def _skillm_search(bin_path: str, query: str, timeout: int, warnings: list[str]) -> list[dict]:
    """Run `skillm search <query>` and parse the output into candidates."""
    try:
        proc = subprocess.run(
            [bin_path, "search", query],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        warnings.append("skillm search timed out")
        return []
    except FileNotFoundError:
        warnings.append("skillm binary disappeared between resolve and run")
        return []
    except OSError as e:
        warnings.append(f"skillm exec error: {e}")
        return []

    if proc.returncode != 0:
        # skillm prints to stderr on bad exit; surface a short hint.
        tail = (proc.stderr or "")[-300:]
        warnings.append(f"skillm exit {proc.returncode}: {tail}".strip())
        return []

    return _parse_skillm_output(proc.stdout)


def _parse_skillm_output(text: str) -> list[dict]:
    """Best-effort parser for `skillm search` output.

    skillm versions vary; we accept either JSON output (common newer
    versions) or human-readable lines like
        "<name>  <path>  <one-line description>".
    """
    text = (text or "").strip()
    if not text:
        return []

    # JSON shape — preferred when available.
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [_normalize_skill_entry(item) for item in data]
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [_normalize_skill_entry(item) for item in data["results"]]

    # Plain-text fallback.
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Expect either:  "<name>\t<path>\t<desc>" or "<name>: <desc>"
        if "\t" in line:
            parts = line.split("\t")
            name = parts[0].strip()
            path = parts[1].strip() if len(parts) > 1 else ""
            desc = parts[2].strip() if len(parts) > 2 else ""
        elif ":" in line:
            name, desc = line.split(":", 1)
            name = name.strip()
            desc = desc.strip()
            path = ""
        else:
            name, desc, path = line, "", ""
        out.append({"name": name, "description": desc, "path": path})
    return out


def _normalize_skill_entry(item) -> dict:
    """Coerce a parsed JSON entry from skillm into our standard shape."""
    if isinstance(item, str):
        return {"name": item, "description": "", "path": ""}
    if not isinstance(item, dict):
        return {"name": str(item), "description": "", "path": ""}
    return {
        "name": item.get("name") or item.get("id") or "",
        "description": item.get("description")
        or item.get("desc")
        or item.get("summary", ""),
        "path": item.get("path") or item.get("file", ""),
    }


def _default_skill_dirs() -> list[str]:
    """Standard locations to look when `skillm` is unavailable."""
    candidates = [
        os.path.expanduser("~/.claude/skills"),
        "skills",
    ]
    return [d for d in candidates if os.path.isdir(d)]


def _fallback_skill_search(
    query: str, skill_dirs: list[str], warnings: list[str],
) -> list[dict]:
    """Naive fallback when `skillm` is unavailable.

    Walks each skill dir, reads SKILL.md frontmatter, and returns
    entries whose name or description contains any token from the query.
    """
    if not skill_dirs:
        warnings.append("no skill directories available for fallback search")
        return []

    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_-]{3,}", query)]
    if not tokens:
        # No usable query tokens — return everything (capped by caller).
        tokens = []

    matches = []
    for d in skill_dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for entry in entries:
            skill_md = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            meta = _read_frontmatter(skill_md)
            name = meta.get("name") or entry
            desc = meta.get("description") or ""
            haystack = f"{name} {desc}".lower()
            if not tokens or any(t in haystack for t in tokens):
                matches.append({
                    "name": name, "description": desc, "path": skill_md,
                })
    return matches


def _read_skill_summary(path: Optional[str]) -> str:
    """Read the first SKILL_SUMMARY_LINE_CAP lines / SKILL_SUMMARY_CHAR_CAP
    chars of a SKILL.md file. Quiet on missing files.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= SKILL_SUMMARY_LINE_CAP:
                    break
                lines.append(line)
        text = "".join(lines)
        if len(text) > SKILL_SUMMARY_CHAR_CAP:
            text = text[:SKILL_SUMMARY_CHAR_CAP] + "\n...[truncated]\n"
        return text
    except OSError:
        return ""


def _read_frontmatter(md_path: str) -> dict:
    """Extract the YAML frontmatter as a dict. Empty dict on any error."""
    try:
        with open(md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        import yaml
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ---- env-scout -----------------------------------------------------------


# Keep the env-scout query language tight. A query is either:
#   * A bare identifier  →  treat as a tool name to `which`
#   * "<tool> --version" →  also probe its version
#   * "path: <expression>" →  test -e on a path
# We keep parsing simple on purpose; the planner emits structured
# queries via the CLI flag, not free text.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")


def run_env_scout(
    checks: list[str],
    *,
    timeout: int = DEFAULT_SCOUT_TIMEOUT,
    max_checks: int = DEFAULT_MAX_ENV_CHECKS,
) -> dict:
    """Probe the environment for tool availability + paths.

    Args:
        checks: list of probe specs. Each spec is one of:
            "<tool>"                e.g. "vcs", "smake", "p4"
            "path:<absolute-path>"  e.g. "path:/home/user/rtl"
        timeout: per-subprocess hard timeout (seconds).
        max_checks: cap on how many probes to run in one call.

    Returns:
        {
          "checks":  [str, ...],     # echoed (post-cap) input
          "results": {
            "<tool>": {
              "kind": "tool",
              "available": bool,
              "path": str | None,
              "version": str | None,
              "warning": str | None,
            },
            "path:/x": {
              "kind": "path",
              "available": bool,
              "type": "file" | "dir" | "missing",
            },
          },
          "warnings": [str, ...],
        }
        Always returns a dict — never raises.
    """
    report: dict = {"checks": [], "results": {}, "warnings": []}
    if not checks:
        report["warnings"].append("env-scout called with no checks")
        return report

    capped = list(checks)[:max_checks]
    if len(checks) > max_checks:
        report["warnings"].append(
            f"env-scout truncated {len(checks)} → {max_checks} checks"
        )
    report["checks"] = capped

    for spec in capped:
        if not isinstance(spec, str) or not spec.strip():
            continue
        spec = spec.strip()
        if spec.startswith("path:"):
            report["results"][spec] = _probe_path(spec[len("path:"):].strip())
        elif _TOOL_NAME_RE.match(spec):
            report["results"][spec] = _probe_tool(spec, timeout)
        else:
            report["results"][spec] = {
                "kind": "unknown",
                "available": False,
                "warning": f"unrecognized check spec: {spec!r}",
            }
    return report


def _probe_tool(name: str, timeout: int) -> dict:
    """which + best-effort --version on a single tool."""
    path = shutil.which(name)
    if not path:
        return {"kind": "tool", "available": False, "path": None,
                "version": None, "warning": "not on PATH"}

    version: Optional[str] = None
    warning: Optional[str] = None
    for flag in ("--version", "-version", "-V"):
        try:
            proc = subprocess.run(
                [path, flag],
                capture_output=True, text=True, timeout=timeout,
            )
            output = (proc.stdout + proc.stderr).strip()
            if output:
                # First non-empty line is usually the version banner.
                version = output.splitlines()[0][:200]
                break
        except (subprocess.TimeoutExpired, OSError) as e:
            warning = f"version probe failed: {e}"
            continue

    return {
        "kind": "tool",
        "available": True,
        "path": path,
        "version": version,
        "warning": warning,
    }


def _probe_path(path: str) -> dict:
    if not path:
        return {"kind": "path", "available": False, "type": "missing",
                "warning": "empty path"}
    if os.path.isdir(path):
        return {"kind": "path", "available": True, "type": "dir"}
    if os.path.isfile(path):
        return {"kind": "path", "available": True, "type": "file"}
    return {"kind": "path", "available": False, "type": "missing"}


# ---- Plan-side dispatch helper ------------------------------------------


def default_scout_fn(scout_type: str, query) -> dict:
    """Dispatcher so callers can hand the Planner a single callable.

    Used by `generate_workflow(scout_fn=default_scout_fn)` for the
    default path. Returns the full report dict — the Planner caller
    decides how to render it into the prompt.
    """
    if scout_type == "skill":
        if not isinstance(query, str):
            return {"query": str(query), "tool": "unavailable",
                    "candidates": [], "warnings": ["query must be a string"]}
        return run_skill_scout(query)
    if scout_type == "env":
        if isinstance(query, str):
            checks = [query]
        elif isinstance(query, (list, tuple)):
            checks = list(query)
        else:
            checks = []
        return run_env_scout(checks)
    return {
        "query": str(query),
        "warnings": [f"unknown scout_type: {scout_type!r}"],
    }
