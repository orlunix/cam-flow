"""Input reference resolution.

Implements: spec/input-ref.md
"""


def resolve_refs(text, state):
    if not text:
        return ""

    result = text
    for k, v in state.items():
        result = result.replace(f"{{{{state.{k}}}}}", str(v))

    return result
