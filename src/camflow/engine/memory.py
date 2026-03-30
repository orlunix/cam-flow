"""Memory store.

Implements: spec/memory.md
"""


def init_memory():
    return {
        "summaries": [],
        "lessons": []
    }


def add_summary(memory, text):
    if text:
        memory["summaries"].append(text)
    return memory


def add_lesson(memory, lesson):
    if lesson:
        memory["lessons"].append(lesson)
    return memory
