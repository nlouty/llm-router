import json
from unittest.mock import MagicMock

from django.test import Client
from django.utils import timezone

from router.models import Model, RequestRecord, ROUTER_RESULT_MAX_LENGTH, Server
from router.route_algorithm.base import ServerSelectionContext
from router.services.parser import RequestParser
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


class _CapturingPrefixCacheChooser(_PrefixCacheChooser):
    def __init__(self, ratios):
        super().__init__(ratios)
        self.prefix_body = None

    def get_all_model_prefix_ratios(self, body, model_names):
        self.prefix_body = body
        return super().get_all_model_prefix_ratios(body, model_names)


def _large_tool_body(model_name="user-model"):
    return json.dumps({
        "model": model_name,
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "expensive_tool",
                            "arguments": "x" * 20000,
                        },
                    },
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "expensive_tool",
                    "description": "d" * 20000,
                },
            },
        ],
    }).encode("utf-8")


def test_auto_route_request_disables_thinking(monkeypatch):
    target_model = Model.objects.create(model_name="target-model")
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
        response.json.return_value = {"choices": [{"message": {"content": "target-model"}}]}
        return response

    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)

    service = ProxyService(chooser=_RoutingChooser())
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
    assert router_result == "target-model"
    assert sent["url"] == "http://router.example/chat/completions"
    assert sent["json"]["model"] == "router-model"
    assert sent["json"]["stream"] is False
    assert sent["json"]["messages"][-1] == {"role": "user", "content": "hello"}
    assert sent["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_auto_route_payload_only_forwards_user_role_messages(monkeypatch):
    target_model = Model.objects.create(model_name="target-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    sent = {}

    def fake_post(url, json, headers, timeout):
        sent["json"] = json
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "target-model"}}]}
        return response

    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)

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

    model, router_result = ProxyService(chooser=_RoutingChooser())._query_routing_llm(
        body,
        MagicMock(id=123),
        context,
        [target_model],
        [target_model.model_name],
    )

    assert model == target_model
    assert router_result == "target-model"
    assert sent["json"]["messages"][0]["role"] == "system"
    assert sent["json"]["messages"][1:] == [
        {"role": "user", "content": "first user request"},
        {"role": "user", "content": [{"type": "text", "text": "second user request"}]},
    ]
    payload_text = json.dumps(sent["json"])
    assert "user system prompt" not in payload_text
    assert "developer skill instructions" not in payload_text
    assert "assistant response" not in payload_text
    assert "mcp tool result" not in payload_text
    assert "secret-skill" not in payload_text
    assert "secret-mcp" not in payload_text
    assert "secret_tool" not in payload_text


def test_auto_selection_prefix_cache_uses_only_user_prompt():
    target_model = Model.objects.create(model_name="target-model")
    chooser = _CapturingPrefixCacheChooser({"target-model": 0.95})

    request_body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "user system prompt"},
            {"role": "developer", "content": "developer skill instructions"},
            {"role": "user", "content": "first user request"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "secret_tool", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "content": "mcp tool result", "tool_call_id": "call_1"},
            {"role": "user", "content": [{"type": "text", "text": "second user request"}]},
        ],
        "tools": [{"type": "function", "function": {"name": "secret_tool"}}],
    }

    model, router_result = ProxyService(chooser=chooser)._get_auto_route_model(
        json.dumps(request_body).encode("utf-8"),
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert router_result == "cache_hit"
    assert json.loads(chooser.prefix_body.decode("utf-8")) == {
        "messages": [
            {"role": "user", "content": "first user request"},
            {"role": "user", "content": [{"type": "text", "text": "second user request"}]},
        ],
    }
    prefix_text = chooser.prefix_body.decode("utf-8")
    assert "user system prompt" not in prefix_text
    assert "developer skill instructions" not in prefix_text
    assert "secret_tool" not in prefix_text
    assert "mcp tool result" not in prefix_text


def test_auto_route_without_user_prompt_can_use_prefix_cache(monkeypatch):
    target_model = Model.objects.create(model_name="target-model")
    Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("cache hit should not call routing LLM")

    monkeypatch.setattr("router.services.proxy.requests.post", fail_if_called)
    chooser = _CapturingPrefixCacheChooser({"target-model": 0.95})
    body = json.dumps({
        "model": "auto",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "assistant", "content": "assistant response"},
            {"role": "tool", "content": "tool result"},
        ],
    }).encode("utf-8")

    model, router_result = ProxyService(chooser=chooser)._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == target_model
    assert router_result == "cache_hit"
    assert chooser.prefix_body == body


def test_auto_route_without_user_prompt_uses_default_model_after_cache_miss(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    Model.objects.create(model_name="target-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("auto request without user prompt should not call routing LLM")

    monkeypatch.setattr("router.services.proxy.requests.post", fail_if_called)
    chooser = _CapturingPrefixCacheChooser({})
    body = json.dumps({
        "model": "auto",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "assistant", "content": "assistant response"},
            {"role": "tool", "content": "tool result"},
        ],
    }).encode("utf-8")

    model, router_result = ProxyService(chooser=chooser)._get_auto_route_model(
        body,
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == fallback_model
    assert router_result == "routing_failed:missing_user_prompt:no user prompt for auto request"
    assert chooser.prefix_body == body


def test_auto_route_without_active_target_model_records_router_result():
    service = ProxyService(chooser=_RoutingChooser())
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model is None
    assert router_result == (
        "routing_failed:missing_target_model:no active target model for auto request"
    )


def test_auto_route_can_use_routing_model_as_prefix_cache_target(monkeypatch):
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("routing request should not be sent on prefix-cache hit")

    monkeypatch.setattr("router.services.proxy.requests.post", fail_if_called)

    service = ProxyService(chooser=_PrefixCacheChooser({"router-model": 0.95}))
    model, router_result = service._get_auto_route_model(
        b'{"model":"auto","messages":[{"role":"user","content":"hello"}]}',
        MagicMock(id=123),
        MagicMock(),
    )

    assert model == routing_model
    assert router_result == "cache_hit"


def test_auto_route_can_use_routing_model_as_final_processing_model(monkeypatch):
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        assert url == "http://router.example/chat/completions"
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "router-model"}}]}
        return response

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

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == routing_model.id
    assert record.task_status == "success"
    assert record.router_result == "router-model"


