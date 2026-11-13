"""Client-disconnect cancellation across every phase (issue #323).

A client that cancels mid-request (e.g. an opencode interrupt) must abort the
in-flight upstream request in every phase — single-node send, PD prefill, PD
decode, streaming — and finish the record as 499 agent_disconnected without
counting a circuit-breaker failure or scheduling a retry.
"""
from __future__ import annotations

import socket
import time
from unittest.mock import MagicMock

import pytest
import requests
from django.utils import timezone

from router.models import RequestRecord, Server
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.disconnect import ClientDisconnectTracker, DisconnectScope
from router.services.proxy import ProxyService, _RetryState
from router.services.proxy_pd_forward import PDForwardService


def _record(stream: bool = False):
    return RequestRecord.objects.create(
        user_ip_id=1, ip_id=None, send_time=timezone.now(),
        model_id=1, task_status="processing",
    )


def _context(record) -> ServerSelectionContext:
    return ServerSelectionContext(
        request_id=record.id, ip_id=None, model_id=1, model_name="m",
        path="chat/completions", method="POST", is_stream=False, body=b"{}",
    )


def _django_request(stream: bool = False):
    req = MagicMock()
    req.method = "POST"
    req.META = {"QUERY_STRING": ""}
    req.headers = {}
    req.body = b"{}"
    req.client_disconnect_tracker = None
    return req


def _scope() -> DisconnectScope:
    return DisconnectScope()


def _finish_499(record, status=499):
    record.status = status
    record.task_status = "agent_disconnected" if status == 499 else "success"
    record.end_time = timezone.now()
    record.save(update_fields=["status", "task_status", "end_time"])
    resp = MagicMock()
    resp.status_code = status
    return resp


def _prefill_response():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "kv_transfer_params": {"remote_engine_id": "e1", "remote_host": "h", "remote_port": 1},
    }
    return response


class _RecordingCircuitBreaker:
    def __init__(self):
        self.failures = []
        self.successes = []

    def record_failure(self, server):
        self.failures.append(server)

    def record_success(self, server):
        self.successes.append(server)


def _make_svc(monkeypatch) -> PDForwardService:
    cb = _RecordingCircuitBreaker()
    proxy = type("P", (), {
        "_build_url": staticmethod(lambda base, path, qs: f"{base}/{path}"),
        "_notify_chooser_response": staticmethod(lambda s, ctx, code: None),
        "_attach_chooser_response_hook": staticmethod(lambda resp, s, ctx, code: resp),
        "_after_finish": staticmethod(lambda vip, m: None),
        "_increment_workload": staticmethod(lambda s: None),
        "_decrement_workload": staticmethod(lambda s: None),
        "_is_connection_failure": staticmethod(ProxyService._is_connection_failure),
        "_deadline_timeout": staticmethod(ProxyService._deadline_timeout),
        "_client_closed_response": staticmethod(
            lambda record, served_as_vip=False, model=None: _finish_499(record)
        ),
        "circuit_breaker": cb,
        "normal_timeout": (5.0, 3660.0),
        "stream_timeout": (30.0, 3660.0),
        "stream_total_timeout": 3660.0,
    })()
    svc = PDForwardService.__new__(PDForwardService)
    svc.proxy = proxy
    svc.circuit_breaker = cb
    svc.normal_timeout = (5.0, 3660.0)
    svc.stream_timeout = (30.0, 3660.0)
    svc.stream_total_timeout = 3660.0
    svc.recompute_max = 3
    svc.prefill_timeout = (5.0, None)
    for name in ("increment_workload", "reserve_active_tokens",
                 "release_active_tokens", "decrement_workload"):
        monkeypatch.setattr(ServerRepository, name, lambda *a, **k: None)
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.RequestRepository.record_attempt",
        staticmethod(lambda *a, **k: None),
    )
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.RequestRepository.record_model_choosing_latency",
        staticmethod(lambda *a, **k: None),
    )
    monkeypatch.setattr(
        "router.services.proxy_pd_forward.append_request_log", lambda *a, **k: None
    )
    return svc


@pytest.mark.django_db
class TestPrefillDisconnect:
    def test_prefill_abort_classified_as_client_closed(self, monkeypatch):
        svc = _make_svc(monkeypatch)
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        scope = _scope()

        def cancelled_prefill(*args, **kwargs):
            scope.disconnect_event.set()
            raise requests.exceptions.ConnectionError("watcher aborted socket")

        monkeypatch.setattr(svc, "_do_prefill", cancelled_prefill)
        released = []
        monkeypatch.setattr(svc, "_release_prefiller", lambda p: released.append(p))

        result = svc.forward(
            _django_request(), "chat/completions", {}, b'{"messages":[]}',
            record, None, _context(record), False, None, False, scope, _RetryState(),
            prefiller,
        )
        assert result.response is not None
        assert result.response.status_code == 499
        assert record.task_status == "agent_disconnected"
        assert released == [prefiller]
        assert svc.circuit_breaker.failures == []
        assert result.should_retry is False

    def test_no_decode_dispatch_after_disconnect(self, monkeypatch):
        svc = _make_svc(monkeypatch)
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        record = _record()
        scope = _scope()
        scope.disconnect_event.set()  # client cancelled during the prefill

        monkeypatch.setattr(
            svc, "_do_prefill",
            lambda *a, **k: (_prefill_response().json(), 11, 0),
        )
        dispatched = []
        monkeypatch.setattr(svc, "_normal_decode", lambda *a, **k: dispatched.append(a))

        result = svc.forward(
            _django_request(), "chat/completions", {}, b'{"messages":[]}',
            record, None, _context(record), False, None, False, scope, _RetryState(),
            prefiller,
        )
        assert result.response is not None
        assert result.response.status_code == 499
        assert dispatched == []
        assert record.task_status == "agent_disconnected"


