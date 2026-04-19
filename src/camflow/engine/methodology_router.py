"""Methodology router.

Simple keyword-based routing from a node's `do` + `with` fields to a
debugging / problem-solving methodology. The returned hint is injected
into the agent's prompt (before the task body) so the model starts with
a shape-appropriate strategy instead of improvising.

Roadmap: §4.1 Exception Handler — Methodology Router.
"""

METHODOLOGIES = {
    "rca": (
        "Methodology: RCA (Root Cause Analysis) — reproduce the issue, "
        "isolate the component, form 3 hypotheses, verify each."
    ),
    "simplify-first": (
        "Methodology: Simplify-first — question assumptions, remove "
        "unnecessary complexity, then build."
    ),
    "search-first": (
        "Methodology: Search-first — find prior art and existing "
        "solutions before writing new code."
    ),
    "working-backwards": (
        "Methodology: Working-backwards — define the desired outcome "
        "first, then design to reach it."
    ),
    "systematic-coverage": (
        "Methodology: Systematic coverage — enumerate cases, "
        "prioritize edge cases, prove correctness."
    ),
}


_KEYWORDS = [
    ("rca",                  ["fix", "bug", "debug", "error", "repair"]),
    ("simplify-first",       ["build", "compile", "make", "package"]),
    ("search-first",         ["research", "analyze", "investigate", "search"]),
    ("working-backwards",    ["design", "architect", "plan"]),
    ("systematic-coverage",  ["test", "verify", "check", "validate"]),
]


def select_methodology_label(node_id, node):
    """Return a short label ('rca' / 'simplify-first' / ... / 'none')."""
    do = node.get("do", "") if isinstance(node, dict) else ""
    with_text = node.get("with", "") if isinstance(node, dict) else ""
    name_text = (node_id or "") + " "
    combined = (name_text + do + " " + with_text).lower()

    for label, words in _KEYWORDS:
        if any(word in combined for word in words):
            return label
    return "none"


def select_methodology(node_id, node):
    """Return the full methodology hint string to inject, or empty string."""
    label = select_methodology_label(node_id, node)
    return METHODOLOGIES.get(label, "")
