"""Test suite for camflow runtime.

Layout:
- TestExpressions   — eval_expr + render_str + render_deep, strict mode.
- TestValidate      — workflow YAML structural validation, mutex rules,
                      skill/tool resolution, cycle detection.
- TestNode          — Node.from_dict, lifecycle, retry counter.
- TestEnvelope      — empty_envelope, normalize_envelope, status enum.
- TestVerify        — auto_schema_check, verify_with_command.
- TestE2E           — ★ end-to-end multi-node DAG with real tool executor:
                      run YAML → load → validate → execute → archive.
                      No LLM cost (uses tool nodes only).

Run:    pytest tests/test_runtime.py -q
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from runner import camc_lib  # noqa
from runner.runtime import (
    ExprError,
    Node,
    Workflow,
    WorkflowParseError,
    auto_schema_check,
    build_run_prompt,
    build_verify_prompt,
    eval_expr,
    empty_envelope,
    load_workflow,
    normalize_envelope,
    parse_workflow_yaml,
    render_deep,
    render_str,
    run_workflow,
    validate_workflow,
    verify_with_command,
    verify_with_human,
)


# ───────────────────────────────────────────────────────────────────────
#  Helpers
# ───────────────────────────────────────────────────────────────────────

def make_executable_tool(path: Path, body: str) -> None:
    """Write a shell script + chmod +x. body is the script contents
    after the shebang line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -e\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def envelope_tool_body(data: dict, status: str = "success") -> str:
    """Bash that prints an envelope JSON to stdout."""
    payload = {
        "status": status,
        "data": data,
        "error": None,
        "feedback": None,
        "request_human": False,
    }
    return f"cat <<'EOF'\n{json.dumps(payload)}\nEOF\n"


# ───────────────────────────────────────────────────────────────────────
#  TestExpressions
# ───────────────────────────────────────────────────────────────────────

class TestExpressions:
    """eval_expr / render_str / render_deep are namespace-agnostic.
    These tests use `nodes` since that's the only namespace the runtime
    exposes; expression engine itself doesn't care about names."""

    def test_simple_attr(self):
        assert eval_expr("nodes.a", {"nodes": {"a": 1}}) == 1

    def test_chained_attr(self):
        assert eval_expr("nodes.a.output.data.y",
                         {"nodes": {"a": {"output": {"data": {"y": "hi"}}}}}) == "hi"

    def test_compare(self):
        assert eval_expr("nodes.x == 1", {"nodes": {"x": 1}}) is True
        assert eval_expr("nodes.x != 1", {"nodes": {"x": 1}}) is False

    def test_bool_ops(self):
        ctx = {"a": True, "b": False}
        assert eval_expr("a and not b", ctx) is True

    def test_undefined_name_raises(self):
        with pytest.raises(ExprError):
            eval_expr("missing.x", {})

    def test_missing_attr_raises(self):
        with pytest.raises(ExprError):
            eval_expr("nodes.missing", {"nodes": {}})

    def test_unsupported_arithmetic(self):
        with pytest.raises(ExprError):
            eval_expr("1 + 1", {})

    def test_unsupported_call(self):
        with pytest.raises(ExprError):
            eval_expr("__import__('os')", {})

    def test_render_simple(self):
        assert render_str("hello {{nodes.name}}",
                          {"nodes": {"name": "world"}}) == "hello world"

    def test_render_strict_missing(self):
        """this spec has NO `?` optional marker; missing → ExprError."""
        with pytest.raises(ExprError):
            render_str("{{nodes.missing}}", {"nodes": {}})

    def test_render_dict_serialized(self):
        assert render_str("{{nodes.x}}",
                          {"nodes": {"x": {"a": 1}}}) == '{"a": 1}'

    def test_render_deep(self):
        ctx = {"nodes": {"x": "hi", "n": 5}}
        out = render_deep(
            {"a": "{{nodes.x}}", "b": [{"c": "{{nodes.n}}"}]},
            ctx,
        )
        assert out == {"a": "hi", "b": [{"c": "5"}]}


# ───────────────────────────────────────────────────────────────────────
#  TestValidate
# ───────────────────────────────────────────────────────────────────────

class TestValidate:
    def _wf(self, **node_overrides) -> dict:
        node = {
            "id": "n",
            "goal": "do x",
            "steps": ["one"],
            "run": {"tool": "scripts/x.sh"},
        }
        node.update(node_overrides)
        return {"workflow": "t", "version": "1.0", "nodes": [node]}

    def test_minimal_valid(self):
        # Without project_root: skips skill/tool resolution.
        assert validate_workflow(self._wf()) == []

    def test_missing_id(self):
        wf = self._wf()
        del wf["nodes"][0]["id"]
        errs = validate_workflow(wf)
        assert any("missing" in e and "id" in e.lower() for e in errs)

    def test_missing_goal(self):
        wf = self._wf()
        del wf["nodes"][0]["goal"]
        errs = validate_workflow(wf)
        assert any("goal" in e for e in errs)

    def test_empty_steps_rejected(self):
        wf = self._wf(steps=[])
        errs = validate_workflow(wf)
        assert any("steps" in e for e in errs)

    def test_run_must_have_skill_or_tool(self):
        wf = self._wf(run={})
        errs = validate_workflow(wf)
        assert any("skill" in e and "tool" in e for e in errs)

    def test_run_skill_xor_tool(self):
        wf = self._wf(run={"skill": "x", "tool": "y"})
        errs = validate_workflow(wf)
        assert any("exactly one" in e for e in errs)

    def test_verify_criterion_xor_command(self):
        wf = self._wf(verify={"criterion": "x", "command": "y"})
        errs = validate_workflow(wf)
        assert any("at most one" in e for e in errs)

    def test_unknown_needs(self):
        wf = self._wf(needs=["nonexistent"])
        errs = validate_workflow(wf)
        assert any("unknown node 'nonexistent'" in e for e in errs)

    def test_cycle_detected(self):
        wf = {
            "workflow": "c", "version": "1.0",
            "nodes": [
                {"id": "a", "goal": "x", "steps": ["s"],
                 "needs": ["b"], "run": {"tool": "x.sh"}},
                {"id": "b", "goal": "y", "steps": ["s"],
                 "needs": ["a"], "run": {"tool": "y.sh"}},
            ],
        }
        errs = validate_workflow(wf)
        assert any("cycle" in e for e in errs)

    def test_bad_schema_type(self):
        wf = self._wf(output_schema={"x": "blob"})
        errs = validate_workflow(wf)
        assert any("output_schema" in e and "blob" in e for e in errs)

    def test_skill_must_exist(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        wf = self._wf(run={"skill": "nonexistent_skill"})
        errs = validate_workflow(wf, project_root=proj)
        assert any("'nonexistent_skill' not found" in e for e in errs)

    def test_tool_must_be_executable(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Write file but DON'T chmod +x
        (scripts / "noexec.sh").write_text("#!/bin/bash\necho hi")
        wf = self._wf(run={"tool": "scripts/noexec.sh"})
        errs = validate_workflow(wf, project_root=proj)
        assert any("not found or not executable" in e for e in errs)

    def test_parse_yaml_strips_fences(self):
        text = "```yaml\nworkflow: t\nversion: '1.0'\nnodes:\n  - id: n\n    goal: x\n    steps: [a]\n    run: {tool: x.sh}\n```"
        wf = parse_workflow_yaml(text)
        assert wf["workflow"] == "t"

    def test_parse_yaml_empty_raises(self):
        with pytest.raises(WorkflowParseError, match="empty"):
            parse_workflow_yaml("")


# ───────────────────────────────────────────────────────────────────────
#  TestNode
# ───────────────────────────────────────────────────────────────────────

class TestNode:
    def test_from_dict_minimal(self):
        n = Node.from_dict({
            "id": "x",
            "goal": "do",
            "steps": ["a"],
            "run": {"tool": "scripts/x.sh"},
        })
        assert n.id == "x"
        assert n.goal == "do"
        assert n.steps == ["a"]
        assert n.needs == []
        assert n.run_config == {"tool": "scripts/x.sh"}
        assert n.verify_config is None
        assert n.retry_max == 1
        assert n.lifecycle == "waiting"
        assert n.result is None
        assert n.retry_count == 0

    def test_from_dict_full(self):
        n = Node.from_dict({
            "id": "y",
            "goal": "do",
            "steps": ["a", "b"],
            "needs": ["upstream"],
            "run": {"skill": "analyzer"},
            "output_schema": {"f": "string"},
            "verify": {"command": "test 1"},
            "retry": 3,
        })
        assert n.run_config == {"skill": "analyzer"}
        assert n.output_schema == {"f": "string"}
        assert n.verify_config == {"command": "test 1"}
        assert n.retry_max == 3
        assert n.needs == ["upstream"]

    def test_is_ready_no_needs(self):
        n = Node.from_dict({"id": "x", "goal": "g", "steps": ["s"],
                            "run": {"tool": "x.sh"}})
        assert n.is_ready({"x": n}) is True

    def test_is_ready_dep_unfinished(self):
        a = Node.from_dict({"id": "a", "goal": "g", "steps": ["s"],
                            "run": {"tool": "x.sh"}})
        b = Node.from_dict({"id": "b", "goal": "g", "steps": ["s"],
                            "needs": ["a"], "run": {"tool": "x.sh"}})
        assert b.is_ready({"a": a, "b": b}) is False
        # Mark a done+success → b ready
        a.lifecycle = "done"
        a.result = "success"
        assert b.is_ready({"a": a, "b": b}) is True

    def test_is_ready_dep_failed(self):
        a = Node.from_dict({"id": "a", "goal": "g", "steps": ["s"],
                            "run": {"tool": "x.sh"}})
        b = Node.from_dict({"id": "b", "goal": "g", "steps": ["s"],
                            "needs": ["a"], "run": {"tool": "x.sh"}})
        a.lifecycle = "done"
        a.result = "fail"
        assert b.is_ready({"a": a, "b": b}) is False  # dep failed → never ready


# ───────────────────────────────────────────────────────────────────────
#  TestEnvelope
# ───────────────────────────────────────────────────────────────────────

class TestEnvelope:
    def test_empty_default_fail(self):
        env = empty_envelope()
        assert env["status"] == "fail"
        assert env["data"] == {}
        assert env["error"] is None
        assert env["feedback"] is None
        assert env["request_human"] is False

    def test_normalize_valid(self):
        env = normalize_envelope({
            "status": "success",
            "data": {"x": 1},
        })
        assert env["status"] == "success"
        assert env["data"] == {"x": 1}

    def test_normalize_bad_status(self):
        env = normalize_envelope({"status": "ok", "data": {}})
        assert env["status"] == "fail"
        assert env["error"]["code"] == "BAD_STATUS"

    def test_normalize_unknown_status(self):
        env = normalize_envelope({"status": "completed", "data": {}})
        assert env["status"] == "fail"
        assert "completed" in env["error"]["message"]


# ───────────────────────────────────────────────────────────────────────
#  TestVerify
# ───────────────────────────────────────────────────────────────────────

class TestVerify:
    def test_schema_pass(self):
        ok, _ = auto_schema_check(
            {"data": {"x": "hi", "n": 5}},
            {"x": "string", "n": "integer"},
        )
        assert ok is True

    def test_schema_missing_field(self):
        ok, reason = auto_schema_check(
            {"data": {"x": "hi"}},
            {"x": "string", "n": "integer"},
        )
        assert ok is False
        assert "missing" in reason and "n" in reason

    def test_schema_wrong_type(self):
        ok, reason = auto_schema_check(
            {"data": {"n": "not-an-int"}},
            {"n": "integer"},
        )
        assert ok is False
        assert "integer" in reason

    def test_schema_boolean_not_int(self):
        # bool MUST NOT match integer (defensive — bool is subclass of int in py)
        ok, reason = auto_schema_check(
            {"data": {"n": True}},
            {"n": "integer"},
        )
        assert ok is False

    def test_schema_empty_no_check(self):
        ok, _ = auto_schema_check({"data": {"anything": 1}}, {})
        assert ok is True

    def test_command_zero_exit_passes(self, tmp_path):
        # Build a minimal Run + Node
        wf = {
            "workflow": "vc", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/x.sh"},
            }],
        }
        run = Workflow(wf, tmp_path)
        try:
            node = run.nodes_by_id["n"]
            ok, _ = verify_with_command("true", run, node, {"data": {}}, 1)
            assert ok is True
        finally:
            run.cleanup()

    def test_command_nonzero_exit_fails(self, tmp_path):
        wf = {
            "workflow": "vc", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/x.sh"},
            }],
        }
        run = Workflow(wf, tmp_path)
        try:
            node = run.nodes_by_id["n"]
            ok, reason = verify_with_command("false", run, node, {"data": {}}, 1)
            assert ok is False
            assert "exited" in reason
        finally:
            run.cleanup()

    def test_command_reads_agent_output_json(self, tmp_path):
        """verify_with_command writes envelope to attempt-dir before
        running cmd, so cmd can read it."""
        wf = {
            "workflow": "vc", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/x.sh"},
            }],
        }
        run = Workflow(wf, tmp_path)
        try:
            node = run.nodes_by_id["n"]
            envelope = {"status": "success", "data": {"x": 42}}
            cmd = ("python3 -c \"import json; "
                   "exit(0 if json.load(open('agent_output.json'))['data']['x']==42 else 1)\"")
            ok, _ = verify_with_command(cmd, run, node, envelope, 1)
            assert ok is True
        finally:
            run.cleanup()


# ───────────────────────────────────────────────────────────────────────
#  TestVerifyHuman — stdin Q&A, "approve" gate
# ───────────────────────────────────────────────────────────────────────