@pytest.mark.django_db
class TestNormalDecodeDisconnect:
    def test_decode_abort_classified_as_client_closed(self, monkeypatch):
        svc = _make_svc(monkeypatch)
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        decoder = Server.objects.create(
            base_url="http://d.example", role="decoder", group_id="g1"
        )
        record = _record()
        scope = _scope()

        def cancelled_decode(*args, **kwargs):
            scope.disconnect_event.set()
            raise requests.exceptions.ConnectionError("watcher aborted socket")

        monkeypatch.setattr(svc, "_post_decode", cancelled_decode)
        monkeypatch.setattr(
            ServerRepository, "pick_least_tokens_decoder",
            classmethod(lambda cls, group, attempted: decoder),
        )
        released = []
        monkeypatch.setattr(
            svc, "_release_decoder", lambda d, tokens: released.append((d, tokens))
        )

        result = svc._normal_decode(
            "chat/completions", {}, b'{"messages":[]}', record, _context(record),
            False, None, _RetryState(), prefiller, {"remote_engine_id": "e1"},
            11, 0, "P: p", 12, scope,
        )
        assert result.response is not None
        assert result.response.status_code == 499
        assert record.task_status == "agent_disconnected"
        assert released == [(decoder, 11)]
        assert svc.circuit_breaker.failures == []
        assert result.should_retry is False


@pytest.mark.django_db
class TestStreamDecodeDisconnect:
    def test_mid_stream_abort_classified_as_client_disconnected(self, monkeypatch):
        svc = _make_svc(monkeypatch)
        prefiller = Server.objects.create(
            base_url="http://p.example", role="prefiller", group_id="g1"
        )
        decoder = Server.objects.create(
            base_url="http://d.example", role="decoder", group_id="g1"
        )
        record = _record(stream=True)
        scope = DisconnectScope()

        upstream = MagicMock()
        upstream.status_code = 200

        def iter_content(chunk_size=8192):
            scope.disconnect_event.set()
            raise requests.exceptions.ConnectionError("watcher aborted socket")

        upstream.iter_content = iter_content

        def fake_request(self, method, url, **kwargs):
            return upstream

        monkeypatch.setattr(CancellableUpstreamRequest, "request", fake_request)
        finished = {}
        monkeypatch.setattr(
            "router.services.proxy_pd_forward.proxy_response.finish_stream_client_disconnected",
            lambda rec, target, attempts: finished.update(
                status=499, task_status="agent_disconnected"
            ),
        )
        monkeypatch.setattr(
            ServerRepository, "pick_least_tokens_decoder",
            classmethod(lambda cls, group, attempted: decoder),
        )
        releases = []
        monkeypatch.setattr(
            ServerRepository, "release_active_tokens", lambda d, n: releases.append(d)
        )

        response = svc._stream_decode(
            _django_request(stream=True), "chat/completions", {}, b'{"messages":[]}',
            record, _context(record), False, None, _RetryState(),
            prefiller, {}, 11, 0, "P: p", scope,
        )
        assert list(response.streaming_content) == []
        assert finished == {"status": 499, "task_status": "agent_disconnected"}
        assert releases == [decoder]
        assert svc.circuit_breaker.failures == []
        # The generator owned the scope and closed it when streaming ended.
        assert scope.stop_event.is_set()


@pytest.mark.django_db
class TestSingleNodeStreamDisconnect:
    def test_mid_stream_abort_classified_as_client_disconnected(self, monkeypatch):
        from router.services import proxy_response

        service = ProxyService()
        server = Server.objects.create(base_url="http://m.example", is_online=True)
        record = _record(stream=True)
        context = _context(record)
        scope = DisconnectScope()

        upstream = MagicMock()

        def iter_content(chunk_size=8192):
            scope.disconnect_event.set()
            raise requests.exceptions.ConnectionError("watcher aborted socket")

        upstream.iter_content = iter_content
        finished = {}
        monkeypatch.setattr(
            proxy_response, "finish_stream_client_disconnected",
            lambda rec, target, attempts: finished.update(
                status=499, task_status="agent_disconnected"
            ),
        )

        response = service._stream_success(
            _django_request(stream=True), upstream, record, server, "m", 200, "OK",
            "m.example", 1, context, False, None, scope,
        )
        assert list(response.streaming_content) == []
        assert finished == {"status": 499, "task_status": "agent_disconnected"}
        assert scope.stop_event.is_set()


class TestScopeWiring:
    def test_scope_without_tracker_has_client_but_no_watcher(self):
        scope = DisconnectScope()
        assert scope.upstream_client is not None
        assert scope.watcher is None
        scope.close()
        assert scope.stop_event.is_set()

    def test_watcher_cancels_upstream_client_on_client_disconnect(self):
        client_sock, peer = socket.socketpair()
        tracker = ClientDisconnectTracker(client_sock)
        scope = DisconnectScope(tracker, check_interval=0.05)
        try:
            assert scope.watcher is not None
            peer.close()  # the client (agent) interrupts the request
            deadline = time.monotonic() + 2
            while not scope.disconnect_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert scope.disconnect_event.is_set()
            assert scope.upstream_client._controller.cancelled
        finally:
            scope.close()
            client_sock.close()
