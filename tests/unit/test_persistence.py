"""Unit tests for backend.persistence — atomic writes + crash safety."""

import json
import os

import pytest

from camflow.backend.persistence import (
    append_trace_atomic,
    load_state,
    load_trace,
    save_state_atomic,
)


class TestSaveStateAtomic:
    def test_writes_and_reads_back(self, tmp_path):
        p = tmp_path / ".camflow" / "state.json"
        save_state_atomic(str(p), {"pc": "start"})
        assert load_state(str(p)) == {"pc": "start"}

    def test_creates_parent_dir(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "state.json"
        save_state_atomic(str(p), {"x": 1})
        assert p.exists()

    def test_existing_state_preserved_on_write_failure(self, tmp_path, monkeypatch):
        """If the json dump fails mid-write, the old state.json stays intact."""
        p = tmp_path / "state.json"
        save_state_atomic(str(p), {"old": True})

        # Attempt to save an object that fails to serialize
        bad = {"x": object()}  # object() is not JSON-serializable
        with pytest.raises(TypeError):
            save_state_atomic(str(p), bad)

        # Old state still readable
        assert load_state(str(p)) == {"old": True}

        # No tmp file left behind
        leftovers = [f for f in os.listdir(tmp_path) if "tmp" in f]
        assert not leftovers, f"leftover tmp files: {leftovers}"

    def test_overwrites_atomically(self, tmp_path):
        p = tmp_path / "state.json"
        save_state_atomic(str(p), {"v": 1})
        save_state_atomic(str(p), {"v": 2})
        assert load_state(str(p)) == {"v": 2}


class TestAppendTraceAtomic:
    def test_appends_line(self, tmp_path):
        p = tmp_path / "trace.log"
        append_trace_atomic(str(p), {"step": 1})
        append_trace_atomic(str(p), {"step": 2})
        content = p.read_text().strip().split("\n")
        assert len(content) == 2
        assert json.loads(content[0]) == {"step": 1}
        assert json.loads(content[1]) == {"step": 2}

    def test_load_trace_skips_malformed(self, tmp_path):
        p = tmp_path / "trace.log"
        p.write_text('{"step": 1}\nnot json\n{"step": 2}\n')
        items = load_trace(str(p))
        # Two valid lines; the bad one is skipped
        assert [i["step"] for i in items] == [1, 2]

    def test_load_trace_missing_file(self, tmp_path):
        assert load_trace(str(tmp_path / "nope.log")) == []