class TestVerifyHuman:
    def _node(self):
        return Node.from_dict({
            "id": "n",
            "goal": "g",
            "steps": ["s"],
            "run": {"tool": "x.sh"},
            "verify": {"human": "Looks good?"},
        })

    def test_no_tty_rejects(self, monkeypatch):
        # When stdin is not a TTY, must reject with stable feedback.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        ok, fb = verify_with_human(self._node(), {"data": {}}, "Looks good?")
        assert ok is False
        assert "no TTY" in fb

    def test_approve_passes(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "approve")
        ok, fb = verify_with_human(self._node(), {"data": {"x": 1}},
                                   "Approve?")
        assert ok is True

    def test_approve_case_insensitive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "  APPROVE  ")
        ok, _ = verify_with_human(self._node(), {"data": {}}, "?")
        assert ok is True

    def test_anything_else_rejects_with_feedback(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input",
                            lambda _: "make it more concise")
        ok, fb = verify_with_human(self._node(), {"data": {}}, "?")
        assert ok is False
        assert fb == "make it more concise"

    def test_validator_accepts_human(self):
        wf = {
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
                "verify": {"human": "Looks good?"},
            }],
        }
        errs = validate_workflow(wf)
        assert all("verify" not in e for e in errs)

    def test_validator_rejects_two_verify_types(self):
        wf = {
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
                "verify": {"human": "ok?", "command": "true"},
            }],
        }
        errs = validate_workflow(wf)
        assert any("at most one" in e for e in errs)


# ───────────────────────────────────────────────────────────────────────
#  TestPrompt — workflow.context injection, run + verify prompts
# ───────────────────────────────────────────────────────────────────────

class TestPrompt:
    def _node(self):
        return Node.from_dict({
            "id": "n",
            "goal": "do the thing",
            "steps": ["s1", "s2"],
            "run": {"tool": "x.sh"},
        })

    def test_run_prompt_no_context(self):
        out = build_run_prompt(self._node(), {"k": "v"})
        assert "# Workflow Context" not in out
        assert "# Goal\ndo the thing" in out

    def test_run_prompt_with_context(self):
        out = build_run_prompt(self._node(), {"k": "v"},
                               workflow_context="ip=peregrine5d1\ntree=prgn5d1")
        assert "# Workflow Context\nip=peregrine5d1\ntree=prgn5d1" in out
        # context must come before goal
        assert out.index("# Workflow Context") < out.index("# Goal")

    def test_run_prompt_blank_context_skipped(self):
        out = build_run_prompt(self._node(), {}, workflow_context="   \n  ")
        assert "# Workflow Context" not in out

    def test_verify_prompt_with_context(self):
        out = build_verify_prompt(self._node(), {"status": "success"},
                                  workflow_context="shared facts")
        assert "# Workflow Context\nshared facts" in out

    def test_verify_prompt_has_evidence_protocol(self):
        """No-hollow-approve guard: verify prompt must require concrete
        evidence per step, listing acceptable + unacceptable forms."""
        out = build_verify_prompt(self._node(), {"status": "success"})
        # The protocol heading is present
        assert "Evidence protocol" in out
        # Acceptable-evidence and not-acceptable-evidence sections both there
        assert "Acceptable evidence" in out
        assert "NOT acceptable evidence" in out
        # The output schema includes the `evidence` field
        assert '"evidence"' in out
        # Specifically, the prompt rejects vague approve language
        assert "looks correct" in out.lower() or "vibes" in out.lower()


# ───────────────────────────────────────────────────────────────────────
#  TestCamcLib — timeout semantics
# ───────────────────────────────────────────────────────────────────────

class TestCamcLib:
    def test_default_skill_timeout_is_none(self):
        # No env var set → wait forever (None)
        assert camc_lib._parse_timeout_env("CAMFLOW_NONEXISTENT_KEY_XYZ") is None

    def test_parse_timeout_zero_is_none(self, monkeypatch):
        monkeypatch.setenv("CAMFLOW_TEST_TIMEOUT", "0")
        assert camc_lib._parse_timeout_env("CAMFLOW_TEST_TIMEOUT") is None

    def test_parse_timeout_positive(self, monkeypatch):
        monkeypatch.setenv("CAMFLOW_TEST_TIMEOUT", "120")
        assert camc_lib._parse_timeout_env("CAMFLOW_TEST_TIMEOUT") == 120

    def test_parse_timeout_garbage_is_none(self, monkeypatch):
        monkeypatch.setenv("CAMFLOW_TEST_TIMEOUT", "abc")
        assert camc_lib._parse_timeout_env("CAMFLOW_TEST_TIMEOUT") is None

    def test_wait_for_file_finds_existing(self, tmp_path):
        (tmp_path / "out.json").write_text('{"ok":true}')
        # timeout_s=None, but file already exists → returns immediately
        p = camc_lib.wait_for_file(tmp_path, "out.json", timeout_s=1)
        assert p == tmp_path / "out.json"

    def test_wait_for_file_times_out(self, tmp_path):
        with pytest.raises(camc_lib.CamcTimeout):
            camc_lib.wait_for_file(tmp_path, "missing.json", timeout_s=1)


# ───────────────────────────────────────────────────────────────────────
#  TestValidateContext — workflow.context type
# ───────────────────────────────────────────────────────────────────────

class TestValidateContext:
    def test_context_string_ok(self):
        wf = {
            "context": "shared facts",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
            }],
        }
        # may have skill/tool errors when project_root passed; we only
        # care that context-on-string doesn't raise
        errs = validate_workflow(wf)
        assert "workflow.context" not in " ".join(errs)

    def test_context_non_string_rejected(self):
        wf = {
            "context": {"a": "b"},  # dict, not string
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
            }],
        }
        errs = validate_workflow(wf)
        assert any("workflow.context: must be a string" in e for e in errs)


# ───────────────────────────────────────────────────────────────────────
#  TestE2E — ★ END-TO-END test ★
# ───────────────────────────────────────────────────────────────────────

