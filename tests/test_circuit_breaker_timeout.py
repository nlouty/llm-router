"""Regression tests for the circuit breaker on timeout / slow-server failures.

Guards against the bug where a server that consistently timed out (returning
502/504) never accumulated ``consecutive_failures`` and so never opened its
circuit, because:

* the read-timeout / stream-timeout handlers did not call ``record_failure``;
* streaming requests recorded ``record_success`` at header time (before the
  body streamed), resetting the counter before a mid-stream failure could count.

These run end-to-end through ``ProxyService.forward`` so they exercise the real
recording path, not the repository in isolation.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from router.models import Model, Server
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.proxy import ProxyService


class _ChooserOnce:
    """Always pick the same server, once per request (fresh state each forward)."""

    def __init__(self, server):
        self._server = server

    def choose(self, candidates, context, attempted):
        if self._server.id in attempted:
            return None
        return self._server

    def on_response(self, server, context, status_code):
        return None


def _streaming_upstream(status=200, chunks=None, body=b"{}"):
    upstream = MagicMock()
    upstream.status_code = status
    upstream.reason = "OK" if status < 400 else "Bad"
    upstream.content = body
    upstream.headers = {}
    if chunks is not None:
        upstream.iter_content = lambda chunk_size=8192: iter(chunks)
    return upstream


def _django_request():
    dr = MagicMock()
    dr.method = "POST"
    dr.headers = {}
    dr.META = {"QUERY_STRING": ""}
    dr.client_disconnect_tracker = None
    return dr


def _make_service(server, *, stream):
    Model.objects.create(model_name="m")
    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=stream, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    return service, parsed


# --------------------------------------------------------------------------- #
# Non-streaming read timeout -> must record a failure and open the circuit.   #
# --------------------------------------------------------------------------- #
def test_non_stream_read_timeout_records_failure_and_opens_circuit(monkeypatch):
    server = Server.objects.create(model_id=None, base_url="http://slow-nonstream.example", is_online=True)
    service, parsed = _make_service(server, stream=False)

    def boom(self, method, url, **kwargs):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(CancellableUpstreamRequest, "request", boom)

    for _ in range(3):
        response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
        assert response.status_code == 504

    server.refresh_from_db()
    assert server.consecutive_failures == 3
    assert server.circuit_state == "open"
    assert server not in __import__(
        "router.repositories.servers", fromlist=["ServerRepository"]
    ).ServerRepository.list_all_online()


# --------------------------------------------------------------------------- #
# Streaming read timeout: 200 headers must NOT reset the counter, and the     #
# timeout must count as a failure so the circuit opens after the threshold.   #
# --------------------------------------------------------------------------- #
def test_stream_read_timeout_records_failure_and_opens_circuit(monkeypatch):
    server = Server.objects.create(model_id=None, base_url="http://slow-stream.example", is_online=True)
    service, parsed = _make_service(server, stream=True)

    def gen(chunk_size=8192):
        raise requests.exceptions.ReadTimeout("slow")
        yield  # noqa: makes this a generator

    upstream = _streaming_upstream(200)
    upstream.iter_content = gen
    monkeypatch.setattr(
        "router.services.proxy.requests.request",
        lambda method, url, **kw: upstream,
    )

    for _ in range(3):
        response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
        list(response.streaming_content)  # drain so the generator (and failure) runs

    server.refresh_from_db()
    assert server.consecutive_failures == 3
    assert server.circuit_state == "open"


def test_stream_total_timeout_records_failure(monkeypatch):
    server = Server.objects.create(model_id=None, base_url="http://slow-total.example", is_online=True)
    service, parsed = _make_service(server, stream=True)
    # Force the total-timeout deadline into the past so the first chunk trips it.
    service.stream_total_timeout = -1.0

    upstream = _streaming_upstream(200, chunks=[b"data: partial\n\n"])
    monkeypatch.setattr(
        "router.services.proxy.requests.request",
        lambda method, url, **kw: upstream,
    )

    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
    list(response.streaming_content)

    server.refresh_from_db()
    assert server.consecutive_failures == 1


# --------------------------------------------------------------------------- #
# A stream that completes normally must still reset the counter (success is    #
# recorded on full completion, not at header time).                           #
# --------------------------------------------------------------------------- #
def test_successful_stream_resets_failure_counter(monkeypatch):
    server = Server.objects.create(
        model_id=None,
        base_url="http://ok-stream.example",
        is_online=True,
        consecutive_failures=2,
    )
    service, parsed = _make_service(server, stream=True)

    upstream = _streaming_upstream(200, chunks=[b"data: x\n\n", b"data: [DONE]\n\n"])
    monkeypatch.setattr(
        "router.services.proxy.requests.request",
        lambda method, url, **kw: upstream,
    )

    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
    list(response.streaming_content)

    server.refresh_from_db()
    assert server.consecutive_failures == 0
    assert server.circuit_state == "closed"
