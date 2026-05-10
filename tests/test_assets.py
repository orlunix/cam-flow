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
    r"^\s*verify:\s*\[",      # v0.x list-form verify (now dict-only)
    r"^\s*type:\s*agent",     # v0.x `verify: [{type: agent}]` element
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

    def test_prompt_analyzer_surfaces_test_files(self):
        """Lock the post-codex-review-planner-gap-greenlight nudge:
        prompt_analyzer must emit a test_files field and must allow
        reading explicitly-named artifacts (so downstream design can
        plan around tests it didn't have to invent)."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "prompt_analyzer" /
                     "SKILL.md")
        assert "test_files" in text, (
            "prompt_analyzer must declare a test_files output field — "
            "the designer relies on it to plan audit nodes."
        )
        # Section heading proving the read-named-artifacts permission
        # is the new official policy, not a stray hint.
        assert "## On reading files" in text

    def test_planner_workflow_yaml_includes_test_files_schema(self):
        """understand node's output_schema must include test_files
        (else the designer's downstream auto-injection won't know to
        carry the field, and the verify criterion can't reference it)."""
        spec = yaml.safe_load(_read(BUILTIN_PLANNER_YAML))
        understand = next(n for n in spec["nodes"] if n["id"] == "understand")
        assert "test_files" in (understand.get("output_schema") or {}), (
            "understand.output_schema must include test_files: array"
        )

    def test_planner_workflow_yaml_includes_deterministic_test_scripts(self):
        """understand.output_schema must carry deterministic_test_scripts
        so the designer's audit-node mandatory check has a structured
        signal. design_dag.verify.criterion must reference it so the
        gate fires."""
        spec = yaml.safe_load(_read(BUILTIN_PLANNER_YAML))
        understand = next(n for n in spec["nodes"] if n["id"] == "understand")
        assert "deterministic_test_scripts" in (
            understand.get("output_schema") or {}), (
            "understand.output_schema must include "
            "deterministic_test_scripts: array"
        )
        design = next(n for n in spec["nodes"] if n["id"] == "design_dag")
        verify_crit = (design.get("verify") or {}).get("criterion") or ""
        assert "deterministic_test_scripts" in verify_crit, (
            "design_dag.verify.criterion must reference "
            "deterministic_test_scripts so audit-node-mandatory is "
            "actually gated."
        )

    def test_planner_render_yaml_uses_compiled_workflow_validator(self):
        """Planner render_yaml must deterministically reject invalid or
        non-portable compiled workflows so render_yaml retries with concrete
        feedback before user workflow execution starts."""
        spec = yaml.safe_load(_read(BUILTIN_PLANNER_YAML))
        render = next(n for n in spec["nodes"] if n["id"] == "render_yaml")
        command = (render.get("verify") or {}).get("command") or ""
        assert "_validate-compiled-workflow" in command
        assert "agent_output.json" in command

    def test_prompt_analyzer_declares_deterministic_test_scripts(self):
        """prompt_analyzer must declare the deterministic_test_scripts
        field with the envelope-emitting contract so it doesn't
        misclassify raw-output scripts."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "prompt_analyzer" /
                     "SKILL.md")
        assert "deterministic_test_scripts" in text
        # The envelope contract must be spelled out — otherwise the
        # analyzer might list any script with "test" in its name.
        assert "envelope-emitting" in text.lower() or \
               "JSON envelope" in text

    def test_workflow_designer_has_audit_node_mandatory_check(self):
        """workflow_designer must preserve deterministic audit evidence
        without emitting legacy run.tool nodes."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "Audit-node mandatory check" in text
        assert "command_runner" in text
        assert "Do not emit `run.tool`" in text
        # Reference the structured signal (avoid silent skipping).
        assert "deterministic_test_scripts" in text

    def test_workflow_designer_audit_schema_matches_actual_envelope(self):
        """codex-audit-node-schema-followup: each audit node's
        output_schema must match the SPECIFIC script's actual envelope
        fields. The previous uniform-schema assumption halted runs when
        scripts emitted different shapes (e.g. only invariant runner
        emits failed_tests). The SKILL.md must teach: declare only what
        the script emits; minimum-viable schema is
        passed/tests_run/output; failed_tests added only when the
        analyzer confirms the script emits it."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        # Must caution against uniform schema across audit nodes.
        assert "envelope_data_fields" in text, (
            "designer must reference the analyzer's per-script "
            "envelope_data_fields summary, not assume a uniform shape."
        )
        # Must spell out the rejection-on-missing-field rule so the
        # LLM understands why over-declaring is dangerous.
        normalized = " ".join(text.split()).lower()
        assert ("rejects envelopes missing declared fields" in normalized
                or "missing declared fields" in normalized
                or "declared-but-missing fields halt" in normalized)
        # Minimum-viable schema must be named.
        assert "minimum-viable" in text.lower() or \
               "minimum viable" in text.lower()

    def test_workflow_designer_emits_workflow_goal(self):
        """Goal-driven supplement §3.1 — workflow_designer must emit
        a top-level workflow_goal field as a concrete restatement of
        the user's objective; the runtime persists this as v1.1
        Workflow.goal."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "workflow_goal" in text, (
            "workflow_designer must declare a workflow_goal output field "
            "(persisted as Workflow.goal at the top of the compiled YAML)."
        )
        # Section heading proving the discipline is the new official
        # rule, not a stray hint.
        assert "Workflow goal" in text
        # Must also tie Node.goal back to workflow_goal.
        normalized = " ".join(text.split()).lower()
        assert ("must map back to workflow_goal" in normalized
                or "non-trivial node.goal must map back" in normalized
                or "every non-trivial node.goal must map back" in normalized)

    def test_yaml_writer_emits_top_level_goal(self):
        """yaml_writer must carry workflow_goal through to the
        compiled YAML's top-level `goal:` block (the existing v1.1
        Workflow.goal field). Without this rule, the LLM may drop the
        field even if design_dag emits it."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "yaml_writer" /
                     "SKILL.md")
        # The format example must show top-level goal:.
        assert "\ngoal: |" in text or "\ngoal:" in text
        # The rule itself — strip backticks and lowercase before matching
        # so "Carry `workflow_goal` through" matches "carry workflow_goal
        # through".
        plain = text.replace("`", "").lower()
        assert "carry workflow_goal through" in plain

    def test_prompt_analyzer_deterministic_scripts_carries_field_summary(self):
        """prompt_analyzer's deterministic_test_scripts must surface
        per-script envelope_data_fields so the designer can declare a
        matching audit-node schema. List-of-paths alone is not enough
        (caused the canonical-003 default_audit halt)."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "prompt_analyzer" /
                     "SKILL.md")
        assert "envelope_data_fields" in text, (
            "prompt_analyzer must declare a per-script "
            "envelope_data_fields summary so the designer can match "
            "the audit node's schema to what the script actually emits."
        )

    def test_workflow_designer_lists_repo_skills(self):
        """Designer must point at concrete repo skills it can reach for
        — without this, LLMs invent names like gather_context /
        regression_review (live A/B finding, codex-review-live-ab-result-001)."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "Available repo skills" in text
        for skill_name in ("analyzer", "code_writer", "command_runner",
                           "reviewer"):
            assert f"`{skill_name}`" in text, (
                f"workflow_designer SKILL.md must mention the {skill_name!r} "
                f"skill so Planner reaches for it instead of inventing names."
            )

    def test_workflow_designer_prefers_verify_command_for_deterministic_tests(self):
        """The retry-with-feedback gate only fires if generative nodes
        use verify.command for deterministic gates. The SKILL.md must
        say so explicitly."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "deterministic test command" in text.lower(), (
            "workflow_designer must teach: prefer verify.command when a "
            "deterministic test command is available."
        )
        assert "verify.command" in text

    def test_workflow_designer_verify_examples_do_not_require_jq(self):
        """Remote execution environments may not have jq installed. The
        Planner's verify.command examples must use Python stdlib JSON
        parsing so generated workflows do not fail with exit 127 before
        the real verification condition is checked."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "Python stdlib" in text or "python stdlib" in text.lower()
        assert "json.load(open(\"agent_output.json\"))" in text
        assert "jq -r .data.passed agent_output.json" not in text
        assert "test \"$(jq" not in text

    def test_workflow_designer_teaches_portable_command_policy(self):
        """Planner must teach command availability and wrapper fallback so
        generated workflows do not depend on unavailable host tools."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        normalized = " ".join(text.split()).lower()
        assert "portable command rule" in normalized
        assert "command -v" in text
        assert "run.skill" in text and "load time" in normalized
        assert "Do not emit `run.tool`" in text
        assert "run.skill" in text

    def test_yaml_writer_teaches_portable_verify_command(self):
        """yaml_writer is the last chance to avoid non-portable command
        gates before render_yaml verification runs."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "yaml_writer" /
                     "SKILL.md")
        normalized = " ".join(text.split()).lower()
        assert "portable" in normalized and "verify.command" in normalized
        assert "jq" in text and "python" in normalized
        assert "missing/non-general command" in normalized

    def test_workflow_designer_warns_about_verify_cwd(self):
        """Lock the post-codex-review-planner-rerun-001 fix: Planner
        must be told that verify.command runs from the node's attempt
        directory (not project root), and shown the walk-up-to-marker
        pattern so generated commands don't fail with exit 127."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        # The cwd warning itself must be present.
        assert "attempt directory" in text.lower()
        # The walk-up pattern's defining loop must be shown verbatim,
        # so the LLM has a concrete template to copy.
        assert "while [ ! -f " in text
        assert 'dirname "$P"' in text
        # The "fail loudly if no marker" guard is mandatory — without it
        # a missing marker silently cds to /. Tests must keep this
        # guard documented.
        assert "exit 2" in text
        # Marker priority list must mention multiple project types so
        # Planner doesn't overfit to one ecosystem.
        for marker in ("pyproject.toml", "package.json", "Cargo.toml",
                       "go.mod"):
            assert marker in text, (
                f"workflow_designer SKILL.md must list {marker!r} "
                f"as one of the project-root markers (cross-language "
                f"verify.command guidance)."
            )

    def test_workflow_designer_has_replan_context_section(self):
        """Per codex-blind-maze-oracle Phase A: when the user prompt
        carries a `# Replan Context` block (operator ran
        `camflow replan` after a halt), the designer must know to
        treat it as re-design context, not noise."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "# Replan Context" in text
        # Must distinguish local vs. structural fixes so the replan
        # doesn't gratuitously redesign on every halt.
        normalized = " ".join(text.split()).lower()
        assert "local" in normalized and "structural" in normalized
        # Must reference dag_revisions/ replay so the LLM understands
        # why minimal-diff replans are preferred.
        assert "dag_revisions" in text

    def test_workflow_designer_has_implement_per_spec_recipe(self):
        """The 'implement code per spec' shape — analyzer / implementer /
        audit / reviewer — must be present as a named recipe so Planner
        produces inspectable multi-step DAGs on this class of task."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "Common shape: implement code per spec" in text
        # Must call out the audit-tool-node component specifically —
        # that's the part the live Planner skipped pre-fix.
        assert "audit" in text.lower()

    def test_workflow_designer_output_schema_type_allowlist(self):
        """P0 — Planner must enumerate the five legal output_schema type
        names (string/integer/number/boolean/array) and forbid common
        leaks (bool/int/array of <X>/nested object schemas)."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        # The five legal types must each be present as a backtick-quoted
        # type name (so they read as types, not just words).
        for legal in ("`string`", "`integer`", "`number`", "`boolean`",
                      "`array`"):
            assert legal in text, (
                f"workflow_designer SKILL.md must list {legal} as a "
                f"legal output_schema type name."
            )
        # Forbidden leaks must be explicitly flagged.
        for forbidden in ("`bool`", "`int`", "array of"):
            assert forbidden in text, (
                f"workflow_designer SKILL.md must explicitly forbid "
                f"{forbidden!r} (Planner has been observed to leak it)."
            )

    def test_yaml_writer_output_schema_type_allowlist(self):
        """P0 — yaml_writer must mirror the type allow-list so it
        normalizes upstream slips before emitting the final YAML."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "yaml_writer" /
                     "SKILL.md")
        for legal in ("`string`", "`integer`", "`number`", "`boolean`",
                      "`array`"):
            assert legal in text
        # yaml_writer's job is to *normalize*, so it must list the
        # common rewrites.
        for rewrite in ("bool` → `boolean`", "int` → `integer`",
                        "list` → `array`"):
            assert rewrite in text, (
                f"yaml_writer SKILL.md must show the normalization "
                f"rule {rewrite!r}."
            )

    def test_workflow_designer_has_verbatim_5node_template(self):
        """P1 — workflow_designer must include the literal 5-node DAG
        block so the LLM has a concrete template to copy. Anchor on
        the five canonical node ids appearing in a single fenced block."""
        text = _read(BUILTIN_PLANNER_SKILLS_DIR / "workflow_designer" /
                     "SKILL.md")
        assert "Verbatim template" in text or "verbatim template" in text
        # Each canonical id must appear as a YAML node id.
        for node_id in ("id: analyzer", "id: implementer",
                        "id: test_runner", "id: invariant_checker",
                        "id: reviewer"):
            assert node_id in text, (
                f"5-node verbatim template must include `{node_id}`."
            )
        # The implementer's verify.command in the template must include
        # the walk-up loop (anchors the cwd-safe pattern in the example,
        # not just in prose).
        assert "while [ ! -f " in text and "exit 2" in text


