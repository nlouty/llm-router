import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from django.http import HttpResponse
from django.test import Client
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Ips, Model, RequestRecord, Server
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.services.admission import AdmissionResult
from router.services.proxy import ProxyService
from router.utils import token_count


class _FakeTokenizer:
    """Duck-typed fast tokenizer: one token per character (small counts)."""

    is_fast = True

    def encode(self, text, add_special_tokens=True):
        return [1] * len(text)


def test_small_request_routing_uses_fast_estimate():
    # Small-request routing is gated on the fast token estimate (no model needed
    # because no tokenizer is selected yet). 0 means no countable body.
    router = AutoRouteAlgorithm()
    assert router.should_route_small_request(MagicMock(estimated_full_body_tokens=0)) is False
    assert router.should_route_small_request(MagicMock(estimated_full_body_tokens=1)) is True
    assert router.should_route_small_request(MagicMock(estimated_full_body_tokens=2999)) is True
    assert router.should_route_small_request(MagicMock(estimated_full_body_tokens=3000)) is False


def test_v1_models_routes_to_random_online_server_without_model_id(monkeypatch):
    Model.objects.create(model_name="model-a")
    model_server = Server.objects.create(model_id=1, base_url="http://model.example", is_online=True)
    shared_server = Server.objects.create(model_id=None, base_url="http://shared.example", is_online=True)
    offline_server = Server.objects.create(model_id=None, base_url="http://offline.example", is_online=False)
    deleted_server = Server.objects.create(model_id=None, base_url="http://deleted.example", is_online=True, deleted_at=timezone.now())

    service = ProxyService()
    choices = []

    def choose(candidates):
        choices.append(list(candidates))
        return shared_server

    monkeypatch.setattr("router.services.proxy.random.choice", choose)

    candidates = service._candidates_for_request("models", None)

    assert candidates == [shared_server]
    assert choices == [[model_server, shared_server]]
    assert offline_server not in choices[0]
    assert deleted_server not in choices[0]


def test_v1_models_endpoint_answered_locally_without_upstream(monkeypatch):
    """GET /v1/models is answered by the gateway itself (issue #246): no
    upstream request is made and the payload is the capability list."""
    monkeypatch.setattr(
        "router.services.admission.timezone.localtime",
        lambda *args: datetime(2026, 6, 1, 12, 0, 0),  # Monday noon -> no boost
    )
    Server.objects.create(model_id=None, base_url="http://shared.example", is_online=True)

    def fail_if_called(self_inner, method, url, **kwargs):
        raise AssertionError(f"upstream should not be called for GET /v1/models, got {url}")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fail_if_called,
    )

    response = Client().get("/v1/models", SERVER_PORT="8001")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"] == [
        {
            "id": "auto",
            "object": "model",
            "created": 0,
            "owned_by": "gateway",
            "max_context": None,
            "max_output_tokens": 40000,
            "concurrent_limit": 6,
        }
    ]
    # Gateway internals are not exposed to users.
    for hidden in ("port", "vip_channel", "concurrent_multiplier", "concurrent_boost_active"):
        assert hidden not in payload


def test_non_models_request_without_model_id_uses_null_model_servers():
    Server.objects.create(model_id=1, base_url="http://model.example", is_online=True)
    shared_server = Server.objects.create(model_id=None, base_url="http://shared.example", is_online=True)

    candidates = ProxyService()._candidates_for_request("chat/completions", None)

    assert candidates == [shared_server]


class _RoutingChooser:
    def choose(self, candidates, context, attempted):
        return candidates[0]

    @staticmethod
    def _text_from_body(body):
        return "route this prompt"


class _PrefixCacheChooser(_RoutingChooser):
    def __init__(self, ratios):
        self.ratios = ratios

    def get_all_model_prefix_ratios(self, body, model_names):
        return {name: self.ratios.get(name, 0.0) for name in model_names}


