"""Forced-session routing (issue #357).

Normal-port ``/v1/chat/completions`` requests whose session id is listed in
``router.forced_routing_sessions`` (default: dsh-pet, chrys-judge, chrys-pet)
are served by the routing model (``models.is_routing_model = TRUE``) whatever
model they name — the explicit-model rewrite that small-request routing
(issue #227) never does. The redirect is neither auto- nor size-gated, runs
after multimodal detection, never applies on the VIP port, and falls through
unchanged when no routing model has a routable pool.
"""

import json
from unittest.mock import MagicMock

import pytest
from django.test import Client
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import (
    ExternalModelMapping,
    ExternalRoute,
    Ips,
    Model,
    RequestRecord,
    Server,
    UserIP,
)
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext

FORCED_SESSIONS = ["dsh-pet", "chrys-judge", "chrys-pet"]

USER_URL = "http://user.example/chat/completions"
ROUTER_URL = "http://router.example/chat/completions"
EXT_URL = "http://ext.example/v1/chat/completions"

SSE_CHUNKS = [
    b'data: {"id":"c1","choices":[{"delta":{"content":"he"}}]}\n\n',
    b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
    b"data: [DONE]\n\n",
]


def _setup(user_model_extra=None, routing_pool="online"):
    """A concrete model with one online server, plus a routing model whose
    pool is online, offline (``offline``) or absent altogether (``none``).
    max_tokens sits above the parser-injected default so max-token admission
    never interferes with the routing assertions."""
    user_model = Model.objects.create(
        model_name="user-model", max_tokens=65536, **(user_model_extra or {})
    )
    Server.objects.create(model_id=user_model.id, base_url="http://user.example", is_online=True)
    if routing_pool == "none":
        return user_model, None
    routing_model = Model.objects.create(
        model_name="router-model", is_routing_model=True, max_tokens=65536
    )
    Server.objects.create(
        model_id=routing_model.id,
        base_url="http://router.example",
        is_online=routing_pool == "online",
    )
    return user_model, routing_model


def _patch_upstream(monkeypatch, calls, chunks=None):
    """Record every (url, parsed body) the proxy sends and answer with a plain
    200 upstream, or with an SSE stream when *chunks* is given."""

    def fake_request(self_inner, method, url, **kwargs):
        calls.append((url, json.loads(kwargs["data"].decode("utf-8"))))
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        if chunks:
            upstream.headers = {"Content-Type": "text/event-stream"}
            upstream.iter_content = lambda chunk_size=8192: iter(chunks)
            upstream.close = lambda: None
        else:
            upstream.headers = {"Content-Type": "application/json"}
            upstream.content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    return calls


def _post(session, model="user-model", content="hello", stream=False, image=False, **extra):
    message = {"role": "user", "content": content}
    if image:
        message["content"] = [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    body = {"model": model, "messages": [message]}
    if stream:
        body["stream"] = True
    return Client().post(
        "/v1/chat/completions",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_SESSION_ID=session,
        **extra,
    )


def _record():
    return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).get()


@pytest.mark.parametrize("session", FORCED_SESSIONS)
def test_forced_session_with_concrete_model_is_redirected_to_routing_model(monkeypatch, session):
    user_model, routing_model = _setup()
    calls = _patch_upstream(monkeypatch, [])

    response = _post(session, model="user-model")

    assert response.status_code == 200
    assert [url for url, _ in calls] == [ROUTER_URL]
    sent = calls[0][1]
    assert sent["model"] == "router-model"
    assert sent["chat_template_kwargs"]["enable_thinking"] is False
    record = _record()
    assert record.session == session
    assert record.model_id == routing_model.id
    assert record.router_result == "user-model:forced_session_routing"
    assert record.task_status == "success"
    # The rewrite happens before any classification: no llm-choosing row.
    assert not RequestRecord.objects.filter(ip_id=LLM_CHOOSING_IP_ID).exists()


def test_forced_session_streaming_request_is_redirected(monkeypatch):
    user_model, routing_model = _setup()
    calls = _patch_upstream(monkeypatch, [], chunks=SSE_CHUNKS)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "user-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        # Any supported session header identifies the session, here Chrys.
        HTTP_CHRYS_SESSION_ID="chrys-pet",
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    assert b"he" in b"".join(response.streaming_content)
    assert calls[0][0] == ROUTER_URL
    assert calls[0][1]["model"] == "router-model"
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["chat_template_kwargs"]["enable_thinking"] is False
    record = _record()
    assert record.session == "chrys-pet"
    assert record.model_id == routing_model.id
    assert record.router_result == "user-model:forced_session_routing"
    assert record.task_status == "success"


