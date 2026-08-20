"""llm-choosing request timeout budget tests.

The choosing request re-enters the router's own pipeline in-process
(ProxyService.forward_internal). It is capped at proxy.llm_choosing_timeout_
seconds: every upstream attempt's (connect, read) socket timeouts are clamped
to the remaining budget, so a hung routing server is disconnected at the
deadline and the choosing call ends as a 504 instead of blocking for up to
normal_read_timeout_seconds (900s) per attempt.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from router.models import Model, RequestRecord, Server
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.route_algorithm.base import ServerSelectionContext
from router.services.proxy import ProxyService


class _FirstChooser:
    def choose(self, candidates, context, attempted):
        return candidates[0]


def _context(body: bytes) -> ServerSelectionContext:
    return ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
    )


def _query_routing(service, body, target_models):
    context = _context(body)
    return service.auto_router._query_routing_llm(
        body,
        MagicMock(id=123),
        context,
        target_models,
        [model.model_name for model in target_models],
    )


def _routing_models():
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    return target_model, routing_model


def _routing_ok_upstream():
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.reason = "OK"
    upstream.headers = {}
    upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
    return upstream


def test_llm_choosing_clamps_single_node_socket_timeout_to_budget(monkeypatch):
    target_model, routing_model = _routing_models()
    Server.objects.create(
        model_id=routing_model.id, base_url="http://m.example", is_online=True
    )

    captured = {}

    def fake_request(self_inner, method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return _routing_ok_upstream()

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService(_FirstChooser())
    service.llm_choosing_timeout = 2
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    connect_timeout, read_timeout = captured["timeout"]
    # Socket timeouts are clamped to the remaining choosing budget (2s), not
    # the normal (5, 900) client-request timeouts.
    assert 0 < read_timeout <= 2
    assert 0 < connect_timeout <= 2


def test_llm_choosing_clamps_pd_prefill_and_decode_timeouts_to_budget(monkeypatch):
    target_model, routing_model = _routing_models()
    Server.objects.create(
        model_id=routing_model.id,
        base_url="http://p.example",
        is_online=True,
        role="prefiller",
        group_id="g1",
    )
    Server.objects.create(
        model_id=routing_model.id,
        base_url="http://d.example",
        is_online=True,
        role="decoder",
        group_id="g1",
    )

    timeouts = []

    def fake_pd_post(url, headers=None, data=None, timeout=None):
        timeouts.append(timeout)
        response = MagicMock()
        if "p.example" in url:
            response.status_code = 200
            response.json.return_value = {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
                "kv_transfer_params": {
                    "remote_engine_id": "e1",
                    "remote_host": "h",
                    "remote_port": 1,
                },
            }
            return response
        response.status_code = 200
        response.reason = "OK"
        response.headers = {"content-type": "application/json"}
        response.content = (
            b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":0}}}'
        )
        return response

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_pd_post)

    service = ProxyService(_FirstChooser())
    service.llm_choosing_timeout = 2
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    # Both PD phases (prefill then decode) are clamped to the remaining
    # choosing budget instead of prefill (5, 300) / normal (5, 900).
    assert len(timeouts) == 2
    for connect_timeout, read_timeout in timeouts:
        assert 0 < read_timeout <= 2
        assert 0 < connect_timeout <= 2


def test_llm_choosing_deadline_exhaustion_skips_upstream_and_falls_back(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    target_model, routing_model = _routing_models()
    Server.objects.create(
        model_id=routing_model.id, base_url="http://m.example", is_online=True
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("upstream must not be called once the choosing budget is spent")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fail_if_called,
    )

    service = ProxyService(_FirstChooser())
    service.llm_choosing_timeout = 0  # deadline passes before the first attempt
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == fallback_model
    assert "routing_failed" in router_result
    record = RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)
    assert record.task_status == "failed"
