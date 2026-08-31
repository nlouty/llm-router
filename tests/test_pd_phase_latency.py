"""PD phase-latency recording: `prefill_latency` and `decode_latency`.

`prefill_latency` is the wall time of the prefill probe, persisted the moment
prefill completes (together with `input_token_cnt`/`final_prefix_cache`, whose
values are equally final then), so it survives a later decode failure.
`decode_latency` is the wall time of the decode phase, persisted when the phase
ends — success, upstream error, or disconnect — covering KV-transfer wait,
decoder re-selection, and all recompute rounds. Single-node requests leave both
NULL.
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
    model = Model.objects.create(model_name="phase-model", max_tokens=40000)
    Server.objects.create(model_id=model.id, base_url="http://phase-up.example", is_online=True)
    return model


def _seed_pd_cluster(monkeypatch):
    _disable_cmdb(monkeypatch)
    model = Model.objects.create(model_name="phase-pd-model", max_tokens=131072)
    Server.objects.create(
        model_id=model.id, base_url="http://phase-p.example", is_online=True,
        role="prefiller", group_id="g1",
    )
    Server.objects.create(
        model_id=model.id, base_url="http://phase-d.example", is_online=True,
        role="decoder", group_id="g1",
    )
    return model


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


def _decode_error_response():
    resp = MagicMock()
    resp.status_code = 500
    resp.reason = "Internal Server Error"
    resp.headers = {"content-type": "application/json"}
    resp.content = b'{"error":{"message":"boom"}}'
    return resp


@pytest.mark.django_db
def test_pd_non_stream_records_both_phase_latencies(monkeypatch):
    model = _seed_pd_cluster(monkeypatch)

    def fake_post(url, headers=None, data=None, timeout=None):
        if "phase-p.example" in url:
            time.sleep(0.05)  # prefill work before the probe response
            return _prefill_ok_response()
        assert "phase-d.example" in url
        return _decode_ok_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "phase-pd-model", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 200

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.prefill_latency is not None
    assert record.prefill_latency >= 50
    assert record.decode_latency is not None
    assert record.decode_latency >= 0
    # Non-stream ttft ends at prefill completion: prefill fits inside the window.
    assert record.prefill_latency <= record.ttft


@pytest.mark.django_db
def test_pd_stream_records_both_phase_latencies(monkeypatch):
    model = _seed_pd_cluster(monkeypatch)
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}],'
        b'"usage":{"prompt_tokens":100,"completion_tokens":1,'
        b'"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
        b"data: [DONE]\n\n"
    )

    def fake_post(url, headers=None, data=None, timeout=None):
        assert "phase-p.example" in url
        time.sleep(0.05)  # prefill runs before any decode chunk exists
        return _prefill_ok_response()

    def fake_stream_request(method, url, headers=None, data=None, stream=True, timeout=None):
        assert "phase-d.example" in url
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
            "model": "phase-pd-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }),
        content_type="application/json",
    )
    assert response.status_code == 200
    b"".join(response.streaming_content)

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.prefill_latency is not None
    assert record.prefill_latency >= 50
    assert record.decode_latency is not None
    # Streaming ttft covers dispatch + prefill + decode start, so prefill fits inside.
    assert record.prefill_latency <= record.ttft


@pytest.mark.django_db
def test_pd_decode_failure_keeps_prefill_latency(monkeypatch):
    model = _seed_pd_cluster(monkeypatch)

    def fake_post(url, headers=None, data=None, timeout=None):
        if "phase-p.example" in url:
            return _prefill_ok_response()
        assert "phase-d.example" in url
        return _decode_error_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "phase-pd-model", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 500

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "failed"
    # prefill_latency is persisted at prefill completion, before decode ran.
    assert record.prefill_latency is not None
    # decode_latency is persisted at decode-phase end even on failure.
    assert record.decode_latency is not None
    # ttft stays a success-path metric.
    assert record.ttft is None


@pytest.mark.django_db
def test_single_node_request_leaves_phase_latencies_null(monkeypatch):
    model = _seed_single_node(monkeypatch)

    def fake_perform(self, django_request, server, upstream_url, headers, body, is_stream, upstream_client):
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
        data=json.dumps({"model": "phase-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    b"".join(response.streaming_content)

    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.prefill_latency is None
    assert record.decode_latency is None
