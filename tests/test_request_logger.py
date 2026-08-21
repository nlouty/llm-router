import json
from pathlib import Path
from datetime import datetime

import pytest

from router.services import request_logger


@pytest.fixture(autouse=True)
def reset_request_logger_cache(monkeypatch):
    monkeypatch.setattr(request_logger, "_LOG_PATH_CACHE", None)
    request_logger._REQUEST_LOG_FILE_CACHE.clear()
    request_logger.clear_request_log_buffers()
    monkeypatch.setattr(request_logger, "_current_log_time", lambda: datetime(2026, 6, 8, 12, 34))
    monkeypatch.delenv("LLM_ROUTER_VERBOSE_REQUEST_LOG", raising=False)


def test_append_request_log_writes_line(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    request_logger.append_request_log(123, '{"event":"server_attempt"}')
    request_logger.flush_request_log(123)

    path = tmp_path / "2026" / "06" / "08" / "12" / "34" / "123.log"
    assert path.read_text(encoding="utf-8") == '{"event":"server_attempt"}\n'


def test_append_request_log_resolves_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(request_logger, "BASE_DIR", tmp_path)
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", "logs/requests")

    request_logger.append_request_log(123, '{"event":"multi_server_route"}')
    request_logger.flush_request_log(123)

    path = Path(tmp_path / "logs" / "requests" / "2026" / "06" / "08" / "12" / "34" / "123.log")
    assert path.read_text(encoding="utf-8") == '{"event":"multi_server_route"}\n'


def test_append_error_log_uses_request_log_file(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    request_logger.append_request_log(123, '{"event":"server_attempt"}')
    request_logger.append_error_log(123, '{"event":"upstream_error"}')
    request_logger.flush_request_log(123)

    path = tmp_path / "2026" / "06" / "08" / "12" / "34" / "123.log"
    assert path.read_text(encoding="utf-8") == (
        '{"event":"server_attempt"}\n'
        '{"event":"upstream_error"}\n'
    )


def test_same_request_keeps_first_minute_bucket(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    times = iter([
        datetime(2026, 6, 8, 12, 34),
        datetime(2026, 6, 8, 12, 35),
    ])
    monkeypatch.setattr(request_logger, "_current_log_time", lambda: next(times))

    request_logger.append_request_log(123, "first")
    request_logger.append_request_log(123, "second")
    request_logger.flush_request_log(123)

    first_path = tmp_path / "2026" / "06" / "08" / "12" / "34" / "123.log"
    second_path = tmp_path / "2026" / "06" / "08" / "12" / "35" / "123.log"
    assert first_path.read_text(encoding="utf-8") == "first\nsecond\n"
    assert not second_path.exists()


def test_append_verbose_request_log_writes_pretty_full_json_body(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_VERBOSE_REQUEST_LOG", "1")
    request_body = {
        "model": "target-model",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "developer", "content": "developer instructions"},
            {"role": "user", "content": "first user request"},
            {"role": "assistant", "content": "assistant response"},
            {"role": "tool", "content": "tool result"},
        ],
        "tools": [{"type": "function", "function": {"name": "secret_tool"}}],
    }

    request_logger.append_verbose_request_log(123, json.dumps(request_body).encode("utf-8"))
    request_logger.flush_request_log(123)

    log_files = list(tmp_path.rglob("123.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    payload = json.loads(log_text)
    assert payload == {
        "event": "user_request",
        "request_id": 123,
        "body": request_body,
    }
    assert '\n  "body": {\n' in log_text


def test_append_verbose_request_log_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))

    request_logger.append_verbose_request_log(123, b'{"messages":[{"role":"user","content":"hello"}]}')

    assert list(tmp_path.rglob("123.log")) == []