class _FailingPrefixCacheChooser(_RoutingChooser):
    def get_all_model_prefix_ratios(self, body, model_names):
        raise AssertionError("prefix-cache ratios should not be checked")


class _StubProxy:
    """Adapts the legacy fake_post(url, json, headers, timeout) contract to the
    in-process forward_internal entry the choosing path now uses."""

    def __init__(self, post_fn):
        self._post = post_fn

    def forward_internal(self, body, model, path="chat/completions"):
        payload = json.loads(body)
        response = self._post(f"stub://{model.model_name}/{path}", payload, {}, None)
        content = response.content
        if not isinstance(content, (bytes, bytearray)):
            try:
                content = json.dumps(response.json()).encode("utf-8")
            except Exception:
                content = b""
        elif not content:
            content = b""
        if isinstance(content, str):
            content = content.encode("utf-8")
        try:
            status = int(response.status_code)
        except (TypeError, ValueError):
            status = 200
        return HttpResponse(content, status=status)


def _external_request_record():
    return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get()


def _llm_choosing_record():
    return RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)


def test_auto_route_request_disables_thinking():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["url"] = url
        sent["json"] = json
        sent["headers"] = headers
        sent["timeout"] = timeout
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    service = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))
    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
    )

    model, router_result = service._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:5"
    assert sent["json"]["model"] == "router-model"
    assert sent["json"]["stream"] is False
    assert sent["json"]["messages"][-1] == {
        "role": "user",
        "content": "Here is the user's 1st message:\n```\nhello\n```\n",
    }
    assert sent["json"]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.django_db
def test_count_tokens_after_selection_respects_toggle(monkeypatch):
    # Tokenization runs only when the toggle is on AND the resolved model has a
    # model_path. With it off (the default) no tokenizer is loaded at all.
    from router.repositories.requests import RequestRepository
    from router.services.parser import ParsedRequest

    model = Model.objects.create(model_name="m", model_path="/tmp/fake-path")
    record = RequestRepository.create_processing(None, model.id, False, None)
    parsed = ParsedRequest(
        body=b'{"model":"m","messages":[{"role":"user","content":"hello"}]}',
        model_name="m",
        stream=False,
        max_tokens=None,
        is_json=True,
    )
    calls = []

    def fake_get_tokenizer(path):
        calls.append(path)
        return _FakeTokenizer()

    monkeypatch.setattr(token_count, "_get_tokenizer", fake_get_tokenizer)
    service = ProxyService()

    service.tokenizer_enabled = False
    service._count_tokens_after_selection(parsed, record, model)
    assert record.estimate_tokens == 0
    assert calls == []

    service.tokenizer_enabled = True
    service._count_tokens_after_selection(parsed, record, model)
    assert record.estimate_tokens > 0
    assert calls == ["/tmp/fake-path"]


@pytest.mark.django_db
def test_count_tokens_after_selection_skips_model_without_path(monkeypatch):
    from router.repositories.requests import RequestRepository
    from router.services.parser import ParsedRequest

    model = Model.objects.create(model_name="m")  # no model_path
    record = RequestRepository.create_processing(None, model.id, False, None)
    parsed = ParsedRequest(
        body=b'{"model":"m","messages":[{"role":"user","content":"hello"}]}',
        model_name="m",
        stream=False,
        max_tokens=None,
        is_json=True,
    )
    monkeypatch.setattr(token_count, "_get_tokenizer", lambda path: _FakeTokenizer())
    service = ProxyService()
    service.tokenizer_enabled = True
    service._count_tokens_after_selection(parsed, record, model)
    # No model_path -> nothing counted, no "no model_path" noise logged.
    assert record.estimate_tokens == 0


