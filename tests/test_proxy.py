import json
from datetime import timedelta
from unittest.mock import MagicMock

from django.test import Client
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Ips, Model, RequestRecord, Server
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.services.admission import AdmissionResult
from router.services.proxy import ProxyService


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


def test_v1_models_endpoint_allows_missing_model_name(monkeypatch):
    Server.objects.create(model_id=None, base_url="http://shared.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert method == "GET"
        assert url == "http://shared.example/models"
        assert kwargs["data"] is None
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b'{"data":[]}'
        upstream.headers = {"content-type": "application/json"}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().get("/v1/models")

    assert response.status_code == 200
    assert response.content == b'{"data":[]}'


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


class _FailingRoutingChooser:
    def choose(self, candidates, context, attempted):
        raise AssertionError("routing LLM server selection should use workload directly")


def _external_request_record():
    return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get()


def _llm_choosing_record():
    return RequestRecord.objects.get(ip_id=LLM_CHOOSING_IP_ID)


def test_auto_route_request_disables_thinking(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    service = AutoRouteAlgorithm(_RoutingChooser())
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
    assert sent["url"] == "http://router.example/chat/completions"
    assert sent["json"]["model"] == "router-model"
    assert sent["json"]["stream"] is False
    assert sent["json"]["messages"][-1] == {
        "role": "user",
        "content": "Here is the user's 1st message:\n```\nhello\n```\n",
    }
    assert sent["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_auto_route_records_llm_choosing_request_row(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.content = (
            b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":2}}}'
        )
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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
    assert record.model_id == routing_model.id
    assert record.target_pod_ip == "http://router.example"
    assert record.attempt_count == 1
    assert record.status == "200 OK"
    assert record.task_status == "success"
    assert record.input_token_cnt == 11
    assert record.output_token_cnt == 3
    assert record.final_prefix_cache == 2
    assert record.latency is not None


def test_auto_route_choosing_request_uses_least_workload_server(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    busy = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://busy-router.example",
        is_online=True,
        workload=4,
    )
    idle = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://idle-router.example",
        is_online=True,
        workload=1,
    )

    def fake_post(url, json, headers, timeout):
        assert url == "http://idle-router.example/chat/completions"
        busy.refresh_from_db()
        idle.refresh_from_db()
        assert busy.workload == 4
        assert idle.workload == 2
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    model, router_result = AutoRouteAlgorithm(_FailingRoutingChooser())._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:5"
    busy.refresh_from_db()
    idle.refresh_from_db()
    assert busy.workload == 4
    assert idle.workload == 1
    assert _llm_choosing_record().target_pod_ip == "http://idle-router.example"


def test_auto_route_choosing_request_randomizes_tied_workload_servers(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    first = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://first-router.example",
        is_online=True,
        workload=1,
    )
    second = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://second-router.example",
        is_online=True,
        workload=1,
    )
    choices = []

    def choose(options):
        choices.append(list(options))
        return options[1]

    def fake_post(url, json, headers, timeout):
        assert url == "http://second-router.example/chat/completions"
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.workload == 1
        assert second.workload == 2
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    monkeypatch.setattr("router.route_algorithm.least_connection.random.choice", choose)
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    model, router_result = AutoRouteAlgorithm(_FailingRoutingChooser())._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "complexity:5"
    assert [[server.id for server in options] for options in choices] == [[first.id, second.id]]
    assert _llm_choosing_record().target_pod_ip == "http://second-router.example"


