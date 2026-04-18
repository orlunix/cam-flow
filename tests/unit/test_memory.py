"""Unit tests for engine.memory."""

from camflow.engine.memory import (
    MAX_LESSONS,
    add_lesson_deduped,
    init_memory,
    prune_lessons,
)


def test_init_memory():
    m = init_memory()
    assert m == {"summaries": [], "lessons": []}


class TestDeduped:
    def test_adds_new_lesson(self):
        lessons = []
        add_lesson_deduped(lessons, "hello")
        assert lessons == ["hello"]

    def test_dedupes_exact_match(self):
        lessons = ["x"]
        add_lesson_deduped(lessons, "x")
        assert lessons == ["x"]

    def test_noop_on_empty(self):
        lessons = []
        add_lesson_deduped(lessons, "")
        add_lesson_deduped(lessons, None)
        add_lesson_deduped(lessons, "   ")
        assert lessons == []

    def test_strips_whitespace_before_dedup(self):
        lessons = ["foo"]
        add_lesson_deduped(lessons, "  foo  ")
        assert lessons == ["foo"]

    def test_fifo_prune_at_cap(self):
        lessons = [f"L{i}" for i in range(MAX_LESSONS)]
        add_lesson_deduped(lessons, "new")
        assert len(lessons) == MAX_LESSONS
        assert lessons[0] == "L1"  # L0 dropped
        assert lessons[-1] == "new"

    def test_returns_same_list(self):
        lessons = []
        out = add_lesson_deduped(lessons, "x")
        assert out is lessons


class TestPruneLessons:
    def test_prunes_to_cap(self):
        lessons = [f"L{i}" for i in range(15)]
        prune_lessons(lessons, max_lessons=5)
        assert len(lessons) == 5
        assert lessons[0] == "L10"