def test_auto_route_records_llm_choosing_request_row(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        sent_body = json.loads(kwargs["data"])
        assert sent_body["model"] == "router-model"
        assert sent_body["stream"] is False
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = (
            b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":2}}}'
        )
        upstream.headers = {"content-type": "application/json"}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    service = ProxyService(_RoutingChooser())
    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
    )

    model, router_result = service.auto_router._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:5"
    record = _llm_choosing_record()
    assert record.ip_id == LLM_CHOOSING_IP_ID
    assert record.user_agent == "llm-choosing"
    assert record.model_id == routing_model.id
    assert record.target_pod_ip == "http://router.example"
    assert record.status == "200 OK"
    assert record.task_status == "success"
    assert record.input_token_cnt == 11
    assert record.output_token_cnt == 3
    assert record.final_prefix_cache == 2


def test_auto_route_choosing_picks_least_loaded_routing_model_pool():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    busy_model = Model.objects.create(model_name="busy-router", is_routing_model=True)
    idle_model = Model.objects.create(model_name="idle-router", is_routing_model=True)
    Server.objects.create(model_id=busy_model.id, base_url="http://busy.example", is_online=True, workload=4)
    Server.objects.create(model_id=idle_model.id, base_url="http://idle.example", is_online=True, workload=1)

    picked = AutoRouteAlgorithm()._pick_routing_model([busy_model, idle_model])

    assert picked == idle_model


def test_auto_route_choosing_randomizes_tied_routing_model_pools(monkeypatch):
    first = Model.objects.create(model_name="first-router", is_routing_model=True)
    second = Model.objects.create(model_name="second-router", is_routing_model=True)
    Server.objects.create(model_id=first.id, base_url="http://first.example", is_online=True, workload=1)
    Server.objects.create(model_id=second.id, base_url="http://second.example", is_online=True, workload=1)

    choices = []

    def choose(options):
        choices.append(list(options))
        return options[1]

    monkeypatch.setattr("router.route_algorithm.auto.random.choice", choose)

    picked = AutoRouteAlgorithm()._pick_routing_model([first, second])

    assert picked == second
    assert choices == [[first, second]]


def test_auto_route_choosing_skips_decoder_only_routing_model():
    # A routing model whose only servers are decoders (never directly routable)
    # or prefillers without a decoder in the cluster is not a valid choosing
    # target; a model with a routable pool must win.
    decoder_only = Model.objects.create(model_name="decoder-router", is_routing_model=True)
    stranded_prefiller = Model.objects.create(model_name="stranded-router", is_routing_model=True)
    healthy = Model.objects.create(model_name="healthy-router", is_routing_model=True)
    Server.objects.create(model_id=decoder_only.id, base_url="http://d.example", is_online=True, role="decoder", group_id="g1")
    Server.objects.create(model_id=stranded_prefiller.id, base_url="http://p.example", is_online=True, role="prefiller", group_id="g2")
    Server.objects.create(model_id=healthy.id, base_url="http://m.example", is_online=True, workload=2)

    picked = AutoRouteAlgorithm()._pick_routing_model([decoder_only, stranded_prefiller, healthy])

    assert picked == healthy


def test_auto_route_choosing_exception_falls_back_to_default_model():
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def boom(url, json, headers, timeout):
        raise RuntimeError("routing down")

    service = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(boom))
    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
    )

    model, router_result = service._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == fallback_model
    assert router_result == "routing_error:exception:routing down"
    # The internal pipeline never ran, so no llm-choosing record was created.
    assert not RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).exists()


def test_auto_route_payload_only_forwards_user_role_messages():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["json"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":4}'}}]}
        return response

    request_body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "user system prompt"},
            {"role": "developer", "content": "developer skill instructions"},
            {"role": "user", "content": "first user request"},
            {"role": "assistant", "content": "assistant response"},
            {"role": "tool", "content": "mcp tool result"},
            {"role": "user", "content": [{"type": "text", "text": "second user request"}], "name": "alice"},
        ],
        "skills": [{"name": "secret-skill"}],
        "mcp_servers": [{"name": "secret-mcp"}],
        "tools": [{"type": "function", "function": {"name": "secret_tool"}}],
    }
    body = json.dumps(request_body).encode("utf-8")
    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
    )

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:4"
    assert sent["json"]["messages"][0]["role"] == "system"
    assert sent["json"]["messages"][1:] == [
        {
            "role": "user",
            "content": "Here is the user's 1st message:\n```\nfirst user request\n```\n",
        },
        {
            "role": "user",
            "content": "Here is the user's 2nd message:\n```\nsecond user request\n```\n",
        },
    ]
    payload_text = json.dumps(sent["json"])
    assert "user system prompt" not in payload_text
    assert "developer skill instructions" not in payload_text
    assert "assistant response" not in payload_text
    assert "mcp tool result" not in payload_text
    assert "secret-skill" not in payload_text
    assert "secret-mcp" not in payload_text
    assert "secret_tool" not in payload_text


