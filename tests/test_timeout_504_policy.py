"""Timeout-budget policy: a request that exceeds the router's timeout budget
must end as 504 Gateway Timeout, not as a synthetic 502.

Background: an upstream that drops the connection after ~900s surfaces as a
requests.ConnectionError, which the retry policy (#198) retries. Three such
attempts (~2700s) previously ended in "502 Bad Gateway". The absolute budget
in _route_with_retry stops that retry storm and reports the 504 the client
should have seen at ~900s.
"""
from __future__ import annotations

import json
import time as time_module
from unittest.mock import MagicMock

import pytest
import requests

from router.models import Model, RequestRecord, Server
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.services import request_logger
from router.services.proxy import ProxyService


@pytest.fixture(autouse=True)
def reset_request_logger_cache(monkeypatch):
    monkeypatch.setattr(request_logger, "_LOG_PATH_CACHE", None)
    request_logger._REQUEST_LOG_FILE_CACHE.clear()


def _make_upstream(status: int = 200, body: bytes = b"{}"):
    upstream = MagicMock()
    upstream.status_code = status
    upstream.reason = "OK" if status == 200 else "Bad"
    upstream.content = body
    upstream.headers = {}
    return upstream


def _django_request(method: str = "POST", body: bytes = b"{}"):
    req = MagicMock()
    req.method = method
    req.headers = {}
    req.body = body
    req.META = {"QUERY_STRING": ""}
    req.client_disconnect_tracker = None
    return req


def _embeddings_service(monkeypatch, tmp_path, normal_timeout=(5, 900), stream_total_timeout=900):
    monkeypatch.setitem(request_logger.APP_CONFIG, "log_path", str(tmp_path))
    Model.objects.create(model_name="emb")
    first = Server.objects.create(base_url="http://first.example", is_online=True)
    second = Server.objects.create(base_url="http://second.example", is_online=True)
    first.workload = 0
    first.save()
    second.workload = 5
    second.save()
    service = ProxyService()
    service.normal_timeout = normal_timeout
    service.stream_total_timeout = stream_total_timeout
    return service


def _record():
    return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get()


def _parsed():
    return MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)


@pytest.mark.django_db
def test_late_connection_error_returns_504_without_retry(monkeypatch, tmp_path):
    """The user's case: the connection dies after the timeout budget has
    elapsed -> must be 504, exactly one attempt, no retry storm."""
    service = _embeddings_service(monkeypatch, tmp_path, normal_timeout=(0.01, 0.05))

    attempts = []

    def fake_request(self_inner, method, url, **kwargs):
        attempts.append(url)
        time_module.sleep(0.1)  # longer than the 0.05s budget
        raise requests.ConnectionError("connection dropped")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = service.forward(_django_request(), "embeddings", _parsed(), None, None, None)

    assert response.status_code == 504
    assert json.loads(response.content)["error"]["type"] == "gateway_timeout_error"
    assert len(attempts) == 1  # no retry after the budget expired

    record = _record()
    assert record.status == "504 Gateway Timeout"
    assert record.fail_reason == "request timeout"
    assert record.target_pod_ip == "http://first.example"
    assert record.attempt_count == 1

    log_files = list(tmp_path.rglob(f"{record.id}.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert '"event": "timeout_budget_exceeded"' in content


@pytest.mark.django_db
def test_fast_connection_error_still_retries(monkeypatch, tmp_path):
    """A quick connection failure (body never reached the upstream) must keep
    the #198 retry behavior: second server is tried and the request succeeds."""
    service = _embeddings_service(monkeypatch, tmp_path)

    attempts = []

    def fake_request(self_inner, method, url, **kwargs):
        attempts.append(url)
        if url.startswith("http://first.example"):
            raise requests.ConnectionError("boom")
        return _make_upstream(200, b'{"data":[]}')

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = service.forward(_django_request(), "embeddings", _parsed(), None, None, None)

    assert response.status_code == 200
    assert len(attempts) == 2
    assert attempts[0].startswith("http://first.example")
    assert attempts[1].startswith("http://second.example")


@pytest.mark.django_db
def test_timeout_response_does_not_trigger_opencode_delay(monkeypatch, tmp_path):
    """The 180s opencode backpressure delay must not run after a 504: the
    client already waited the full budget, and sleeping only risks the
    timeout response being cut off (surfacing as a spurious 502)."""
    delay_calls = []
    monkeypatch.setattr(
        ProxyService, "_maybe_delay_opencode_failure",
        lambda self, ua, status: delay_calls.append((ua, status)),
    )

    service = _embeddings_service(monkeypatch, tmp_path, normal_timeout=(0.01, 0.05))

    def fake_request(self_inner, method, url, **kwargs):
        time_module.sleep(0.1)
        raise requests.ConnectionError("connection dropped")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = service.forward(
        _django_request(), "embeddings", _parsed(), None, None, "opencode/1.2.30"
    )

    assert response.status_code == 504
    assert delay_calls == []


@pytest.mark.django_db
def test_502_still_trigger_opencode_delay(monkeypatch, tmp_path):
    """The backpressure delay is preserved for fast 502 failures (opencode UA)."""
    delay_calls = []
    monkeypatch.setattr(
        ProxyService, "_maybe_delay_opencode_failure",
        lambda self, ua, status: delay_calls.append((ua, status)),
    )

    service = _embeddings_service(monkeypatch, tmp_path)

    def fake_request(self_inner, method, url, **kwargs):
        raise requests.exceptions.ChunkedEncodingError("truncated")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = service.forward(
        _django_request(), "embeddings", _parsed(), None, None, "opencode/1.2.30"
    )

    assert response.status_code == 502
    assert delay_calls == [("opencode/1.2.30", 502)]
