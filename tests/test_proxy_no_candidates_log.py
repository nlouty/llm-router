import json

import pytest
from django.test import Client

from router.models import Model, RequestRecord
from router.services import request_logger


@pytest.fixture(autouse=True)
def reset_request_logger_cache(monkeypatch):
    monkeypatch.setattr(request_logger, "_LOG_PATH_CACHE", None)
    request_logger._REQUEST_LOG_FILE_CACHE.clear()


def _post_chat(tmp_path, monkeypatch, model_name="any-model"):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    Model.objects.create(model_name=model_name, max_tokens=65536)
    return Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": model_name, "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )


def _request_log_file(tmp_path, record_id):
    files = list(tmp_path.rglob(f"{record_id}.log"))
    assert len(files) == 1, f"expected exactly one per-request log for {record_id}, found {files}"
    return files[0]


@pytest.mark.django_db
def test_no_candidates_502_writes_per_request_log(tmp_path, monkeypatch):
    """A 502 caused by no available server must still produce a per-request log file."""
    response = _post_chat(tmp_path, monkeypatch)
    assert response.status_code == 502

    record = RequestRecord.objects.get(task_status="failed")
    assert record.status == "502 Bad Gateway"
    assert record.fail_reason == "no available server for model any-model"

    content = _request_log_file(tmp_path, record.id).read_text(encoding="utf-8")
    assert '"event": "request_received"' in content
    assert '"model": "any-model"' in content
    assert '"event": "no_candidates"' in content
    assert "no available server for model any-model" in content


@pytest.mark.django_db
def test_no_candidates_logs_request_context_with_messages_redacted(tmp_path, monkeypatch):
    """Issue #225: a 502 must keep headers + a partial body (tools/options) but
    drop ``messages`` for privacy, even on paths that previously logged nothing."""
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    Model.objects.create(model_name="any-model", max_tokens=65536)
    Client().post(
        "/v1/chat/completions",
        data=json.dumps({
            "model": "any-model",
            "messages": [{"role": "user", "content": "top-secret-prompt"}],
            "tools": [{"type": "function", "function": {"name": "do_thing"}}],
            "mcp_servers": ["weather"],
        }),
        content_type="application/json",
        **{"HTTP_AUTHORIZATION": "Bearer my-api-key", "HTTP_USER_AGENT": "opencode/1.0"},
    )

    record = RequestRecord.objects.get(task_status="failed")
    content = _request_log_file(tmp_path, record.id).read_text(encoding="utf-8")

    context_lines = [line for line in content.splitlines() if '"event": "request_context"' in line]
    assert len(context_lines) == 1
    # Lines are prefixed "[<timestamp>] " (issue #262); strip it before parsing.
    payload = json.loads(context_lines[0].partition("] ")[2])
    logged_body = json.loads(payload["request_body"])
    assert "messages" not in logged_body            # privacy: removed
    assert "top-secret-prompt" not in content        # never leaks anywhere in the log
    assert logged_body["tools"]                      # kept
    assert logged_body["mcp_servers"] == ["weather"] # kept
    assert payload["request_headers"].get("User-Agent") == "opencode/1.0"
    assert "Authorization" not in payload["request_headers"]  # secret redacted
    assert "Bearer my-api-key" not in content
