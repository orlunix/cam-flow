"""Unit tests for camflow.cli_entry.ctl — dispatcher framework only.

Verbs themselves (read-state, replan, etc.) are tested in their own
suites once they land in later commits. This file just exercises the
registry, the queue helpers, and the CLI argument plumbing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from camflow.cli_entry import ctl as ctl_module
from camflow.cli_entry.ctl import (
    AUTONOMY_AUTONOMOUS,
    AUTONOMY_CONFIRM,
    CONTROL_PENDING,
    CONTROL_QUEUE,
    VerbSpec,
    ctl_command,
    dispatch,
    list_verb_names,
    queue_approved,
    queue_pending,
    register_verb,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty verb registry; restore after."""
    saved = dict(ctl_module.VERBS)
    ctl_module.VERBS.clear()
    yield
    ctl_module.VERBS.clear()
    ctl_module.VERBS.update(saved)


# ---- VerbSpec validation ----------------------------------------------


class TestVerbSpec:
    def test_invalid_autonomy_rejected(self):
        with pytest.raises(ValueError, match="autonomy must be one of"):
            VerbSpec(name="x", autonomy="maybe", handler=lambda a, p: 0)

    def test_autonomous_requires_handler(self):
        with pytest.raises(ValueError, match="autonomous verbs require a handler"):
            VerbSpec(name="x", autonomy=AUTONOMY_AUTONOMOUS, handler=None)

    def test_confirm_handler_optional(self):
        # Confirm verbs don't run a handler — they queue.
        spec = VerbSpec(name="x", autonomy=AUTONOMY_CONFIRM)
        assert spec.handler is None


# ---- registry ----------------------------------------------------------


class TestRegisterVerb:
    def test_register_and_list(self):
        register_verb(VerbSpec(
            name="read-state",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=lambda a, p: 0,
            help="print state.json",
        ))
        register_verb(VerbSpec(
            name="replan",
            autonomy=AUTONOMY_CONFIRM,
            help="re-spawn Planner",
        ))
        assert list_verb_names() == ["read-state", "replan"]

    def test_duplicate_name_rejected(self):
        register_verb(VerbSpec(
            name="dup",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=lambda a, p: 0,
        ))
        with pytest.raises(ValueError, match="already registered"):
            register_verb(VerbSpec(
                name="dup",
                autonomy=AUTONOMY_AUTONOMOUS,
                handler=lambda a, p: 0,
            ))


# ---- dispatch ----------------------------------------------------------


class TestDispatch:
    def test_unknown_verb_returns_2(self, tmp_path, capsys):
        rc = dispatch("nope", [], project_dir=str(tmp_path))
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown verb 'nope'" in err

    def test_autonomous_runs_handler(self, tmp_path):
        called = {}

        def _handler(args, project_dir):
            called["project_dir"] = project_dir
            called["foo"] = args.foo
            return 0

        def _add_args(p):
            p.add_argument("--foo", default="bar")

        register_verb(VerbSpec(
            name="echo",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=_handler,
            add_args=_add_args,
        ))
        rc = dispatch("echo", ["--foo", "qux"], project_dir=str(tmp_path))
        assert rc == 0
        assert called["foo"] == "qux"
        assert called["project_dir"] == str(tmp_path)

    def test_handler_exception_becomes_exit_1(self, tmp_path, capsys):
        def _handler(args, project_dir):
            raise RuntimeError("boom")

        register_verb(VerbSpec(
            name="raises",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=_handler,
        ))
        rc = dispatch("raises", [], project_dir=str(tmp_path))
        assert rc == 1
        assert "boom" in capsys.readouterr().err

    def test_confirm_verb_queues_to_pending(self, tmp_path):
        def _add_args(p):
            p.add_argument("--reason", required=True)

        register_verb(VerbSpec(
            name="replan",
            autonomy=AUTONOMY_CONFIRM,
            add_args=_add_args,
            help="re-spawn Planner",
        ))

        rc = dispatch(
            "replan",
            ["--reason", "OOM on test"],
            project_dir=str(tmp_path),
        )
        assert rc == 0

        pending = tmp_path / ".camflow" / CONTROL_PENDING
        assert pending.exists()
        lines = pending.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["verb"] == "replan"
        assert entry["args"] == {"reason": "OOM on test"}
        assert "expires_at" in entry

        # control.jsonl (approved queue) must NOT have it yet.
        approved = tmp_path / ".camflow" / CONTROL_QUEUE
        assert not approved.exists()

    def test_argparse_error_returns_nonzero(self, tmp_path, capsys):
        def _add_args(p):
            p.add_argument("--required", required=True)

        register_verb(VerbSpec(
            name="strict",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=lambda a, p: 0,
            add_args=_add_args,
        ))
        rc = dispatch("strict", [], project_dir=str(tmp_path))
        assert rc != 0


# ---- queue helpers -----------------------------------------------------


class TestQueues:
    def test_queue_pending_writes_jsonl_and_trace(self, tmp_path):
        queue_pending(
            tmp_path,
            verb="replan",
            args={"reason": "OOM"},
            issued_by="steward-7c2a",
            flow_id="flow_001",
        )

        pending = tmp_path / ".camflow" / CONTROL_PENDING
        entry = json.loads(pending.read_text().strip())
        assert entry["verb"] == "replan"
        assert entry["issued_by"] == "steward-7c2a"
        assert entry["flow_id"] == "flow_001"

        # control_command trace event written too.
        trace = tmp_path / ".camflow" / "trace.log"
        assert trace.exists()
        events = [json.loads(l) for l in trace.read_text().splitlines() if l.strip()]
        assert len(events) == 1
        assert events[0]["kind"] == "control_command"
        assert events[0]["verb"] == "replan"
        assert events[0]["queue"] == "pending"
        assert events[0]["actor"] == "steward-7c2a"

    def test_queue_approved_writes_to_control_jsonl(self, tmp_path):
        queue_approved(
            tmp_path,
            verb="kill-worker",
            args={},
            issued_by="steward-7c2a",
            flow_id="flow_001",
        )
        approved = tmp_path / ".camflow" / CONTROL_QUEUE
        entry = json.loads(approved.read_text().strip())
        assert entry["verb"] == "kill-worker"

        trace = tmp_path / ".camflow" / "trace.log"
        events = [json.loads(l) for l in trace.read_text().splitlines() if l.strip()]
        assert events[0]["queue"] == "approved"


# ---- ctl_command CLI plumbing ------------------------------------------


class TestCtlCommand:
    def test_no_args_prints_help(self, capsys):
        rc = ctl_command([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "usage: camflow ctl" in out

    def test_help_flag_prints_help(self, capsys):
        rc = ctl_command(["--help"])
        assert rc == 0
        assert "usage: camflow ctl" in capsys.readouterr().out

    def test_project_dir_flag_passes_through(self, tmp_path):
        seen = {}

        def _handler(args, project_dir):
            seen["project_dir"] = project_dir
            return 0

        register_verb(VerbSpec(
            name="check",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=_handler,
        ))
        rc = ctl_command(["check", "--project-dir", str(tmp_path)])
        assert rc == 0
        assert seen["project_dir"] == str(tmp_path)

    def test_project_dir_equals_form(self, tmp_path):
        seen = {}

        def _handler(args, project_dir):
            seen["project_dir"] = project_dir
            return 0

        register_verb(VerbSpec(
            name="check",
            autonomy=AUTONOMY_AUTONOMOUS,
            handler=_handler,
        ))
        rc = ctl_command(["check", f"--project-dir={tmp_path}"])
        assert rc == 0
        assert seen["project_dir"] == str(tmp_path)
