import json
import logging

import pytest
from django.test import Client
from unittest.mock import MagicMock

from router.models import Model, RequestRecord, Server
from router.services import request_logger


def _streaming_upstream(status=200, chunks=None):
    upstream = MagicMock()
    upstream.status_code = status
    upstream.reason = "OK" if status < 400 else "Bad"
    upstream.headers = {}
    upstream.iter_content = lambda chunk_size=8192: iter(chunks or [])
    return upstream


@pytest.fixture(autouse=True)
def reset_request_logger_cache(monkeypatch):
    monkeypatch.setattr(request_logger, "_LOG_PATH_CACHE", None)
    request_logger._REQUEST_LOG_FILE_CACHE.clear()


def _force_unhandled_exception(monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr("router.views.IdentityService.resolve", _boom)


@pytest.mark.django_db
def test_unhandled_exception_returns_502_with_self_describing_fail_reason(monkeypatch):
    _force_unhandled_exception(monkeypatch)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "any-model", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )

    assert response.status_code == 502
    # client-facing message stays generic; internals are not leaked.
    assert response.json()["error"]["message"] == "502 Bad Gateway"

    record = RequestRecord.objects.last()
    assert record is not None
    assert record.status == "502 Bad Gateway"
    assert record.fail_reason.startswith("unhandled ValueError")
    assert "kaboom" in record.fail_reason


@pytest.mark.django_db
def test_unhandled_exception_writes_full_traceback_to_request_log(tmp_path, monkeypatch):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    _force_unhandled_exception(monkeypatch)

    Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "any-model"}),
        content_type="application/json",
    )

    record = RequestRecord.objects.last()
    log_files = list(tmp_path.rglob(f"{record.id}.log"))
    assert len(log_files) == 1

    content = log_files[0].read_text(encoding="utf-8")
    assert "ValueError" in content
    assert "kaboom" in content
    assert "Traceback (most recent call last)" in content


@pytest.mark.django_db
def test_unhandled_exception_logs_one_line_summary_in_main_log(tmp_path, monkeypatch, caplog):
    # router.views does not propagate (install_pd_handler sets propagate=False),
    # so attach caplog's handler directly to observe its records.
    views_logger = logging.getLogger("router.views")
    caplog.handler.setLevel(logging.DEBUG)
    views_logger.addHandler(caplog.handler)
    try:
        _force_unhandled_exception(monkeypatch)
        Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "any-model"}),
            content_type="application/json",
        )
    finally:
        views_logger.removeHandler(caplog.handler)

    error_records = [
        record for record in caplog.records
        if record.name == "router.views" and record.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "\n" not in message  # exactly one line, no traceback
    assert error_records[0].exc_info is None
    assert "ValueError" in message

    # The traceback is emitted at DEBUG, which the main log (ERROR+) ignores.
    debug_records = [
        record for record in caplog.records
        if record.name == "router.views" and record.levelno == logging.DEBUG
    ]
    assert any("Traceback" in record.getMessage() for record in debug_records)


def _seed_online_model(monkeypatch):
    # Disable the background CMDB thread so it cannot collide on sqlite writes
    # during the test, and allow the parser's injected default max_tokens.
    monkeypatch.setattr("router.views.CMDBService.fetch_and_save_user", lambda self, ip: None)
    model = Model.objects.create(model_name="m", max_tokens=40000)
    Server.objects.create(model_id=model.id, base_url="http://up.example", is_online=True)
    return model


@pytest.mark.django_db
def test_unhandled_exception_inside_forward_creates_one_record(tmp_path, monkeypatch):
    # Scenario 1: an exception raised INSIDE forward() must finish the single
    # processing record forward() created (exactly one row), not propagate to
    # views.py which would create a second, orphaning the first.
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    _seed_online_model(monkeypatch)

    def _boom(*args, **kwargs):
        raise ValueError("boom-inside-forward")

    monkeypatch.setattr("router.services.proxy.ProxyService._route_with_retry", _boom)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )

    assert response.status_code == 502
    # Exactly one row: the processing record finished in place, no duplicate.
    assert RequestRecord.objects.count() == 1
    record = RequestRecord.objects.get()
    assert record.task_status == "failed"
    assert record.status == "502 Bad Gateway"
    assert record.fail_reason.startswith("unhandled ValueError")
    assert "boom-inside-forward" in record.fail_reason


@pytest.mark.django_db
def test_unhandled_exception_inside_forward_writes_traceback_to_its_log(tmp_path, monkeypatch):
    # The full traceback must land in the SAME record's per-request log file,
    # not a different record id's file.
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    _seed_online_model(monkeypatch)

    def _boom(*args, **kwargs):
        raise ValueError("boom-inside-forward")

    monkeypatch.setattr("router.services.proxy.ProxyService._route_with_retry", _boom)

    Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )

    record = RequestRecord.objects.get()
    log_files = list(tmp_path.rglob(f"{record.id}.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "ValueError" in content
    assert "boom-inside-forward" in content
    assert "Traceback (most recent call last)" in content


@pytest.mark.django_db
def test_unhandled_exception_in_stream_finishes_record_and_logs(tmp_path, monkeypatch):
    # Scenario 2: a non-requests exception raised during streaming (e.g. a bug
    # surfacing mid-iteration) must finish the record and log the traceback,
    # instead of escaping to gunicorn and orphaning the row.
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    _seed_online_model(monkeypatch)

    def fake_perform(self, django_request, server, upstream_url, headers, body, is_stream, upstream_client):
        upstream = _streaming_upstream(status=200)

        def gen():
            yield b"data: hello\n\n"
            raise ValueError("boom-mid-stream")

        upstream.iter_content = lambda chunk_size=8192: gen()
        return upstream

    monkeypatch.setattr("router.services.proxy.ProxyService._perform_request", fake_perform)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    # Drive the streaming generator (it runs lazily).
    b"".join(response.streaming_content)

    record = RequestRecord.objects.get()
    assert record.task_status == "failed"
    assert record.status == "502 Bad Gateway"
    log_files = list(tmp_path.rglob(f"{record.id}.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "ValueError" in content
    assert "boom-mid-stream" in content
    assert "Traceback (most recent call last)" in content