def test_auto_route_payload_forwards_medium_user_messages_in_full():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    medium_content = "x" * 1200
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["json"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": medium_content}],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        body,
        MagicMock(id=123),
        MagicMock(),
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:5"
    assert sent["json"]["messages"][-1] == {
        "role": "user",
        "content": f"Here is the user's 1st message:\n```\n{medium_content}\n```\n",
    }


def test_auto_route_payload_collapses_very_long_user_messages():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    head = "h" * 500
    tail = "t" * 500
    middle = "m" * 600
    long_content = head + middle + tail
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["json"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": long_content}],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        body,
        MagicMock(id=123),
        MagicMock(),
        [target_model],
        [target_model.model_name],
    )

    omitted = len(middle)
    assert model == target_model
    assert router_result == "complexity:5"
    assert sent["json"]["messages"][-1] == {
        "role": "user",
        "content": f"Here is the user's 1st message:\n```\n{head} ... collapsed {omitted} chars ... {tail}\n```\n",
    }


def test_auto_route_without_active_target_model_records_router_result():
    service = AutoRouteAlgorithm(_RoutingChooser())
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model is None
    assert router_result == (
        "routing_failed:missing_target_model:no auto-routing target model for auto request"
    )


def test_auto_route_complexity_can_select_auto_false_target():
    low_model = Model.objects.create(
        model_name="low-model",
        auto=False,
        complexity_min=1,
        complexity_max=3,
    )
    Model.objects.create(
        model_name="high-model",
        auto=True,
        complexity_min=7,
        complexity_max=10,
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":1}'}}]}
        return response

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"simple"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == low_model
    assert router_result == "complexity:1"


def test_text_only_content_parts_do_not_use_multimodal_bypass():
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    Model.objects.create(model_name="vision-model", auto=True, multimodal=True)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    body = json.dumps({
        "model": "auto",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ],
    }).encode("utf-8")

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert router_result == "complexity:5"


def test_chat_image_content_parts_use_multimodal_bypass():
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True)
    Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for image auto requests")

    body = json.dumps({
        "model": "auto",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc123"},
                    },
                ],
            },
        ],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fail_if_called))._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == vision_model
    assert router_result == "multimodal_bypass"


def test_separate_image_message_uses_multimodal_bypass():
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True)
    Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for image auto requests")

    body = json.dumps({
        "model": "auto",
        "messages": [
            {"role": "user", "content": "@snapshot.png 这图片里讲了什么？"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,"},
                    },
                ],
            },
        ],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fail_if_called))._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == vision_model
    assert router_result == "multimodal_bypass"


