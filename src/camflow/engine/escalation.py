"""Failure escalation ladder.

Maps a node's retry count to a per-level intervention prompt. Level
advances with consecutive failures at the same node; resets (via
`state.retry_counts[node_id] = 0`) when the node succeeds or the
workflow moves to a different node.

Roadmap: §4.2 Exception Handler — Failure Escalation Ladder.
"""

ESCALATION_PROMPTS = {
    0: "",  # L0: normal — no extra instruction
    1: (
        "WARNING: Your previous approach failed. Try a FUNDAMENTALLY "
        "DIFFERENT strategy. Do not repeat what was tried before."
    ),
    2: (
        "DEEP DIVE REQUIRED: Read the source code carefully. Form 3 "
        "distinct hypotheses about the root cause. Test each one "
        "before making changes."
    ),
    3: (
        "DIAGNOSTIC MODE: Before any fix attempt, complete this "
        "checklist: 1) Read all related files 2) Check all imports "
        "and dependencies 3) Verify the test expectations are "
        "correct 4) Check for typos and off-by-one errors 5) Look at "
        "git diff to see what changed."
    ),
    4: (
        "ESCALATION: Multiple approaches have failed. Consider: Is "
        "the task description correct? Is there a misunderstanding? "
        "Write your analysis to node-result.json with status=fail "
        "and a detailed explanation of what you tried and why it "
        "failed. A human will review."
    ),
}


def get_escalation_level(state, node_id):
    """Map retry_counts[node_id] to L0..L4.

    n = 0  → L0  (first attempt, no warning)
    n = 1  → L1  (first retry, "try different")
    n = 2  → L2  (second retry, "deep dive")
    n = 3–4 → L3 (third/fourth retry, "diagnostic")
    n >= 5 → L4  (escalation)
    """
    counts = state.get("retry_counts", {}) if isinstance(state, dict) else {}
    n = counts.get(node_id, 0) if isinstance(counts, dict) else 0
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n == 2:
        return 2
    if n <= 4:
        return 3
    return 4


def get_escalation_prompt(state, node_id):
    """Return the intervention text for the current escalation level, or ""."""
    level = get_escalation_level(state, node_id)
    return ESCALATION_PROMPTS.get(level, "")
