"""PD-aware llm-choosing tests (issue #235).

The choosing request re-enters the router's own pipeline in-process
(ProxyService.forward_internal), so it follows the same PD logic as any
client request: candidates come from list_pd_holders, a chosen prefiller is
served through the two-phase prefill -> decode path, and the choosing record
keeps the llm-choosing conventions (ip_id = 0, user_agent = "llm-choosing").
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


def test_llm_choosing_via_pd_cluster_uses_two_phase_and_records_pd_target(monkeypatch):
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    prefiller = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://p.example",
        is_online=True,
        role="prefiller",
        group_id="g1",
    )
    decoder = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://d.example",
        is_online=True,
        role="decoder",
        group_id="g1",
    )

    def fake_pd_post(url, headers=None, data=None, timeout=None):
        body = json.loads(data)
        if "p.example" in url:
            # Phase 1: prefill-only, non-stream, max_tokens=1, remote decode.
            assert body["kv_transfer_params"]["do_remote_decode"] is True
            assert body["kv_transfer_params"]["do_remote_prefill"] is False
            assert body["stream"] is False
            assert body["max_tokens"] == 1
            response = MagicMock()
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
        # Phase 2: decode carries the prefiller's kv_transfer_params.
        assert "d.example" in url
        assert body["kv_transfer_params"]["remote_engine_id"] == "e1"
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.headers = {"content-type": "application/json"}
        response.content = (
            b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":0}}}'
        )
        return response

    monkeypatch.setattr(
        "router.services.proxy_pd_forward.requests.post", fake_pd_post
    )

    service = ProxyService(_FirstChooser())
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    record = RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)
    assert record.user_agent == "llm-choosing"
    assert record.model_id == routing_model.id
    assert record.target_pod_ip == f"P: {prefiller.base_url} -- D: {decoder.base_url}"
    assert record.task_status == "success"
    assert record.input_token_cnt == 11
    assert record.output_token_cnt == 3
    assert record.is_stream is False


def test_llm_choosing_skips_decoders_and_uses_mixed_server(monkeypatch):
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    # A decoder-only cluster is never a choosing target; the mixed server is.
    Server.objects.create(
        model_id=routing_model.id,
        base_url="http://d.example",
        is_online=True,
        role="decoder",
        group_id="g1",
    )
    mixed = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://m.example",
        is_online=True,
    )

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://m.example/chat/completions"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService(_FirstChooser())
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    record = RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)
    assert record.target_pod_ip == mixed.base_url


def test_llm_choosing_does_not_recurse_when_routing_model_has_auto_flag(monkeypatch):
    # A routing model with auto = TRUE must not re-enter the choosing
    # algorithm: forward_internal forces auto selection off.
    routing_model = Model.objects.create(
        model_name="router-model", is_routing_model=True, auto=True
    )
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10
    )
    Server.objects.create(
        model_id=routing_model.id, base_url="http://router.example", is_online=True
    )

    calls = []

    def fake_request(self_inner, method, url, **kwargs):
        calls.append(url)
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService(_FirstChooser())
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    # Exactly one upstream call and one choosing record: no recursion.
    assert len(calls) == 1
    assert RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).count() == 1


def test_llm_choosing_records_routing_failure_when_pipeline_returns_502(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(
        model_id=routing_model.id, base_url="http://router.example", is_online=True
    )

    # All candidates get dropped before the upstream call: the pipeline
    # returns a synthetic 502 and the choosing call falls back.
    service = ProxyService(_FirstChooser())
    service.max_attempts_per_request = 0

    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == fallback_model
    assert "routing_failed" in router_result
    record = RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)
    assert record.task_status == "failed"


def test_llm_choosing_via_pd_cluster_with_prefix_prefiller(monkeypatch):
    """A prefix-prefiller (p-prefiller, issue #276) takes the same two-phase
    PD path as an n-prefiller: PD dispatch is role-style agnostic."""
    target_model = Model.objects.create(
        model_name="target-model-pp", auto=True, complexity_min=1, complexity_max=10
    )
    routing_model = Model.objects.create(model_name="router-model-pp", is_routing_model=True)
    prefiller = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://pp.example",
        is_online=True,
        role="prefix-prefiller",
        group_id="g1",
    )
    decoder = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://dd.example",
        is_online=True,
        role="decoder",
        group_id="g1",
    )

    def fake_pd_post(url, headers=None, data=None, timeout=None):
        response = MagicMock()
        if "pp.example" in url:
            response.status_code = 200
            response.json.return_value = {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 9},
                },
                "kv_transfer_params": {
                    "remote_engine_id": "e1",
                    "remote_host": "h",
                    "remote_port": 1,
                },
            }
            return response
        assert "dd.example" in url
        response.status_code = 200
        response.reason = "OK"
        response.headers = {"content-type": "application/json"}
        response.content = (
            b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":0}}}'
        )
        return response

    monkeypatch.setattr(
        "router.services.proxy_pd_forward.requests.post", fake_pd_post
    )

    service = ProxyService(_FirstChooser())
    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    model, router_result = _query_routing(service, body, [target_model])

    assert model == target_model
    assert router_result == "complexity:5"
    record = RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)
    assert record.target_pod_ip == f"P: {prefiller.base_url} -- D: {decoder.base_url}"
    assert record.task_status == "success"
