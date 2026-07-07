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
