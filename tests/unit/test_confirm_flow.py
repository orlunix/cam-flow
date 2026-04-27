"""Phase B confirm flow — camflow chat --pending interactive review."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from camflow.cli_entry import chat as chat_module
from camflow.cli_entry.chat import chat_command


def _now_iso(offset_seconds: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_pending(tmp_path: Path, entries: list[dict]) -> None:
    cf = tmp_path / ".camflow"
    cf.mkdir(parents=True, exist_ok=True)
    p = cf / "control-pending.jsonl"
    p.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [
        json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _seed_pointer(tmp_path: Path) -> None:
    cf = tmp_path / ".camflow"
    cf.mkdir(parents=True, exist_ok=True)
    (cf / "steward.json").write_text(json.dumps({
        "agent_id": "steward-7c2a", "name": "steward-7c2a",
    }))


# ---- empty queue --------------------------------------------------------


class TestEmptyQueue:
    def test_no_pending_returns_0(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        rc = chat_command(
            ["--project-dir", str(tmp_path), "--pending"]
        )
        assert rc == 0
        assert "no pending confirms" in capsys.readouterr().out


# ---- approve / reject / never -------------------------------------------


class TestUserDecisions:
    def test_yes_to_all_moves_to_approved(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [{
            "ts": _now_iso(),
            "expires_at": _now_iso(offset_seconds=1800),
            "verb": "spawn",
            "args": {"node": "fix"},
            "issued_by": "steward-7c2a",
            "flow_id": "flow_xx",
        }])
        rc = chat_command([
            "--project-dir", str(tmp_path),
            "--pending", "--yes-to-all",
        ])
        assert rc == 0
        approved = _read_jsonl(tmp_path / ".camflow" / "control.jsonl")
        assert len(approved) == 1
        assert approved[0]["verb"] == "spawn"
        # Pending queue truncated.
        assert _read_jsonl(
            tmp_path / ".camflow" / "control-pending.jsonl"
        ) == []

    def test_no_to_all_moves_to_rejected(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [{
            "ts": _now_iso(),
            "expires_at": _now_iso(offset_seconds=1800),
            "verb": "skip",
            "args": {"reason": "test"},
            "issued_by": "user",
            "flow_id": "flow_xx",
        }])
        rc = chat_command([
            "--project-dir", str(tmp_path),
            "--pending", "--no-to-all",
        ])
        assert rc == 0
        rejected = _read_jsonl(
            tmp_path / ".camflow" / "control-rejected.jsonl"
        )
        assert len(rejected) == 1
        assert rejected[0]["resolution"] == "rejected"
        approved = tmp_path / ".camflow" / "control.jsonl"
        assert not approved.exists() or approved.read_text() == ""

    def test_never_sets_block_override(
        self, tmp_path, monkeypatch, capsys,
    ):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [{
            "ts": _now_iso(),
            "expires_at": _now_iso(offset_seconds=1800),
            "verb": "kill-worker",
            "args": {"reason": "x"},
            "issued_by": "steward-7c2a",
            "flow_id": "flow_xx",
        }])
        # Drive interactive prompt with stdin.
        monkeypatch.setattr(
            "builtins.input", lambda *a, **k: "never",
        )
        rc = chat_command(
            ["--project-dir", str(tmp_path), "--pending"]
        )
        assert rc == 0
        # Override persisted.
        from camflow.steward.autonomy import LEVEL_BLOCK, load_config
        cfg = load_config(tmp_path)
        assert cfg.overrides.get("kill-worker") == LEVEL_BLOCK
        # Entry recorded as blocked.
        rejected = _read_jsonl(
            tmp_path / ".camflow" / "control-rejected.jsonl"
        )
        assert rejected[0]["resolution"] == "blocked-by-user"


# ---- timeout-deny -------------------------------------------------------


class TestTimeoutDeny:
    def test_expired_entry_auto_rejected(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [
            {
                "ts": _now_iso(),
                "expires_at": _now_iso(offset_seconds=-60),  # past
                "verb": "spawn",
                "args": {"node": "old"},
                "issued_by": "user",
                "flow_id": "flow_a",
            },
            {
                "ts": _now_iso(),
                "expires_at": _now_iso(offset_seconds=600),  # future
                "verb": "spawn",
                "args": {"node": "new"},
                "issued_by": "user",
                "flow_id": "flow_a",
            },
        ])
        rc = chat_command([
            "--project-dir", str(tmp_path),
            "--pending", "--no-to-all",  # reject the surviving one
        ])
        assert rc == 0
        rejected = _read_jsonl(
            tmp_path / ".camflow" / "control-rejected.jsonl"
        )
        # Both entries rejected — one timeout, one user-rejected.
        assert len(rejected) == 2
        resolutions = sorted(r["resolution"] for r in rejected)
        assert resolutions == ["rejected", "timeout-rejected"]

    def test_all_expired_no_prompt(self, tmp_path, capsys):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [{
            "ts": _now_iso(),
            "expires_at": _now_iso(offset_seconds=-1),
            "verb": "skip",
            "args": {"reason": "stale"},
            "issued_by": "user",
        }])
        rc = chat_command(
            ["--project-dir", str(tmp_path), "--pending"]
        )
        assert rc == 0
        rejected = _read_jsonl(
            tmp_path / ".camflow" / "control-rejected.jsonl"
        )
        assert len(rejected) == 1
        assert rejected[0]["resolution"] == "timeout-rejected"


# ---- trace integration -------------------------------------------------


class TestTraceEvents:
    def test_each_resolution_emits_trace(self, tmp_path):
        _seed_pointer(tmp_path)
        _write_pending(tmp_path, [
            {
                "ts": _now_iso(),
                "expires_at": _now_iso(offset_seconds=600),
                "verb": "spawn",
                "args": {"node": "x"},
                "issued_by": "user",
                "flow_id": "f",
            },
        ])
        chat_command([
            "--project-dir", str(tmp_path),
            "--pending", "--yes-to-all",
        ])
        trace = _read_jsonl(tmp_path / ".camflow" / "trace.log")
        resolutions = [
            e for e in trace if e.get("kind") == "control_resolution"
        ]
        assert any(r["resolution"] == "approved" for r in resolutions)


# ---- timeout config integration ---------------------------------------


def test_queue_pending_uses_config_timeout(tmp_path):
    """Verify ctl.dispatch reads steward-config.yaml's
    confirm.timeout_minutes when stamping expires_at."""
    from camflow.cli_entry.ctl import dispatch
    from camflow.cli_entry.ctl_mutate import _register_all
    from camflow.steward.autonomy import (
        AutonomyConfig, PRESET_DEFAULT, write_config,
    )
    _register_all()

    # 5-minute custom timeout.
    write_config(
        tmp_path,
        AutonomyConfig(preset=PRESET_DEFAULT, confirm_timeout_minutes=5),
    )

    rc = dispatch(
        "spawn", ["--node", "build"],
        project_dir=str(tmp_path),
    )
    assert rc == 0
    pending = _read_jsonl(
        tmp_path / ".camflow" / "control-pending.jsonl"
    )
    assert len(pending) == 1
    expires_at = pending[0]["expires_at"]
    issued_at = pending[0]["ts"]
    # The 5-minute window means expires - issued ~= 300s.
    issued_dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    delta = (expires_dt - issued_dt).total_seconds()
    assert 290 < delta < 310
