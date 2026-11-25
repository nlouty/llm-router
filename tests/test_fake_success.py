"""Fake-success detection tests (issue #353).

A 2xx chat completion that reports zero output tokens means the server failed
to process the request. The router must not generate a new response for the
client — the upstream body/stream passes through unchanged — but the attempt
counts as a circuit-breaker failure instead of a success, so a server that
keeps returning empty completions opens its circuit and is dropped from
rotation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from django.test import Client

from router.models import Model, RequestRecord, Server
from router.services import proxy_response
from router.services.cancellable_upstream import CancellableUpstreamRequest


FAKE_BODY = (
    b'{"choices":[{"message":{"content":""}}],'
    b'"usage":{"prompt_tokens":7,"completion_tokens":0,'
    b'"prompt_tokens_details":{"cached_tokens":0}}}'
)

FAKE_STREAM = (
    b'data: {"choices":[{"delta":{}}],"usage":null}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":0,'
    b'"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
    b'data: [DONE]\n\n'
)


def _upstream(content: bytes):
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.reason = "OK"
    upstream.headers = {"content-type": "application/json"}
    upstream.content = content
    return upstream


def _stream_upstream(sse: bytes):
    upstream = _upstream(b"")
    upstream.iter_content = lambda chunk_size=8192: iter([sse])
    upstream.close = lambda: None
    return upstream


def _patch_upstream(monkeypatch, handler):
    monkeypatch.setattr(CancellableUpstreamRequest, "request", handler)


def _post(payload, **extra):
    return Client().post(
        "/v1/chat/completions",
        data=json.dumps(payload),
        content_type="application/json",
        **extra,
    )


def test_find_json_usage_distinguishes_absent_from_zero():
    assert proxy_response.find_json_usage(b'{"choices": []}') is None
    assert proxy_response.find_json_usage(b"not json") is None
    assert proxy_response.find_json_usage(b'{"usage": null}') is None
    assert proxy_response.find_json_usage(FAKE_BODY) == {
        "prompt_tokens": 7,
        "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def test_is_fake_success_requires_reported_zero_on_chat_path():
    zero = {"prompt_tokens": 5, "completion_tokens": 0}
    assert proxy_response.is_fake_success("chat/completions", zero) is True
    assert proxy_response.is_fake_success("chat/completions/", zero) is True
    # No usage at all, or no completion_tokens key: not evaluable.
    assert proxy_response.is_fake_success("chat/completions", None) is False
    assert proxy_response.is_fake_success("chat/completions", {"prompt_tokens": 5}) is False
    assert proxy_response.is_fake_success("chat/completions", {"completion_tokens": 3}) is False
    # Non-chat endpoints (embeddings usage has no completion_tokens).
    assert proxy_response.is_fake_success("embeddings", zero) is False
    assert proxy_response.is_fake_success("models", zero) is False


@pytest.mark.django_db
def test_normal_fake_success_counts_failure_and_passes_body_through(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    server = Server.objects.create(model_id=model.id, base_url="http://fake.example", is_online=True)

    _patch_upstream(monkeypatch, lambda self, method, url, **kwargs: _upstream(FAKE_BODY))

    response = _post({"model": "user-model", "messages": [{"role": "user", "content": "hi"}]})

    # The client sees the upstream response unchanged; no retry, no synthesis.
    assert response.status_code == 200
    assert response.content == FAKE_BODY
    # The only effect is breaker accounting: a failure, not a success.
    server.refresh_from_db()
    assert server.consecutive_failures == 1
    assert server.circuit_state == "closed"
    # Request-record finishing is unchanged.
    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.output_token_cnt == 0


@pytest.mark.django_db
def test_repeated_fake_successes_open_the_circuit(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    # Two failures already counted; the threshold (3) opens on the next one.
    server = Server.objects.create(
        model_id=model.id,
        base_url="http://fake.example",
        is_online=True,
        consecutive_failures=2,
    )

    def fake_request(self, method, url, **kwargs):
        return _upstream(FAKE_BODY)

    _patch_upstream(monkeypatch, fake_request)

    response = _post({"model": "user-model", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    server.refresh_from_db()
    # Exactly 3: the fake success neither reset the counter (no record_success)
    # nor was swallowed — it counted like any upstream failure.
    assert server.consecutive_failures == 3
    assert server.circuit_state == "open"

    # Open circuit: the server is gone from rotation, so the next request has
    # no candidate and fails fast instead of receiving another empty answer.
    response = _post({"model": "user-model", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 502


@pytest.mark.django_db
def test_normal_success_keeps_counter_reset(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    server = Server.objects.create(
        model_id=model.id,
        base_url="http://ok.example",
        is_online=True,
        consecutive_failures=2,
    )

    body = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
    _patch_upstream(monkeypatch, lambda self, method, url, **kwargs: _upstream(body))

    response = _post({"model": "user-model", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    server.refresh_from_db()
    assert server.consecutive_failures == 0
    assert server.circuit_state == "closed"


@pytest.mark.django_db
def test_stream_fake_success_counts_failure(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    server = Server.objects.create(model_id=model.id, base_url="http://fake.example", is_online=True)

    _patch_upstream(monkeypatch, lambda self, method, url, **kwargs: _stream_upstream(FAKE_STREAM))

    response = _post({"model": "user-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    streamed = b"".join(response.streaming_content)

    # The client already received the empty stream; it passes through.
    assert response.status_code == 200
    assert b'"completion_tokens":0' in streamed
    server.refresh_from_db()
    assert server.consecutive_failures == 1


@pytest.mark.django_db
def test_stream_with_content_but_no_usage_frame_not_flagged(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    server = Server.objects.create(model_id=model.id, base_url="http://ok.example", is_online=True)

    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    _patch_upstream(monkeypatch, lambda self, method, url, **kwargs: _stream_upstream(sse))

    response = _post({"model": "user-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    b"".join(response.streaming_content)

    # A server that ignores stream_options.include_usage is not evaluable:
    # real content streamed, so this is not a fake success.
    server.refresh_from_db()
    assert server.consecutive_failures == 0


@pytest.mark.django_db
def test_embeddings_without_completion_tokens_not_flagged(monkeypatch):
    model = Model.objects.create(model_name="user-model", max_tokens=131072)
    server = Server.objects.create(model_id=model.id, base_url="http://emb.example", is_online=True)

    body = b'{"data": [], "usage": {"prompt_tokens": 5, "total_tokens": 5}}'
    _patch_upstream(monkeypatch, lambda self, method, url, **kwargs: _upstream(body))

    response = Client().post(
        "/v1/embeddings",
        data=json.dumps({"model": "user-model", "input": "hi"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    server.refresh_from_db()
    assert server.consecutive_failures == 0


@pytest.mark.django_db
def test_pd_fake_decode_counts_decoder_failure_not_prefiller(monkeypatch):
    model = Model.objects.create(model_name="pd-model", max_tokens=131072)
    prefiller = Server.objects.create(
        model_id=model.id, base_url="http://p.example", is_online=True,
        role="prefiller", group_id="g1", consecutive_failures=2,
    )
    decoder = Server.objects.create(
        model_id=model.id, base_url="http://d.example", is_online=True,
        role="decoder", group_id="g1",
    )

    def fake_request(self, method, url, **kwargs):
        if "p.example" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.reason = "OK"
            resp.json.return_value = {
                "usage": {"prompt_tokens": 11, "completion_tokens": 1},
                "kv_transfer_params": {
                    "remote_engine_id": "e1",
                    "remote_host": "h",
                    "remote_port": 1,
                },
            }
            return resp
        assert "d.example" in url
        return _upstream(FAKE_BODY)

    _patch_upstream(monkeypatch, fake_request)

    response = _post({"model": "pd-model", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    # The decoder produced nothing: failure. The prefiller did its job: its
    # success is still recorded (counter reset from the preset 2).
    decoder.refresh_from_db()
    assert decoder.consecutive_failures == 1
    prefiller.refresh_from_db()
    assert prefiller.consecutive_failures == 0


@pytest.mark.django_db
def test_pd_stream_without_content_counts_decoder_failure(monkeypatch):
    model = Model.objects.create(model_name="pd-model", max_tokens=131072)
    prefiller = Server.objects.create(
        model_id=model.id, base_url="http://p.example", is_online=True,
        role="prefiller", group_id="g1",
    )
    decoder = Server.objects.create(
        model_id=model.id, base_url="http://d.example", is_online=True,
        role="decoder", group_id="g1",
    )

    def fake_request(self, method, url, **kwargs):
        if "p.example" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.reason = "OK"
            resp.json.return_value = {
                "usage": {"prompt_tokens": 11, "completion_tokens": 1},
                "kv_transfer_params": {
                    "remote_engine_id": "e1",
                    "remote_host": "h",
                    "remote_port": 1,
                },
            }
            return resp
        assert "d.example" in url
        return _stream_upstream(FAKE_STREAM)

    _patch_upstream(monkeypatch, fake_request)

    response = _post({"model": "pd-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    b"".join(response.streaming_content)

    decoder.refresh_from_db()
    assert decoder.consecutive_failures == 1
    prefiller.refresh_from_db()
    assert prefiller.consecutive_failures == 0
