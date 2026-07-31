from __future__ import annotations

from unittest.mock import MagicMock

import requests
from django.test import Client

from router.models import Model, RequestRecord, Server
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.services.proxy import ProxyService


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


def test_whitelist_rejects_unknown_path():
    response = Client().post("/v1/something/unknown", data="{}", content_type="application/json")
    assert response.status_code == 501


def test_whitelist_rejects_completions_path():
    response = Client().post("/v1/completions", data="{}", content_type="application/json")
    assert response.status_code == 501


def test_embeddings_skips_auto_router(monkeypatch):
    """Embeddings must not invoke the chat-completions auto-routing algorithm."""
    Model.objects.create(model_name="emb")
    server = Server.objects.create(base_url="http://e1.example", is_online=True)

    def fail_resolve(self, *args, **kwargs):
        raise AssertionError("auto-router.resolve must not be called for embeddings")

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.resolve", fail_resolve)

    service = ProxyService()
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        lambda self_inner, method, url, **kwargs: _make_upstream(200, b'{"data":[]}'),
    )

    django_request = _django_request()
    parsed = MagicMock(
        stream=False,
        body=b'{"model":"emb","input":"hi"}',
        model_name="emb",
        estimated_full_body_tokens=0,
    )
    response = service.forward(django_request, "embeddings", parsed, None, None, None)

    assert response.status_code == 200
    assert server.workload == 0  # incremented then decremented through the attempt


def test_embeddings_body_forwarded_unchanged(monkeypatch):
    """Embeddings body must reach upstream byte-for-byte (no max_tokens/model rewrite)."""
    Model.objects.create(model_name="emb")
    Server.objects.create(base_url="http://e2.example", is_online=True)

    captured = {}

    def fake_request(self_inner, method, url, **kwargs):
        captured["data"] = kwargs.get("data")
        captured["url"] = url
        return _make_upstream(200, b'{"data":[]}')

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    body = b'{"model":"emb","input":"hello world"}'
    service = ProxyService()
    parsed = MagicMock(stream=False, body=body, model_name="emb", estimated_full_body_tokens=0)
    service.forward(_django_request(), "embeddings", parsed, None, None, None)

    assert captured["data"] == body
    assert captured["url"] == "http://e2.example/embeddings"


def test_embeddings_picks_least_loaded_server(monkeypatch):
    """Embeddings must select the least-loaded (lowest workload) server."""
    Model.objects.create(model_name="emb")
    busy = Server.objects.create(base_url="http://busy.example", is_online=True)
    idle = Server.objects.create(base_url="http://idle.example", is_online=True)
    # Push workload so busy is clearly more loaded.
    busy.workload = 9
    busy.save()
    idle.workload = 0
    idle.save()

    chosen = {}

    def fake_request(self_inner, method, url, **kwargs):
        chosen["url"] = url
        return _make_upstream(200, b'{"data":[]}')

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService()
    parsed = MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)
    service.forward(_django_request(), "embeddings", parsed, None, None, None)

    assert chosen["url"] == "http://idle.example/embeddings"


def test_embeddings_retries_on_failure(monkeypatch):
    """A connection error on the first server should retry on another server."""
    Model.objects.create(model_name="emb")
    first = Server.objects.create(base_url="http://first.example", is_online=True)
    second = Server.objects.create(base_url="http://second.example", is_online=True)
    # Pin workloads so least-connection deterministically picks `first` first.
    first.workload = 0
    first.save()
    second.workload = 5
    second.save()

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

    service = ProxyService()
    parsed = MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)
    response = service.forward(_django_request(), "embeddings", parsed, None, None, None)

    assert response.status_code == 200
    # First server failed (connection error), second server succeeded.
    assert len(attempts) == 2
    assert attempts[0].startswith("http://first.example")
    assert attempts[1].startswith("http://second.example")


def test_embeddings_does_not_retry_on_non_connection_error(monkeypatch):
    """A non-connection RequestException must not retry (POST idempotency, #198)."""
    Model.objects.create(model_name="emb")
    first = Server.objects.create(base_url="http://first.example", is_online=True)
    second = Server.objects.create(base_url="http://second.example", is_online=True)
    first.workload = 0
    first.save()
    second.workload = 5
    second.save()

    attempts = []

    def fake_request(self_inner, method, url, **kwargs):
        attempts.append(url)
        if url.startswith("http://first.example"):
            # A mid-stream transport error: the connection was established, so
            # the server may have started processing — must NOT retry.
            raise requests.exceptions.ChunkedEncodingError("truncated")
        return _make_upstream(200, b'{"data":[]}')

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService()
    parsed = MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)
    response = service.forward(_django_request(), "embeddings", parsed, None, None, None)

    # Only the first server was contacted; the request terminated with 502.
    assert response.status_code == 502
    assert len(attempts) == 1
    assert attempts[0].startswith("http://first.example")


def test_embeddings_no_candidates_returns_502():
    """No servers for the model -> 502."""
    Model.objects.create(model_name="emb")
    # No servers created.

    service = ProxyService()
    parsed = MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)
    response = service.forward(_django_request(), "embeddings", parsed, None, None, None)

    assert response.status_code == 502


def _request_record():
    return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get()


def test_embeddings_records_path_in_router_result(monkeypatch):
    """The embeddings endpoint records the path (e.g. 'embeddings') in router_result."""
    Model.objects.create(model_name="emb")
    Server.objects.create(base_url="http://e.example", is_online=True)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        lambda self_inner, method, url, **kwargs: _make_upstream(200, b'{"data":[]}'),
    )
    service = ProxyService()
    parsed = MagicMock(stream=False, body=b'{"model":"emb","input":"x"}', model_name="emb", estimated_full_body_tokens=0)
    service.forward(_django_request(), "embeddings", parsed, None, None, None)

    assert _request_record().router_result == "embeddings"


def test_models_records_path_in_router_result(monkeypatch):
    """The models endpoint records the path ('models') in router_result, not None."""
    Server.objects.create(base_url="http://m.example", is_online=True)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        lambda self_inner, method, url, **kwargs: _make_upstream(200, b'{"data":[]}'),
    )
    service = ProxyService()
    parsed = MagicMock(stream=False, body=b"", model_name=None, estimated_full_body_tokens=0)
    service.forward(_django_request(method="GET"), "models", parsed, None, None, None)

    assert _request_record().router_result == "models"
