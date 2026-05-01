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
    Run,
    WorkflowParseError,
    auto_schema_check,
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
    def test_simple_attr(self):
        assert eval_expr("state.x", {"state": {"x": 1}}) == 1

    def test_chained_attr(self):
        assert eval_expr("nodes.a.output.data.y",
                         {"nodes": {"a": {"output": {"data": {"y": "hi"}}}}}) == "hi"

    def test_compare(self):
        assert eval_expr("state.x == 1", {"state": {"x": 1}}) is True
        assert eval_expr("state.x != 1", {"state": {"x": 1}}) is False

    def test_bool_ops(self):
        ctx = {"a": True, "b": False}
        assert eval_expr("a and not b", ctx) is True

    def test_undefined_name_raises(self):
        with pytest.raises(ExprError):
            eval_expr("missing.x", {})

    def test_missing_attr_raises(self):
        with pytest.raises(ExprError):
            eval_expr("state.missing", {"state": {}})

    def test_unsupported_arithmetic(self):
        with pytest.raises(ExprError):
            eval_expr("1 + 1", {})

    def test_unsupported_call(self):
        with pytest.raises(ExprError):
            eval_expr("__import__('os')", {})

    def test_render_simple(self):
        assert render_str("hello {{state.name}}",
                          {"state": {"name": "world"}}) == "hello world"

    def test_render_strict_missing(self):
        """v1.0 has NO `?` optional marker; missing → ExprError."""
        with pytest.raises(ExprError):
            render_str("{{state.missing}}", {"state": {}})

    def test_render_dict_serialized(self):
        assert render_str("{{state.x}}",
                          {"state": {"x": {"a": 1}}}) == '{"a": 1}'

    def test_render_deep(self):
        ctx = {"state": {"x": "hi", "n": 5}}
        out = render_deep(
            {"a": "{{state.x}}", "b": [{"c": "{{state.n}}"}]},
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
        assert any("cannot have both" in e for e in errs)

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
            "run": {"skill": "analyzer", "input": {"k": "v"}},
            "output_schema": {"f": "string"},
            "verify": {"command": "test 1"},
            "retry": 3,
        })
        assert n.input_template == {"k": "v"}
        assert n.output_schema == {"f": "string"}
        assert n.verify_config == {"command": "test 1"}
        assert n.retry_max == 3

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
        run = Run(wf, {}, tmp_path)
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
        run = Run(wf, {}, tmp_path)
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
        run = Run(wf, {}, tmp_path)
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

        # Tool 2: read upstream's root_cause from input, return a patch.
        make_executable_tool(
            scripts / "fix.sh",
            r"""
input_json=$(cat)
cause=$(echo "$input_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['cause'])")
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
                    "run": {
                        "tool": "scripts/fix.sh",
                        "input": {
                            "cause": "{{nodes.diagnose.output.data.root_cause}}"
                        },
                    },
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
        result = run_workflow(wf, {}, run_dir)
        assert result == "done", f"expected 'done', got {result!r}"

        # All 3 nodes succeeded
        for nid in ["diagnose", "fix", "test"]:
            out = json.loads(
                (run_dir / "nodes" / nid / "attempt-1" / "output.json").read_text()
            )
            assert out["status"] == "success", f"{nid} status={out['status']}"

        # Verify the template rendered correctly: fix's input.json should
        # contain "null deref at line 42" (came from diagnose's output)
        fix_input = json.loads(
            (run_dir / "nodes" / "fix" / "attempt-1" / "input.json").read_text()
        )
        assert "null deref at line 42" in fix_input["cause"]

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
        assert run_workflow(wf, {}, rd) == "done"
        # Second run (default_run_dir auto-archives)
        rd2 = default_run_dir(proj)
        assert run_workflow(wf, {}, rd2) == "done"

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
        result = run_workflow(wf, {}, proj / ".camflow" / "run")
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
        result = run_workflow(wf, {}, proj / ".camflow" / "run")
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
        result = run_workflow(wf, {}, rd)
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
        result = run_workflow(wf, {}, rd)
        assert result == "halted"
        # Only 1 attempt — request_human skipped retry
        attempts = list((rd / "nodes" / "n").iterdir())
        assert len(attempts) == 1