def test_large_auto_request_from_forced_session_skips_classifier(monkeypatch):
    # Not size-gated: a request far above the small-request limit still goes to
    # the routing model instead of entering complexity classification.
    _setup()
    target_model = Model.objects.create(
        model_name="target-model", auto=True, complexity_min=1, complexity_max=10, max_tokens=65536
    )
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    calls = _patch_upstream(monkeypatch, [])

    response = _post("dsh-pet", model="auto", content="x" * 20000)

    assert response.status_code == 200
    # One upstream call only: the classifier never ran.
    assert [url for url, _ in calls] == [ROUTER_URL]
    assert calls[0][1]["model"] == "router-model"
    record = _record()
    assert record.router_result == "auto:forced_session_routing"
    assert record.model_id == Model.objects.get(model_name="router-model").id


def test_non_matching_session_keeps_requested_model(monkeypatch):
    user_model, _ = _setup()
    calls = _patch_upstream(monkeypatch, [])

    response = _post("sess-1", model="user-model")

    assert response.status_code == 200
    assert [url for url, _ in calls] == [USER_URL]
    assert calls[0][1]["model"] == "user-model"
    assert "chat_template_kwargs" not in calls[0][1]
    record = _record()
    assert record.model_id == user_model.id
    assert record.router_result is None


@pytest.mark.parametrize("routing_pool", ["offline", "none"])
def test_forced_session_without_routable_routing_model_falls_through(monkeypatch, routing_pool):
    user_model, _ = _setup(routing_pool=routing_pool)
    calls = _patch_upstream(monkeypatch, [])

    response = _post("dsh-pet", model="user-model")

    assert response.status_code == 200
    assert [url for url, _ in calls] == [USER_URL]
    record = _record()
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_vip_port_request_from_forced_session_keeps_requested_model(monkeypatch):
    # The VIP port (8008) is never redirected, whatever the session asks for.
    user_model, _ = _setup()
    monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
    Ips.objects.create(ip="10.10.10.12", concurrent_multiplier=1.0, vip=True)
    calls = _patch_upstream(monkeypatch, [])

    response = _post(
        "dsh-pet",
        model="user-model",
        SERVER_PORT="8008",
        REMOTE_ADDR="10.10.10.12",
    )

    assert response.status_code == 200
    assert [url for url, _ in calls] == [USER_URL]
    assert calls[0][1]["model"] == "user-model"
    record = _record()
    assert record.session == "dsh-pet"
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_image_request_with_concrete_model_is_never_forced_onto_routing_model(monkeypatch):
    # The routing model is text-only (issue #324): an image-bearing request
    # from a forced session keeps its requested model instead.
    user_model, _ = _setup()
    calls = _patch_upstream(monkeypatch, [])

    response = _post("dsh-pet", model="user-model", image=True)

    assert response.status_code == 200
    assert [url for url, _ in calls] == [USER_URL]
    record = _record()
    assert record.model_id == user_model.id
    assert record.router_result is None


def test_auto_image_request_from_forced_session_goes_to_vision_model(monkeypatch):
    # Multimodal detection outranks the forced redirect, as it outranks
    # small-request routing.
    _setup()
    vision_model = Model.objects.create(model_name="vision-model", multimodal=True, max_tokens=65536)
    Server.objects.create(model_id=vision_model.id, base_url="http://vision.example", is_online=True)
    calls = _patch_upstream(monkeypatch, [])

    response = _post("chrys-judge", model="auto", image=True)

    assert response.status_code == 200
    assert [url for url, _ in calls] == ["http://vision.example/chat/completions"]
    record = _record()
    assert record.model_id == vision_model.id
    assert record.router_result == "auto:multimodal_bypass"


def test_embeddings_from_forced_session_are_not_redirected(monkeypatch):
    # Only chat completions carry a session and enter model resolution.
    user_model, _ = _setup()
    calls = _patch_upstream(monkeypatch, [])

    response = Client().post(
        "/v1/embeddings",
        data=json.dumps({"model": "user-model", "input": "hi"}),
        content_type="application/json",
        HTTP_X_SESSION_ID="dsh-pet",
    )

    assert response.status_code == 200
    assert calls[0][0] == "http://user.example/embeddings"
    record = _record()
    assert record.model_id == user_model.id
    assert record.router_result == "embeddings"


