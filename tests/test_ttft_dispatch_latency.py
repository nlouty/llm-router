"""TTFT and dispatch-latency recording (issue #256).

`model_choosing_latency` is the window from request receipt (`send_time`) to
the first send of the request to an LLM server (the prefill probe for PD
requests), recorded on the first dispatch of every request. `ttft` runs from
request receipt to the first-token moment: the first non-empty chunk for
streaming requests, prefill completion for non-streaming PD requests (the
probe generates exactly one token). `ttft - model_choosing_latency` is the
LLM-side time to first token as observed by the router.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from django.test import Client

from router.models import Model, RequestRecord, Server


def _disable_cmdb(monkeypatch):
    # Keep the background CMDB thread away from sqlite writes during the test.
    monkeypatch.setattr("router.views.CMDBService.fetch_and_save_user", lambda self, ip: None)


def _seed_single_node(monkeypatch):
    _disable_cmdb(monkeypatch)
    model = Model.objects.create(model_name="ttft-model", max_tokens=40000)
    Server.objects.create(model_id=model.id, base_url="http://ttft-up.example", is_online=True)
    return model


def _seed_pd_cluster(monkeypatch):
    _disable_cmdb(monkeypatch)
    model = Model.objects.create(model_name="ttft-pd-model", max_tokens=131072)
    Server.objects.create(
        model_id=model.id, base_url="http://ttft-p.example", is_online=True,
        role="prefiller", group_id="g1",
    )
    Server.objects.create(
        model_id=model.id, base_url="http://ttft-d.example", is_online=True,
        role="decoder", group_id="g1",
    )
    return model


def _normal_upstream():
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.reason = "OK"
    upstream.headers = {}
    upstream.content = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":5,"completion_tokens":2}}'
    return upstream


def _prefill_ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.reason = "OK"
    resp.json.return_value = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "kv_transfer_params": {
            "remote_engine_id": "e1",
            "remote_host": "h",
            "remote_port": 1,
        },
    }
    return resp


def _decode_ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.reason = "OK"
    resp.headers = {"content-type": "application/json"}
    resp.content = (
        b'{"choices":[{"message":{"content":"ok"}}],'
        b'"usage":{"prompt_tokens":100,"completion_tokens":2,'
        b'"prompt_tokens_details":{"cached_tokens":0}}}'
    )
    return resp


@pytest.mark.django_db
def test_single_node_stream_ttft_anchored_at_request_receipt(monkeypatch):
    model = _seed_single_node(monkeypatch)

    def fake_perform(self, django_request, server, upstream_url, headers, body, is_stream, upstream_client):
        # 50ms elapses between the upstream send and the response headers /
        # first chunk. The old anchor (streaming generator entry) excluded this
        # window; the receipt anchor must include it.
        time.sleep(0.05)
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.headers = {}

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        upstream.iter_content = lambda chunk_size=8192: gen()
        return upstream

    monkeypatch.setattr("router.services.proxy.ProxyService._perform_request", fake_perform)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "ttft-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    b"".join(response.streaming_content)

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.model_choosing_latency is not None
    assert record.ttft is not None
    assert record.ttft >= 50
    assert record.model_choosing_latency <= record.ttft


@pytest.mark.django_db
def test_single_node_non_stream_records_dispatch_latency_without_ttft(monkeypatch):
    model = _seed_single_node(monkeypatch)

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        lambda self, method, url, **kwargs: _normal_upstream(),
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "ttft-model", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 200

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    # Dispatch latency is recorded for every dispatched request, not only
    # auto-routed ones; single-node non-stream keeps ttft NULL.
    assert record.model_choosing_latency is not None
    assert record.ttft is None


@pytest.mark.django_db
def test_pd_non_stream_records_ttft_at_prefill_completion(monkeypatch):
    model = _seed_pd_cluster(monkeypatch)

    def fake_post(url, headers=None, data=None, timeout=None):
        if "ttft-p.example" in url:
            time.sleep(0.05)  # prefill work before the probe response
            return _prefill_ok_response()
        assert "ttft-d.example" in url
        return _decode_ok_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "ttft-pd-model", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 200

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.model_choosing_latency is not None
    # The prefill probe generates exactly one token, so ttft ends when the
    # probe completes: the 50ms prefill delay must be inside the window.
    assert record.ttft is not None
    assert record.ttft >= 50
    assert record.model_choosing_latency <= record.ttft


@pytest.mark.django_db
def test_pd_stream_ttft_includes_prefill_phase(monkeypatch):
    model = _seed_pd_cluster(monkeypatch)
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}],'
        b'"usage":{"prompt_tokens":100,"completion_tokens":1,'
        b'"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
        b'data: [DONE]\n\n'
    )

    def fake_post(url, headers=None, data=None, timeout=None):
        assert "ttft-p.example" in url
        time.sleep(0.05)  # prefill runs before any decode chunk exists
        return _prefill_ok_response()

    def fake_stream_request(method, url, headers=None, data=None, stream=True, timeout=None):
        assert "ttft-d.example" in url
        resp = MagicMock()
        resp.status_code = 200
        resp.reason = "OK"
        resp.iter_content = lambda chunk_size=8192: iter([sse])
        resp.close = lambda: None
        return resp

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)
    monkeypatch.setattr("router.services.proxy_pd_forward.requests.request", fake_stream_request)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({
            "model": "ttft-pd-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }),
        content_type="application/json",
    )
    assert response.status_code == 200
    streamed = b"".join(response.streaming_content)
    assert b'"hi"' in streamed

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.model_choosing_latency is not None
    # ttft is anchored at request receipt, so the prefill phase is included;
    # under the old decode-only anchor this would be a few milliseconds.
    assert record.ttft is not None
    assert record.ttft >= 50
    assert record.model_choosing_latency <= record.ttft