class TestE2E:
    """Full pipeline: load YAML → validate → execute multi-node DAG with
    tool executors → check envelope outputs + trace + archive.

    Uses tool nodes only (no LLM cost). Tests:
      * 3-node DAG ordering
      * Output passing between nodes via {{nodes.X.output.data.Y}} templates
      * Schema validation
      * Verify-command gating
      * Auto-archive on rerun
      * Halt on retry exhaustion
    """

    def _setup_project(self, tmp_path: Path) -> Path:
        """Build a project dir with 3 tools."""
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)

        # Tool 1: produce a fixed root_cause + confidence.
        make_executable_tool(
            scripts / "diagnose.sh",
            envelope_tool_body({
                "root_cause": "null deref at line 42",
                "confidence": 0.9,
            }),
        )

        # Tool 2: read upstream diagnose's root_cause from input, return a patch.
        # Runtime auto-injects upstream envelopes under input.upstream.<id>.
        make_executable_tool(
            scripts / "fix.sh",
            r"""
input_json=$(cat)
cause=$(echo "$input_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['upstream']['diagnose']['data']['root_cause'])")
cat <<EOF
{"status":"success","data":{"patch":"FIXED: $cause","files_changed":["foo.py"]},"error":null,"feedback":null,"request_human":false}
EOF
""",
        )

        # Tool 3: deterministic pass — always returns passed=true.
        make_executable_tool(
            scripts / "test.sh",
            envelope_tool_body({"passed": True, "tests_run": 5}),
        )

        return proj

    def _three_node_workflow(self) -> dict:
        return {
            "workflow": "e2e_demo",
            "version": "1.0",
            "nodes": [
                {
                    "id": "diagnose",
                    "goal": "Find the root cause",
                    "steps": ["read bug", "extract cause"],
                    "run": {"tool": "scripts/diagnose.sh"},
                    "output_schema": {
                        "root_cause": "string",
                        "confidence": "number",
                    },
                    "verify": {
                        "command": ("python3 -c \"import json; "
                                    "exit(0 if json.load(open('agent_output.json'))"
                                    "['data']['confidence'] >= 0.5 else 1)\""),
                    },
                },
                {
                    "id": "fix",
                    "goal": "Write a patch addressing the cause",
                    "steps": ["read cause", "write patch"],
                    "needs": ["diagnose"],
                    "run": {"tool": "scripts/fix.sh"},
                    "output_schema": {
                        "patch": "string",
                        "files_changed": "array",
                    },
                    "verify": {"command": "true"},
                },
                {
                    "id": "test",
                    "goal": "Run tests",
                    "steps": ["execute tests", "report"],
                    "needs": ["fix"],
                    "run": {"tool": "scripts/test.sh"},
                    "output_schema": {
                        "passed": "boolean",
                        "tests_run": "integer",
                    },
                    "verify": {
                        "command": ("python3 -c \"import json; "
                                    "exit(0 if json.load(open('agent_output.json'))"
                                    "['data']['passed'] else 1)\""),
                    },
                },
            ],
        }

    # ── test 1: full happy path through 3-node DAG ─────────────────────
    def test_three_node_dag_completes(self, tmp_path):
        proj = self._setup_project(tmp_path)
        wf = self._three_node_workflow()

        # validate (with project_root → checks tool existence)
        errors = validate_workflow(wf, project_root=proj)
        assert errors == [], f"validation errors: {errors}"

        run_dir = proj / ".camflow" / "run"
        result = run_workflow(wf, run_dir)
        assert result == "done", f"expected 'done', got {result!r}"

        # All 3 nodes succeeded
        for nid in ["diagnose", "fix", "test"]:
            out = json.loads(
                (run_dir / "nodes" / nid / "attempt-1" / "output.json").read_text()
            )
            assert out["status"] == "success", f"{nid} status={out['status']}"

        # Verify upstream auto-injection: fix's input.json carries the
        # full diagnose envelope under `upstream.diagnose`.
        fix_input = json.loads(
            (run_dir / "nodes" / "fix" / "attempt-1" / "input.json").read_text()
        )
        assert fix_input["upstream"]["diagnose"]["data"]["root_cause"] \
            == "null deref at line 42"

        # Verify final envelope of test node
        test_out = json.loads(
            (run_dir / "nodes" / "test" / "attempt-1" / "output.json").read_text()
        )
        assert test_out["data"]["passed"] is True
        assert test_out["data"]["tests_run"] == 5

        # Trace contains all the right events in order
        events = [
            json.loads(line)
            for line in (run_dir / "trace.jsonl").read_text().splitlines()
        ]
        kinds = [e["event"] for e in events]
        assert kinds[0] == "workflow_started"
        assert kinds[-1] == "workflow_completed"
        # Each node went through node_started → verify_started → verify_completed → node_completed
        for nid in ["diagnose", "fix", "test"]:
            assert any(e["event"] == "node_started" and e["node"] == nid for e in events)
            assert any(e["event"] == "node_completed" and e["node"] == nid for e in events)

        # runner.pid was cleaned up
        assert not (run_dir / "runner.pid").exists()

    # ── test 2: rerun auto-archives the prior run ──────────────────────
    def test_rerun_archives_prior(self, tmp_path):
        from runner.runtime import default_run_dir
        proj = self._setup_project(tmp_path)
        wf = self._three_node_workflow()

        # First run
        rd = default_run_dir(proj)
        assert run_workflow(wf, rd) == "done"
        # Second run (default_run_dir auto-archives)
        rd2 = default_run_dir(proj)
        assert run_workflow(wf, rd2) == "done"

        archives = list((proj / ".camflow" / "archives").iterdir())
        assert len(archives) >= 1
        # Archive name contains "success"
        assert any("success" in a.name for a in archives)

    # ── test 3: verify-command gates correctly ─────────────────────────
    def test_verify_command_failure_halts(self, tmp_path):
        proj = self._setup_project(tmp_path)
        # Override test.sh to always return passed=false
        make_executable_tool(
            proj / "scripts" / "test.sh",
            envelope_tool_body({"passed": False, "tests_run": 5}),
        )
        wf = self._three_node_workflow()
        result = run_workflow(wf, proj / ".camflow" / "run")
        assert result == "halted"
        # halt.json written
        halt = json.loads((proj / ".camflow" / "run" / "halt.json").read_text())
        assert halt["halted_node"] == "test"

    # ── test 4: retry-then-success ─────────────────────────────────────
    def test_retry_succeeds_on_second_attempt(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)

        # Tool that fails on first attempt (sentinel file approach)
        sentinel = proj / ".attempt_count"
        sentinel.write_text("0")
        make_executable_tool(
            scripts / "flaky.sh",
            f"""
n=$(cat {sentinel})
n=$((n+1))
echo "$n" > {sentinel}
if [ "$n" -lt 2 ]; then
  cat <<EOF
{{"status":"fail","data":{{}},"error":{{"code":"FLAKY","message":"first attempt fails"}},"feedback":null,"request_human":false}}
EOF
else
  cat <<EOF
{{"status":"success","data":{{"x":42}},"error":null,"feedback":null,"request_human":false}}
EOF
fi
""",
        )

        wf = {
            "workflow": "retry_demo", "version": "1.0",
            "nodes": [{
                "id": "n",
                "goal": "succeed eventually",
                "steps": ["try"],
                "run": {"tool": "scripts/flaky.sh"},
                "output_schema": {"x": "integer"},
                "verify": {"command": "true"},
                "retry": 3,
            }],
        }
        result = run_workflow(wf, proj / ".camflow" / "run")
        assert result == "done"
        # Should have 2 attempt directories
        attempts = sorted((proj / ".camflow" / "run" / "nodes" / "n").iterdir())
        assert len(attempts) == 2

    # ── test 5: retry exhausted → halt ─────────────────────────────────
    def test_retry_exhausted_halts(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Always-fails tool
        make_executable_tool(
            scripts / "always_fails.sh",
            envelope_tool_body({}, status="fail")
            .replace('"status":"fail","data":{}',
                     '"status":"fail","data":{},"error":{"code":"X","message":"always fails"}'),
        )

        wf = {
            "workflow": "exhaust", "version": "1.0",
            "nodes": [{
                "id": "n",
                "goal": "always fail",
                "steps": ["try"],
                "run": {"tool": "scripts/always_fails.sh"},
                "retry": 2,
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "halted"
        events = [json.loads(line)
                  for line in (rd / "trace.jsonl").read_text().splitlines()]
        assert any(e["event"] == "retry_exhausted" for e in events)
        assert any(e["event"] == "workflow_halted" for e in events)

    # ── test 6: request_human halts immediately, skipping retry ────────
    def test_request_human_halts(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Tool requesting human help
        make_executable_tool(
            scripts / "needs_help.sh",
            (
                'cat <<EOF\n'
                '{"status":"fail","data":{},'
                '"error":{"code":"NEED_HUMAN","message":"unclear input"},'
                '"feedback":null,"request_human":true}\n'
                'EOF\n'
            ),
        )

        wf = {
            "workflow": "human", "version": "1.0",
            "nodes": [{
                "id": "n",
                "goal": "ask human",
                "steps": ["check"],
                "run": {"tool": "scripts/needs_help.sh"},
                "retry": 5,    # plenty of retries, but request_human bypasses
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "halted"
        # Only 1 attempt — request_human skipped retry
        attempts = list((rd / "nodes" / "n").iterdir())
        assert len(attempts) == 1

    def test_explicit_oracle_halt_skips_node_retry(self, tmp_path):
        """An explicit replan/halt envelope is not an ordinary failure.

        Even with retry budget available, it must reach workflow halt so
        manual or opt-in auto-replan can create a new DAG revision.
        """
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        make_executable_tool(
            scripts / "submit.sh",
            (
                'cat <<EOF\n'
                '{"status":"fail",'
                '"data":{"halt":true,"replan_required":true},'
                '"error":{"code":"ORACLE_HALT",'
                '"message":"controlled first-submit halt"},'
                '"feedback":"replan at dag_revision >= 2",'
                '"request_human":false}\n'
                'EOF\n'
            ),
        )

        wf = {
            "workflow": "oracle_halt", "version": "1.1",
            "nodes": [{
                "id": "submit",
                "goal": "submit path",
                "steps": ["submit"],
                "run": {"tool": "scripts/submit.sh"},
                "retry": 3,
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "halted"
        attempts = sorted((rd / "nodes" / "submit").iterdir())
        assert [p.name for p in attempts] == ["attempt-1"]

        events = [json.loads(line)
                  for line in (rd / "trace.jsonl").read_text().splitlines()]
        assert any(e["event"] == "explicit_halt_requested" for e in events)
        assert not any(e["event"] == "retry_triggered" for e in events)
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["halted_node"] == "submit"
        assert halt["envelope"]["error"]["code"] == "ORACLE_HALT"

    def test_recoverable_phrase_hint_uses_retry_previous(self, tmp_path):
        """Non-halt oracle feedback still uses normal bounded retry.

        Attempt 1 returns a phrase_hint. Attempt 2 reads it from
        input.previous.data.phrase_hint and succeeds.
        """
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        make_executable_tool(
            scripts / "submit.sh",
            r'''
input_json=$(cat)
phrase=$(INPUT_JSON="$input_json" python3 -c 'import json, os
d = json.loads(os.environ["INPUT_JSON"])
print(((d.get("previous") or {}).get("data") or {}).get("phrase_hint", ""))')
if [ "$phrase" = "CAMFLOW-OPEN" ]; then
  cat <<EOF
{"status":"success","data":{"solved":true,"phrase":"$phrase"},"error":null,"feedback":null,"request_human":false}
EOF
else
  cat <<EOF
{"status":"fail","data":{"phrase_hint":"CAMFLOW-OPEN"},"error":{"code":"PHRASE_REQUIRED","message":"need phrase"},"feedback":"Use phrase_hint","request_human":false}
EOF
fi
''',
        )

        wf = {
            "workflow": "phrase_retry", "version": "1.1",
            "nodes": [{
                "id": "submit",
                "goal": "submit path with optional phrase",
                "steps": ["submit"],
                "run": {"tool": "scripts/submit.sh"},
                "output_schema": {"solved": "boolean", "phrase": "string"},
                "verify": {"command": "true"},
                "retry": 1,
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "done"
        attempt2_input = json.loads(
            (rd / "nodes" / "submit" / "attempt-2" / "input.json").read_text())
        assert attempt2_input["previous"]["data"]["phrase_hint"] == "CAMFLOW-OPEN"
        attempt2_output = json.loads(
            (rd / "nodes" / "submit" / "attempt-2" / "output.json").read_text())
        assert attempt2_output["data"]["phrase"] == "CAMFLOW-OPEN"


class TestOracleMazeWrappers:
    def test_maze_submit_uses_previous_phrase_hint(self, tmp_path):
        """The real maze_submit wrapper must forward retry phrase_hint."""
        repo = Path(__file__).resolve().parents[1]
        wrapper = repo / "examples" / "oracle-maze" / "scripts" / "maze_submit.sh"
        fake_bin = tmp_path / "bin"
        make_executable_tool(
            fake_bin / "curl",
            r'''
body=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-d" ]; then
    shift
    body="$1"
  fi
  shift || true
done
python3 - "$body" <<'PY'
import json
import sys

request = json.loads(sys.argv[1])
args = request["args"]
if args.get("phrase") == "CAMFLOW-OPEN":
    print(json.dumps({
        "ok": True,
        "solved": True,
        "used_phrase": args["phrase"],
    }))
else:
    print(json.dumps({
        "ok": False,
        "solved": False,
        "phrase_hint": "CAMFLOW-OPEN",
        "feedback": "need phrase",
    }))
PY
''',
        )
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["CAMFLOW_ORACLE_BASE_URL"] = "http://fake-oracle"
        env["CAMFLOW_ORACLE_SESSION_ID"] = "session"
        env["CAMFLOW_DAG_REVISION"] = "2"
        input_json = {
            "upstream": {"infer": {"data": {"path": ["N", "E"]}}},
            "previous": {"data": {"phrase_hint": "CAMFLOW-OPEN"}},
            "dag_revision": 2,
        }
        cp = subprocess.run(
            [str(wrapper)],
            input=json.dumps(input_json),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repo,
            env=env,
            check=True,
        )
        envelope = json.loads(cp.stdout)
        assert envelope["status"] == "success", cp.stderr
        assert envelope["data"]["used_phrase"] == "CAMFLOW-OPEN"


# ───────────────────────────────────────────────────────────────────────
#  TestStepping — --steps N debug breakpoints
# ───────────────────────────────────────────────────────────────────────

class TestStepping:
    """`--steps N` halts cleanly with kind=breakpoint after N attempts;
    `camflow resume` continues without resetting node state or bumping
    retry_max (since the node didn't actually fail)."""

    def _setup_three_node_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        for i, name in enumerate(["a", "b", "c"], start=1):
            make_executable_tool(
                scripts / f"{name}.sh",
                envelope_tool_body({"step": i}),
            )
        return proj

    def _three_seq_workflow(self) -> dict:
        return {
            "workflow": "step_demo", "version": "1.0",
            "nodes": [
                {"id": "a", "goal": "first", "steps": ["s"],
                 "run": {"tool": "scripts/a.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
                {"id": "b", "goal": "second", "steps": ["s"],
                 "needs": ["a"],
                 "run": {"tool": "scripts/b.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
                {"id": "c", "goal": "third", "steps": ["s"],
                 "needs": ["b"],
                 "run": {"tool": "scripts/c.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
            ],
        }

    def test_steps_halts_cleanly(self, tmp_path):
        from runner.runtime import run_workflow
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd, max_attempts=1)
        assert result == "halted"

        halt = json.loads((rd / "halt.json").read_text())
        assert halt["kind"] == "breakpoint"
        assert halt["halted_node"] == "a"
        assert "step limit" in halt["reason"]

        # Only the first node ran
        assert (rd / "nodes" / "a" / "attempt-1" / "output.json").exists()
        assert not (rd / "nodes" / "b").exists()
        assert not (rd / "nodes" / "c").exists()

    def test_steps_propagate_fail_false(self, tmp_path):
        """Step halt must NOT mark downstream nodes as done+fail."""
        from runner.runtime import Workflow, run_workflow
        proj = self._setup_three_node_proj(tmp_path)
        wf_dict = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        run_workflow(wf_dict, rd, max_attempts=1)

        # Re-load to inspect what got persisted: downstream nodes
        # should have NO attempt directories (step halt didn't fake-fail
        # them like a real halt would).
        assert not (rd / "nodes" / "b").exists()
        assert not (rd / "nodes" / "c").exists()

    def test_resume_after_step_halt_continues(self, tmp_path, monkeypatch):
        """After --steps halt, resume runs to completion."""
        from runner.runtime import run_workflow, _cmd_resume
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd, max_attempts=1)
        assert (rd / "halt.json").exists()

        # Now resume — should run the remaining 2 nodes
        rc = _cmd_resume([str(rd)])
        assert rc == 0
        for nid in ("a", "b", "c"):
            assert (rd / "nodes" / nid / "attempt-1" / "output.json").exists()
        assert not (rd / "halt.json").exists()  # cleared

    def test_resume_with_more_steps(self, tmp_path):
        """Resume --steps 1 advances exactly 1 more node, then halts again."""
        from runner.runtime import run_workflow, _cmd_resume
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"

        # Step 1: run node a
        run_workflow(wf, rd, max_attempts=1)
        # Step 2: resume, advance node b only
        rc = _cmd_resume([str(rd), "--steps", "1"])
        assert rc == 2  # halted (step limit)
        assert (rd / "halt.json").exists()
        assert (rd / "nodes" / "b" / "attempt-1" / "output.json").exists()
        assert not (rd / "nodes" / "c").exists()
        # Step 3: resume to end
        rc = _cmd_resume([str(rd)])
        assert rc == 0
        assert (rd / "nodes" / "c" / "attempt-1" / "output.json").exists()

    def test_steps_count_includes_retries(self, tmp_path):
        """--steps counts attempts, not nodes — retries also tick the counter."""
        from runner.runtime import run_workflow
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Tool that always fails — will retry up to retry_max
        make_executable_tool(
            scripts / "flaky.sh",
            'echo \'{"status":"fail","data":{},'
            '"error":{"code":"E","message":"nope"},'
            '"feedback":null,"request_human":false}\'\n',
        )
        wf = {
            "workflow": "retries", "version": "1.0",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/flaky.sh"},
                "retry": 5,  # plenty of retry budget
            }],
        }
        rd = proj / ".camflow" / "run"
        # Halt after 2 attempts. The node fails twice (retry 1) then we step-halt.
        result = run_workflow(wf, rd, max_attempts=2)
        assert result == "halted"
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["kind"] == "breakpoint"
        # 2 attempts on disk
        attempts = sorted((rd / "nodes" / "x").iterdir())
        assert len(attempts) == 2


# ───────────────────────────────────────────────────────────────────────
#  TestRerun — `camflow rerun <run_dir> <node>` semantics
# ───────────────────────────────────────────────────────────────────────

class TestRerun:
    """`rerun` re-executes a specific node + every downstream descendant.
    Upstream nodes stay as they were (their outputs are preserved)."""

    def _setup_three_node_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        for i, name in enumerate(["a", "b", "c"], start=1):
            make_executable_tool(
                scripts / f"{name}.sh",
                envelope_tool_body({"step": i}),
            )
        return proj

    def _three_seq_workflow(self) -> dict:
        return {
            "workflow": "rerun_demo", "version": "1.0",
            "nodes": [
                {"id": "a", "goal": "first", "steps": ["s"],
                 "run": {"tool": "scripts/a.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
                {"id": "b", "goal": "second", "steps": ["s"],
                 "needs": ["a"],
                 "run": {"tool": "scripts/b.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
                {"id": "c", "goal": "third", "steps": ["s"],
                 "needs": ["b"],
                 "run": {"tool": "scripts/c.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
            ],
        }

    def test_rerun_resets_target_and_downstream(self, tmp_path):
        from runner.runtime import run_workflow, _cmd_rerun
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        # First: full run completes
        assert run_workflow(wf, rd) == "done"
        for nid in ("a", "b", "c"):
            assert (rd / "nodes" / nid / "attempt-1" / "output.json").exists()

        # Now rerun b — b and c should re-execute (attempt-2/), a stays at attempt-1/
        rc = _cmd_rerun([str(rd), "b"])
        assert rc == 0
        # a: still only attempt-1/ (not re-run)
        assert sorted(p.name for p in (rd / "nodes" / "a").iterdir()) == ["attempt-1"]
        # b and c: now have attempt-2/
        assert (rd / "nodes" / "b" / "attempt-2" / "output.json").exists()
        assert (rd / "nodes" / "c" / "attempt-2" / "output.json").exists()

    def test_rerun_target_only_when_no_downstream(self, tmp_path):
        from runner.runtime import run_workflow, _cmd_rerun
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd)

        # Rerun c (leaf node, no downstream) — only c re-runs
        _cmd_rerun([str(rd), "c"])
        assert sorted(p.name for p in (rd / "nodes" / "a").iterdir()) == ["attempt-1"]
        assert sorted(p.name for p in (rd / "nodes" / "b").iterdir()) == ["attempt-1"]
        assert (rd / "nodes" / "c" / "attempt-2" / "output.json").exists()

    def test_rerun_unknown_node_errors(self, tmp_path, capsys):
        from runner.runtime import run_workflow, _cmd_rerun
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd)

        rc = _cmd_rerun([str(rd), "nonexistent"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "not in workflow" in captured.err

    def test_rerun_with_steps(self, tmp_path):
        """rerun + --steps halts cleanly mid-rerun."""
        from runner.runtime import run_workflow, _cmd_rerun
        proj = self._setup_three_node_proj(tmp_path)
        wf = self._three_seq_workflow()
        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd)

        # Rerun b (which would also redo c) but step-halt after 1 attempt
        rc = _cmd_rerun([str(rd), "b", "--steps", "1"])
        assert rc == 2  # halted by step limit
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["kind"] == "breakpoint"
        # b was re-run (attempt-2/), c was NOT yet
        assert (rd / "nodes" / "b" / "attempt-2" / "output.json").exists()
        assert not (rd / "nodes" / "c" / "attempt-2").exists()

    def test_rerun_clears_old_halt(self, tmp_path):
        """Rerunning after a halt clears halt.json before re-executing."""
        from runner.runtime import run_workflow, _cmd_rerun
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Tool that always succeeds — we'll halt via --steps
        make_executable_tool(scripts / "ok.sh", envelope_tool_body({"x": 1}))
        wf = {
            "workflow": "rh", "version": "1.0",
            "nodes": [
                {"id": "p", "goal": "g", "steps": ["s"],
                 "run": {"tool": "scripts/ok.sh"},
                 "output_schema": {"x": "integer"},
                 "verify": {"command": "true"}},
                {"id": "q", "goal": "g", "steps": ["s"],
                 "needs": ["p"],
                 "run": {"tool": "scripts/ok.sh"},
                 "output_schema": {"x": "integer"},
                 "verify": {"command": "true"}},
            ],
        }
        rd = proj / ".camflow" / "run"
        # Step-halt mid-flow → halt.json present
        run_workflow(wf, rd, max_attempts=1)
        assert (rd / "halt.json").exists()
        # Now rerun p — should clear the halt.json and complete
        rc = _cmd_rerun([str(rd), "p"])
        assert rc == 0
        assert not (rd / "halt.json").exists()


# ───────────────────────────────────────────────────────────────────────
#  TestReviewerFixes — two reviewer-flagged regressions
# ───────────────────────────────────────────────────────────────────────

class TestReviewerFixes:
    """1. `camflow run --from <node>` (no --run-dir) used to call
          default_run_dir() which archives the existing run, leaving
          rerun pointed at an empty dir.
       2. `--steps` halting right after a failed attempt with retry
          budget remaining used to restore as done+fail on resume,
          so the scheduler wouldn't re-pick the node."""

    def _setup_seq_proj(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        for i, name in enumerate(["a", "b"], start=1):
            make_executable_tool(
                scripts / (name + ".sh"),
                envelope_tool_body({"step": i}),
            )
        return proj

    def _seq_workflow(self):
        return {
            "workflow": "rev", "version": "1.0",
            "nodes": [
                {"id": "a", "goal": "first", "steps": ["s"],
                 "run": {"tool": "scripts/a.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
                {"id": "b", "goal": "second", "steps": ["s"],
                 "needs": ["a"],
                 "run": {"tool": "scripts/b.sh"},
                 "output_schema": {"step": "integer"},
                 "verify": {"command": "true"}},
            ],
        }

    def test_run_from_default_path_does_not_archive(self, tmp_path,
                                                    monkeypatch):
        """Reviewer fix #1."""
        from runner.runtime import _cmd_run
        proj = self._setup_seq_proj(tmp_path)
        monkeypatch.chdir(proj)
        wf = self._seq_workflow()

        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd)
        assert (rd / "nodes" / "a" / "attempt-1" / "output.json").exists()

        archives_dir = proj / ".camflow" / "archives"
        archives_before = (set(archives_dir.iterdir())
                           if archives_dir.exists() else set())

        rc = _cmd_run(["--from", "a"])
        assert rc == 0

        archives_after = (set(archives_dir.iterdir())
                          if archives_dir.exists() else set())
        assert archives_after == archives_before, \
            "rerun must not archive the existing run"
        assert (rd / "nodes" / "a" / "attempt-1" / "output.json").exists()
        assert (rd / "nodes" / "a" / "attempt-2" / "output.json").exists()

    def test_step_halt_during_retry_resume_continues(self, tmp_path):
        """Reviewer fix #2."""
        from runner.runtime import _cmd_resume
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        sentinel = proj / ".n"
        sentinel.write_text("0")
        # Tool that fails on attempt 1, succeeds on attempt 2.
        # Build the script body in Python to avoid heredoc-in-heredoc.
        flaky_body = (
            'n=$(cat ' + str(sentinel) + ')\n'
            'n=$((n+1))\n'
            'echo "$n" > ' + str(sentinel) + '\n'
            'if [ "$n" -lt 2 ]; then\n'
            '  echo \'{"status":"fail","data":{},'
            '"error":{"code":"E","message":"first fails"},'
            '"feedback":null,"request_human":false}\'\n'
            'else\n'
            '  echo \'{"status":"success","data":{"step":1},'
            '"error":null,"feedback":null,"request_human":false}\'\n'
            'fi\n'
        )
        make_executable_tool(scripts / "flaky.sh", flaky_body)

        wf = {
            "workflow": "rt", "version": "1.0",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/flaky.sh"},
                "output_schema": {"step": "integer"},
                "verify": {"command": "true"},
                "retry": 3,
            }],
        }
        rd = proj / ".camflow" / "run"

        # Step-halt after attempt-1 failed (retry_triggered fired).
        result = run_workflow(wf, rd, max_attempts=1)
        assert result == "halted"
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["kind"] == "breakpoint"
        assert halt["retry_count"] == 1, (
            "retry_count in halt.json should reflect the post-retry-trigger "
            "value (1), not pre (0)"
        )

        att1 = json.loads(
            (rd / "nodes" / "x" / "attempt-1" / "output.json").read_text()
        )
        assert att1["status"] == "fail"

        # Resume — node must be re-picked, attempt-2 must run + succeed.
        rc = _cmd_resume([str(rd)])
        assert rc == 0
        assert (rd / "nodes" / "x" / "attempt-2" / "output.json").exists()
        att2 = json.loads(
            (rd / "nodes" / "x" / "attempt-2" / "output.json").read_text()
        )
        assert att2["status"] == "success"


# ───────────────────────────────────────────────────────────────────────
#  TestCodexPhase1Fixes — findings 1, 2, 3 from code-review-codex-2026-05-04
# ───────────────────────────────────────────────────────────────────────

class TestToolTimeout:
    """Finding 1 (BLOCKER): subprocess.TimeoutExpired in exec_tool must
    NOT propagate; it must produce a TOOL_TIMEOUT fail envelope so the
    scheduler runs the normal retry/halt flow."""

    def test_timeout_returns_fail_envelope(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from runner import runtime as rt

        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Put any executable on disk (we monkeypatch the actual run).
        make_executable_tool(scripts / "slow.sh", "exit 0\n")

        # Patch subprocess.run inside runtime to raise TimeoutExpired
        # only for our tool — verify.command bash invocations pass through.
        original_run = rt.subprocess.run

        def fake_run(cmd, *args, **kw):
            if isinstance(cmd, list) and cmd and "slow.sh" in cmd[0]:
                raise _sp.TimeoutExpired(cmd=cmd,
                                         timeout=kw.get("timeout", 1),
                                         output="partial output\n")
            return original_run(cmd, *args, **kw)

        monkeypatch.setattr(rt.subprocess, "run", fake_run)

        wf = {
            "workflow": "to", "version": "1.0",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/slow.sh"},
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "halted"  # retry: 1 default; halts after retry exhaust
        out = json.loads(
            (rd / "nodes" / "x" / "attempt-1" / "output.json").read_text()
        )
        assert out["status"] == "fail"
        assert out["error"]["code"] == "TOOL_TIMEOUT"
        # Whatever partial stdout existed should have been preserved.
        partial_text = (rd / "nodes" / "x" / "attempt-1"
                        / "raw_stdout.txt").read_text()
        assert "partial" in partial_text


class TestToolPathContainment:
    """Finding 2: tool resolution must enforce <project>/<rel>; reject
    absolute paths, .. traversal, and symlink escapes."""

    def test_absolute_path_rejected(self, tmp_path):
        from runner.assets import _resolve_tool_path
        outside = tmp_path / "outside.sh"
        outside.write_text("#!/bin/sh\necho hi\n")
        outside.chmod(0o755)
        proj = tmp_path / "proj"
        proj.mkdir()
        # Absolute path → rejected, even though the file exists + is -x.
        assert _resolve_tool_path(str(outside.resolve()), proj) is None

    def test_dotdot_escape_rejected(self, tmp_path):
        from runner.assets import _resolve_tool_path
        outside = tmp_path / "outside.sh"
        outside.write_text("#!/bin/sh\necho hi\n")
        outside.chmod(0o755)
        proj = tmp_path / "proj"
        proj.mkdir()
        assert _resolve_tool_path("../outside.sh", proj) is None

    def test_symlink_escape_rejected(self, tmp_path):
        from runner.assets import _resolve_tool_path
        outside = tmp_path / "outside.sh"
        outside.write_text("#!/bin/sh\necho hi\n")
        outside.chmod(0o755)
        proj = tmp_path / "proj"
        proj.mkdir()
        link = proj / "link.sh"
        link.symlink_to(outside)
        # Symlink resolves outside project_root → rejected.
        assert _resolve_tool_path("link.sh", proj) is None

    def test_legitimate_path_accepted(self, tmp_path):
        from runner.assets import _resolve_tool_path
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        ok = scripts / "ok.sh"
        ok.write_text("#!/bin/sh\necho hi\n")
        ok.chmod(0o755)
        resolved = _resolve_tool_path("scripts/ok.sh", proj)
        assert resolved is not None
        assert resolved.name == "ok.sh"


class TestExprStrictSubscript:
    """Finding 3: dict subscript with a missing key must raise ExprError,
    matching the strict semantics of the attribute branch."""

    def test_dict_missing_key_raises(self):
        with pytest.raises(ExprError):
            eval_expr('nodes["missing"]', {"nodes": {}})

    def test_dict_present_key_ok(self):
        assert eval_expr('nodes["x"]', {"nodes": {"x": 7}}) == 7

    def test_render_dict_missing_key_raises(self):
        with pytest.raises(ExprError):
            render_str('val={{nodes["missing"]}}', {"nodes": {}})


# ───────────────────────────────────────────────────────────────────────
#  TestCodexPhase2Fixes — findings 4, 6, 7 from code-review-codex-2026-05-04
# ───────────────────────────────────────────────────────────────────────

class TestToolAgentOutputPersist:
    """Finding 4: every tool attempt writes attempt-N/agent_output.json
    (the producer's literal stdout) alongside raw_stdout.txt and the
    runtime-validated output.json."""

    def test_tool_success_writes_agent_output_json(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        make_executable_tool(
            scripts / "ok.sh",
            envelope_tool_body({"x": 1}),
        )
        wf = {
            "workflow": "p", "version": "1.0",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/ok.sh"},
                "output_schema": {"x": "integer"},
                "verify": {"command": "true"},
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "done"
        att = rd / "nodes" / "x" / "attempt-1"
        assert (att / "agent_output.json").exists()
        # Content should be parseable JSON matching what the tool emitted.
        producer = json.loads((att / "agent_output.json").read_text())
        assert producer["data"]["x"] == 1
        # raw_stdout.txt also kept.
        assert (att / "raw_stdout.txt").exists()
        # output.json is the runtime-validated envelope.
        assert (att / "output.json").exists()

    def test_tool_bad_output_still_persists_agent_output(self, tmp_path):
        """Even when the tool emits non-JSON, agent_output.json holds
        the raw bytes — useful for debugging."""
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        make_executable_tool(
            scripts / "garbage.sh",
            "echo not-json-at-all\n",
        )
        wf = {
            "workflow": "p", "version": "1.0",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/garbage.sh"},
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "halted"
        att = rd / "nodes" / "x" / "attempt-1"
        # agent_output.json holds the raw stdout (non-JSON).
        agent_text = (att / "agent_output.json").read_text()
        assert "not-json-at-all" in agent_text
        # output.json is the BAD_TOOL_OUTPUT fail envelope.
        out = json.loads((att / "output.json").read_text())
        assert out["status"] == "fail"
        assert out["error"]["code"] == "TOOL_BAD_OUTPUT"


class TestValidateTightenings:
    """Finding 6: validate_workflow rejects malformed schemas."""

    def _wf(self, **node_overrides):
        return {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
                **node_overrides,
            }],
        }

    def test_unsafe_node_id_rejected(self):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "../escape", "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
            }],
        }
        errs = validate_workflow(wf)
        assert any("filesystem-safe" in e for e in errs)

    def test_non_string_node_id_rejected(self):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": 42, "goal": "g", "steps": ["s"],
                "run": {"tool": "x.sh"},
            }],
        }
        errs = validate_workflow(wf)
        assert any("non-string 'id'" in e for e in errs)

    def test_non_string_goal_rejected(self):
        wf = self._wf(goal=42)
        errs = validate_workflow(wf)
        assert any("goal: must be a non-empty string" in e for e in errs)

    def test_steps_with_non_string_element_rejected(self):
        wf = self._wf(steps=["ok", 7, "ok"])
        errs = validate_workflow(wf)
        assert any("steps[1]: must be a non-empty string" in e for e in errs)

    def test_needs_with_non_string_rejected(self):
        wf = self._wf(needs=[42])
        errs = validate_workflow(wf)
        assert any("needs[0]: must be a string" in e for e in errs)

    def test_retry_negative_rejected(self):
        wf = self._wf(retry=-1)
        errs = validate_workflow(wf)
        assert any("retry: must be a non-negative int" in e for e in errs)

    def test_retry_non_int_rejected(self):
        wf = self._wf(retry="two")
        errs = validate_workflow(wf)
        assert any("retry: must be a non-negative int" in e for e in errs)

    def test_retry_float_rejected(self):
        wf = self._wf(retry=1.5)
        errs = validate_workflow(wf)
        assert any("retry: must be a non-negative int" in e for e in errs)

    def test_retry_bool_rejected(self):
        # bool is a subclass of int in Python; tighten checks for that.
        wf = self._wf(retry=True)
        errs = validate_workflow(wf)
        assert any("retry: must be a non-negative int" in e for e in errs)

    def test_unknown_verify_key_rejected(self):
        wf = self._wf(verify={"foo": 1})
        errs = validate_workflow(wf)
        assert any("verify: unknown keys" in e for e in errs)

    def test_legitimate_baseline_still_valid(self):
        wf = self._wf(retry=2, needs=[], verify={"command": "true"})
        errs = validate_workflow(wf)
        assert errs == []

    # ── codex post-fix follow-up: malformed YAML must NOT crash ─────

    def test_non_dict_node_does_not_crash(self):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [
                "this should be a dict, not a string",
                {"id": "ok", "goal": "g", "steps": ["s"],
                 "run": {"tool": "x.sh"}},
            ],
        }
        # Must return errors, not raise AttributeError.
        errs = validate_workflow(wf)
        assert any("nodes[0]: must be a dict" in e for e in errs)
        # The legitimate second node still validates clean.
        assert not any("ok." in e for e in errs)

    def test_non_string_run_skill_rejected(self):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"skill": 42},
            }],
        }
        errs = validate_workflow(wf)
        assert any("run.skill: must be a non-empty string" in e
                   for e in errs)

    def test_empty_run_tool_rejected(self):
        wf = self._wf(**{"run": {"tool": ""}})
        errs = validate_workflow(wf)
        assert any("run.tool: must be a non-empty string" in e
                   for e in errs)

    def test_non_string_verify_command_rejected(self):
        wf = self._wf(verify={"command": 42})
        errs = validate_workflow(wf)
        assert any("verify.command: must be a non-empty string" in e
                   for e in errs)

    def test_empty_verify_criterion_rejected(self):
        wf = self._wf(verify={"criterion": "   "})
        errs = validate_workflow(wf)
        assert any("verify.criterion: must be a non-empty string" in e
                   for e in errs)

    def test_verify_timeout_zero_rejected(self):
        wf = self._wf(verify={"command": "true", "timeout": 0})
        errs = validate_workflow(wf)
        assert any("verify.timeout: must be a positive int" in e
                   for e in errs)

    def test_verify_timeout_negative_rejected(self):
        wf = self._wf(verify={"command": "true", "timeout": -3})
        errs = validate_workflow(wf)
        assert any("verify.timeout: must be a positive int" in e
                   for e in errs)

    def test_verify_timeout_non_int_rejected(self):
        wf = self._wf(verify={"command": "true", "timeout": "30"})
        errs = validate_workflow(wf)
        assert any("verify.timeout: must be a positive int" in e
                   for e in errs)

    def test_output_schema_non_string_field_name_rejected(self):
        wf = self._wf(output_schema={123: "string"})
        errs = validate_workflow(wf)
        assert any("output_schema: field names must be non-empty strings"
                   in e for e in errs)

    # ── codex final-validation-gap follow-up:
    # ── existence check must not crash on garbage value types.

    def test_non_string_run_skill_with_project_root_no_typeerror(
            self, tmp_path):
        """validate_workflow(project_root=...) must NOT raise TypeError
        when run.skill is a non-string. The earlier 'must be a non-empty
        string' error is the only outcome."""
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"skill": 42},
            }],
        }
        # If the existence-check forwarded the int into
        # _resolve_skill_path, this would raise TypeError below.
        errs = validate_workflow(wf, project_root=tmp_path)
        assert any("run.skill: must be a non-empty string" in e
                   for e in errs)
        # And NOT a "skill 'X' not found" message keyed on the int.
        assert not any("not found" in e and "42" in e for e in errs)

    def test_non_string_run_tool_with_project_root_no_typeerror(
            self, tmp_path):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"tool": 7},
            }],
        }
        errs = validate_workflow(wf, project_root=tmp_path)
        assert any("run.tool: must be a non-empty string" in e
                   for e in errs)
        assert not any("not found or not" in e and "7" in e for e in errs)

    def test_empty_run_skill_with_project_root_no_typeerror(
            self, tmp_path):
        wf = {
            "workflow": "v", "version": "1.0",
            "nodes": [{
                "id": "n", "goal": "g", "steps": ["s"],
                "run": {"skill": "   "},
            }],
        }
        errs = validate_workflow(wf, project_root=tmp_path)
        assert any("run.skill: must be a non-empty string" in e
                   for e in errs)


class TestVerifyAgentShape:
    """Finding 7: verify_with_agent must structurally validate the
    evaluator's `data` shape. Missing/wrong-typed fields → reject (and
    consume retry budget like any verify failure)."""

    def _verify_envelope(self, data):
        """Build the envelope a verify-agent would write."""
        return {
            "status": "success",
            "data": data,
            "error": None,
            "feedback": None,
            "request_human": False,
        }

    def _node(self, n_steps=2):
        return Node.from_dict({
            "id": "n", "goal": "g",
            "steps": [f"step{i}" for i in range(1, n_steps + 1)],
            "run": {"tool": "x.sh"},
        })

    def _approved_data(self, n=2):
        return {
            "approved": True,
            "reasoning": "looks ok",
            "step_results": [
                {"step": i + 1, "passed": True,
                 "evidence": "quote",
                 "reasoning": "ok"}
                for i in range(n)
            ],
        }

    def test_well_formed_passes(self, monkeypatch):
        from runner import runtime as rt
        from runner.runtime import verify_with_agent

        def fake_run_and_collect(**kw):
            return ("aid", self._verify_envelope(self._approved_data(2)))

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_run_and_collect)
        wf_dummy = type("W", (), {"run_dir": kw_path(monkeypatch),
                                  "tag": "t",
                                  "spec": {}, "goal": None})()
        ok, _ = verify_with_agent(self._node(2), wf_dummy,
                                  {"status": "success"}, 1)
        assert ok is True

    def test_missing_step_results_rejects(self, monkeypatch):
        from runner import runtime as rt
        from runner.runtime import verify_with_agent

        bad = {"approved": True, "reasoning": "fine"}
        # NO step_results

        def fake_run_and_collect(**kw):
            return ("aid", self._verify_envelope(bad))

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_run_and_collect)
        wf_dummy = type("W", (), {"run_dir": kw_path(monkeypatch),
                                  "tag": "t",
                                  "spec": {}, "goal": None})()
        ok, fb = verify_with_agent(self._node(2), wf_dummy,
                                   {"status": "success"}, 1)
        assert ok is False
        assert "malformed data" in fb
        assert "step_results" in fb

    def test_wrong_step_results_length_rejects(self, monkeypatch):
        from runner import runtime as rt
        from runner.runtime import verify_with_agent

        bad = {
            "approved": True,
            "reasoning": "fine",
            "step_results": [
                {"step": 1, "passed": True, "evidence": "e", "reasoning": "r"},
            ],  # only 1 entry, but node has 2 steps
        }

        def fake_run_and_collect(**kw):
            return ("aid", self._verify_envelope(bad))

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_run_and_collect)
        wf_dummy = type("W", (), {"run_dir": kw_path(monkeypatch),
                                  "tag": "t",
                                  "spec": {}, "goal": None})()
        ok, fb = verify_with_agent(self._node(2), wf_dummy,
                                   {"status": "success"}, 1)
        assert ok is False
        assert "1 entries, expected 2" in fb

    def test_wrong_typed_step_field_rejects(self, monkeypatch):
        from runner import runtime as rt
        from runner.runtime import verify_with_agent

        bad = {
            "approved": True,
            "reasoning": "fine",
            "step_results": [
                {"step": "1", "passed": True,
                 "evidence": "e", "reasoning": "r"},
                {"step": 2, "passed": True,
                 "evidence": "e", "reasoning": "r"},
            ],
        }

        def fake_run_and_collect(**kw):
            return ("aid", self._verify_envelope(bad))

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_run_and_collect)
        wf_dummy = type("W", (), {"run_dir": kw_path(monkeypatch),
                                  "tag": "t",
                                  "spec": {}, "goal": None})()
        ok, fb = verify_with_agent(self._node(2), wf_dummy,
                                   {"status": "success"}, 1)
        assert ok is False
        assert "step must be an int" in fb

    def test_empty_evidence_for_approved_step_NOT_enforced(self, monkeypatch):
        """Reviewer note: evidence-non-empty is prompt-protocol-only.
        Runtime accepts empty evidence on an approved step."""
        from runner import runtime as rt
        from runner.runtime import verify_with_agent

        data = {
            "approved": True,
            "reasoning": "fine",
            "step_results": [
                {"step": 1, "passed": True, "evidence": "", "reasoning": ""},
                {"step": 2, "passed": True, "evidence": "", "reasoning": ""},
            ],
        }

        def fake_run_and_collect(**kw):
            return ("aid", self._verify_envelope(data))

        monkeypatch.setattr(rt.camc, "run_and_collect", fake_run_and_collect)
        wf_dummy = type("W", (), {"run_dir": kw_path(monkeypatch),
                                  "tag": "t",
                                  "spec": {}, "goal": None})()
        ok, _ = verify_with_agent(self._node(2), wf_dummy,
                                  {"status": "success"}, 1)
        assert ok is True


def kw_path(monkeypatch):
    """Helper: a Path to a fresh temp dir for verify_with_agent's
    sub_dir.mkdir() calls in the shape tests above."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="vshape-"))
    return d


# ───────────────────────────────────────────────────────────────────────
#  TestPromptOrdering — finding 8 from code-review-codex-2026-05-04
# ───────────────────────────────────────────────────────────────────────

class TestPromptOrdering:
    """Finding 8: when retry adds a `# Note: previous attempt failed`
    section, it must appear BEFORE `# Output` per spec §8 ordering."""

    def _node(self):
        return Node.from_dict({
            "id": "n", "goal": "g", "steps": ["s"],
            "run": {"skill": "analyzer"},
        })

    def test_retry_note_precedes_output_section(self):
        out = build_run_prompt(
            self._node(),
            {"previous": {"status": "fail", "feedback": "try again"}},
        )
        note_idx = out.index("# Note: previous attempt failed")
        output_idx = out.index("# Output")
        assert note_idx < output_idx, (
            "spec §8 puts retry note before # Output; got note at "
            f"{note_idx} and output at {output_idx}"
        )


# ───────────────────────────────────────────────────────────────────────
#  TestGoalDriven — goal-driven supplement 2026-05-05
# ───────────────────────────────────────────────────────────────────────

class TestGoalDriven:
    """Goal-driven supplement (docs/spec-1.1-goal-driven-supplement-2026-05-05.md).
    Implementation MVP: Workflow.goal persisted; retry prompt is
    goal-driven; DAG revision recorded for the active user workflow."""

    # ── Workflow.goal storage + validation ─────────────────────────────

    def test_workflow_goal_stored_when_present(self, tmp_path):
        from runner.runtime import Workflow
        spec = {
            "workflow": "wf", "version": "1.1",
            "goal": "The persistent objective for this run.",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/x.sh"},
            }],
        }
        wf = Workflow(spec, tmp_path / "rd")
        assert wf.goal == "The persistent objective for this run."

    def test_workflow_goal_absent_means_none(self, tmp_path):
        from runner.runtime import Workflow
        spec = {
            "workflow": "wf", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, tmp_path / "rd")
        assert wf.goal is None

    def test_validate_rejects_non_string_goal(self):
        from runner.runtime import validate_workflow
        spec = {
            "workflow": "wf", "version": "1.1",
            "goal": ["not", "a", "string"],
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"}}],
        }
        errs = validate_workflow(spec)
        assert any("workflow.goal" in e and "string" in e for e in errs)

    # ── DAG revision recording ─────────────────────────────────────────

    def test_dag_revision_recorded_for_user_workflow(self, tmp_path):
        """Per supplement §3.6 — every execution DAG Runtime runs is
        recorded under .camflow/run/dag_revisions/0001/ before
        execution. Mechanical; no scheduling/retry/verify change."""
        from runner.runtime import Workflow
        proj = tmp_path / "proj"
        rd = proj / ".camflow" / "run"
        spec = {
            "workflow": "wf", "version": "1.1",
            "goal": "Prove X.",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, rd)
        rev = rd / "dag_revisions" / "0001"
        assert rev.is_dir()
        assert (rev / "workflow.yaml").is_file()
        # The recorded YAML round-trips to the same dict as the active
        # workflow.yaml.
        active = yaml.safe_load((rd / "workflow.yaml").read_text())
        recorded = yaml.safe_load((rev / "workflow.yaml").read_text())
        assert active == recorded
        manifest = json.loads((rev / "manifest.json").read_text())
        assert manifest["revision"] == 1
        assert manifest["parent_revision"] is None
        assert manifest["reason"] == "initial_plan"
        assert manifest["workflow_goal"] == "Prove X."
        assert wf.dag_revision == 1

    def test_planner_internal_workflow_skips_dag_revision(self, tmp_path):
        """Planner-internal workflows live under .camflow/run/planner/
        and represent the compiler workflow, not the user-facing active
        DAG. They should NOT spawn a dag_revisions/ subdir."""
        from runner.runtime import Workflow
        rd = tmp_path / "proj" / ".camflow" / "run" / "planner"
        spec = {
            "workflow": "planner", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        Workflow(spec, rd)
        assert not (rd / "dag_revisions").exists()

    def test_user_workflow_trace_carries_dag_revision(self, tmp_path):
        """User-workflow trace events get tagged with dag_revision so
        a future replay tool can reconstruct the active plan per
        attempt. Planner-internal events skip the field."""
        from runner.runtime import Workflow
        rd = tmp_path / "proj" / ".camflow" / "run"
        spec = {
            "workflow": "wf", "version": "1.1",
            "goal": "G.",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, rd)
        wf.trace("workflow_started", run_id="probe")
        line = (rd / "trace.jsonl").read_text().splitlines()[-1]
        rec = json.loads(line)
        assert rec.get("dag_revision") == 1, rec

    # ── Goal-driven retry prompt ───────────────────────────────────────

    def test_retry_prompt_is_goal_driven(self):
        """Per supplement §3.3 — retry prompt directs the agent to
        re-read Workflow.goal, Node.goal, and previous.feedback;
        surface plan mismatch instead of looping."""
        node = Node.from_dict({
            "id": "n", "goal": "advance the workflow goal X",
            "steps": ["s"], "run": {"skill": "analyzer"},
        })
        out = build_run_prompt(
            node,
            {"previous": {"status": "fail", "feedback": "missed reqK"}},
        )
        # Workflow.goal mentioned (so agent re-reads workflow context).
        assert "Workflow.goal" in out or "workflow.goal" in out.lower()
        # Node.goal mentioned (so agent re-reads its local goal).
        assert "Node.goal" in out or "node.goal" in out.lower()
        # Plan-mismatch escape: the agent must know retry is bounded
        # and that looping on the same local error is wrong.
        normalized = " ".join(out.split()).lower()
        assert ("fail clearly" in normalized
                or "do not loop" in normalized
                or "do NOT loop".lower() in normalized
                or "retry is a bounded" in normalized)

    def test_no_retry_no_goal_driven_block(self):
        """The goal-driven retry directive only appears when there's a
        previous attempt. Fresh attempts get the regular Goal/Steps."""
        node = Node.from_dict({
            "id": "n", "goal": "g", "steps": ["s"],
            "run": {"skill": "analyzer"},
        })
        out = build_run_prompt(node, {})
        assert "Goal-driven retry" not in out
        assert "previous attempt failed" not in out


# ───────────────────────────────────────────────────────────────────────
#  TestWorkflowGoalInjection — goal-driven supplement, prompt-side
#  Per codex-post-implementation-scope-review: Workflow.goal is stored
#  but the agent must actually SEE it in the prompt for goal-driven
#  retry/verify to mean anything.
# ───────────────────────────────────────────────────────────────────────

class TestWorkflowGoalInjection:
    """Run + verify prompts must carry workflow.goal verbatim under a
    `# Workflow Goal` section when supplied. Absent goal omits cleanly."""

    GOAL = "Implement parse_record satisfying SPEC.md Req 1-4."

    def _node(self, *, goal_text: str = "the local node objective"):
        return Node.from_dict({
            "id": "n", "goal": goal_text, "steps": ["s1", "s2"],
            "run": {"skill": "analyzer"},
        })

    # ── run prompt ─────────────────────────────────────────────────────

    def test_run_prompt_carries_workflow_goal_verbatim(self):
        out = build_run_prompt(self._node(), {},
                               workflow_goal=self.GOAL)
        assert "# Workflow Goal" in out
        assert self.GOAL in out, (
            "the literal Workflow.goal text must appear in the run "
            "prompt — otherwise retry can't actually re-read it."
        )

    def test_run_prompt_workflow_goal_precedes_workflow_context(self):
        out = build_run_prompt(self._node(), {},
                               workflow_goal=self.GOAL,
                               workflow_context="shared facts here")
        wg = out.index("# Workflow Goal")
        wc = out.index("# Workflow Context")
        assert wg < wc, (
            "# Workflow Goal must precede # Workflow Context so the "
            "persistent objective is read first."
        )

    def test_run_prompt_omits_workflow_goal_when_absent(self):
        out = build_run_prompt(self._node(), {},
                               workflow_context="shared facts")
        assert "# Workflow Goal" not in out

    def test_run_prompt_omits_workflow_goal_when_empty_string(self):
        out = build_run_prompt(self._node(), {}, workflow_goal="   ")
        assert "# Workflow Goal" not in out

    # ── retry prompt path: literal goal text alongside directive ───────

    def test_retry_prompt_contains_both_goal_text_and_directive(self):
        out = build_run_prompt(
            self._node(),
            {"previous": {"status": "fail", "feedback": "missed reqK"}},
            workflow_goal=self.GOAL,
        )
        # The literal goal text must be present (so re-read is possible).
        assert self.GOAL in out
        # And the goal-driven directive that tells the agent to do so.
        assert "Goal-driven retry" in out
        assert "Workflow.goal" in out or "workflow.goal" in out.lower()

    # ── verify prompt ──────────────────────────────────────────────────

    def test_verify_prompt_carries_workflow_goal_verbatim(self):
        envelope = {"status": "success", "data": {"x": 1}}
        out = build_verify_prompt(self._node(), envelope,
                                  workflow_goal=self.GOAL)
        assert "# Workflow Goal" in out
        assert self.GOAL in out, (
            "verify prompt must carry the literal Workflow.goal so the "
            "evaluator can judge against the persistent objective, not "
            "only against the local checklist."
        )

    def test_verify_prompt_workflow_goal_precedes_workflow_context(self):
        out = build_verify_prompt(
            self._node(), {"status": "success", "data": {}},
            workflow_goal=self.GOAL,
            workflow_context="shared facts",
        )
        wg = out.index("# Workflow Goal")
        wc = out.index("# Workflow Context")
        assert wg < wc

    def test_verify_prompt_omits_workflow_goal_when_absent(self):
        out = build_verify_prompt(
            self._node(), {"status": "success", "data": {}})
        assert "# Workflow Goal" not in out


# ───────────────────────────────────────────────────────────────────────
#  TestStatus — read-only `camflow status` MVP
# ───────────────────────────────────────────────────────────────────────

class TestStatus:
    """`camflow status` is strictly read-only. Per the
    codex-implement-camflow-status-readonly task, it must NEVER
    instantiate Workflow (which writes workflow.yaml/pid/dag_revisions)
    and must NEVER create/delete/modify run-dir files. State inference
    is artifact-driven."""

    def _make_run_dir(self, tmp_path: Path, *,
                      workflow_name: str = "wf",
                      goal: str | None = None,
                      nodes: list[str] | None = None,
                      events: list[dict] | None = None,
                      pid: int | None = None,
                      halt: dict | None = None,
                      revisions: list[dict] | None = None) -> Path:
        """Synthesize a run dir without touching Workflow."""
        rd = tmp_path / "rd"
        rd.mkdir(parents=True, exist_ok=True)
        wf: dict = {"workflow": workflow_name, "version": "1.1"}
        if goal:
            wf["goal"] = goal
        if nodes is None:
            nodes = ["a"]
        wf["nodes"] = [{"id": n, "goal": "g", "steps": ["s"],
                        "run": {"tool": "scripts/x.sh"}} for n in nodes]
        (rd / "workflow.yaml").write_text(yaml.safe_dump(wf, sort_keys=False))
        if events:
            (rd / "trace.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n"
            )
        if pid is not None:
            (rd / "runner.pid").write_text(str(pid))
        if halt is not None:
            (rd / "halt.json").write_text(json.dumps(halt))
        if revisions:
            for r in revisions:
                rev_dir = rd / "dag_revisions" / f"{r['revision']:04d}"
                rev_dir.mkdir(parents=True)
                (rev_dir / "manifest.json").write_text(json.dumps(r))
                (rev_dir / "workflow.yaml").write_text(
                    yaml.safe_dump(wf, sort_keys=False)
                )
        return rd

    def _make_attempt(self, run_dir: Path, node_id: str, attempt: int,
                      *, output: dict | None = None,
                      prompt: str | None = "P") -> Path:
        att = run_dir / "nodes" / node_id / f"attempt-{attempt}"
        att.mkdir(parents=True, exist_ok=True)
        if prompt is not None:
            (att / "prompt.txt").write_text(prompt)
        if output is not None:
            (att / "output.json").write_text(json.dumps(output))
            (att / "agent_output.json").write_text(json.dumps(output))
        return att

    # ── state inference ───────────────────────────────────────────────

    def test_state_done_when_workflow_completed_event(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(
            tmp_path, nodes=["a"],
            events=[
                {"step": 1, "ts": "t", "event": "workflow_started"},
                {"step": 2, "ts": "t", "event": "node_started", "node": "a"},
                {"step": 3, "ts": "t", "event": "node_completed",
                 "node": "a", "status": "success"},
                {"step": 4, "ts": "t", "event": "workflow_completed",
                 "status": "success"},
            ])
        s = _summarize_status(rd)
        assert s["state"] == "success", s
        assert s["progress"]["done"] == 1

    def test_state_running_when_pid_alive(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(
            tmp_path, pid=os.getpid(),  # this test process is alive
            events=[{"step": 1, "ts": "t", "event": "node_started",
                     "node": "a"}])
        s = _summarize_status(rd)
        assert s["state"] == "running"
        assert s["pid_alive"] is True

    def test_state_stale_when_pid_dead(self, tmp_path):
        from runner.runtime import _summarize_status
        # pid 999999999 is almost certainly dead (and nonexistent).
        rd = self._make_run_dir(tmp_path, pid=999999999)
        s = _summarize_status(rd)
        assert s["state"] == "stale"
        assert s["pid_alive"] is False

    def test_state_halted_when_halt_json_present(self, tmp_path):
        from runner.runtime import _summarize_status
        halt = {
            "halted_node": "a", "kind": "halt",
            "reason": "retry exhausted",
            "envelope": {"feedback": "missing reqK", "error": None},
        }
        rd = self._make_run_dir(tmp_path, halt=halt)
        s = _summarize_status(rd)
        assert s["state"] == "halted"
        assert s["halt"]["halted_node"] == "a"

    def test_state_unknown_when_empty(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = tmp_path / "empty"
        rd.mkdir()
        s = _summarize_status(rd)
        assert s["state"] == "unknown"

    def test_state_missing_when_run_dir_absent(self, tmp_path):
        from runner.runtime import _summarize_status
        s = _summarize_status(tmp_path / "nope")
        assert s["state"] == "missing"
        assert s["exists"] is False

    # ── progress + current node ───────────────────────────────────────

    def test_current_node_is_first_unfinished(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(
            tmp_path, nodes=["a", "b", "c"],
            events=[
                {"step": 1, "ts": "t", "event": "node_started", "node": "a"},
                {"step": 2, "ts": "t", "event": "node_completed",
                 "node": "a", "status": "success"},
                {"step": 3, "ts": "t", "event": "node_started", "node": "b"},
            ])
        self._make_attempt(rd, "a", 1, output={"data": {}})
        self._make_attempt(rd, "b", 1)
        s = _summarize_status(rd)
        assert s["progress"]["done"] == 1
        assert s["progress"]["total"] == 3
        assert s["current_node"]["id"] == "b"
        assert s["current_node"]["phase"] == "running"

    # ── --node focus ──────────────────────────────────────────────────

    def test_node_focus_filters_to_one(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(tmp_path, nodes=["a", "b"])
        self._make_attempt(rd, "a", 1)
        self._make_attempt(rd, "b", 1)
        s = _summarize_status(rd, focus_node="b")
        assert len(s["nodes"]) == 1
        assert s["nodes"][0]["id"] == "b"

    # ── DAG revisions surfaced ────────────────────────────────────────

    def test_dag_revisions_surfaced(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(
            tmp_path,
            revisions=[{
                "revision": 1, "parent_revision": None,
                "reason": "initial_plan", "workflow_goal": "G."}],
            events=[
                {"step": 1, "ts": "t", "event": "node_started",
                 "node": "a", "dag_revision": 1},
            ])
        s = _summarize_status(rd)
        assert s["active_dag_revision"] == 1
        assert len(s["dag_revisions"]) == 1
        assert s["dag_revisions"][0]["reason"] == "initial_plan"

    # ── recent_events ─────────────────────────────────────────────────

    def test_events_limit_returns_tail(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = self._make_run_dir(
            tmp_path,
            events=[{"step": i, "ts": "t", "event": "x"}
                    for i in range(1, 11)])
        s = _summarize_status(rd, events_limit=3)
        assert "recent_events" in s
        assert len(s["recent_events"]) == 3
        assert s["recent_events"][-1]["step"] == 10

    # ── --json + --events via _cmd_status ─────────────────────────────

    def test_cmd_status_json_round_trips(self, tmp_path, capsys):
        from runner.runtime import _cmd_status
        rd = self._make_run_dir(
            tmp_path, goal="prove X.",
            events=[{"step": 1, "ts": "t", "event": "workflow_started"}])
        rc = _cmd_status(["--run-dir", str(rd), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["state"] == "unknown"  # no completed/halt/pid
        assert parsed["workflow_goal"] == "prove X."

    # ── --planner sub-run ─────────────────────────────────────────────

    def test_planner_flag_targets_planner_subdir(self, tmp_path, capsys):
        from runner.runtime import _cmd_status
        # Synthesize a planner sub-run only (no user run).
        proj = tmp_path / "proj"
        planner_rd = proj / ".camflow" / "run" / "planner"
        planner_rd.mkdir(parents=True)
        (planner_rd / "workflow.yaml").write_text(
            yaml.safe_dump({
                "workflow": "planner", "version": "1.1", "goal": "compile",
                "nodes": [{"id": "u", "goal": "g", "steps": ["s"],
                           "run": {"tool": "scripts/x.sh"}}],
            }, sort_keys=False))
        # --planner with explicit --run-dir (which should append /planner).
        rc = _cmd_status(["--run-dir", str(proj / ".camflow" / "run"),
                          "--planner", "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["run_dir"].endswith("planner")
        assert parsed["workflow_name"] == "planner"

    # ── human render contains the right markers ───────────────────────

    def test_human_render_shows_state_progress_halt(self, tmp_path):
        from runner.runtime import _summarize_status, _render_status_human
        halt = {
            "halted_node": "a", "kind": "halt", "reason": "retry exhausted",
            "envelope": {"feedback": "missed reqK", "error": None},
        }
        rd = self._make_run_dir(
            tmp_path, goal="Prove X.", nodes=["a", "b"],
            halt=halt,
            events=[
                {"step": 1, "ts": "t", "event": "node_started", "node": "a"},
                {"step": 2, "ts": "t", "event": "workflow_halted",
                 "node": "a", "reason": "retry exhausted"},
            ])
        self._make_attempt(rd, "a", 1, output={"status": "fail"})
        s = _summarize_status(rd)
        text = _render_status_human(s)
        assert "state:" in text and "halted" in text
        assert "goal:" in text and "Prove X." in text
        assert "HALT:" in text
        assert "missed reqK" in text
        assert "camflow resume" in text
        # Node table heading.
        assert "phase" in text and "attempt" in text

    # ── --output dumps focused node's output.json ─────────────────────

    def test_output_flag_dumps_focused_output(self, tmp_path, capsys):
        from runner.runtime import _cmd_status
        rd = self._make_run_dir(tmp_path, nodes=["a"])
        self._make_attempt(rd, "a", 1, output={"status": "success",
                                                "data": {"k": "v"}})
        rc = _cmd_status(["--run-dir", str(rd), "--node", "a", "--output"])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"k": "v"' in out

    # ── strictly no mutation ──────────────────────────────────────────

    def test_no_mutation_invariant(self, tmp_path, capsys):
        """Run status; assert every file in run_dir is byte-identical
        before vs. after, and no files are added or removed."""
        import hashlib
        from runner.runtime import _cmd_status
        rd = self._make_run_dir(
            tmp_path, goal="Prove X.", nodes=["a", "b"],
            pid=999999999,
            events=[
                {"step": 1, "ts": "t", "event": "node_started",
                 "node": "a", "dag_revision": 1},
            ],
            revisions=[{"revision": 1, "parent_revision": None,
                        "reason": "initial_plan", "workflow_goal": "Prove X."}])
        self._make_attempt(rd, "a", 1, output={"status": "success",
                                                "data": {}})

        def snapshot(root: Path) -> dict[str, tuple[int, str]]:
            snap = {}
            for p in root.rglob("*"):
                if p.is_file():
                    rel = str(p.relative_to(root))
                    data = p.read_bytes()
                    snap[rel] = (len(data),
                                 hashlib.sha256(data).hexdigest())
            return snap

        before = snapshot(rd)
        # Run several status modes that exercise different code paths.
        for argv in (
            ["--run-dir", str(rd)],
            ["--run-dir", str(rd), "--json"],
            ["--run-dir", str(rd), "--events", "5"],
            ["--run-dir", str(rd), "--node", "a", "--output"],
        ):
            rc = _cmd_status(argv)
            assert rc == 0
            capsys.readouterr()  # drain
        after = snapshot(rd)
        assert before == after, (
            f"camflow status mutated run_dir!\n"
            f"  added/removed: {set(before) ^ set(after)}\n"
            f"  changed: { {k for k in before & after.keys() if before[k] != after[k]} }"
        )


# ───────────────────────────────────────────────────────────────────────
#  TestToolDagRevisionInjection — Phase A oracle-maze plumbing
# ───────────────────────────────────────────────────────────────────────

class TestToolDagRevisionInjection:
    """Tools that talk to external systems need to know which DAG
    revision is active. Per codex-blind-maze-oracle Phase A, runtime
    must surface dag_revision both as an env var (CAMFLOW_DAG_REVISION)
    and as a JSON field in the tool's stdin (dag_revision)."""

    def _make_proj_with_echo_tool(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # Tool that echoes back what it sees. We capture the env var
        # and the input dict's dag_revision into the data envelope.
        body = (
            r'''
input_json=$(cat)
ev="${CAMFLOW_DAG_REVISION:-MISSING}"
rev=$(echo "$input_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('dag_revision','MISSING'))")
cat <<EOF
{"status":"success","data":{"env_rev":"$ev","input_rev":"$rev"},"error":null,"feedback":null,"request_human":false}
EOF
'''
        )
        make_executable_tool(scripts / "echo.sh", body)
        return proj

    def test_env_and_input_carry_dag_revision_on_fresh_run(self, tmp_path):
        proj = self._make_proj_with_echo_tool(tmp_path)
        wf = {
            "workflow": "wf", "version": "1.1",
            "goal": "Capture dag_revision in tool input.",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["echo"],
                "run": {"tool": "scripts/echo.sh"},
                "output_schema": {"env_rev": "string",
                                  "input_rev": "string"},
                "verify": {"command": "true"},
            }],
        }
        rd = proj / ".camflow" / "run"
        result = run_workflow(wf, rd)
        assert result == "done"
        out = json.loads(
            (rd / "nodes" / "x" / "attempt-1" / "output.json").read_text()
        )
        # Both channels deliver the same revision number.
        assert out["data"]["env_rev"] == "1", out
        assert out["data"]["input_rev"] == "1", out

    def test_workflow_init_exports_dag_revision_env(self, tmp_path,
                                                     monkeypatch):
        """Skill agents run inside camc-spawned tmux sessions and
        inherit env from the runtime process. Workflow.__init__ must
        export CAMFLOW_DAG_REVISION into os.environ so wrappers
        invoked by skill agents (e.g. via Bash inside the agent's
        tmux) see the active revision.

        Live finding (codex-blind-maze-oracle): without this, skill
        agents calling wrapper scripts always saw the wrapper's
        fallback default of 1, so a replanned run kept submitting at
        rev 1 to the oracle even when CamFlow was at rev 2/3."""
        from runner.runtime import Workflow
        monkeypatch.delenv("CAMFLOW_DAG_REVISION", raising=False)
        rd = tmp_path / "proj" / ".camflow" / "run"
        spec = {
            "workflow": "wf", "version": "1.1",
            "goal": "g.",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        Workflow(spec, rd)
        assert os.environ.get("CAMFLOW_DAG_REVISION") == "1"

    def test_workflow_init_planner_internal_does_not_export_env(
            self, tmp_path, monkeypatch):
        """Planner-internal workflows aren't the user-facing active
        DAG; they shouldn't clobber the env var, which the user
        workflow owns."""
        from runner.runtime import Workflow
        monkeypatch.setenv("CAMFLOW_DAG_REVISION", "42")
        rd = tmp_path / "proj" / ".camflow" / "run" / "planner"
        spec = {
            "workflow": "planner", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        Workflow(spec, rd)
        # Untouched — planner-internal init must not overwrite.
        assert os.environ.get("CAMFLOW_DAG_REVISION") == "42"

    def test_input_json_includes_dag_revision_field(self, tmp_path):
        """Skill nodes don't get the env var (camc agents read input.json
        from disk inside their tmux session) — but they DO see the
        dag_revision field in input.json. Verify the field is written."""
        proj = self._make_proj_with_echo_tool(tmp_path)
        wf = {
            "workflow": "wf", "version": "1.1",
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["echo"],
                "run": {"tool": "scripts/echo.sh"},
                "verify": {"command": "true"},
                "output_schema": {"env_rev": "string", "input_rev": "string"},
            }],
        }
        rd = proj / ".camflow" / "run"
        run_workflow(wf, rd)
        inp = json.loads(
            (rd / "nodes" / "x" / "attempt-1" / "input.json").read_text())
        assert inp["dag_revision"] == 1


