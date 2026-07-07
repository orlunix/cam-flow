import json
from pathlib import Path

import pytest

from runner import v12
from runner.runtime import Node, auto_schema_check, build_run_prompt, empty_envelope


def test_object_output_schema_is_enforced():
    ok, _ = auto_schema_check(empty_envelope("success", data={"verdict": {"type": "RTL"}}), {"verdict": "object"})
    assert ok
    ok, reason = auto_schema_check(empty_envelope("success", data={"verdict": "RTL"}), {"verdict": "object"})
    assert not ok and "verdict" in reason


def test_input_schema_requires_input():
    with pytest.raises(ValueError, match="--input is required"):
        v12.load_input(None, {"case_id": "string"})


def test_v12_rejects_dynamic_control_key(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n")
    spec = {"workflow": "x", "version": "1.2", "nodes": [{"id": "n", "goal": "g", "steps": ["s"], "run": {"skill": "demo"}, "next": "bad"}]}
    assert any("unknown keys" in error for error in v12.validate_workflow(spec, tmp_path))


def test_prompt_contains_workflow_input():
    node = Node.from_dict({"id": "n", "goal": "g", "steps": ["s"], "run": {"skill": "demo"}})
    prompt = build_run_prompt(node, {"run_input": {"case_id": "case-1"}})
    assert "# Workflow Input" in prompt
    assert '"case_id": "case-1"' in prompt


def test_case_slug_and_collision_safe_name(tmp_path):
    assert v12.case_slug("raw/path:1", "fallback") == "raw_path_1"
    assert v12.case_slug("../", "fallback") == "case"


def test_v12_rerun_mode_does_not_expose_dag_revision(tmp_path, monkeypatch):
    from runner import runtime as rt
    root = tmp_path
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n")
    run = root / "run"
    spec = {"workflow": "x", "version": "1.2", "nodes": [{"id": "n", "goal": "g", "steps": ["s"], "run": {"skill": "demo"}, "verify": {"command": "true"}}]}
    (run / "nodes" / "n" / "attempt-1").mkdir(parents=True)
    (run / "workflow.yaml").write_text(json.dumps(spec))
    (run / "input.json").write_text(json.dumps({"case_id": "c"}))
    (run / "nodes" / "n" / "attempt-1" / "output.json").write_text(json.dumps({"status": "success", "data": {}, "error": None, "feedback": None, "request_human": False}))
    seen = []
    def fake(**kw):
        seen.append(json.loads((Path(kw["workspace"]) / "input.json").read_text()))
        env = {"status": "success", "data": {}, "error": None, "feedback": None, "request_human": False}
        (Path(kw["workspace"]) / kw["output_file"]).write_text(json.dumps(env))
        return "agent", env
    monkeypatch.setattr(rt.camc, "run_and_collect", fake)
    assert rt._do_rerun(run, "n", "", None) == 0
    assert "dag_revision" not in seen[0]


def test_pack_copies_only_reusable_artifacts(tmp_path):
    source = tmp_path / "plan"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n")
    (source / "workflow.yaml").write_text("workflow: demo\nversion: '1.2'\nnodes:\n  - id: n\n    goal: g\n    steps: [s]\n    run: {skill: demo}\n")
    (source / "input.template.json").write_text('{"case_id": "string"}')
    (source / "input.json").write_text('{"case_id": "real"}')
    (source / "trace.jsonl").write_text('{}\n')
    output = tmp_path / "bundle"
    assert v12.cmd_pack([str(source), "--out", str(output)]) == 0
    assert (output / "workflow.yaml").is_file()
    assert (output / "skills" / "demo" / "SKILL.md").is_file()
    assert (output / "package_manifest.json").is_file()
    assert not (output / "input.json").exists()
    assert not (output / "trace.jsonl").exists()


def test_plan_generates_editable_artifacts_or_fails_for_missing_input(tmp_path, capsys):
    assert v12.cmd_plan(["debug hang case_id=bug_1 sim_log=/x/sim.log trace_log=/x/trace.log", "--out", str(tmp_path / "plan")]) == 0
    plan = tmp_path / "plan"
    assert (plan / "workflow.yaml").is_file()
    assert (plan / "input.json").is_file()
    assert (plan / "input.template.json").is_file()
    assert (plan / "skills" / "investigator" / "SKILL.md").is_file()
    assert v12.cmd_plan(["debug hang case_id=bug_1", "--out", str(tmp_path / "bad")]) == 1
    assert "Missing required fields" in capsys.readouterr().err