def test_auto_route_without_routing_model_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
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

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fail_if_called)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == (
        "routing_failed:missing_routing_model:no routing model configured"
    )


def test_auto_route_invalid_routing_result_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "not-a-configured-model"}}],
        }
        return response

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

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == (
        "routing_failed:invalid_routing_result:router returned no active model: not-a-configured-model"
    )


def test_update_body_model_can_disable_thinking():
    service = ProxyService(chooser=_RoutingChooser())

    body = service._update_body_model(
        b'{"model":"auto","stream":true,"chat_template_kwargs":{"tokenize":false}}',
        "target-model",
        disable_thinking=True,
    )
    data = json.loads(body.decode("utf-8"))

    assert data["model"] == "target-model"
    assert data["stream"] is True
    assert data["chat_template_kwargs"] == {"tokenize": False, "enable_thinking": False}


def test_router_result_formatter_keeps_extended_detail():
    detail = "x" * (ROUTER_RESULT_MAX_LENGTH + 50)

    router_result = ProxyService._format_router_result("routing_failed", 502, detail)

    assert len(router_result) == ROUTER_RESULT_MAX_LENGTH
    assert len(router_result) > 100


def test_small_request_threshold_uses_full_json_body():
    parsed = RequestParser().parse(_large_tool_body())

    assert parsed.estimated_input_tokens > ProxyService.SMALL_REQUEST_ROUTING_TOKEN_LIMIT
    assert ProxyService(chooser=_RoutingChooser())._should_route_small_request(parsed) is False


def test_request_record_estimate_tokens_uses_full_json_body(monkeypatch):
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
        upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == user_model.id
    assert record.estimate_tokens > ProxyService.SMALL_REQUEST_ROUTING_TOKEN_LIMIT


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
        estimated_input_tokens=2999,
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


def test_three_thousand_token_non_auto_request_keeps_user_model(monkeypatch):
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
        estimated_input_tokens=3000,
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


def test_small_auto_request_uses_routing_server_before_auto_route(monkeypatch):
    target_model = Model.objects.create(model_name="target-model")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fail_auto_route(*args, **kwargs):
        raise AssertionError("small auto request should not call the routing LLM")

    def fake_request(self_inner, method, url, **kwargs):
        assert url == "http://router.example/chat/completions"
        data = json.loads(kwargs["data"].decode("utf-8"))
        assert data["model"] == "router-model"
        assert data["chat_template_kwargs"] == {"enable_thinking": False}
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr("router.services.proxy.requests.post", fail_auto_route)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    monotonic_values = iter([10.0, 10.125])
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
        estimated_input_tokens=10,
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
    record = RequestRecord.objects.get()
    assert record.model_id == routing_model.id
    assert record.router_result == "small_request_routing"
    assert record.model_choosing_latency == 125


def test_auto_route_failed_routing_uses_deepseek_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=fallback_model.id, base_url="http://deepseek.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        assert url == "http://router.example/chat/completions"
        response = MagicMock()
        response.status_code = 502
        response.reason = "Bad Gateway"
        response.content = b'{"error":{"message":"router down","type":"server_error","code":null}}'
        return response

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

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == "routing_failed:502:server_error: router down"


def test_auto_route_without_routing_server_uses_fallback_and_records_router_result(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
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

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fail_if_called)
    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.get()
    assert record.model_id == fallback_model.id
    assert record.task_status == "success"
    assert record.router_result == (
        "routing_failed:missing_routing_server:no available routing server"
    )


def test_auto_route_failed_routing_without_fallback_server_finishes_same_record(monkeypatch):
    fallback_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    def fake_post(url, json, headers, timeout):
        response = MagicMock()
        response.status_code = 502
        response.reason = "Bad Gateway"
        response.content = b'{"error":{"message":"router down","type":"server_error","code":null}}'
        return response

    monkeypatch.setattr("router.services.proxy.ProxyService._check_cache_hit", lambda *args: None)
    monkeypatch.setattr("router.services.proxy.requests.post", fake_post)

    response = Client().post(
        "/v1/chat/completions",
        data=_large_tool_body(model_name="auto"),
        content_type="application/json",
    )

    assert response.status_code == 502
    assert response.json() == {"error": {"message": "502 Bad Gateway", "type": "server_error", "code": None}}
    assert RequestRecord.objects.count() == 1

    record = RequestRecord.objects.get()
    assert record.model_id == fallback_model.id
    assert record.task_status == "failed"
    assert record.status == "502 Bad Gateway"
    assert record.fail_reason == "no available server for model DeepSeek-V4-Flash"
    assert record.attempt_count == 0
    assert record.target_pod_ip is None
    assert record.router_result == "routing_failed:502:server_error: router down"
