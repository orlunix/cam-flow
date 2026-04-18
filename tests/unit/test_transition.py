"""Unit tests for engine.transition.resolve_next."""

from camflow.engine.transition import resolve_next


def _ok(output=None, state_updates=None, control=None):
    return {
        "status": "success",
        "summary": "",
        "output": output or {},
        "state_updates": state_updates or {},
        "control": control or {"action": None, "target": None, "reason": None},
        "error": None,
    }


def _fail(output=None, control=None):
    return {
        "status": "fail",
        "summary": "",
        "output": output or {},
        "state_updates": {},
        "control": control or {"action": None, "target": None, "reason": None},
        "error": {"code": "NODE_FAIL"},
    }


class TestAbort:
    def test_control_abort_wins(self):
        node = {"do": "cmd true", "next": "nope", "transitions": [{"if": "fail", "goto": "nope2"}]}
        result = _ok(control={"action": "abort", "target": None, "reason": None})
        t = resolve_next("a", node, result, {})
        assert t["workflow_status"] == "aborted"
        assert t["next_pc"] is None


class TestWait:
    def test_wait_stays_at_node(self):
        node = {"do": "agent x", "next": "b"}
        result = _ok(control={"action": "wait", "target": None, "reason": None})
        t = resolve_next("a", node, result, {})
        assert t["workflow_status"] == "waiting"
        assert t["next_pc"] == "a"
        assert t["resume_pc"] == "a"

    def test_wait_with_target_sets_resume_pc(self):
        node = {"do": "agent x"}
        result = _ok(control={"action": "wait", "target": "checkpoint", "reason": None})
        t = resolve_next("a", node, result, {})
        assert t["resume_pc"] == "checkpoint"


class TestIfFail:
    def test_if_fail_wins_over_next(self):
        node = {"do": "cmd x", "next": "done", "transitions": [{"if": "fail", "goto": "retry"}]}
        t = resolve_next("a", node, _fail(), {})
        assert t["next_pc"] == "retry"
        assert t["workflow_status"] == "running"

    def test_no_if_fail_rule_falls_through_to_failed(self):
        node = {"do": "cmd x"}
        t = resolve_next("a", node, _fail(), {})
        assert t["workflow_status"] == "failed"


class TestConditions:
    def test_output_key_truthy(self):
        node = {"do": "cmd x", "transitions": [{"if": "output.has_bug", "goto": "fix"}]}
        t = resolve_next("a", node, _ok(output={"has_bug": True}), {})
        assert t["next_pc"] == "fix"

    def test_output_key_falsy_skips(self):
        node = {
            "do": "cmd x",
            "next": "done",
            "transitions": [{"if": "output.has_bug", "goto": "fix"}],
        }
        t = resolve_next("a", node, _ok(output={"has_bug": False}), {})
        assert t["next_pc"] == "done"

    def test_state_key(self):
        node = {"do": "cmd x", "transitions": [{"if": "state.needs_fix", "goto": "fix"}]}
        t = resolve_next("a", node, _ok(), {"needs_fix": 1})
        assert t["next_pc"] == "fix"

    def test_rules_evaluated_in_order(self):
        node = {
            "do": "cmd x",
            "transitions": [
                {"if": "output.a", "goto": "aa"},
                {"if": "output.b", "goto": "bb"},
            ],
        }
        t = resolve_next("n", node, _ok(output={"a": 1, "b": 1}), {})
        assert t["next_pc"] == "aa"


class TestGoto:
    def test_control_goto(self):
        node = {"do": "cmd x", "next": "default"}
        result = _ok(control={"action": "goto", "target": "elsewhere", "reason": None})
        t = resolve_next("a", node, result, {})
        assert t["next_pc"] == "elsewhere"


class TestDefault:
    def test_next_when_no_conditions(self):
        node = {"do": "cmd x", "next": "b"}
        t = resolve_next("a", node, _ok(), {})
        assert t["next_pc"] == "b"

    def test_done_when_no_next(self):
        node = {"do": "cmd x"}
        t = resolve_next("a", node, _ok(), {})
        assert t["workflow_status"] == "done"
        assert t["next_pc"] is None

    def test_failed_when_fail_no_transitions(self):
        node = {"do": "cmd x"}
        t = resolve_next("a", node, _fail(), {})
        assert t["workflow_status"] == "failed"