def test_auto_route_prefix_cache_uses_only_auto_selectable_models():
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    ignored_model = Model.objects.create(model_name="ignored-model")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent on prefix-cache hit")

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"router-model": 0.99, "target-model": 0.95}), proxy=_StubProxy(fail_if_called))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"earlier"},{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert ignored_model.complexity_min is None
    assert routing_model.complexity_min is None
    assert router_result == "cache_hit"


def test_auto_route_prefix_cache_can_select_auto_false_target():
    Model.objects.create(model_name="high-model", auto=True, complexity_min=7, complexity_max=10)
    low_model = Model.objects.create(
        model_name="low-model",
        auto=False,
        complexity_min=1,
        complexity_max=3,
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent on prefix-cache hit")

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"low-model": 0.95, "high-model": 0.5}), proxy=_StubProxy(fail_if_called))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"earlier"},{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == low_model
    assert router_result == "cache_hit"


def test_auto_route_prefix_cache_multiple_hits_uses_routing_llm():
    low_model = Model.objects.create(
        model_name="low-model",
        auto=False,
        complexity_min=1,
        complexity_max=3,
    )
    Model.objects.create(
        model_name="high-model",
        auto=True,
        complexity_min=7,
        complexity_max=10,
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":1}'}}]}
        return response

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"low-model": 0.95, "high-model": 0.9}), proxy=_StubProxy(fake_post))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"earlier"},{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == low_model
    assert router_result == "complexity:1"


def test_auto_route_single_user_prompt_skips_prefix_cache_and_uses_routing_llm(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    model, router_result = AutoRouteAlgorithm(_FailingPrefixCacheChooser(), proxy=_StubProxy(fake_post))._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert router_result == "complexity:5"


def test_case_insensitive_auto_request_selects_target_model(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    def fake_request(self_inner, method, url, **kwargs):
        data = json.loads(kwargs["data"].decode("utf-8"))
        if url == "http://router.example/chat/completions":
            # The internal llm-choosing call routes through the same pipeline.
            assert data["model"] == "router-model"
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://target.example/chat/completions"
        assert data["model"] == "target-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "AUTO", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == target_model.id
    assert record.router_result == "AUTO:complexity:5"


def test_model_auto_flag_triggers_auto_selection_on_normal_channel(monkeypatch):
    source_model = Model.objects.create(model_name="source-model", auto=True)
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=source_model.id, base_url="http://source.example", is_online=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    def fake_request(self_inner, method, url, **kwargs):
        data = json.loads(kwargs["data"].decode("utf-8"))
        if url == "http://router.example/chat/completions":
            # The internal llm-choosing call routes through the same pipeline.
            assert data["model"] == "router-model"
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":4}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://target.example/chat/completions"
        assert data["model"] == "target-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "source-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == target_model.id
    assert record.router_result == "source-model:complexity:4"


def test_original_request_latency_remains_end_to_end_when_llm_choosing_is_logged(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    base_time = timezone.now()
    # now() consumers in the new in-process flow (requests module): outer
    # record creation, choosing pool pick (list_pd_holders), choosing record
    # creation, choosing pool pick, choosing dispatch latency, choosing
    # circuit-breaker success, choosing finish, outer pool pick, outer
    # dispatch latency, outer circuit-breaker success, outer finish.
    request_times = [
        base_time,                                  # 1. outer create
        base_time + timedelta(milliseconds=100),    # 2. choosing pool pick
        base_time + timedelta(milliseconds=100),    # 3. choosing create
        base_time + timedelta(milliseconds=250),    # 4. choosing pool pick
        base_time + timedelta(milliseconds=250),    # 5. choosing dispatch latency
        base_time + timedelta(milliseconds=250),    # 6. choosing cb success
        base_time + timedelta(milliseconds=250),    # 7. choosing finish
        base_time + timedelta(milliseconds=400),    # 8. outer pool pick
        base_time + timedelta(milliseconds=400),    # 9. outer dispatch latency
        base_time + timedelta(milliseconds=400),    # 10. outer cb success
        base_time + timedelta(milliseconds=700),    # 11. outer finish
    ]

    def fake_now():
        if request_times:
            return request_times.pop(0)
        return base_time + timedelta(milliseconds=700)

    def fake_request(self_inner, method, url, **kwargs):
        if url == "http://router.example/chat/completions":
            # The internal llm-choosing call routes through the same pipeline.
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://target.example/chat/completions"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.repositories.requests.timezone.now", fake_now)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    body = b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None
    django_request.body = body
    parsed = MagicMock(
        stream=False,
        body=body,
        model_name="auto",
        estimated_full_body_tokens=3000,
    )

    response = ProxyService(chooser=_RoutingChooser()).forward(
        django_request,
        "chat/completions",
        parsed,
        1,
        None,
        None,
    )

    assert response.status_code == 200
    original = _external_request_record()
    choosing = _llm_choosing_record()
    assert original.model_id == target_model.id
    assert original.latency == 700
    assert original.router_result == "auto:complexity:5"
    assert choosing.model_id == routing_model.id
    assert choosing.latency == 150


def test_auto_entrance_concurrency_uses_requested_model_then_routes_by_complexity(monkeypatch):
    source_model = Model.objects.create(model_name="source-model", auto=True, concurrent_limit=2)
    target_model = Model.objects.create(
        model_name="low-model",
        auto=False,
        complexity_min=1,
        complexity_max=3,
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=source_model.id, base_url="http://source.example", is_online=True)
    Server.objects.create(model_id=target_model.id, base_url="http://low.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    concurrency_calls = []

    def fake_check_concurrency(self, ip, model, is_auto=False):
        concurrency_calls.append((model, is_auto))
        return AdmissionResult(True)

    def fake_request(self_inner, method, url, **kwargs):
        data = json.loads(kwargs["data"].decode("utf-8"))
        if url == "http://router.example/chat/completions":
            # The internal llm-choosing call routes through the same pipeline.
            assert data["model"] == "router-model"
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":1}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://low.example/chat/completions"
        assert data["model"] == "low-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.admission.AdmissionService.check_concurrency",
        fake_check_concurrency,
    )
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "source-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert concurrency_calls == [(source_model, True)]
    record = _external_request_record()
    assert record.model_id == target_model.id
    assert record.router_result == "source-model:complexity:1"


def test_auto_entrance_multimodal_request_selects_auto_false_multimodal_model(monkeypatch):
    source_model = Model.objects.create(model_name="source-model", auto=True)
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True)
    Server.objects.create(model_id=source_model.id, base_url="http://source.example", is_online=True)
    Server.objects.create(model_id=vision_model.id, base_url="http://vision.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    # The multimodal bypass must skip the choosing pipeline entirely (no
    # llm-choosing record is created by the internal forward).
    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://vision.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "vision-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({
            "model": "source-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc123"},
                        },
                    ],
                },
            ],
        }),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == vision_model.id
    assert record.router_result == "source-model:multimodal_bypass"
    assert not RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).exists()


