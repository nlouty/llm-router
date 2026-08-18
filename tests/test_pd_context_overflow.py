"""PD prefill context-overflow retry tests (issue #248).

A PD request's prefill probe (max_tokens=1) is the first upstream call, so a
prompt that exceeds the prefiller's context window surfaces as that server's
400 "maximum context length" error during prefill. The router must retry on a
same-model server with a strictly larger context window instead of failing
terminally. Decoders are never gated by the window check: candidates come
from list_pd_holders (mixed/prefiller roles only), matching the operator
invariant prefillers.context_window + max_completion_tokens <
decoders.context_window — decoder windows stay NULL (unlimited) here.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from django.test import Client

from router.models import Model, RequestRecord, Server


OVERFLOW_BODY = json.dumps({
    "object": "error",
    "message": (
        "This model's maximum context length is 300000 tokens. However, you "
        "requested 1 output tokens and your prompt contains at least 300000 "
        "input tokens, for a total of at least 300001 tokens. Please reduce "
        "the length of the input prompt or the number of requested output "
        "tokens. (parameter=input_tokens, value=300000)"
    ),
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}).encode("utf-8")


def _setup_small_cluster(model):
    p1 = Server.objects.create(
        model_id=model.id, base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", context_window=300000,
    )
    Server.objects.create(
        model_id=model.id, base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", context_window=None,
    )
    return p1


def _setup_large_cluster(model):
    # A non-zero workload makes the large prefiller deterministically more
    # loaded than the idle small one: the preble chooser picks prefillers by
    # Server.workload (ties are broken randomly).
    p2 = Server.objects.create(
        model_id=model.id, base_url="http://p2.example", is_online=True,
        role="prefiller", group_id="g2", context_window=1000000, workload=5,
    )
    Server.objects.create(
        model_id=model.id, base_url="http://d2.example", is_online=True,
        role="decoder", group_id="g2", context_window=None,
    )
    return p2


def _overflow_response():
    resp = MagicMock()
    resp.status_code = 400
    resp.reason = "Bad Request"
    resp.content = OVERFLOW_BODY
    return resp


def _prefill_ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.reason = "OK"
    resp.json.return_value = {
        "usage": {
            "prompt_tokens": 300050,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "kv_transfer_params": {
            "remote_engine_id": "e2",
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
        b'"usage":{"prompt_tokens":300050,"completion_tokens":2,'
        b'"prompt_tokens_details":{"cached_tokens":0}}}'
    )
    return resp


def test_pd_prefill_overflow_retries_on_larger_window_prefiller(monkeypatch):
    model = Model.objects.create(model_name="Long-Model", max_tokens=131072)
    p1 = _setup_small_cluster(model)
    p2 = _setup_large_cluster(model)

    contacted = []

    def fake_post(url, headers=None, data=None, timeout=None):
        contacted.append(url)
        if "p1.example" in url:
            return _overflow_response()
        if "p2.example" in url:
            return _prefill_ok_response()
        assert "d2.example" in url
        return _decode_ok_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "Long-Model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )

    assert response.status_code == 200
    assert json.loads(response.content)["choices"][0]["message"]["content"] == "ok"
    # Small prefiller overflowed, then the large cluster served prefill+decode.
    assert contacted == [
        "http://p1.example/chat/completions",
        "http://p2.example/chat/completions",
        "http://d2.example/chat/completions",
    ]
    record = RequestRecord.objects.get(model_id=model.id)
    assert record.attempt_count == 2
    assert record.target_pod_ip == "P: http://p2.example -- D: http://d2.example"
    assert record.task_status == "success"
    # An overflow is a routing fact, not a health failure: the breaker on the
    # small prefiller must not count it.
    p1.refresh_from_db()
    assert p1.consecutive_failures == 0


def test_pd_prefill_overflow_without_larger_prefiller_returns_real_body(monkeypatch):
    model = Model.objects.create(model_name="Long-Model", max_tokens=131072)
    _setup_small_cluster(model)

    contacted = []

    def fake_post(url, headers=None, data=None, timeout=None):
        contacted.append(url)
        return _overflow_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "Long-Model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )

    # No larger-window server exists: the real upstream overflow error surfaces.
    assert response.status_code == 400
    assert b"maximum context length is 300000 tokens" in response.content
    assert contacted == ["http://p1.example/chat/completions"]
    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "failed"
    assert "300000" in (record.fail_reason or "")


def test_pd_prefill_non_overflow_400_is_terminal_and_counts_failure(monkeypatch):
    model = Model.objects.create(model_name="Long-Model", max_tokens=131072)
    p1 = _setup_small_cluster(model)

    body = json.dumps({
        "error": {"message": "unsupported parameter", "type": "invalid_request_error"}
    }).encode("utf-8")

    def fake_post(url, headers=None, data=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 400
        resp.reason = "Bad Request"
        resp.content = body
        return resp

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "Long-Model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )

    # A non-overflow 400 keeps today's terminal behavior and does trip the breaker.
    assert response.status_code == 400
    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "failed"
    p1.refresh_from_db()
    assert p1.consecutive_failures >= 1


def test_pd_prefill_overflow_stream_retries_before_any_client_bytes(monkeypatch):
    model = Model.objects.create(model_name="Long-Model", max_tokens=131072)
    p1 = _setup_small_cluster(model)
    p2 = _setup_large_cluster(model)

    contacted = []
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}],'
        b'"usage":{"prompt_tokens":300050,"completion_tokens":1,'
        b'"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
        b'data: [DONE]\n\n'
    )

    def fake_post(url, headers=None, data=None, timeout=None):
        contacted.append(url)
        if "p1.example" in url:
            return _overflow_response()
        assert "p2.example" in url
        return _prefill_ok_response()

    def fake_stream_request(method, url, headers=None, data=None, stream=True, timeout=None):
        contacted.append(url)
        assert "d2.example" in url
        resp = MagicMock()
        resp.status_code = 200
        resp.reason = "OK"
        resp.iter_content = lambda chunk_size=8192: iter([sse])
        resp.close = lambda: None
        return resp

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)
    monkeypatch.setattr("router.services.proxy_pd_forward.requests.request", fake_stream_request)

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({
            "model": "Long-Model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        }),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )

    assert response.status_code == 200
    streamed = b"".join(response.streaming_content)
    # The client sees the large cluster's SSE stream, not an error frame.
    assert b'"hi"' in streamed
    assert b'"error"' not in streamed
    assert contacted == [
        "http://p1.example/chat/completions",
        "http://p2.example/chat/completions",
        "http://d2.example/chat/completions",
    ]
    record = RequestRecord.objects.get(model_id=model.id)
    assert record.task_status == "success"
    assert record.target_pod_ip == "P: http://p2.example -- D: http://d2.example"
