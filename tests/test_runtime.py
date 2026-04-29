"""Unit + smoke tests for runner.runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make src/ importable without `pip install -e .`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runner.runtime import (  # noqa: E402
    ExprError,
    eval_expr,
    render_deep,
    run_workflow,
    validate_workflow,
)


# ─── Expression evaluator ──────────────────────────────────────────────

class TestExprEval:
    def test_int_literal(self):
        assert eval_expr("42", {}) == 42

    def test_string_literal(self):
        assert eval_expr("'hello'", {}) == "hello"

    def test_bool_literal(self):
        assert eval_expr("True", {}) is True

    def test_compare(self):
        assert eval_expr("1 == 1", {}) is True
        assert eval_expr("1 != 1", {}) is False
        assert eval_expr("2 > 1", {}) is True

    def test_chained_attr(self):
        ctx = {"state": {"x": {"y": 5}}}
        assert eval_expr("state.x.y", ctx) == 5

    def test_attr_missing_raises(self):
        ctx = {"state": {}}
        with pytest.raises(ExprError):
            eval_expr("state.missing", ctx)

    def test_subscript_list(self):
        ctx = {"a": [10, 20, 30]}
        assert eval_expr("a[1]", ctx) == 20

    def test_bool_ops(self):
        assert eval_expr("True and False", {}) is False
        assert eval_expr("True or False", {}) is True
        assert eval_expr("not False", {}) is True

    def test_unsupported_arithmetic(self):
        with pytest.raises(ExprError):
            eval_expr("1 + 1", {})

    def test_unsupported_call(self):
        with pytest.raises(ExprError):
            eval_expr("len('a')", {})


# ─── Template renderer ──────────────────────────────────────────────────

class TestRender:
    def test_simple_substitution(self):
        ctx = {"state": {"err": "boom"}}
        assert render_deep("err: {{state.err}}", ctx) == "err: boom"

    def test_optional_missing_returns_empty(self):
        ctx = {"state": {}}
        assert render_deep("{{state.missing?}}", ctx) == ""

    def test_optional_present_returns_value(self):
        ctx = {"state": {"x": "yes"}}
        assert render_deep("{{state.x?}}", ctx) == "yes"

    def test_required_missing_raises(self):
        ctx = {"state": {}}
        with pytest.raises(ExprError):
            render_deep("{{state.missing}}", ctx)

    def test_dict_value_serialized_as_json(self):
        ctx = {"state": {"d": {"k": 1}}}
        assert render_deep("{{state.d}}", ctx) == '{"k": 1}'

    def test_deep_render_in_dict(self):
        ctx = {"state": {"x": "1"}}
        out = render_deep({"a": "{{state.x}}", "b": ["{{state.x}}"]}, ctx)
        assert out == {"a": "1", "b": ["1"]}


# ─── Validation ─────────────────────────────────────────────────────────

class TestValidate:
    def test_valid(self):
        wf = {"nodes": [{"id": "a", "uses": "tool.x"}]}
        assert validate_workflow(wf) == []

    def test_unknown_dep(self):
        wf = {"nodes": [{"id": "a", "uses": "tool.x", "needs": ["zzz"]}]}
        errs = validate_workflow(wf)
        assert any("unknown" in e for e in errs)

    def test_cycle(self):
        wf = {"nodes": [
            {"id": "a", "uses": "tool.x", "needs": ["b"]},
            {"id": "b", "uses": "tool.x", "needs": ["a"]},
        ]}
        errs = validate_workflow(wf)
        assert any("cycle" in e for e in errs)

    def test_duplicate_id(self):
        wf = {"nodes": [
            {"id": "a", "uses": "tool.x"},
            {"id": "a", "uses": "tool.x"},
        ]}
        errs = validate_workflow(wf)
        assert any("duplicate" in e for e in errs)


# ─── End-to-end smoke ───────────────────────────────────────────────────

ECHO_DEMO_DIR = ROOT / "examples" / "echo-retry"


class TestSmokeEchoDemo:
    def test_workflow_runs_to_completion(self, tmp_path):
        import yaml
        wf = yaml.safe_load((ECHO_DEMO_DIR / "workflow.yaml").read_text())
        state = json.loads((ECHO_DEMO_DIR / "state.json").read_text())
        result = run_workflow(wf, state, tmp_path)
        assert result == "success"

    def test_trace_contains_expected_events(self, tmp_path):
        import yaml
        wf = yaml.safe_load((ECHO_DEMO_DIR / "workflow.yaml").read_text())
        state = json.loads((ECHO_DEMO_DIR / "state.json").read_text())
        run_workflow(wf, state, tmp_path)
        events = [
            json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()
        ]
        kinds = [e["event"] for e in events]
        assert "workflow_started" in kinds
        assert "workflow_completed" in kinds
        # summarize_success must RUN (when=passed==true)
        completed = [e for e in events if e["event"] == "node_completed"]
        assert any(e["node"] == "summarize_success" for e in completed), \
            "summarize_success should have run because test.passed=true"
        # summarize_failure must be skipped (when=passed==false)
        skipped = [e for e in events if e["event"] == "node_skipped"]
        assert any(e["node"] == "summarize_failure" for e in skipped)
        # neither summarize_* should have hit a when-error
        when_errs = [e for e in skipped if "when error" in e.get("reason", "")]
        assert when_errs == [], f"unexpected when errors: {when_errs}"

    def test_attempts_persisted_to_disk(self, tmp_path):
        import yaml
        wf = yaml.safe_load((ECHO_DEMO_DIR / "workflow.yaml").read_text())
        state = json.loads((ECHO_DEMO_DIR / "state.json").read_text())
        run_workflow(wf, state, tmp_path)
        # spot-check one node's output JSON
        out = json.loads((tmp_path / "nodes" / "fix" / "attempt-1" / "output.json").read_text())
        assert out["status"] == "success"
        assert out["data"]["patch"].startswith("diff")


# ─── Skip propagation on failure ────────────────────────────────────────

class TestHaltAndSkipPropagation:
    def test_failure_without_retry_halts(self, tmp_path):
        """Node fails + no retry → workflow_halted (was workflow_failed in v<=0.6
        path-retry semantics). Downstream nodes get status=skipped."""
        wf = {
            "workflow": "fail_demo",
            "version": 0.6,
            "nodes": [
                {"id": "a", "mock": {"status": "failure",
                                     "error": {"code": "BOOM", "message": "x"}}},
                {"id": "b", "needs": ["a"], "mock": {"status": "success"}},
            ],
        }
        result = run_workflow(wf, {}, tmp_path)
        assert result == "halted"
        events = [
            json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()
        ]
        # workflow_halted recorded
        assert any(e["event"] == "workflow_halted" for e in events)
        # halt.json sidecar written
        halt = json.loads((tmp_path / "halt.json").read_text())
        assert halt["halted_node"] == "a"
        # b should be marked skipped (upstream halted)
        skipped = [e for e in events if e["event"] == "node_skipped"]
        assert any(e["node"] == "b" for e in skipped)

    def test_explicit_halted_status_from_node(self, tmp_path):
        """Skill/tool envelope returning status=halted halts the workflow."""
        wf = {
            "workflow": "explicit_halt",
            "version": 0.6,
            "nodes": [
                {"id": "a", "mock": {
                    "status": "halted",
                    "error": {"code": "NEED_HELP",
                              "message": "ambiguous instruction; need clarification"},
                }},
                {"id": "b", "needs": ["a"], "mock": {"status": "success"}},
            ],
        }
        result = run_workflow(wf, {}, tmp_path)
        assert result == "halted"
        halt = json.loads((tmp_path / "halt.json").read_text())
        assert halt["halted_node"] == "a"
        assert halt["envelope"]["error"]["code"] == "NEED_HELP"

    def test_auto_schema_check_without_verify_list(self, tmp_path):
        """Schema is checked automatically — user need not declare {type: schema}."""
        wf = {
            "workflow": "auto_schema",
            "version": 0.6,
            "nodes": [
                {"id": "n",
                 "mock": {"status": "success", "data": {"wrong_field": 1}},
                 "output_schema": {"required_field": "string"}},
                # NO verify list at all
            ],
        }
        result = run_workflow(wf, {}, tmp_path)
        assert result == "halted"   # schema fail → no retry → halt
        events = [json.loads(line) for line in
                  (tmp_path / "trace.jsonl").read_text().splitlines()]
        assert any(e["event"] == "verify_failed" for e in events)
        assert any(
            "missing field 'required_field'" in e.get("reason", "")
            for e in events
        )

    def test_workspace_dir_created_for_tool(self, tmp_path):
        """Tools see a per-attempt workspace dir as cwd + CAMFLOW_WORKSPACE."""
        proj = tmp_path / "proj"
        (proj / "tools").mkdir(parents=True)
        # tool prints CAMFLOW_WORKSPACE in its data; also writes a file there
        (proj / "tools" / "ws.sh").write_text(
            '#!/usr/bin/env bash\n'
            'set -e\n'
            'echo "from-tool" > written-by-tool.txt\n'
            'cat <<EOF\n'
            '{"status":"success","data":{"ws":"$CAMFLOW_WORKSPACE"},'
            '"error":null,"metrics":{},"artifacts":[]}\n'
            'EOF\n'
        )
        (proj / "tools" / "ws.sh").chmod(0o755)
        wf = {"workflow": "ws", "version": 0.6,
              "nodes": [{"id": "n", "uses": "tool.ws"}]}
        rd = proj / ".camflow" / "runs" / "test-run"
        run_workflow(wf, {}, rd)
        # workspace dir exists under attempt-1
        ws = rd / "nodes" / "n" / "attempt-1" / "workspace"
        assert ws.is_dir()
        assert (ws / "input.json").exists()
        assert (ws / "raw_stdout.txt").exists()
        # tool's cwd was the workspace — file landed inside
        assert (ws / "written-by-tool.txt").read_text().strip() == "from-tool"

    def test_retry_exhausted_halts(self, tmp_path):
        """retry max_attempts reached without satisfying `until` → halt."""
        wf = {
            "workflow": "exhaust_demo",
            "version": 0.6,
            "nodes": [
                {"id": "n", "mock": {"status": "success",
                                     "data": {"ok": False}},
                 "output_schema": {"ok": "boolean"},
                 "retry": {"until": "nodes.n.latest.output.data.ok == true",
                           "max_attempts": 2}},
            ],
        }
        result = run_workflow(wf, {}, tmp_path)
        assert result == "halted"
        events = [json.loads(line) for line in
                  (tmp_path / "trace.jsonl").read_text().splitlines()]
        assert any(e["event"] == "retry_exhausted" for e in events)
        assert any(e["event"] == "workflow_halted" for e in events)


# ─── CLI subcommands: status / trace / resume / stop ────────────────────

class TestCLISubcommands:
    def _seed_halted_run(self, tmp_path):
        """Helper: seed a tmp run dir by running halt-demo into it."""
        from runner.runtime import main as cli_main
        wf = {
            "workflow": "exhaust_demo",
            "version": 0.6,
            "nodes": [
                {"id": "n", "mock": {"status": "success",
                                     "data": {"ok": False, "feedback": "bad"}},
                 "output_schema": {"ok": "boolean", "feedback": "string"},
                 "retry": {"until": "nodes.n.latest.output.data.ok == true",
                           "max_attempts": 2}},
            ],
        }
        rd = tmp_path / "run"
        run_workflow(wf, {}, rd)
        return rd

    def test_runner_pid_cleaned_after_run(self, tmp_path):
        rd = self._seed_halted_run(tmp_path)
        assert not (rd / "runner.pid").exists()

    def test_status_shows_halt(self, tmp_path, capsys):
        from runner.runtime import main as cli_main
        rd = self._seed_halted_run(tmp_path)
        rc = cli_main(["status", str(rd)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "state:    halted" in out
        assert "HALTED at: n#2" in out

    def test_status_json(self, tmp_path, capsys):
        from runner.runtime import main as cli_main
        rd = self._seed_halted_run(tmp_path)
        rc = cli_main(["status", str(rd), "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        s = json.loads(out)
        assert s["halted"] is True
        assert s["halt"]["halted_node"] == "n"

    def test_trace_tail(self, tmp_path, capsys):
        from runner.runtime import main as cli_main
        rd = self._seed_halted_run(tmp_path)
        rc = cli_main(["trace", str(rd), "--tail", "3"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "workflow_halted" in out
        # Should be 3 lines
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 3

    def test_resume_continues_step_numbering(self, tmp_path):
        from runner.runtime import main as cli_main
        rd = self._seed_halted_run(tmp_path)
        before = (rd / "trace.jsonl").read_text().splitlines()
        cli_main(["resume", str(rd), "--feedback", "force"])
        after = (rd / "trace.jsonl").read_text().splitlines()
        # Resume should append, not overwrite
        assert len(after) > len(before)
        # Step numbers should be globally monotonic
        steps = [json.loads(line)["step"] for line in after]
        assert steps == sorted(steps)

    def test_stop_no_pid_returns_error(self, tmp_path, capsys):
        from runner.runtime import main as cli_main
        rd = self._seed_halted_run(tmp_path)
        # halt-demo has terminated; runner.pid should not exist
        rc = cli_main(["stop", str(rd)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "no runner.pid" in err


# ─── Retry demo (end-to-end retry / feedback / multi-step) ─────────────

class TestRetryDemo:
    def test_multi_step_retry_to_success(self, tmp_path):
        import shutil
        import yaml

        src = ROOT / "examples" / "retry-demo"
        dst = tmp_path / "retry-demo"
        shutil.copytree(src, dst)
        for tool in (dst / "tools").iterdir():
            tool.chmod(0o755)

        wf = yaml.safe_load((dst / "workflow.yaml").read_text())
        state = json.loads((dst / "state.json").read_text())
        run_dir = dst / ".camflow" / "runs" / "test-run"
        result = run_workflow(wf, state, run_dir)
        assert result == "success"

        events = [
            json.loads(line) for line in
            (run_dir / "trace.jsonl").read_text().splitlines()
        ]

        # Exactly 2 retry triggers on the test node itself (passed=false at attempts 1 & 2)
        retries = [e for e in events if e["event"] == "retry_triggered"]
        assert len(retries) == 2
        assert all(e["node"] == "test" for e in retries)

        # fix runs once; test has 3 attempts (driven by tester.sh's CAMFLOW_ATTEMPT)
        fix_completes = [e for e in events
                         if e.get("node") == "fix" and e["event"] == "node_completed"]
        test_completes = [e for e in events
                          if e.get("node") == "test" and e["event"] == "node_completed"]
        assert len(fix_completes) == 1
        assert len(test_completes) == 3

        # Final summarize ran (when=passed==true was true after retries converged)
        completed_nodes = {
            e["node"] for e in events if e["event"] == "node_completed"
        }
        assert "summarize" in completed_nodes


# ─── Tool execution path ────────────────────────────────────────────────

class TestToolExec:
    def test_tool_node_runs_shell(self, tmp_path):
        # Set up a mini project with a tool
        proj = tmp_path / "proj"
        proj.mkdir()
        tools = proj / "tools"
        tools.mkdir()
        (tools / "echo.sh").write_text(
            '#!/usr/bin/env bash\n'
            'set -e\n'
            'input=$(cat)\n'
            'echo "{\\"status\\":\\"success\\",\\"data\\":{\\"got\\":$input},'
            '\\"error\\":null,\\"metrics\\":{},\\"artifacts\\":[]}"\n'
        )
        (tools / "echo.sh").chmod(0o755)
        wf = {
            "workflow": "tool_demo",
            "version": 0.6,
            "nodes": [
                {"id": "n", "uses": "tool.echo", "input": {"x": 1}},
            ],
        }
        # Run dir layout: <project>/.camflow/runs/<id>
        run_dir = proj / ".camflow" / "runs" / "test-run"
        result = run_workflow(wf, {}, run_dir)
        assert result == "success"
        out = json.loads((run_dir / "nodes" / "n" / "attempt-1" / "output.json").read_text())
        assert out["status"] == "success"
        assert out["data"]["got"] == {"x": 1}
