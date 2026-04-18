"""Unit tests for engine.state_enricher."""

from camflow.engine.state_enricher import (
    MAX_COMPLETED,
    MAX_FAILED_APPROACHES,
    enrich_state,
    init_structured_fields,
)


def _state():
    return init_structured_fields({"pc": "fix", "status": "running"})


def _success(**extra):
    r = {"status": "success", "summary": "did a thing",
         "state_updates": {}, "output": {}, "error": None}
    r.update(extra)
    return r


def _fail(**extra):
    r = {"status": "fail", "summary": "it broke",
         "state_updates": {}, "output": {}, "error": {"code": "NODE_FAIL"}}
    r.update(extra)
    return r


class TestInit:
    def test_init_creates_structured_fields(self):
        s = init_structured_fields({})
        assert s["iteration"] == 0
        assert s["completed"] == []
        assert s["lessons"] == []
        assert s["failed_approaches"] == []
        assert s["blocked"] is None
        assert "key_files" in s["active_state"]

    def test_init_is_idempotent(self):
        s = {"iteration": 5, "lessons": ["keep"], "completed": [{"x": 1}]}
        init_structured_fields(s)
        assert s["iteration"] == 5
        assert s["lessons"] == ["keep"]
        assert len(s["completed"]) == 1


class TestIterationCounter:
    def test_increments_each_call(self):
        s = _state()
        enrich_state(s, "a", _success())
        enrich_state(s, "b", _success())
        enrich_state(s, "c", _success())
        assert s["iteration"] == 3


class TestSuccess:
    def test_appends_to_completed(self):
        s = _state()
        enrich_state(s, "fix", _success(summary="fixed divide",
                                        state_updates={"files_touched": ["calculator.py"],
                                                       "lines": "L16-18"}))
        assert len(s["completed"]) == 1
        entry = s["completed"][0]
        assert entry["node"] == "fix"
        assert entry["action"] == "fixed divide"
        assert entry["file"] == "calculator.py"
        assert entry["lines"] == "L16-18"

    def test_success_clears_blocked(self):
        s = _state()
        s["blocked"] = {"node": "fix", "reason": "prev"}
        enrich_state(s, "fix", _success())
        assert s["blocked"] is None

    def test_success_drops_matching_failed_approaches(self):
        s = _state()
        s["failed_approaches"] = [
            {"node": "fix", "approach": "old try", "iteration": 1},
            {"node": "test", "approach": "keep me", "iteration": 2},
        ]
        enrich_state(s, "fix", _success())
        nodes = [f["node"] for f in s["failed_approaches"]]
        assert "fix" not in nodes
        assert "test" in nodes

    def test_resolved_extended(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"resolved": "bug A"}))
        assert "bug A" in s["resolved"]

    def test_resolved_dedup(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"resolved": "bug A"}))
        enrich_state(s, "fix", _success(state_updates={"resolved": "bug A"}))
        assert s["resolved"].count("bug A") == 1

    def test_completed_bounded(self):
        s = _state()
        for i in range(MAX_COMPLETED + 5):
            enrich_state(s, "fix", _success(summary=f"done {i}"))
        assert len(s["completed"]) == MAX_COMPLETED
        # oldest dropped
        assert s["completed"][0]["action"] == f"done {5}"


class TestFailure:
    def test_sets_blocked(self):
        s = _state()
        enrich_state(s, "test", _fail(summary="3 tests failed"))
        assert s["blocked"]["node"] == "test"
        assert "3 tests failed" in s["blocked"]["reason"]

    def test_appends_failed_approaches(self):
        s = _state()
        enrich_state(s, "fix", _fail(summary="tried A"))
        enrich_state(s, "fix", _fail(summary="tried B"))
        approaches = [f["approach"] for f in s["failed_approaches"]]
        assert "tried A" in approaches
        assert "tried B" in approaches

    def test_failed_approaches_bounded(self):
        s = _state()
        for i in range(MAX_FAILED_APPROACHES + 3):
            enrich_state(s, "fix", _fail(summary=f"try {i}"))
        assert len(s["failed_approaches"]) == MAX_FAILED_APPROACHES


class TestLessons:
    def test_lesson_added(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"new_lesson": "check edge cases"}))
        assert "check edge cases" in s["lessons"]

    def test_lesson_deduped(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"new_lesson": "L"}))
        enrich_state(s, "fix", _success(state_updates={"new_lesson": "L"}))
        assert s["lessons"].count("L") == 1

    def test_lesson_not_applied_to_state_updates_downstream(self):
        """enrich_state pops new_lesson so it doesn't leak as arbitrary state key."""
        r = _success(state_updates={"new_lesson": "x"})
        s = _state()
        enrich_state(s, "fix", r)
        assert "new_lesson" not in s


class TestFiles:
    def test_union_key_files(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"files_touched": ["a.py"]}))
        enrich_state(s, "fix", _success(state_updates={"files_touched": ["b.py", "a.py"]}))
        assert set(s["active_state"]["key_files"]) == {"a.py", "b.py"}

    def test_single_file_string_accepted(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"files_touched": "calc.py"}))
        assert "calc.py" in s["active_state"]["key_files"]


class TestTestOutput:
    def test_cmd_output_captured(self):
        s = _state()
        enrich_state(s, "test", _fail(), cmd_output="FAILED x\nFAILED y\n")
        assert "FAILED x" in s["test_output"]

    def test_cmd_output_truncated(self):
        s = _state()
        enrich_state(s, "test", _fail(), cmd_output="x" * 10000)
        assert len(s["test_output"]) == 3000

    def test_stdout_tail_on_fail_captured(self):
        s = _state()
        enrich_state(s, "test", _fail(output={"stdout_tail": "FAILED foo"}))
        assert s["test_output"] == "FAILED foo"

    def test_stdout_tail_on_success_not_overwritten(self):
        s = _state()
        s["test_output"] = "old"
        enrich_state(s, "test", _success(output={"stdout_tail": "clean"}))
        # success without cmd_output shouldn't overwrite test_output
        assert s["test_output"] == "old"


class TestNextSteps:
    def test_next_steps_replaced(self):
        s = _state()
        s["next_steps"] = ["old"]
        enrich_state(s, "fix", _success(state_updates={"next_steps": ["new1", "new2"]}))
        assert s["next_steps"] == ["new1", "new2"]

    def test_next_steps_string(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"next_steps": "do X"}))
        assert s["next_steps"] == ["do X"]


class TestActiveTask:
    def test_active_task_from_update(self):
        s = _state()
        enrich_state(s, "fix", _success(state_updates={"active_task": "Fix bug 1"}))
        assert s["active_task"] == "Fix bug 1"

    def test_default_active_task_when_none(self):
        s = _state()
        enrich_state(s, "fix", _success())
        assert "fix" in s["active_task"]

    def test_existing_active_task_preserved(self):
        s = _state()
        s["active_task"] = "Top-level goal"
        enrich_state(s, "fix", _success())
        assert s["active_task"] == "Top-level goal"
