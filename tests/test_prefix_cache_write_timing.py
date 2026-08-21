"""The Redis prefix-cache write must happen after the response is delivered.

``on_response`` (the HSET+EXPIRE batch) used to run before the response was
handed to the client (non-stream) / before the first streamed byte (stream,
at header receipt). It is now invoked from a ``response.close()`` callback,
which gunicorn runs after writing every chunk to the client socket — so a
slow Redis can no longer delay the user-visible response or TTFT.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from router.models import Model, Server
from router.services.proxy import ProxyService


class _SpyChooser:
    def __init__(self):
        self.calls = []

    def choose(self, candidates, context, attempted):
        return candidates[0]

    def on_response(self, server, context, status_code):
        self.calls.append((server.id, status_code))


def _django_request():
    req = MagicMock()
    req.method = "POST"
    req.headers = {}
    req.body = b"{}"
    req.META = {"QUERY_STRING": ""}
    req.client_disconnect_tracker = None
    return req


def _parsed(stream):
    return MagicMock(
        stream=stream,
        body=b'{"model":"m","messages":[{"role":"user","content":"hi"}]}',
        model_name="m",
        estimated_full_body_tokens=10,
        data=None,
    )


@pytest.mark.django_db
def test_non_stream_prefix_cache_write_waits_for_response_close(monkeypatch):
    model = Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=model.id, base_url="http://m.example", is_online=True)
    spy = _SpyChooser()

    def fake_request(self_inner, method, url, **kwargs):
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    service = ProxyService(chooser=spy)

    response = service.forward(_django_request(), "chat/completions", _parsed(False), 1, model, None)

    assert response.status_code == 200
    # The hook must not run while the response is being produced...
    assert spy.calls == []
    # ...it runs only once the WSGI server has finished sending the response.
    response.close()
    assert spy.calls == [(server.id, 200)]


@pytest.mark.django_db
def test_stream_prefix_cache_write_waits_for_response_close(monkeypatch):
    model = Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=model.id, base_url="http://m.example", is_online=True)
    spy = _SpyChooser()

    upstream = MagicMock()
    upstream.status_code = 200
    upstream.reason = "OK"
    upstream.headers = {}
    upstream.iter_content.return_value = iter([b"data: {}\n\n", b"data: [DONE]\n\n"])
    monkeypatch.setattr("router.services.proxy.requests.request", lambda *a, **k: upstream)

    service = ProxyService(chooser=spy)

    response = service.forward(_django_request(), "chat/completions", _parsed(True), 1, model, None)

    assert b"".join(response)  # consume the stream fully
    # Not at header receipt, and not at stream completion either:
    assert spy.calls == []
    response.close()
    assert spy.calls == [(server.id, 200)]


def test_close_hook_runs_callback_even_when_original_close_raises():
    # The cache write must be attempted even if the response's own close()
    # raises, mirroring gunicorn's finally semantics.
    from django.http import HttpResponse

    calls = []
    response = HttpResponse(b"ok")

    def broken_close():
        calls.append("original")
        raise OSError("socket already closed")

    response.close = broken_close
    attached = ProxyService._attach_response_close_hook(
        response, lambda: calls.append("callback")
    )
    with pytest.raises(OSError):
        attached.close()
    assert calls == ["original", "callback"]
