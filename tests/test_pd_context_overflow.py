"""Context-overflow handling on the PD prefill path (issue #248).

A prefiller that rejects a request with a 400 context-overflow must make the
router switch to a same-model server with a strictly larger context window
(e.g. a long-context pd-mix node) instead of surfacing the 400. Only
prefiller/mixed windows are compared — decoders are never routing candidates —
which is safe because the deployment guarantees
``prefiller.context_window + max_completion_tokens < decoder.context_window``.

A client-caused overflow must never tick the prefiller's circuit breaker,
while any other HTTP 400 still does (consecutive non-overflow 400s indicate a
sick server).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from django.test import Client
from django.utils import timezone

from router.models import Model, RequestRecord, Server
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.services.proxy import ProxyService, _RetryState
from router.services.proxy_pd_forward import PDForwardService, _PrefillHttpError


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


def _make_svc(monkeypatch, select_fn):
    """Build a PDForwardService wired to stub proxy/circuit-breaker bits.

    ``select_fn`` is the stub for ``proxy._select_candidates``; ``failures``
    collects the ids of servers whose circuit breaker was ticked.
    """
    failures = []
    fake_cb = type("CB", (), {
        "record_success": staticmethod(lambda s: None),
        "record_failure": staticmethod(lambda s: failures.append(s.id)),
    })()
    fake_proxy = type("P", (), {
        "_build_url": staticmethod(lambda base, path, qs: f"{base}/{path}"),
        "_notify_chooser_response": staticmethod(lambda s, ctx, code: None),
        "_after_finish": staticmethod(lambda vip, m: None),
        "_increment_workload": staticmethod(lambda s: None),
        "_decrement_workload": staticmethod(lambda s: None),
        "_is_connection_failure": staticmethod(ProxyService._is_connection_failure),
        "_select_candidates": staticmethod(select_fn),
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
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.proxy_logging.log_request_context_for",
        lambda ctx: None,
    )
    return svc, failures


def _http_error(status_code: int, message: str) -> _PrefillHttpError:
    upstream = MagicMock()
    upstream.status_code = status_code
    upstream.reason = "Bad Request"
    upstream.headers = {"content-type": "application/json"}
    upstream.content = json.dumps({"error": {"message": message}}).encode()
    return _PrefillHttpError(status_code, "Bad Request", upstream, upstream.content)


def _run_forward(svc, prefiller, record, body):
    state = _RetryState()
    result = svc.forward(
        _django_request(), "chat/completions", {}, body,
        record, None, _context(record), False, None, False, None, state, prefiller,
    )
    return result, state


@pytest.mark.django_db
class TestPrefillContextOverflow:
    def test_overflow_switches_to_larger_window_candidates(self, monkeypatch):
        bigger = Server.objects.create(
            base_url="http://mix.example", role="mixed", context_window=100000
        )
        captured = {}

        def select_fn(path, model, vip, min_context_window=0):
            captured["min_context_window"] = min_context_window
            return [bigger], False

        svc, failures = _make_svc(monkeypatch, select_fn)
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1",
            context_window=1000,
        )

        def boom(*args, **kwargs):
            raise _http_error(400, "context window 1000 exceeded")

        monkeypatch.setattr(PDForwardService, "_do_prefill", boom)

        record = _record()
        body = b'{"messages":[]}'
        result, state = _run_forward(svc, prefiller, record, body)

        assert result.should_retry is True
        assert result.response is None
        assert result.candidates == [bigger]
        assert result.body == body
        # Only strictly larger windows are requested from the repository.
        assert captured["min_context_window"] == 1000
        # A client-caused overflow never ticks the prefiller's circuit breaker.
        assert failures == []
        # The record stays neutral so a later success/failure finishes it.
        record.refresh_from_db()
        assert record.task_status == "processing"
        # The real 400 is retained for replay if the retry exhausts.
        assert state.last_status == 400
        assert state.last_fail_reason == "context window 1000 exceeded"

    def test_overflow_without_larger_candidates_returns_real_400(self, monkeypatch):
        svc, failures = _make_svc(monkeypatch, lambda *a, **k: ([], False))
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1",
            context_window=1000,
        )

        def boom(*args, **kwargs):
            raise _http_error(400, "context window 1000 exceeded")

        monkeypatch.setattr(PDForwardService, "_do_prefill", boom)

        record = _record()
        result, state = _run_forward(svc, prefiller, record, b'{"messages":[]}')

        assert result.should_retry is False
        assert result.response is not None
        assert result.response.status_code == 400
        assert b"context window 1000 exceeded" in result.response.content
        # Even without a switch target the overflow is client-caused: no tick.
        assert failures == []

    def test_non_overflow_400_is_terminal_and_ticks_breaker(self, monkeypatch):
        svc, failures = _make_svc(monkeypatch, lambda *a, **k: ([], False))
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1",
            context_window=1000,
        )

        def boom(*args, **kwargs):
            raise _http_error(400, "rate limited by upstream")

        monkeypatch.setattr(PDForwardService, "_do_prefill", boom)

        record = _record()
        result, state = _run_forward(svc, prefiller, record, b'{"messages":[]}')

        assert result.should_retry is False
        assert result.response is not None
        assert result.response.status_code == 400
        assert failures == [prefiller.id]

    def test_null_context_window_never_switches_and_ticks_breaker(self, monkeypatch):
        svc, failures = _make_svc(monkeypatch, lambda *a, **k: ([], False))
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1",
            context_window=None,
        )

        def boom(*args, **kwargs):
            raise _http_error(400, "context window 1000 exceeded")

        monkeypatch.setattr(PDForwardService, "_do_prefill", boom)

        record = _record()
        result, state = _run_forward(svc, prefiller, record, b'{"messages":[]}')

        assert result.should_retry is False
        assert result.response is not None
        assert result.response.status_code == 400
        assert failures == [prefiller.id]

    def test_non_400_overflow_message_does_not_switch(self, monkeypatch):
        svc, failures = _make_svc(monkeypatch, lambda *a, **k: ([], False))
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1",
            context_window=1000,
        )

        def boom(*args, **kwargs):
            raise _http_error(500, "context window 1000 exceeded")

        monkeypatch.setattr(PDForwardService, "_do_prefill", boom)

        record = _record()
        result, state = _run_forward(svc, prefiller, record, b'{"messages":[]}')

        assert result.should_retry is False
        assert result.response is not None
        assert result.response.status_code == 500
        assert failures == [prefiller.id]


@pytest.mark.django_db
def test_prefiller_context_overflow_switches_to_long_context_mixed_server(monkeypatch):
    # Issue #248 end-to-end: a PD request whose prefiller overflows must be
    # re-routed to a same-model pd-mix server with a larger context window.
    model = Model.objects.create(model_name="Other-Model", max_tokens=65536)
    Server.objects.create(
        model_id=model.id, base_url="http://d.example",
        role="decoder", group_id="g1", is_online=True,
    )
    Server.objects.create(
        model_id=model.id, base_url="http://p.example",
        role="prefiller", group_id="g1", is_online=True, context_window=1000,
    )

    def fake_prefill(url, **kwargs):
        # The long-context pd-mix server only appears after the first routing
        # decision, so the initial pick deterministically lands on the
        # prefiller and the overflow retry must discover the new server.
        if not Server.objects.filter(base_url="http://mix.example").exists():
            Server.objects.create(
                model_id=model.id, base_url="http://mix.example",
                role="mixed", is_online=True, context_window=100000,
            )
        upstream = MagicMock()
        upstream.status_code = 400
        upstream.reason = "Bad Request"
        upstream.headers = {"content-type": "application/json"}
        upstream.content = (
            b'{"error": {"message": "This model\'s maximum context length is 1000 tokens.'
            b' Please reduce the length of the input prompt."}}'
        )
        return upstream

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_prefill)

    contacted = []

    def fake_request(self_inner, method, url, **kwargs):
        contacted.append(url)
        upstream = MagicMock()
        upstream.headers = {"content-type": "application/json"}
        if "mix.example" in url:
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices": [{"message": {"content": "ok"}}]}'
        else:
            upstream.status_code = 400
            upstream.reason = "Bad Request"
            upstream.content = b'{"error": {"message": "unexpected"}}'
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "Other-Model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )

    assert response.status_code == 200
    assert any("mix.example" in url for url in contacted)
