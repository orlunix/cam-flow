"""Error classification.

Implements: spec/error-classifier.md
"""


def classify_error(raw_output, parse_ok, result=None):
    if not parse_ok:
        return {
            "code": "PARSE_ERROR",
            "retryable": True,
            "reason": "agent output could not be parsed as JSON"
        }

    if result and result.get("status") == "fail":
        return {
            "code": "NODE_FAIL",
            "retryable": True,
            "reason": result.get("summary", "node returned fail")
        }

    return None