def test_auto_route_choosing_request_decrements_workload_after_exception(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    server = Server.objects.create(
        model_id=routing_model.id,
        base_url="http://router.example",
        is_online=True,
        workload=3,
    )

    def fake_post(url, json, headers, timeout):
        server.refresh_from_db()
        assert server.workload == 4
        raise RuntimeError("routing down")

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    model, router_result = AutoRouteAlgorithm(_FailingRoutingChooser())._query_routing_llm(
        context.body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == fallback_model
    assert router_result == "routing_error:exception:routing down"
    server.refresh_from_db()
    assert server.workload == 3
    record = _llm_choosing_record()
    assert record.task_status == "failed"
    assert record.status == "502 Bad Gateway"


def test_auto_route_payload_only_forwards_user_role_messages(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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


def test_auto_route_payload_forwards_medium_user_messages_in_full(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": medium_content}],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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


def test_auto_route_payload_collapses_very_long_user_messages(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": long_content}],
    }).encode("utf-8")

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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


def test_auto_route_complexity_can_select_auto_false_target(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"simple"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == low_model
    assert router_result == "complexity:1"


def test_text_only_content_parts_do_not_use_multimodal_bypass(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert router_result == "complexity:5"


def test_chat_image_content_parts_use_multimodal_bypass(monkeypatch):
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True)
    Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for image auto requests")

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)

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

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == vision_model
    assert router_result == "multimodal_bypass"


def test_separate_image_message_uses_multimodal_bypass(monkeypatch):
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True)
    Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for image auto requests")

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)

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

    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == vision_model
    assert router_result == "multimodal_bypass"


def test_auto_route_prefix_cache_uses_only_auto_selectable_models(monkeypatch):
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    ignored_model = Model.objects.create(model_name="ignored-model")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent on prefix-cache hit")

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"router-model": 0.99, "target-model": 0.95}))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"earlier"},{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert ignored_model.complexity_min is None
    assert routing_model.complexity_min is None
    assert router_result == "cache_hit"


def test_auto_route_prefix_cache_can_select_auto_false_target(monkeypatch):
    Model.objects.create(model_name="high-model", auto=True, complexity_min=7, complexity_max=10)
    low_model = Model.objects.create(
        model_name="low-model",
        auto=False,
        complexity_min=1,
        complexity_max=3,
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent on prefix-cache hit")

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"low-model": 0.95, "high-model": 0.5}))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"earlier"},{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == low_model
    assert router_result == "cache_hit"


def test_auto_route_prefix_cache_multiple_hits_uses_routing_llm(monkeypatch):
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    service = AutoRouteAlgorithm(_PrefixCacheChooser({"low-model": 0.95, "high-model": 0.9}))
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

    model, router_result = AutoRouteAlgorithm(_FailingPrefixCacheChooser())._get_auto_route_model(
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

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://target.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "target-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":4}'}}]}
        return response

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://target.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "target-model"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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
    request_times = [
        base_time,
        base_time + timedelta(milliseconds=100),
        base_time + timedelta(milliseconds=250),
        base_time + timedelta(milliseconds=400),
        base_time + timedelta(milliseconds=700),
    ]

    def fake_now():
        if request_times:
            return request_times.pop(0)
        return base_time + timedelta(milliseconds=700)

    def fake_post(url, json, headers, timeout):
        assert url == "http://router.example/chat/completions"
        response = MagicMock()
        response.status_code = 200
        response.reason = "OK"
        response.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://target.example/chat/completions"
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.repositories.requests.timezone.now", fake_now)
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":1}'}}]}
        return response

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://low.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for image auto requests")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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


def test_auto_false_concrete_model_request_keeps_requested_model_for_multimodal(monkeypatch):
    vision_model = Model.objects.create(model_name="vision-model", auto=False, multimodal=True, max_tokens=65536)
    Server.objects.create(model_id=vision_model.id, base_url="http://vision.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for non-auto concrete model requests")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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


def test_model_auto_flag_keeps_original_model_on_vip_channel(monkeypatch):
    source_model = Model.objects.create(model_name="source-model", auto=True)
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=source_model.id, base_url="http://source.example", is_online=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
    Ips.objects.create(ip="10.10.10.12", concurrent_multiplier=1.0, vip=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent for concrete VIP model requests")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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
    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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
    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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
    model, router_result = AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)

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
    AutoRouteAlgorithm(_RoutingChooser())._query_routing_llm(
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


def test_small_auto_request_uses_routing_model_before_complexity(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for small auto requests")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "auto:small_request_routing"


def test_auto_route_without_routing_model_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash", auto=True, complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent without routing models")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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


def test_small_auto_request_uses_routing_model_directly(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for small auto requests")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "auto:small_request_routing"


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


def test_small_non_auto_request_uses_routing_server_and_disables_thinking(monkeypatch):
    user_model = Model.objects.create(model_name="user-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_request(self_inner, method, url, **kwargs):
        assert method == "POST"
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


def test_three_thousand_token_non_auto_request_skips_unneeded_routing(monkeypatch):
    user_model = Model.objects.create(model_name="user-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for explicit model requests")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called on prefix-cache hit")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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


def test_small_auto_request_records_small_request_latency(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for small auto requests")

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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
    assert record.model_choosing_latency == 125


def test_small_auto_request_routes_directly_to_routing_server(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for small auto requests")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "auto:small_request_routing"


def test_auto_route_without_routing_server_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash", auto=True, complexity_min=1, complexity_max=10)
    Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent without routing servers")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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


def test_small_auto_request_succeeds_with_routing_server(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing LLM should not be called for small auto requests")

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
    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fail_if_called)
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
    assert RequestRecord.objects.count() == 1

    record = RequestRecord.objects.get()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "auto:small_request_routing"


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

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"complexity":5}'}}]}
        return response

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

    monkeypatch.setattr("router.route_algorithm.auto.requests.post", fake_post)
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
