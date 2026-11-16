"""Regression tests for Server.workload accounting in the streaming proxy path.

These guard against the bug where a failed upstream.close() in the stream
generator's ``finally`` block ran before the workload decrement, permanently
leaking workload on a server with no record left in 'processing' state for
cleanup_stale to reclaim.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import requests

from router.models import Model, Server
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.proxy import ProxyService


class _ChooserOnce:
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


def _setup():
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://stream-audit.example", is_online=True)
    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=True, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    return server, service, parsed


def test_stream_workload_decrements_even_when_upstream_close_raises(monkeypatch):
    server, service, parsed = _setup()
    upstream = _streaming_upstream(200, chunks=[b"data: x\n\n", b"data: [DONE]\n\n"])

    def boom():
        raise RuntimeError("close failed")
    upstream.close = boom
    monkeypatch.setattr(
        CancellableUpstreamRequest, "request",
        lambda self, method, url, **kw: upstream,
    )
    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)

    list(response.streaming_content)  # close failure is swallowed, not propagated
    server.refresh_from_db()
    assert server.workload == 0


def test_stream_workload_decrements_when_client_disconnects_mid_stream(monkeypatch):
    server, service, parsed = _setup()

    def gen(chunk_size=8192):
        yield b"data: hello\n\n"
        raise ConnectionResetError("client gone")
    upstream = _streaming_upstream(200)
    upstream.iter_content = gen
    monkeypatch.setattr(
        CancellableUpstreamRequest, "request",
        lambda self, method, url, **kw: upstream,
    )
    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)

    try:
        list(response.streaming_content)
    except ConnectionResetError:
        pass
    server.refresh_from_db()
    assert server.workload == 0


def test_stream_workload_decrements_on_read_timeout(monkeypatch):
    server, service, parsed = _setup()

    def gen(chunk_size=8192):
        raise requests.exceptions.ReadTimeout("slow")
        yield  # noqa
    upstream = _streaming_upstream(200)
    upstream.iter_content = gen
    monkeypatch.setattr(
        CancellableUpstreamRequest, "request",
        lambda self, method, url, **kw: upstream,
    )
    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)

    list(response.streaming_content)
    server.refresh_from_db()
    assert server.workload == 0


def test_stream_error_path_close_raises_still_decrements(monkeypatch):
    """Stream attempt whose upstream returns 500 then close() raises: the record
    still finishes (not orphaned in 'processing') and workload reaches 0."""
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://err-close.example", is_online=True)
    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=True, body=b"{}", model_name="m", estimated_full_body_tokens=0)

    upstream = _streaming_upstream(500, body=b'{"error":{"message":"boom"}}')

    def boom():
        raise RuntimeError("close failed")
    upstream.close = boom
    monkeypatch.setattr(
        CancellableUpstreamRequest, "request",
        lambda self, method, url, **kw: upstream,
    )
    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
    assert response.status_code == 500
    server.refresh_from_db()
    assert server.workload == 0
    from router.models import RequestRecord
    rec = RequestRecord.objects.get(target_pod_ip="http://err-close.example")
    assert rec.task_status == "failed"
    assert rec.status == "500 Internal Server Error"