def test_forced_session_list_is_configurable_and_exact(monkeypatch):
    assert AutoRouteAlgorithm.is_forced_routing_session("dsh-pet") is True
    assert AutoRouteAlgorithm.is_forced_routing_session("chrys-judge") is True
    assert AutoRouteAlgorithm.is_forced_routing_session("chrys-pet") is True
    # Exact match after stripping, like the session-sticky lookup.
    assert AutoRouteAlgorithm.is_forced_routing_session(" DSH-Pet ") is False
    assert AutoRouteAlgorithm.is_forced_routing_session("dsh-pet-2") is False
    assert AutoRouteAlgorithm.is_forced_routing_session("chrys-pet:abc") is False
    assert AutoRouteAlgorithm.is_forced_routing_session(" ") is False
    assert AutoRouteAlgorithm.is_forced_routing_session(None) is False
    assert AutoRouteAlgorithm.is_forced_routing_session(123) is False

    monkeypatch.setitem(APP_CONFIG.setdefault("router", {}), "forced_routing_sessions", ["other-agent"])
    assert AutoRouteAlgorithm.is_forced_routing_session("dsh-pet") is False
    assert AutoRouteAlgorithm.is_forced_routing_session("other-agent") is True

    monkeypatch.setitem(APP_CONFIG["router"], "forced_routing_sessions", [])
    assert AutoRouteAlgorithm.is_forced_routing_session("other-agent") is False


def test_forced_result_is_not_a_sticky_anchor():
    assert AutoRouteAlgorithm.is_sticky_anchor_result("user-model:forced_session_routing") is False
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:forced_session_routing") is False
    # Unchanged for genuine model choices.
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:complexity:5") is True
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:session-sticky") is True


@pytest.mark.django_db
def test_forced_record_does_not_pin_the_session(monkeypatch):
    # Should the session later leave the configured list, its old forced rows
    # must not pull later auto requests onto the routing model.
    _, routing_model = _setup()
    RequestRecord.objects.create(
        user_ip_id=0,
        ip_id=1,
        send_time=timezone.now(),
        model_id=routing_model.id,
        task_status="success",
        session="dsh-pet",
        router_result="auto:forced_session_routing",
    )
    monkeypatch.setitem(APP_CONFIG.setdefault("router", {}), "forced_routing_sessions", [])
    context = ServerSelectionContext(
        request_id=1,
        ip_id=1,
        model_id=0,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b"{}",
        origin_model_name="auto",
        session="dsh-pet",
    )

    assert AutoRouteAlgorithm()._resolve_sticky_model(context) is None


def _seed_external_route(internal="user-model"):
    ip = Ips.objects.create(ip="127.0.0.1")
    UserIP.objects.create(ip_id=ip.id, employee_no="E1001", user_name="External Tester")
    ExternalRoute.objects.create(
        name="vendor-a",
        base_url="http://ext.example/v1",
        employee_no="E1001",
        api_key="sk-emp-1",
        model_mapping_policy=1,
    )
    ExternalModelMapping.objects.create(
        policy_id=1, internal_model_name=internal, external_model_name="User-Model-Ext"
    )


def test_mapped_concrete_model_from_forced_session_stays_internal(monkeypatch):
    # Issue #357 wins over the issue #287 view-level divert: a mapped concrete
    # model requested by a forced session is rewritten to the internal routing
    # model instead of being forwarded to the employee's provider.
    _, routing_model = _setup()
    _seed_external_route()
    calls = _patch_upstream(monkeypatch, [])

    response = _post("chrys-pet", model="user-model")

    assert response.status_code == 200
    assert [url for url, _ in calls] == [ROUTER_URL]
    assert calls[0][1]["model"] == "router-model"
    record = _record()
    assert record.model_id == routing_model.id
    assert record.router_result == "user-model:forced_session_routing"


def test_mapped_concrete_model_from_other_session_still_goes_external(monkeypatch):
    _setup()
    _seed_external_route()
    calls = _patch_upstream(monkeypatch, [])

    response = _post("sess-1", model="user-model")

    assert response.status_code == 200
    assert [url for url, _ in calls] == [EXT_URL]
    assert calls[0][1]["model"] == "User-Model-Ext"
    assert _record().router_result == "external:vendor-a:user-model"
