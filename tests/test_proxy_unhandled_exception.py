import json
import logging

import pytest
from django.test import Client

from router.models import RequestRecord
from router.services import request_logger


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