class TestReviewerSkill:
    """P2 — reviewer SKILL.md must require per-requirement evidence
    (file:line range or passing test name from upstream audit
    envelopes), not generic 'looks good' approves."""

    REVIEWER_SKILL = SHIPPED_SKILLS_DIR / "reviewer" / "SKILL.md"

    def test_skill_md_exists(self):
        assert self.REVIEWER_SKILL.exists()

    def test_requires_per_requirement_evidence(self):
        text = _read(self.REVIEWER_SKILL)
        assert "Per-requirement evidence" in text or \
               "per-requirement evidence" in text.lower()

    def test_lists_file_line_or_test_citation_alternatives(self):
        text = _read(self.REVIEWER_SKILL)
        # Both citation forms must be named so the agent knows which
        # is acceptable.
        assert "file:line" in text
        assert "test name" in text.lower() or "test-name" in text.lower()

    def test_flags_hollow_approves_explicitly(self):
        """The SKILL.md must teach what does NOT count as evidence —
        otherwise LLMs default to feel-good summaries."""
        text = _read(self.REVIEWER_SKILL)
        # The hollow-approve anti-pattern must be flagged.
        assert "hollow approves" in text.lower() or \
               "not evidence" in text.lower() or \
               "Generic statements are NOT evidence" in text

    def test_evidence_tied_to_workflow_goal(self):
        """Goal-driven supplement §3.4 — final audit must prove the
        original Workflow.goal, not just node success. Reviewer
        SKILL.md must say evidence traces back to Workflow.goal."""
        text = _read(self.REVIEWER_SKILL)
        # Workflow.goal must be referenced explicitly so the reviewer
        # judges against the persistent objective, not the last error.
        assert "Workflow.goal" in text
        # The "node success isn't sufficient" guardrail must be present.
        normalized = " ".join(text.split()).lower()
        assert ("necessary but not sufficient" in normalized
                or "necessary but NOT sufficient".lower() in normalized
                or "node-level success alone is necessary" in normalized)


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