def test_auto_false_concrete_model_request_keeps_requested_model_for_multimodal(monkeypatch):
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True, max_tokens=65536)
    Server.objects.create(model_id=vision_model.id, base_url="http://vision.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    # Non-auto concrete requests never enter the choosing pipeline.
    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://vision.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "vision-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc123"},
                        },
                    ],
                },
            ],
        }),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == vision_model.id
    assert record.router_result is None
    assert not RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).exists()


def test_model_auto_flag_keeps_original_model_on_vip_channel(monkeypatch):
    source_model = Model.objects.create(model_name="source-model", auto=True)
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=source_model.id, base_url="http://source.example", is_online=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
    Ips.objects.create(ip="10.10.10.12", concurrent_multiplier=1.0, vip=True)

    # VIP channels skip auto selection, so the choosing pipeline never runs.
    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://source.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "source-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "source-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        SERVER_PORT="8008",
        REMOTE_ADDR="10.10.10.12",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == source_model.id
    assert record.router_result is None
    assert not RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).exists()


def test_auto_route_multiple_matching_complexity_ranges_use_fallback(monkeypatch):
    broad_model = Model.objects.create(model_name="broad-model", auto=True, complexity_min=1, complexity_max=10)
    narrow_model = Model.objects.create(model_name="narrow-model", auto=True, complexity_min=7, complexity_max=8)
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":7}'}}]}
        return response

    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hard task"}]}',
    )
    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [broad_model, narrow_model],
        [broad_model.model_name, narrow_model.model_name],
    )

    assert model == fallback_model
    assert router_result == (
        "routing_failed:multiple_models_for_complexity:"
        "complexity 7 matched multiple auto-routing target models: broad-model,narrow-model"
    )


