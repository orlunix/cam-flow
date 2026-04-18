"""Unit tests for engine.error_classifier."""

from camflow.engine.error_classifier import classify_error, retry_mode


class TestClassifyError:
    def test_parse_error(self):
        e = classify_error("garbage", parse_ok=False)
        assert e["code"] == "PARSE_ERROR"
        assert e["retryable"] is True

    def test_node_fail(self):
        e = classify_error("...", parse_ok=True, result={"status": "fail", "summary": "bad"})
        assert e["code"] == "NODE_FAIL"
        assert "bad" in e["reason"]

    def test_no_error_on_success(self):
        e = classify_error("...", parse_ok=True, result={"status": "success"})
        assert e is None


class TestRetryMode:
    def test_none_for_no_error(self):
        assert retry_mode(None) == "none"

    def test_transient_codes(self):
        for code in ("PARSE_ERROR", "AGENT_TIMEOUT", "AGENT_CRASH", "CAMC_ERROR"):
            assert retry_mode({"code": code}) == "transient"

    def test_task_codes(self):
        for code in ("NODE_FAIL", "CMD_FAIL", "CMD_TIMEOUT", "CMD_NOT_FOUND", "CMD_ERROR"):
            assert retry_mode({"code": code}) == "task"

    def test_unknown_defaults_to_task(self):
        # unknown codes should default to "task" — safer (adds context on retry)
        assert retry_mode({"code": "MADE_UP_CODE"}) == "task"
