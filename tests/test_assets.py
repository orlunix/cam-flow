"""Asset smoke tests for camflow v1.1.

Locks the contract between runtime and the on-disk assets it depends on:
  * builtin/planner/workflow.yaml validates against the schema
  * Planner's private skills exist
  * shipped skills/ are discoverable
  * active prompt / skill files don't carry pre-v1.1 keywords
  * runtime prompt builders emit the required sections (the prompt
    *protocol* itself, which is a runtime contract)

If any of these break, runtime would fail in ways far more confusing
than these tests' assertion errors. Per
`docs/camflow-asset-management-plan-001-2026-05-03.md` §5 P1.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runner.runtime import (  # noqa: E402
    Node,
    build_run_prompt,
    build_verify_prompt,
    parse_workflow_yaml,
    validate_workflow,
    _resolve_skill_path,
)


BUILTIN_PLANNER_DIR = ROOT / "builtin" / "planner"
BUILTIN_PLANNER_YAML = BUILTIN_PLANNER_DIR / "workflow.yaml"
BUILTIN_PLANNER_SKILLS_DIR = BUILTIN_PLANNER_DIR / "skills"

SHIPPED_SKILLS_DIR = ROOT / "skills"


# ─── Active-contract directories that must stay v1.1-clean ────────────
#
# These are the dirs where stale v0.x semantics would mislead the
# runtime, the Planner, or future contributors. `archive/` is exempt
# (that's literally where deprecated content lives); review notes
# under docs/ that *discuss* deprecated keywords by name are also
# excluded by file-pattern (see ACTIVE_CONTRACT_FILES below).
ACTIVE_CONTRACT_FILES = [
    *(BUILTIN_PLANNER_DIR.rglob("*.md")),
    *(BUILTIN_PLANNER_DIR.rglob("*.yaml")),
    *(SHIPPED_SKILLS_DIR.rglob("*.md")),
    ROOT / "docs" / "spec.md",
    # Top-level project docs are also user-facing contract: a future
    # contributor reading CLAUDE.md or README.md must see only v1.1
    # paths and conventions.
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
]

# Patterns that indicate pre-v1.1 contract leaking into active files.
# A match is *only* a regression when it's not paired with an explicit
# negation ("no `state:`", "doesn't exist", etc) — so the assertion
# below filters those out manually rather than via complex regex.
DEPRECATED_PATTERNS = [
    r"\bv0\.6\b",
    r"^\s*state:",            # `state:` block at YAML top-level (v1.0)
    r"^\s*inputs:",           # `inputs:` (intermediate naming, also cut)
    r"\brun\.input\b",        # `run.input:` field on a node
    r"\bretry\.until\b",      # old retry-expression mechanism
    r"^\s*uses:",             # v0.x `uses: skill.X` syntax
    r'status:\s*"?halted"?',  # status enum value that doesn't exist
    r"\bverify-N\b",          # run-dir doc residue: it's `verify/`, not `verify-N/`
    r"verify-<n>",            # ditto, parameterized form
]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ─── builtin Planner ──────────────────────────────────────────────────


class TestBuiltinPlanner:
    def test_workflow_yaml_exists(self):
        assert BUILTIN_PLANNER_YAML.exists(), \
            f"missing builtin Planner workflow.yaml: {BUILTIN_PLANNER_YAML}"

    def test_workflow_yaml_parses(self):
        text = _read(BUILTIN_PLANNER_YAML)
        spec = parse_workflow_yaml(text)
        assert spec.get("workflow") == "planner"
        assert isinstance(spec.get("nodes"), list) and len(spec["nodes"]) >= 1

    def test_workflow_yaml_validates_against_schema(self):
        spec = yaml.safe_load(_read(BUILTIN_PLANNER_YAML))
        errors = validate_workflow(spec, project_root=BUILTIN_PLANNER_DIR)
        assert errors == [], (
            "builtin Planner workflow.yaml must validate cleanly under "
            f"the v1.1 schema, got: {errors}"
        )

    def test_planner_private_skills_exist(self):
        spec = yaml.safe_load(_read(BUILTIN_PLANNER_YAML))
        for n in spec["nodes"]:
            run = n.get("run") or {}
            skill = run.get("skill")
            if skill:
                resolved = _resolve_skill_path(skill, BUILTIN_PLANNER_DIR)
                assert resolved is not None, (
                    f"Planner node {n['id']!r} references skill "
                    f"{skill!r} but no SKILL.md found"
                )
                assert resolved.exists()

    def test_planner_skills_dir_layout(self):
        # Spec-mandated layout: builtin/planner/skills/<name>/SKILL.md
        for sub in ("prompt_analyzer", "workflow_designer", "yaml_writer"):
            p = BUILTIN_PLANNER_SKILLS_DIR / sub / "SKILL.md"
            assert p.exists(), f"missing builtin Planner skill: {p}"


# ─── shipped skills/ ──────────────────────────────────────────────────


class TestShippedSkills:
    def test_skills_dir_exists(self):
        assert SHIPPED_SKILLS_DIR.is_dir(), \
            f"shipped skills/ dir missing: {SHIPPED_SKILLS_DIR}"

    def test_each_skill_has_skill_md(self):
        for d in SHIPPED_SKILLS_DIR.iterdir():
            if not d.is_dir():
                continue
            assert (d / "SKILL.md").exists(), (
                f"shipped skill {d.name!r} is missing SKILL.md"
            )

    def test_each_skill_resolves(self):
        # The runtime resolves bare skill names against project_root /
        # repo_root. Since SHIPPED_SKILLS_DIR == repo_root/skills, any
        # name under it must be discoverable via _resolve_skill_path.
        for d in SHIPPED_SKILLS_DIR.iterdir():
            if not d.is_dir() or not (d / "SKILL.md").exists():
                continue
            resolved = _resolve_skill_path(d.name, ROOT)
            assert resolved is not None, f"can't resolve skill {d.name!r}"
            assert resolved == d / "SKILL.md"


# ─── prompt protocol contract ─────────────────────────────────────────


class TestPromptProtocol:
    """build_run_prompt + build_verify_prompt together ARE the
    runtime's contract with every spawned agent. Every section the
    runtime promises to inject must actually appear."""

    def _node(self, *, with_steps=True, output_schema=None):
        return Node.from_dict({
            "id": "demo",
            "goal": "diagnose the bug",
            "steps": ["read foo.py", "identify variable"] if with_steps
                     else ["s1"],
            "needs": ["upstream_id"],
            "run": {"skill": "analyzer"},
            "output_schema": output_schema or {"root_cause": "string"},
        })

    def test_run_prompt_has_goal_section(self):
        out = build_run_prompt(self._node(), {})
        assert "# Goal" in out
        assert "diagnose the bug" in out

    def test_run_prompt_has_steps_section(self):
        out = build_run_prompt(self._node(), {})
        assert "# Steps" in out
        assert "read foo.py" in out
        assert "identify variable" in out

    def test_run_prompt_has_workflow_context_when_provided(self):
        out = build_run_prompt(self._node(), {},
                               workflow_context="shared facts")
        assert "# Workflow Context" in out
        assert "shared facts" in out

    def test_run_prompt_omits_workflow_context_when_blank(self):
        out = build_run_prompt(self._node(), {})
        assert "# Workflow Context" not in out

    def test_run_prompt_has_upstream_outputs_when_provided(self):
        upstream_env = {"status": "success", "data": {"x": 1}}
        out = build_run_prompt(
            self._node(),
            {"upstream": {"upstream_id": upstream_env}},
        )
        assert "# Upstream Outputs" in out
        assert "upstream_id" in out

    def test_run_prompt_has_envelope_protocol(self):
        out = build_run_prompt(self._node(), {})
        # The agent must be told the envelope shape it owes back.
        assert '"status"' in out
        assert "success" in out and "fail" in out
        assert '"data"' in out
        assert '"error"' in out

    def test_run_prompt_has_output_schema_section(self):
        out = build_run_prompt(self._node(), {},
                               workflow_context=None)
        # Schema fields must appear so the agent knows what to produce.
        assert "root_cause" in out

    def test_run_prompt_retry_note_when_previous_present(self):
        out = build_run_prompt(
            self._node(),
            {"previous": {"status": "fail", "error": {"message": "bad"},
                          "feedback": "try harder"}},
        )
        # Retry banner so the agent knows to read previous.feedback.
        assert "previous" in out.lower()
        # And the actual feedback content must be present.
        assert "try harder" in out

    def test_verify_prompt_has_evidence_protocol(self):
        out = build_verify_prompt(self._node(), {"status": "success"})
        # The evidence protocol is what prevents hollow approves.
        assert "Evidence protocol" in out
        assert "Acceptable evidence" in out
        assert "NOT acceptable evidence" in out

    def test_verify_prompt_has_envelope_being_verified(self):
        envelope = {"status": "success", "data": {"root_cause": "x"}}
        out = build_verify_prompt(self._node(), envelope)
        # The envelope being checked must literally appear.
        assert "root_cause" in out

    def test_verify_prompt_workflow_context_pass_through(self):
        out = build_verify_prompt(self._node(), {"status": "success"},
                                  workflow_context="shared")
        assert "# Workflow Context" in out
        assert "shared" in out


# ─── deprecated-keyword sweep ─────────────────────────────────────────


class TestNoDeprecatedSemantics:
    """Active v1.1 contract files must not contain pre-v1.1 keywords
    except as explicit negations (Planner teaching itself "no
    run.input" is fine; an actual `run.input:` field would not be).
    """

    def _is_negation_context(self, line: str, pattern: str) -> bool:
        # Treat the line as an intentional negation if it explicitly
        # says "this thing is gone / cannot be used / has no per-node
        # user input" etc. Includes the cuts/forbidden tables in the
        # spec, which list the deprecated names ON PURPOSE.
        lower = line.lower()
        negation_markers = [
            "no `", "no '", "no inputs:", "no state:", "no run.input",
            "no `state:", "no `inputs:", "no `run.input",
            "doesn't exist", "don't exist", "doesn't have",
            "doesn't have them", "they don't exist",
            "removed", "is gone", "no longer", "not coming back",
            "no `retry.until", "previously",
            "matches the camflow schema (no",
            "matches camflow schema (no",
            "conform to camflow spec (no",
            "no inputs:/state:/run.input",
            "cannot declare",  # spec phrasing for "this is forbidden"
            "auto-injected",   # cuts-table cells routinely use this
            "is internal counter",  # ditto
            "all cut",         # CLAUDE.md DO/DON'T phrasing
            "don't bring back",  # CLAUDE.md DO/DON'T phrasing
        ]
        return any(m in lower for m in negation_markers)

    @pytest.mark.parametrize("path", [str(p) for p in ACTIVE_CONTRACT_FILES])
    def test_no_deprecated_keywords(self, path):
        p = Path(path)
        text = _read(p)
        offending = []
        for ln_no, line in enumerate(text.splitlines(), start=1):
            for pat in DEPRECATED_PATTERNS:
                if re.search(pat, line):
                    if self._is_negation_context(line, pat):
                        continue
                    offending.append((ln_no, pat, line.strip()))
        assert not offending, (
            f"{p.relative_to(ROOT)} contains pre-v1.1 keywords:\n  "
            + "\n  ".join(f"L{n} ({pat}): {ln}" for n, pat, ln in offending)
        )