def test_auto_route_without_matching_complexity_uses_fallback(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=3)
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":8}'}}]}
        return response

    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hard task"}]}',
    )
    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == fallback_model
    assert router_result == (
        "routing_failed:no_model_for_complexity:complexity 8 has no matching auto-routing target model"
    )


def test_auto_route_invalid_complexity_uses_fallback(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "target-model"}}]}
        return response

    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
    )
    model, router_result = AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == fallback_model
    assert router_result == (
        "routing_failed:invalid_routing_result:router returned no valid complexity: target-model"
    )


def test_routing_complexity_extracts_numbers_from_imperfect_responses():
    assert AutoRouteAlgorithm._routing_complexity('```json\n{"complexity":8}\n```') == 8
    assert AutoRouteAlgorithm._routing_complexity('The request complexity is 6.') == 6
    assert AutoRouteAlgorithm._routing_complexity('{"complexity": 9,}') == 9
    assert AutoRouteAlgorithm._routing_complexity('{"complexity":7.5}') is None


def test_routing_payload_requests_structured_output(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["payload"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    context = ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
    )
    AutoRouteAlgorithm(_RoutingChooser(), proxy=_StubProxy(fake_post))._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    response_format = sent["payload"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["properties"]["complexity"] == {"type": "integer", "minimum": 1, "maximum": 10}
    assert schema["required"] == ["complexity"]
    assert schema["additionalProperties"] is False


def test_small_counted_request_uses_routing_model_before_complexity(monkeypatch):
    Model.objects.create(model_name="user-model", max_tokens=30000, auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        assert data["chat_template_kwargs"] == {"enable_thinking": False}
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "user-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "user-model:small_request_routing"


def test_auto_route_without_routing_model_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash", auto=True, complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://deepseek.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "DeepSeek-V4-Flash"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == (
        "auto:routing_failed:missing_routing_model:no routing model configured"
    )


def test_small_counted_request_uses_routing_model_directly(monkeypatch):
    Model.objects.create(model_name="user-model", max_tokens=30000, auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "user-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "user-model:small_request_routing"


def test_update_body_model_can_disable_thinking():
    service = AutoRouteAlgorithm(_RoutingChooser())

    body = service.update_body_model(
        b'{"model":"auto","stream":true,"chat_template_kwargs":{"tokenize":false}}',
        "target-model",
        disable_thinking=True,
    )
    data = json.loads(body.decode("utf-8"))

    assert data["model"] == "target-model"
    assert data["stream"] is True
    assert data["chat_template_kwargs"] == {"tokenize": False, "enable_thinking": False}


def test_small_non_auto_request_stays_on_requested_model(monkeypatch):
    user_model = Model.objects.create(model_name="user-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert method == "POST"
        assert url == "http://user.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "user-model"
        assert "chat_template_kwargs" not in data
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None
    parsed = MagicMock(
        stream=False,
        body=b'{"model":"user-model","messages":[{"role":"user","content":"hello"}]}',
        model_name="user-model",
        estimated_full_body_tokens=2999,
    )

    response = ProxyService(chooser=_RoutingChooser()).forward(
        django_request,
        "chat/completions",
        parsed,
        None,
        user_model,
        None,
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_three_thousand_token_non_auto_request_skips_unneeded_routing(monkeypatch):
    user_model = Model.objects.create(model_name="user-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://user.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "user-model"
        assert "chat_template_kwargs" not in data
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None
    parsed = MagicMock(
        stream=False,
        body=b'{"model":"user-model","messages":[{"role":"user","content":"hello"}]}',
        model_name="user-model",
        estimated_full_body_tokens=3000,
    )

    response = ProxyService(chooser=_RoutingChooser()).forward(
        django_request,
        "chat/completions",
        parsed,
        None,
        user_model,
        None,
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_non_auto_request_does_not_call_routing_llm_and_keeps_user_model(monkeypatch):
    user_model = Model.objects.create(model_name="user-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://user.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "user-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None
    parsed = MagicMock(
        stream=False,
        body=b'{"model":"user-model","messages":[{"role":"user","content":"hello"}]}',
        model_name="user-model",
        estimated_full_body_tokens=3000,
    )

    response = ProxyService(chooser=_PrefixCacheChooser({"user-model": 0.95})).forward(
        django_request,
        "chat/completions",
        parsed,
        None,
        user_model,
        None,
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_small_auto_request_records_dispatch_latency(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    monotonic_values = iter([10.0, 10.125, 10.125])
    monkeypatch.setattr("router.services.proxy.time.monotonic", lambda: next(monotonic_values))

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None
    parsed = MagicMock(
        stream=False,
        body=b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
        model_name="auto",
        estimated_full_body_tokens=10,
    )

    response = ProxyService(chooser=_RoutingChooser()).forward(
        django_request,
        "chat/completions",
        parsed,
        None,
        None,
        None,
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.router_result == "auto:small_request_routing"
    # model_choosing_latency now covers request receipt to the first upstream
    # send, so it is recorded on the dispatch of every request (auto or not).
    assert record.model_choosing_latency is not None


def test_small_counted_request_routes_directly_to_routing_server(monkeypatch):
    Model.objects.create(model_name="user-model", max_tokens=30000, auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "user-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "user-model:small_request_routing"


def test_auto_route_without_routing_server_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash", auto=True, complexity_min=1, complexity_max=10)
    Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://deepseek.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "DeepSeek-V4-Flash"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == (
        "auto:routing_failed:missing_routing_server:no available routing server"
    )


def test_small_counted_request_succeeds_with_routing_server(monkeypatch):
    Model.objects.create(model_name="user-model", max_tokens=30000, auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm._check_cache_hit", lambda *args: None)
    # Choosing must not run on this path: the internal pipeline creates no
    # llm-choosing record.
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "user-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert RequestRecord.objects.count() == 1

    record = RequestRecord.objects.get()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "user-model:small_request_routing"


def test_deprecated_model_blocks_normal_user_but_serves_vip(monkeypatch):
    deprecated_model = Model.objects.create(
        model_name="glm-5",
        deprecation="glm-5 is deprecated, please use glm-6.",
        max_tokens=65536,
    )
    Server.objects.create(model_id=deprecated_model.id, base_url="http://glm5.example", is_online=True)
    monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
    Ips.objects.create(ip="10.10.10.20", concurrent_multiplier=1.0, vip=True)

    # Normal port: deprecation blocks the request with 400.
    normal_response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "glm-5", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )
    assert normal_response.status_code == 400
    assert normal_response.json()["error"]["message"] == "glm-5 is deprecated, please use glm-6."

    # VIP port: the VIP user is let through to the model's own servers.
    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://glm5.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "glm-5"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    vip_response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "glm-5", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        SERVER_PORT="8008",
        REMOTE_ADDR="10.10.10.20",
    )

    assert vip_response.status_code == 200
    record = RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get(task_status="success")
    assert record.model_id == deprecated_model.id
    assert record.router_result is None


def test_deprecated_model_with_complexity_bounds_serves_auto_request(monkeypatch):
    deprecated_model = Model.objects.create(
        model_name="glm-5",
        deprecation="glm-5 is deprecated, please use glm-6.",
        complexity_min=1,
        complexity_max=10,
    )
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=deprecated_model.id, base_url="http://glm5.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    def fake_request(self_inner, method, url, **kwargs):
        data = json.loads(kwargs["data"].decode("utf-8"))
        if url == "http://router.example/chat/completions":
            # The internal llm-choosing call routes through the same pipeline.
            assert data["model"] == "router-model"
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://glm5.example/chat/completions"
        assert data["model"] == "glm-5"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = _external_request_record()
    assert record.model_id == deprecated_model.id
    assert record.router_result == "auto:complexity:5"
