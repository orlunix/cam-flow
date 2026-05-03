"""Test suite for camflow v1.0 (runner_v2).

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

Run:    pytest tests/test_v2.py -q
"""
from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from runner_v2 import camc_lib  # noqa
from runner_v2.runtime import (
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
    """Bash that prints a v1.0 envelope JSON to stdout."""
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
    exposes in v1.1; expression engine itself doesn't care about names."""

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
        """v1.1 has NO `?` optional marker; missing → ExprError."""
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
        # In v1.1, runtime auto-injects upstream envelopes under input.upstream.<id>.
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
        from runner_v2.runtime import default_run_dir
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
        from runner_v2.runtime import run_workflow
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
        from runner_v2.runtime import Workflow, run_workflow
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
        from runner_v2.runtime import run_workflow, _cmd_resume
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
        from runner_v2.runtime import run_workflow, _cmd_resume
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
        from runner_v2.runtime import run_workflow
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
