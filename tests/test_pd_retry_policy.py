"""Retry-policy tests for PD disaggregation and single-node paths.

Issue #198: a request must be retried on another server ONLY when the failure
is a connection failure (the request body never reached the upstream). A read
timeout means the connection was up and the server may have started processing,
so it must not retry — otherwise the original record's trace is lost when the
whole request is re-run on a new server.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from django.utils import timezone

from router.models import RequestRecord, Server
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.services.proxy import ProxyService, _RetryState
from router.services.proxy_pd_forward import PDForwardService


def _record():
    return RequestRecord.objects.create(
        user_ip_id=1, ip_id=None, send_time=timezone.now(),
        model_id=1, task_status="processing",
    )


def _context(record):
    return ServerSelectionContext(
        request_id=record.id, ip_id=None, model_id=1, model_name="m",
        path="chat/completions", method="POST", is_stream=False, body=b"{}",
    )


def _django_request():
    req = MagicMock()
    req.META = {"QUERY_STRING": ""}
    return req


def _make_svc(monkeypatch):
    """Build a PDForwardService wired to stub proxy/circuit-breaker/repo bits."""
    fake_cb = type("CB", (), {
        "record_success": staticmethod(lambda s: None),
        "record_failure": staticmethod(lambda s: None),
    })()
    fake_proxy = type("P", (), {
        "_build_url": staticmethod(lambda base, path, qs: f"{base}/{path}"),
        "_notify_chooser_response": staticmethod(lambda s, ctx, code: None),
        "_after_finish": staticmethod(lambda vip, m: None),
        "_increment_workload": staticmethod(lambda s: None),
        "_decrement_workload": staticmethod(lambda s: None),
        "_is_connection_failure": staticmethod(ProxyService._is_connection_failure),
        "circuit_breaker": fake_cb,
        "normal_timeout": 5,
    })()

    svc = PDForwardService.__new__(PDForwardService)
    svc.proxy = fake_proxy
    svc.circuit_breaker = fake_cb
    svc.normal_timeout = 5
    svc.stream_timeout = (30, 900)
    svc.stream_total_timeout = 900
    svc.recompute_max = 3

    # Avoid DB side-effects and file IO in these unit tests.
    monkeypatch.setattr(ServerRepository, "increment_workload", lambda d: None)
    monkeypatch.setattr(ServerRepository, "reserve_active_tokens", lambda d, n: None)
    monkeypatch.setattr(ServerRepository, "release_active_tokens", lambda d, n: None)
    monkeypatch.setattr(ServerRepository, "decrement_workload", lambda d: None)
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.RequestRepository.record_attempt",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.append_request_log", lambda *a, **k: None
    )
    return svc


@pytest.mark.django_db
class TestPrefillRetryPolicy:
    def _svc(self, monkeypatch, exc):
        svc = _make_svc(monkeypatch)

        def fake_prefill(*args, **kwargs):
            raise exc

        monkeypatch.setattr(PDForwardService, "_do_prefill", fake_prefill)
        return svc

    def _run(self, svc):
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        state = _RetryState()
        result = svc.forward(
            _django_request(), "chat/completions", {}, b'{"messages":[]}',
            record, None, _context(record), False, None, False, None, state, prefiller,
        )
        return result, state

    def test_prefill_read_timeout_does_not_retry(self, monkeypatch):
        # The headline bug: a long prefill read-timeout must NOT retry.
        result, state = self._run(self._svc(monkeypatch, requests.exceptions.ReadTimeout("slow")))
        assert result.should_retry is False
        assert result.response is None
        assert state.last_status == 504

    def test_prefill_connection_error_retries(self, monkeypatch):
        result, state = self._run(self._svc(monkeypatch, requests.ConnectionError("refused")))
        assert result.should_retry is True
        assert state.last_status == 502

    def test_prefill_other_request_exception_does_not_retry(self, monkeypatch):
        result, state = self._run(
            self._svc(monkeypatch, requests.exceptions.ChunkedEncodingError("truncated"))
        )
        assert result.should_retry is False
        assert state.last_status == 502


@pytest.mark.django_db
class TestDecodeRetryPolicy:
    def _run(self, monkeypatch, post_decode_fn, decoder_ids):
        svc = _make_svc(monkeypatch)
        monkeypatch.setattr(PDForwardService, "_post_decode", staticmethod(post_decode_fn))

        decoders = [
            Server.objects.create(base_url=f"http://d{i}.example", role="decoder", group_id="g1")
            for i in range(len(decoder_ids))
        ]
        it = iter(decoders)
        monkeypatch.setattr(
            ServerRepository, "pick_least_tokens_decoder",
            lambda group_id, attempted: next(it, None),
        )

        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        state = _RetryState()
        result = svc._normal_decode(
            "chat/completions", {}, b'{"messages":[]}', record, _context(record),
            False, None, state, prefiller, {}, 1, 0, "P: x",
        )
        return result, state

    def test_decode_read_timeout_does_not_retry(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ReadTimeout("slow")

        result, state = self._run(monkeypatch, boom, ["d0"])
        assert result.should_retry is False
        assert result.response is None
        assert state.last_status == 504

    def test_decode_connection_error_retries(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("refused")

        result, state = self._run(monkeypatch, boom, ["d0"])
        assert result.should_retry is True
        assert state.last_status == 502

    def test_decode_other_request_exception_does_not_retry(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ChunkedEncodingError("truncated")

        result, state = self._run(monkeypatch, boom, ["d0"])
        assert result.should_retry is False
        assert state.last_status == 502

    def test_no_routable_decoder_does_not_retry(self, monkeypatch):
        svc = _make_svc(monkeypatch)
        monkeypatch.setattr(
            ServerRepository, "pick_least_tokens_decoder", lambda group_id, attempted: None
        )
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        state = _RetryState()
        result = svc._normal_decode(
            "chat/completions", {}, b'{"messages":[]}', record, _context(record),
            False, None, state, prefiller, {}, 1, 0, "P: x",
        )
        assert result.should_retry is False
        assert result.response is None
        assert state.last_status == 502
        assert "no routable decoder" in state.last_reason

    def test_recompute_limit_exceeded_does_not_retry(self, monkeypatch):
        content = (
            b'{"choices":[{"message":{"content":""},"stop_reason":"recomputed"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":0}}'
        )

        def always_recomputed(*args, **kwargs):
            resp = type("R", (), {"reason": "OK", "headers": {}})()
            return resp, content, 200

        svc = _make_svc(monkeypatch)
        svc.recompute_max = 1
        monkeypatch.setattr(PDForwardService, "_post_decode", staticmethod(always_recomputed))

        decoders = [
            Server.objects.create(base_url=f"http://d{i}.example", role="decoder", group_id="g1")
            for i in range(4)
        ]
        it = iter(decoders)
        monkeypatch.setattr(
            ServerRepository, "pick_least_tokens_decoder",
            lambda group_id, attempted: next(it, None),
        )

        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        state = _RetryState()
        result = svc._normal_decode(
            "chat/completions", {}, b'{"messages":[]}', record, _context(record),
            False, None, state, prefiller, {}, 1, 0, "P: x",
        )
        assert result.should_retry is False
        assert result.response is None
        assert state.last_status == 502