# ───────────────────────────────────────────────────────────────────────
#  TestReplan — manual halt-time Planner re-entry (Phase A)
# ───────────────────────────────────────────────────────────────────────

class TestReplan:
    """`camflow replan <run_dir>` — manual halt-time Planner re-entry.
    Per codex-blind-maze-oracle Phase A. Auto-replan is Phase B and
    NOT covered here. These tests stub the Planner workflow with a
    canned new spec so they're deterministic + LLM-free."""

    def _stage_halted_run(self, tmp_path: Path,
                          *, halt_kind: str = "halt") -> Path:
        """Synthesize a halted user run dir without touching Workflow."""
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # A simple tool node that always succeeds — used by the
        # replanned (rev 2) workflow.
        make_executable_tool(
            scripts / "ok.sh",
            envelope_tool_body({"value": 42}),
        )

        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        # Original prompt persisted by camflow run.
        (rd / "prompt.txt").write_text("solve the maze")
        # Prior compiled workflow.yaml (rev 1).
        prior_spec = {
            "workflow": "rev1", "version": "1.1",
            "goal": "Solve the maze.",
            "nodes": [{
                "id": "submit", "goal": "submit path",
                "steps": ["s"],
                "run": {"tool": "scripts/ok.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
            }],
        }
        (rd / "workflow.yaml").write_text(
            yaml.safe_dump(prior_spec, sort_keys=False))
        # Initial revision recording (matches Workflow.__init__ behavior).
        (rd / "dag_revisions" / "0001").mkdir(parents=True)
        (rd / "dag_revisions" / "0001" / "workflow.yaml").write_text(
            yaml.safe_dump(prior_spec, sort_keys=False))
        (rd / "dag_revisions" / "0001" / "manifest.json").write_text(
            json.dumps({
                "revision": 1, "parent_revision": None,
                "reason": "initial_plan",
                "workflow_goal": "Solve the maze.",
            }))
        # Some prior nodes/ artifacts (rev 1 attempt that "failed").
        att = rd / "nodes" / "submit" / "attempt-1"
        att.mkdir(parents=True)
        (att / "output.json").write_text(json.dumps({
            "status": "fail",
            "error": {"code": "ORACLE_HALT",
                      "message": "first submit halts even when correct"},
            "feedback": "rev=1 halt by design",
        }))
        # Trace + halt.json.
        events = [
            {"step": 1, "ts": "t", "event": "workflow_started",
             "dag_revision": 1},
            {"step": 2, "ts": "t", "event": "node_started",
             "node": "submit", "attempt": 1, "dag_revision": 1},
            {"step": 3, "ts": "t", "event": "node_failed",
             "node": "submit", "attempt": 1, "reason": "oracle halt",
             "dag_revision": 1},
            {"step": 4, "ts": "t", "event": "workflow_halted",
             "node": "submit", "reason": "request_human or halt",
             "dag_revision": 1},
        ]
        (rd / "trace.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n")
        halt_info = {
            "halted_node": "submit",
            "kind": halt_kind,
            "reason": "oracle returned halt=true at rev 1",
            "envelope": {
                "status": "fail",
                "error": {"code": "ORACLE_HALT",
                          "message": "first submit halts at rev 1"},
                "feedback": "submit again on a fresh dag_revision",
            },
        }
        (rd / "halt.json").write_text(json.dumps(halt_info))
        return rd

    def _stub_planner(self, monkeypatch, new_yaml: str,
                      *, capture: dict | None = None) -> None:
        """Patch _cmd_replan's Planner re-entry path so it returns
        new_yaml without spawning real camc agents."""
        from runner import runtime as rt

        orig_workflow_cls = rt.Workflow

        class _StubWorkflow:
            def __init__(self, spec, run_dir, *, project_root=None,
                         resume=False, replan=False):
                self.spec = spec
                self.run_dir = Path(run_dir)
                self.run_dir.mkdir(parents=True, exist_ok=True)
                self.run_id = "stub-planner"
                self.dag_revision = 1
                self._is_user_workflow = False
                self.nodes_by_id = {
                    "render_yaml": type("N", (), {
                        "id": "render_yaml",
                        "output": {
                            "status": "success",
                            "data": {"yaml_text": new_yaml},
                        },
                    })()
                }
                if capture is not None:
                    capture["spec"] = spec
                    capture["context"] = spec.get("context", "")

            def trace(self, *args, **kwargs):  # noqa: ARG002
                pass

            def execute_dag(self, *, max_attempts=None):  # noqa: ARG002
                # Pretend the planner sub-workflow ran cleanly.
                return "done"

            def cleanup(self):
                pass

        # Only stub when invoked under planner-rev<N> sub-dir; the user
        # workflow.yaml execution must use the real Workflow class so we
        # can verify replan semantics on disk.
        original_init = orig_workflow_cls.__init__

        def patched_init(self, spec, run_dir, *, resume=False,
                         replan=False, project_root=None):
            rd = Path(run_dir).resolve()
            if "planner-rev" in rd.name:
                # Hand off to the stub by mutating self into a stub clone.
                stub = _StubWorkflow(spec, rd, project_root=project_root,
                                     resume=resume, replan=replan)
                self.__class__ = _StubWorkflow
                self.__dict__.update(stub.__dict__)
                return
            original_init(self, spec, rd, resume=resume, replan=replan,
                          project_root=project_root)

        monkeypatch.setattr(rt.Workflow, "__init__", patched_init)

    def test_replan_creates_revision_2_and_executes(self, tmp_path,
                                                     monkeypatch):
        from runner.runtime import _cmd_replan
        rd = self._stage_halted_run(tmp_path)
        # New rev2 spec — same single node, succeeds at rev 2.
        new_yaml = (
            "workflow: rev2\n"
            "version: \"1.1\"\n"
            "goal: \"Solve the maze (revision 2 after halt).\"\n"
            "nodes:\n"
            "  - id: submit\n"
            "    goal: \"submit at rev 2\"\n"
            "    steps: [\"s\"]\n"
            "    run: {tool: scripts/ok.sh}\n"
            "    output_schema: {value: integer}\n"
            "    verify: {command: \"true\"}\n"
        )
        self._stub_planner(monkeypatch, new_yaml)
        rc = _cmd_replan([str(rd)])
        assert rc == 0

        # dag_revisions/0002/ created.
        rev2 = rd / "dag_revisions" / "0002"
        assert rev2.is_dir()
        manifest = json.loads((rev2 / "manifest.json").read_text())
        assert manifest["revision"] == 2
        assert manifest["parent_revision"] == 1
        assert manifest["reason"] == "manual_replan_after_halt"
        assert manifest["halted_node"] == "submit"
        # Active workflow.yaml replaced.
        active = yaml.safe_load((rd / "workflow.yaml").read_text())
        assert active["workflow"] == "rev2"
        assert active["goal"].startswith("Solve the maze")

        # Prior nodes/ archived under dag_revisions/0001/.
        archived_nodes = rd / "dag_revisions" / "0001" / "nodes"
        assert archived_nodes.is_dir()
        assert (archived_nodes / "submit" / "attempt-1" /
                "output.json").exists()
        # Prior halt.json archived too.
        assert (rd / "dag_revisions" / "0001" / "halt.json").is_file()
        # Active halt.json gone.
        assert not (rd / "halt.json").exists()

        # Rev 2 actually executed: a fresh nodes/submit/attempt-1
        # directory exists with success output.
        out = json.loads(
            (rd / "nodes" / "submit" / "attempt-1" / "output.json").read_text()
        )
        assert out["status"] == "success"
        assert out["data"]["value"] == 42

        # Rev 2 trace events tagged dag_revision=2.
        events = [
            json.loads(line) for line in
            (rd / "trace.jsonl").read_text().splitlines() if line
        ]
        rev2_events = [e for e in events if e.get("dag_revision") == 2]
        assert rev2_events, "rev 2 user-workflow events must carry dag_revision=2"
        # And rev 1 events are still in the trace (continuous history).
        assert any(e.get("dag_revision") == 1 for e in events)

    def test_replan_rejects_when_not_halted(self, tmp_path, capsys):
        from runner.runtime import _cmd_replan
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "prompt.txt").write_text("p")
        (rd / "workflow.yaml").write_text("workflow: w\nversion: '1.1'\nnodes: []\n")
        # No halt.json → replan refuses.
        rc = _cmd_replan([str(rd)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "halt.json" in err or "halted run" in err

    def test_replan_rejects_missing_run_dir(self, tmp_path, capsys):
        from runner.runtime import _cmd_replan
        rc = _cmd_replan([str(tmp_path / "nope")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_replan_rejects_when_prompt_missing(self, tmp_path, capsys):
        from runner.runtime import _cmd_replan
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "halt.json").write_text(json.dumps({"halted_node": "x",
                                                   "kind": "halt"}))
        (rd / "workflow.yaml").write_text("workflow: w\n")
        # No prompt.txt → replan needs it to preserve the goal.
        rc = _cmd_replan([str(rd)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "prompt" in err

    def test_replan_extends_planner_prompt_with_halt_context(self,
                                                              tmp_path,
                                                              monkeypatch):
        """The planner re-entry prompt must include the # Replan
        Context block carrying halt info + prior YAML."""
        from runner.runtime import _cmd_replan
        rd = self._stage_halted_run(tmp_path)
        new_yaml = (
            "workflow: rev2\nversion: \"1.1\"\n"
            "goal: \"g\"\n"
            "nodes:\n"
            "  - id: submit\n"
            "    goal: g\n"
            "    steps: [s]\n"
            "    run: {tool: scripts/ok.sh}\n"
            "    output_schema: {value: integer}\n"
            "    verify: {command: \"true\"}\n"
        )
        captured: dict = {}
        self._stub_planner(monkeypatch, new_yaml, capture=captured)
        rc = _cmd_replan([str(rd)])
        assert rc == 0
        ctx = captured.get("context", "")
        # The planner must have seen the original prompt.
        assert "solve the maze" in ctx
        # And the replan-context header.
        assert "# Replan Context" in ctx
        # And the halted-node identity.
        assert "submit" in ctx
        # And a snippet of the prior YAML.
        assert "workflow: rev1" in ctx


# ───────────────────────────────────────────────────────────────────────
#  TestPhaseBAutoReplan — opt-in halt-time auto-replan
# ───────────────────────────────────────────────────────────────────────

class TestPhaseBAutoReplan:
    """Phase B: workflows can declare `on_halt: replan` + `max_replans: N`
    to have runtime auto-invoke `_perform_replan` on halt up to N times,
    without operator intervention. Default behavior (no `on_halt` field
    or explicit `manual`) is unchanged from Phase A."""

    # ── validation ────────────────────────────────────────────────────

    def test_validate_accepts_on_halt_replan(self):
        from runner.runtime import validate_workflow
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "replan", "max_replans": 2,
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"}}],
        }
        assert validate_workflow(spec) == []

    def test_validate_rejects_unknown_on_halt(self):
        from runner.runtime import validate_workflow
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "loop_forever",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"}}],
        }
        errs = validate_workflow(spec)
        assert any("on_halt" in e for e in errs)

    def test_validate_rejects_non_int_max_replans(self):
        from runner.runtime import validate_workflow
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "replan", "max_replans": "two",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"}}],
        }
        errs = validate_workflow(spec)
        assert any("max_replans" in e for e in errs)

    def test_validate_rejects_max_replans_above_hard_ceiling(self):
        from runner.runtime import validate_workflow, _MAX_REPLANS_HARD_CEILING
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "replan",
            "max_replans": _MAX_REPLANS_HARD_CEILING + 1,
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"skill": "analyzer"}}],
        }
        errs = validate_workflow(spec)
        assert any("max_replans" in e for e in errs)

    # ── Workflow attribute storage ────────────────────────────────────

    def test_workflow_default_on_halt_is_manual(self, tmp_path):
        from runner.runtime import Workflow
        spec = {
            "workflow": "w", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, tmp_path / "rd")
        assert wf.on_halt == "manual"
        assert wf.max_replans == 0

    def test_workflow_on_halt_replan_default_max_is_1(self, tmp_path):
        from runner.runtime import Workflow
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "replan",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, tmp_path / "rd")
        assert wf.on_halt == "replan"
        assert wf.max_replans == 1

    def test_workflow_max_replans_clamped_to_hard_ceiling(self, tmp_path):
        from runner.runtime import Workflow, _MAX_REPLANS_HARD_CEILING
        spec = {
            "workflow": "w", "version": "1.1",
            "on_halt": "replan",
            "max_replans": _MAX_REPLANS_HARD_CEILING + 5,
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }
        wf = Workflow(spec, tmp_path / "rd")
        assert wf.max_replans == _MAX_REPLANS_HARD_CEILING

    # ── auto-replan execution (stubbed Planner) ───────────────────────

    def _stage_halting_project(self, tmp_path: Path) -> Path:
        """Project with a tool node that always halts (verify always
        fails). After auto-replan installs a different node, the new
        node succeeds. The 'replan' is implemented by the stub
        Planner returning a different YAML."""
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        # ok.sh always succeeds.
        make_executable_tool(
            scripts / "ok.sh",
            envelope_tool_body({"value": 1}),
        )
        # bad.sh emits a successful envelope but verify=false (gate fails).
        make_executable_tool(
            scripts / "bad.sh",
            envelope_tool_body({"value": 0}),
        )
        return proj

    def _initial_spec(self) -> dict:
        # bad.sh + verify "false" → schema check passes, command verify fails.
        return {
            "workflow": "rev1", "version": "1.1",
            "goal": "Make the run succeed even after halt.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/bad.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "false"},
                "retry": 1,
            }],
        }

    def _replan_spec(self) -> dict:
        # ok.sh + verify "true" — replanned shape that succeeds.
        return {
            "workflow": "rev2", "version": "1.1",
            "goal": "Make the run succeed even after halt.",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{
                "id": "x", "goal": "g", "steps": ["s"],
                "run": {"tool": "scripts/ok.sh"},
                "output_schema": {"value": "integer"},
                "verify": {"command": "true"},
                "retry": 1,
            }],
        }

    def _stub_planner(self, monkeypatch, replan_yaml: str) -> None:
        """Patch Workflow under planner-rev<N>/ with a stub returning
        the canned replan_yaml — same pattern as TestReplan."""
        from runner import runtime as rt
        orig_init = rt.Workflow.__init__

        class _StubWorkflow:
            def __init__(self, spec, run_dir, *, project_root=None,
                         resume=False, replan=False):
                self.spec = spec
                self.run_dir = Path(run_dir)
                self.run_dir.mkdir(parents=True, exist_ok=True)
                self.run_id = "stub-planner"
                self.dag_revision = 1
                self._is_user_workflow = False
                self.nodes_by_id = {
                    "render_yaml": type("N", (), {
                        "id": "render_yaml",
                        "output": {
                            "status": "success",
                            "data": {"yaml_text": replan_yaml},
                        },
                    })()
                }

            def trace(self, *args, **kwargs):  # noqa: ARG002
                pass

            def execute_dag(self, *, max_attempts=None):  # noqa: ARG002
                return "done"

            def cleanup(self):
                pass

        def patched_init(self, spec, run_dir, *, resume=False,
                         replan=False, project_root=None):
            rd = Path(run_dir).resolve()
            if "planner-rev" in rd.name:
                stub = _StubWorkflow(spec, rd, project_root=project_root,
                                     resume=resume, replan=replan)
                self.__class__ = _StubWorkflow
                self.__dict__.update(stub.__dict__)
                return
            orig_init(self, spec, rd, resume=resume, replan=replan,
                      project_root=project_root)

        monkeypatch.setattr(rt.Workflow, "__init__", patched_init)

    def test_default_no_on_halt_field_no_auto_replan(self, tmp_path,
                                                      monkeypatch):
        """Phase B preserves Phase A: workflows without on_halt halt
        as before; the auto-replan loop never fires."""
        from runner.runtime import _execute_with_optional_auto_replan
        proj = self._stage_halting_project(tmp_path)
        rd = proj / ".camflow" / "run"
        (rd.parent).mkdir(parents=True, exist_ok=True)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "prompt.txt").write_text("p")
        spec = self._initial_spec()
        del spec["on_halt"]
        del spec["max_replans"]
        # Stub the Planner so any inadvertent re-entry would be
        # observable (it shouldn't fire).
        self._stub_planner(monkeypatch, "should not be used")
        result = _execute_with_optional_auto_replan(spec, rd)
        assert result == "halted"
        # No dag_revisions/0002 was created — Phase A behavior preserved.
        assert not (rd / "dag_revisions" / "0002").exists()

    def test_on_halt_replan_creates_revision_2_and_succeeds(
            self, tmp_path, monkeypatch):
        from runner.runtime import _execute_with_optional_auto_replan
        proj = self._stage_halting_project(tmp_path)
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("solve x")
        replan_yaml_text = yaml.safe_dump(self._replan_spec(),
                                          sort_keys=False)
        self._stub_planner(monkeypatch, replan_yaml_text)

        result = _execute_with_optional_auto_replan(
            self._initial_spec(), rd)
        assert result == "done", (
            f"auto-replan should have recovered to done; got {result}"
        )
        rev2 = rd / "dag_revisions" / "0002"
        assert rev2.is_dir()
        manifest = json.loads((rev2 / "manifest.json").read_text())
        assert manifest["reason"] == "auto_replan_after_halt"
        assert manifest["replan_count"] == 1
        assert manifest["parent_revision"] == 1

    def test_max_replans_caps_loop(self, tmp_path, monkeypatch):
        """If the replanned spec ALSO fails, runtime stops after
        max_replans; it does NOT loop forever."""
        from runner.runtime import _execute_with_optional_auto_replan
        proj = self._stage_halting_project(tmp_path)
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("solve x")
        # Replan returns ANOTHER halting spec — same bad.sh + verify false.
        replan_yaml_text = yaml.safe_dump(self._initial_spec(),
                                          sort_keys=False)
        self._stub_planner(monkeypatch, replan_yaml_text)

        result = _execute_with_optional_auto_replan(
            self._initial_spec(), rd)
        assert result == "halted"
        # Exactly one auto-replan was attempted (max_replans=1).
        rev2 = rd / "dag_revisions" / "0002"
        assert rev2.is_dir()
        # And no rev 3 was created — the cap held.
        assert not (rd / "dag_revisions" / "0003").exists()

    def test_breakpoint_halt_does_not_trigger_auto_replan(
            self, tmp_path, monkeypatch):
        """Phase B auto-replan must only fire on real halts (kind=halt).
        --steps debug breakpoints (kind=breakpoint) should never
        trigger Planner re-entry."""
        from runner.runtime import _execute_with_optional_auto_replan
        proj = self._stage_halting_project(tmp_path)
        rd = proj / ".camflow" / "run"
        rd.mkdir(parents=True)
        (rd / "prompt.txt").write_text("solve x")
        self._stub_planner(monkeypatch, "should not be used")

        # max_attempts=1 + retry: 1 + verify=false → hits the
        # `breakpoint` kind via execute_dag's max_attempts path.
        spec = self._initial_spec()
        spec["nodes"][0]["retry"] = 3  # so the runtime breakpoints, not exhausts
        result = _execute_with_optional_auto_replan(
            spec, rd, max_attempts=1)
        assert result == "halted"
        halt = json.loads((rd / "halt.json").read_text())
        assert halt["kind"] == "breakpoint"
        # No auto-replan fired — breakpoints aren't real halts.
        assert not (rd / "dag_revisions" / "0002").exists()

    # ── status reporting ──────────────────────────────────────────────

    def test_status_reports_replan_progress(self, tmp_path):
        from runner.runtime import _summarize_status
        rd = tmp_path / "rd"
        rd.mkdir()
        # Write a workflow.yaml that opts in.
        (rd / "workflow.yaml").write_text(yaml.safe_dump({
            "workflow": "w", "version": "1.1",
            "on_halt": "replan", "max_replans": 2,
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }, sort_keys=False))
        # Stage two recorded revisions: 0001 (initial) and 0002 (one replan).
        (rd / "dag_revisions" / "0001").mkdir(parents=True)
        (rd / "dag_revisions" / "0001" / "manifest.json").write_text(
            json.dumps({"revision": 1, "parent_revision": None,
                        "reason": "initial_plan"}))
        (rd / "dag_revisions" / "0002").mkdir(parents=True)
        (rd / "dag_revisions" / "0002" / "manifest.json").write_text(
            json.dumps({"revision": 2, "parent_revision": 1,
                        "reason": "auto_replan_after_halt",
                        "replan_count": 1}))

        s = _summarize_status(rd)
        assert s["on_halt"] == "replan"
        assert s["max_replans"] == 2
        assert s["replan_count"] == 1

    def test_status_human_render_shows_on_halt_line(self, tmp_path):
        from runner.runtime import (_summarize_status,
                                     _render_status_human)
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "workflow.yaml").write_text(yaml.safe_dump({
            "workflow": "w", "version": "1.1",
            "on_halt": "replan", "max_replans": 1,
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }, sort_keys=False))
        s = _summarize_status(rd)
        text = _render_status_human(s)
        assert "on_halt: replan" in text
        # Used 0/1 since no auto-replans have happened yet.
        assert "0/1" in text or "0 / 1" in text

    def test_status_human_render_omits_line_for_default_manual(self, tmp_path):
        from runner.runtime import (_summarize_status,
                                     _render_status_human)
        rd = tmp_path / "rd"
        rd.mkdir()
        (rd / "workflow.yaml").write_text(yaml.safe_dump({
            "workflow": "w", "version": "1.1",
            "nodes": [{"id": "x", "goal": "g", "steps": ["s"],
                       "run": {"tool": "scripts/x.sh"}}],
        }, sort_keys=False))
        s = _summarize_status(rd)
        text = _render_status_human(s)
        assert "on_halt:" not in text  # silent baseline preserved


class TestCLI:
    def test_help_flag_exits_success(self, capsys):
        from runner.runtime import main

        assert main(["--help"]) == 0
        captured = capsys.readouterr()
        assert "Usage:" in captured.err

    def test_no_args_prints_usage_and_fails(self, capsys):
        from runner.runtime import main

        assert main([]) == 1
        captured = capsys.readouterr()
        assert "Usage:" in captured.err
