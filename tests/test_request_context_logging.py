import json
from datetime import datetime

import pytest

from router.services import proxy_logging, request_logger


@pytest.fixture(autouse=True)
def reset_request_logger_cache(monkeypatch):
    monkeypatch.setattr(request_logger, "_LOG_PATH_CACHE", None)
    request_logger._REQUEST_LOG_FILE_CACHE.clear()
    monkeypatch.setattr(request_logger, "_current_log_time", lambda: datetime(2026, 6, 8, 12, 34))


def _log_lines(tmp_path):
    request_logger.flush_request_log(777)
    files = list(tmp_path.rglob("777.log"))
    assert len(files) == 1, f"expected one log file, found {files}"
    # Lines are prefixed "[<timestamp>] " (issue #262); strip it before parsing.
    return [
        json.loads(line.partition("] ")[2])
        for line in files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_log_request_context_drops_messages_keeps_tools_and_options(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    body = {
        "model": "gpt",
        "messages": [{"role": "user", "content": "secret prompt"}],
        "tools": [{"type": "function", "function": {"name": "search"}}],
        "mcp_servers": ["weather"],
        "max_tokens": 128,
        "stream": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret-token",
        "Csb-Token": "csb-secret",
        "User-Agent": "opencode/1.0",
    }

    proxy_logging.log_request_context(
        777, "POST", "/v1/chat/completions", headers, json.dumps(body).encode("utf-8")
    )

    payload = _log_lines(tmp_path)[0]
    assert payload["event"] == "request_context"
    assert payload["method"] == "POST"
    assert payload["url"] == "/v1/chat/completions"
    # messages removed entirely for privacy (issue #225)
    logged_body = json.loads(payload["request_body"])
    assert "messages" not in logged_body
    # but tools, mcp_servers, and other options are preserved
    assert logged_body["tools"] == body["tools"]
    assert logged_body["mcp_servers"] == body["mcp_servers"]
    assert logged_body["model"] == "gpt"
    assert logged_body["max_tokens"] == 128
    # secret headers redacted, others kept
    assert "Authorization" not in payload["request_headers"]
    assert "Csb-Token" not in payload["request_headers"]
    assert payload["request_headers"]["User-Agent"] == "opencode/1.0"


def test_log_request_context_handles_non_json_body(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    proxy_logging.log_request_context(777, "POST", "/v1/x", {}, b"not-json-payload")

    body_str = _log_lines(tmp_path)[0]["request_body"]
    assert body_str == "not-json-payload"


def test_log_request_context_handles_empty_body(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    proxy_logging.log_request_context(777, "POST", "/v1/x", {}, b"")

    assert _log_lines(tmp_path)[0]["request_body"] == ""


def test_log_error_detail_redacts_request_but_keeps_response(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    body = {"model": "gpt", "messages": [{"role": "user", "content": "hi"}], "tools": [1]}
    response = {"error": {"message": "context too long"}}

    proxy_logging.log_error_detail(
        777, "POST", "http://upstream/v1/chat/completions",
        {"Authorization": "Bearer x", "Content-Type": "application/json"},
        json.dumps(body).encode("utf-8"), 400, json.dumps(response).encode("utf-8"),
    )

    lines = _log_lines(tmp_path)
    # request context is emitted as its own uniform event (issue #225)
    ctx = next(p for p in lines if p["event"] == "request_context")
    assert ctx["url"] == "http://upstream/v1/chat/completions"
    assert "messages" not in json.loads(ctx["request_body"])
    assert "Authorization" not in ctx["request_headers"]
    # the upstream_error event now carries only the response
    err = next(p for p in lines if p["event"] == "upstream_error")
    assert err["response_status"] == 400
    assert json.loads(err["response_body"]) == response
    assert "request_body" not in err


def test_log_request_context_for_derives_from_context(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    class _Ctx:
        request_id = 777
        method = "POST"
        path = "chat/completions/"
        headers = {"User-Agent": "x", "Authorization": "Bearer y"}
        body = b'{"messages": [{"role": "user", "content": "z"}], "tools": []}'

    proxy_logging.log_request_context_for(_Ctx())

    payload = _log_lines(tmp_path)[0]
    assert payload["url"] == "/v1/chat/completions"
    assert "messages" not in json.loads(payload["request_body"])
    assert "Authorization" not in payload["request_headers"]
