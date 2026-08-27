"""Per-server api_key forwarding (issue #279).

Servers behind a gateway that only accepts one specific key must receive that
key on every upstream send — normal, stream, and both PD phases — instead of
the client's own Authorization header. Servers without an api_key keep today's
behavior: the client's Authorization is forwarded verbatim.
"""
from __future__ import annotations

from unittest.mock import MagicMock

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


def _django_request(headers=None):
    dr = MagicMock()
    dr.method = "POST"
    dr.headers = headers or {"Authorization": "Bearer sk-user-key", "Content-Type": "application/json"}
    dr.body = b'{"model":"m","messages":[]}'
    dr.META = {"QUERY_STRING": ""}
    dr.client_disconnect_tracker = None
    return dr


def _make_upstream(status=200, body=b"{}"):
    upstream = MagicMock()
    upstream.status_code = status
    upstream.reason = "OK" if status < 400 else "Bad"
    upstream.content = body
    upstream.headers = {}
    return upstream


def _streaming_upstream(chunks=(b"data: {}\n\n",)):
    upstream = _make_upstream(200)
    upstream.iter_content = lambda chunk_size=8192: iter(chunks)
    return upstream


def test_non_stream_sends_server_api_key(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(base_url="http://keyed.example", is_online=True, api_key="sk-server-secret")

    captured = {}

    def fake_request(self, method, url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _make_upstream(200, b'{"choices":[]}')

    monkeypatch.setattr(CancellableUpstreamRequest, "request", fake_request)

    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=False, body=b'{"model":"m","messages":[]}', model_name="m", estimated_full_body_tokens=0)
    service.forward(_django_request(), "chat/completions", parsed, None, None, None)

    assert captured["headers"]["Authorization"] == "Bearer sk-server-secret"
    assert "sk-user-key" not in str(captured["headers"].values())


def test_non_stream_without_api_key_forwards_client_authorization(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(base_url="http://plain.example", is_online=True)

    captured = {}

    def fake_request(self, method, url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _make_upstream(200, b'{"choices":[]}')

    monkeypatch.setattr(CancellableUpstreamRequest, "request", fake_request)

    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=False, body=b'{"model":"m","messages":[]}', model_name="m", estimated_full_body_tokens=0)
    service.forward(_django_request(), "chat/completions", parsed, None, None, None)

    assert captured["headers"]["Authorization"] == "Bearer sk-user-key"


def test_stream_sends_server_api_key(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(base_url="http://keyed-stream.example", is_online=True, api_key="sk-server-secret")

    captured = {}

    def fake_request(method, url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _streaming_upstream()

    monkeypatch.setattr("router.services.proxy.requests.request", fake_request)

    service = ProxyService(chooser=_ChooserOnce(server))
    parsed = MagicMock(stream=True, body=b'{"model":"m","messages":[],"stream":true}', model_name="m", estimated_full_body_tokens=0)
    response = service.forward(_django_request(), "chat/completions", parsed, None, None, None)
    list(response.streaming_content)

    assert captured["headers"]["Authorization"] == "Bearer sk-server-secret"


def test_pd_sends_each_phase_its_own_server_api_key(monkeypatch):
    """Prefill and decode must each authenticate with their own server's key."""
    from router.services.proxy_pd_forward import PDForwardService

    prefiller = Server.objects.create(
        base_url="http://p.example", is_online=True, role="prefiller", group_id="g1", api_key="sk-prefill-secret"
    )
    decoder = Server.objects.create(
        base_url="http://d.example", is_online=True, role="decoder", group_id="g1", api_key="sk-decode-secret"
    )

    seen = {}

    def fake_pd_post(url, headers=None, data=None, timeout=None):
        seen[url] = headers.get("Authorization")
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.json.return_value = {"usage": {"prompt_tokens": 11, "completion_tokens": 1}}
        response.content = b"{}"
        return response

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_pd_post)

    service = PDForwardService(MagicMock())
    client_headers = {"Authorization": "Bearer sk-user-key", "Content-Type": "application/json"}
    body = b'{"model":"m","messages":[{"role":"user","content":"hello"}]}'
    service._do_prefill(prefiller, "http://p.example/chat/completions", client_headers, body)
    service._post_decode(decoder, "http://d.example/chat/completions", client_headers, body)

    assert seen == {
        "http://p.example/chat/completions": "Bearer sk-prefill-secret",
        "http://d.example/chat/completions": "Bearer sk-decode-secret",
    }


def test_health_check_sends_server_credentials(monkeypatch):
    from router.services.server_health import ServerHealthService

    server = Server.objects.create(
        base_url="http://keyed-health.example", is_online=True, api_key="sk-server-secret", csb_token="csb-tok"
    )

    captured = {}

    def fake_get(url, timeout=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("router.services.server_health.requests.get", fake_get)

    assert ServerHealthService().check_once(server) is True
    assert captured["url"] == "http://keyed-health.example/healthy"
    assert captured["headers"] == {"Authorization": "Bearer sk-server-secret", "csb-token": "csb-tok"}
