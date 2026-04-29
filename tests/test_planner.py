"""Tests for the workflow YAML parser used by the Planner pipeline.

The Planner itself is now a 1-node workflow (`uses: skill.planner`,
template at `prompts/planner.md`); its prompt construction is exercised
through the runtime's `_exec_skill` path. What's still worth covering
in pure pytest is the YAML-text → validated-dict utility, since planner
output flows through it.
"""

from __future__ import annotations

import textwrap

import pytest

from runner.runtime import (
    WorkflowParseError,
    parse_workflow_yaml,
)


VALID_WORKFLOW_YAML = textwrap.dedent("""\
    workflow: demo
    version: 0.6
    nodes:
      - id: a
        uses: skill.analyze
      - id: b
        needs: [a]
        uses: skill.summarize
""")


class TestParseWorkflowYaml:
    def test_clean_yaml_round_trips(self):
        wf = parse_workflow_yaml(VALID_WORKFLOW_YAML)
        assert wf["workflow"] == "demo"
        assert len(wf["nodes"]) == 2

    def test_strips_yaml_fence(self):
        wrapped = "```yaml\n" + VALID_WORKFLOW_YAML + "\n```"
        wf = parse_workflow_yaml(wrapped)
        assert wf["workflow"] == "demo"

    def test_strips_plain_fence(self):
        wrapped = "```\n" + VALID_WORKFLOW_YAML + "\n```"
        wf = parse_workflow_yaml(wrapped)
        assert wf["workflow"] == "demo"

    def test_empty_raises(self):
        with pytest.raises(WorkflowParseError, match="empty"):
            parse_workflow_yaml("")

    def test_whitespace_only_raises(self):
        with pytest.raises(WorkflowParseError, match="empty"):
            parse_workflow_yaml("   \n  \n   ")

    def test_invalid_yaml_raises(self):
        with pytest.raises(WorkflowParseError, match="YAML"):
            parse_workflow_yaml("not: valid: yaml: : :")

    def test_non_dict_raises(self):
        with pytest.raises(WorkflowParseError, match="not a dict"):
            parse_workflow_yaml("- just\n- a list")

    def test_missing_nodes_raises(self):
        bad = "workflow: x\nversion: 0.6\n"
        with pytest.raises(WorkflowParseError, match="validation"):
            parse_workflow_yaml(bad)

    def test_unknown_dep_raises(self):
        bad = textwrap.dedent("""\
            workflow: x
            version: 0.6
            nodes:
              - id: a
                uses: skill.x
                needs: [zzz_does_not_exist]
        """)
        with pytest.raises(WorkflowParseError, match="validation"):
            parse_workflow_yaml(bad)

    def test_cycle_raises(self):
        bad = textwrap.dedent("""\
            workflow: x
            version: 0.6
            nodes:
              - id: a
                uses: skill.x
                needs: [b]
              - id: b
                uses: skill.x
                needs: [a]
        """)
        with pytest.raises(WorkflowParseError, match="validation"):
            parse_workflow_yaml(bad)
