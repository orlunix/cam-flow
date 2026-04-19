"""Unit tests for the planner module.

Covers:
  * extract_yaml_block (raw / fenced variants)
  * build_planner_prompt renders context (skills, env, CLAUDE.md, examples)
  * generate_workflow end-to-end with a mocked LLM
  * validate_plan_quality:
      - orphans
      - infinite loops without max_retries
      - missing verify/methodology/allowed_tools/escalation_max on agents
      - {{state.x}} refs without a producer
      - valid plans pass
  * ascii_graph renders without crash
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from camflow.planner.examples import FEW_SHOT_EXAMPLES, render_examples
from camflow.planner.planner import (
    ascii_graph,
    extract_yaml_block,
    generate_workflow,
)
from camflow.planner.prompt_template import build_planner_prompt
from camflow.planner.validator import format_report, validate_plan_quality


# ---- YAML extraction ---------------------------------------------------


class TestExtractYamlBlock:
    def test_raw_yaml(self):
        txt = "start:\n  do: cmd echo hi\n"
        assert extract_yaml_block(txt).startswith("start:")

    def test_yaml_fenced(self):
        resp = "Here it is:\n```yaml\nstart:\n  do: cmd echo hi\n```\nAll set."
        out = extract_yaml_block(resp)
        assert out.startswith("start:")
        assert "```" not in out

    def test_bare_fence(self):
        resp = "```\nstart:\n  do: cmd echo hi\n```"
        assert extract_yaml_block(resp).startswith("start:")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            extract_yaml_block("")

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            extract_yaml_block(None)


# ---- Prompt template --------------------------------------------------


class TestBuildPlannerPrompt:
    def test_renders_request(self):
        p = build_planner_prompt("Fix the calculator bugs")
        assert "Fix the calculator bugs" in p
        assert "workflow.yaml" in p.lower()

    def test_includes_skills_section(self):
        p = build_planner_prompt(
            "x",
            skills_list=[("debug-skill", "10-phase debug loop"),
                          ("task-router", "Triage start nodes")],
        )
        assert "debug-skill" in p
        assert "10-phase debug loop" in p
        assert "task-router" in p

    def test_includes_env_info(self):
        p = build_planner_prompt("x", env_info={"hostname": "server-01", "python": "3.12"})
        assert "server-01" in p
        assert "python" in p

    def test_includes_claude_md_truncated(self):
        md = "a" * 10000
        p = build_planner_prompt("x", claude_md=md)
        # Should be truncated (4000 char cap)
        assert "...[truncated]" in p

    def test_includes_few_shot_examples(self):
        p = build_planner_prompt("x")
        assert "Example 1" in p
        assert "Example 2" in p
        assert "Example 3" in p


class TestRenderExamples:
    def test_renders_each_example(self):
        out = render_examples()
        assert len(FEW_SHOT_EXAMPLES) == 3
        for i in range(1, 4):
            assert f"Example {i}" in out


# ---- Quality validator ------------------------------------------------


class TestValidatePlanQuality:
    def test_valid_minimal_plan_passes(self):
        wf = {
            "start": {"do": "cmd echo hi"},
        }
        errors, warnings = validate_plan_quality(wf)
        assert errors == []
        # (no agent nodes → no agent-field warnings)

    def test_empty_plan_is_error(self):
        errors, warnings = validate_plan_quality({})
        assert any("no nodes" in e for e in errors)

    def test_dangling_goto_is_error(self):
        wf = {"start": {"do": "cmd x", "next": "nope"}}
        errors, _ = validate_plan_quality(wf)
        assert any("missing node 'nope'" in e for e in errors)

    def test_orphan_node_is_error(self):
        wf = {
            "start": {"do": "cmd x"},
            "lonely": {"do": "cmd y"},  # unreachable
        }
        errors, _ = validate_plan_quality(wf)
        assert any("orphan" in e and "lonely" in e for e in errors)

    def test_self_loop_without_retries_is_error(self):
        wf = {"loop": {"do": "cmd x", "next": "loop"}}
        errors, _ = validate_plan_quality(wf)
        assert any("max_retries" in e for e in errors)

    def test_self_loop_with_retries_ok(self):
        wf = {"loop": {"do": "cmd x", "next": "loop", "max_retries": 3}}
        errors, _ = validate_plan_quality(wf)
        assert not any("max_retries" in e for e in errors)

    def test_two_node_cycle_without_retries_is_error(self):
        wf = {
            "fix": {"do": "agent claude", "next": "test"},
            "test": {"do": "cmd false",
                      "transitions": [{"if": "fail", "goto": "fix"}]},
        }
        errors, _ = validate_plan_quality(wf)
        assert any("max_retries" in e for e in errors)

    def test_two_node_cycle_with_retries_ok(self):
        wf = {
            "fix": {"do": "agent claude", "methodology": "rca",
                     "escalation_max": 3, "allowed_tools": ["Read", "Edit"],
                     "max_retries": 3, "verify": "true",
                     "with": "Fix it", "next": "test"},
            "test": {"do": "cmd false",
                      "transitions": [{"if": "fail", "goto": "fix"}]},
        }
        errors, _ = validate_plan_quality(wf)
        cycle_errors = [e for e in errors if "loop forever" in e.lower() or "max_retries" in e]
        assert cycle_errors == []

    def test_agent_missing_fields_is_warning_not_error(self):
        wf = {
            "fix": {"do": "agent claude", "with": "Fix", "next": "done"},
            "done": {"do": "cmd echo ok"},
        }
        errors, warnings = validate_plan_quality(wf)
        # Missing agent fields are warnings, not errors
        assert not any("fix" in e and "verify" in e for e in errors)
        assert any("fix" in w and "verify" in w for w in warnings)
        assert any("fix" in w and "methodology" in w for w in warnings)
        assert any("fix" in w and "allowed_tools" in w for w in warnings)
        assert any("fix" in w and "escalation_max" in w for w in warnings)

    def test_invalid_methodology_is_warning(self):
        wf = {"n": {"do": "agent claude",
                      "methodology": "made-up",
                      "with": "x"}}
        _, warnings = validate_plan_quality(wf)
        assert any("made-up" in w for w in warnings)

    def test_unproduced_state_ref_is_warning(self):
        wf = {
            "a": {"do": "agent claude", "with": "use {{state.magic}}",
                    "next": "b"},
            "b": {"do": "cmd echo {{state.magic}}"},
        }
        _, warnings = validate_plan_quality(wf)
        assert any("state.magic" in w for w in warnings)

    def test_state_ref_with_producer_ok(self):
        wf = {
            "a": {"do": "agent claude",
                    "with": "write state_updates.magic = 'value'",
                    "next": "b"},
            "b": {"do": "agent claude",
                    "with": "use {{state.magic}}"},
        }
        _, warnings = validate_plan_quality(wf)
        # No warning about unproduced state.magic
        assert not any("state.magic" in w and "no node" in w for w in warnings)


# ---- Format report ------------------------------------------------------


class TestFormatReport:
    def test_empty_is_ok_message(self):
        out = format_report([], [])
        assert "OK" in out

    def test_errors_and_warnings_included(self):
        out = format_report(["bad thing"], ["watch out"])
        assert "bad thing" in out
        assert "watch out" in out


# ---- generate_workflow end-to-end --------------------------------------


class TestGenerateWorkflow:
    def test_happy_path(self, tmp_path):
        # A tiny valid workflow the mock LLM will return
        fake_response = textwrap.dedent("""
            ```yaml
            start:
              do: cmd echo hi
              next: done
            done:
              do: cmd echo done
            ```
        """).strip()

        def fake_llm(prompt):
            # Confirm the prompt carries the user's request
            assert "Fix the bugs" in prompt
            return fake_response

        wf = generate_workflow("Fix the bugs", llm_call=fake_llm)
        assert "start" in wf
        assert "done" in wf
        assert wf["start"]["next"] == "done"

    def test_raises_on_invalid_yaml(self):
        def fake_llm(prompt):
            return "not: [valid: yaml"

        with pytest.raises(ValueError, match="invalid YAML"):
            generate_workflow("x", llm_call=fake_llm)

    def test_raises_on_empty_response(self):
        def fake_llm(prompt):
            return ""

        with pytest.raises(ValueError):
            generate_workflow("x", llm_call=fake_llm)

    def test_raises_on_dsl_invalid(self):
        # Valid YAML but DSL-invalid (unknown node field)
        def fake_llm(prompt):
            return "start:\n  do: bananas x\n"

        with pytest.raises(ValueError, match="DSL validation"):
            generate_workflow("x", llm_call=fake_llm)


# ---- ASCII graph --------------------------------------------------------


class TestAsciiGraph:
    def test_empty_workflow(self):
        assert "empty" in ascii_graph({}).lower()

    def test_renders_nodes(self):
        wf = {
            "start": {"do": "cmd pytest",
                       "transitions": [
                           {"if": "fail", "goto": "fix"},
                           {"if": "success", "goto": "done"},
                       ]},
            "fix": {"do": "agent claude", "methodology": "rca",
                      "verify": "pytest",
                      "next": "start"},
            "done": {"do": "cmd echo done"},
        }
        out = ascii_graph(wf)
        assert "start" in out
        assert "fix" in out
        assert "done" in out
        assert "fail" in out
        assert "methodology=rca" in out
