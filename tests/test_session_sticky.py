import json

import pytest
from django.test import Client
from django.utils import timezone

from router.models import Model, RequestRecord, Server
from router.repositories.requests import LLM_CHOOSING_IP_ID
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext
from router.utils.session import extract_session_id


def _context(session=None, auto_model_selection=True):
    return ServerSelectionContext(
        request_id=1,
        ip_id=1,
        model_id=0,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b'{"model":"auto","messages":[]}',
        origin_model_name="auto",
        auto_model_selection=auto_model_selection,
        session=session,
    )


def _anchor_record(session, model, router_result):
    return RequestRecord.objects.create(
        user_ip_id=0,
        ip_id=1,
        send_time=timezone.now(),
        model_id=model.id,
        task_status="success",
        session=session,
        router_result=router_result,
    )


def test_extract_session_id_fixed_priority_and_case_insensitivity():
    assert extract_session_id({"Chrys-Session-Id": "a"}) == "a"
    assert extract_session_id({"X-Session-Affinity": "b"}) == "b"
    assert extract_session_id({"x-session-id": "c"}) == "c"
    assert extract_session_id({"X-Session-Id": "c"}) == "c"
    assert extract_session_id({"X-Snap-Traceid": "d"}) == "d"
    # Fixed priority: Chrys wins even if later headers are also present.
    assert (
        extract_session_id(
            {
                "x-session-id": "codeagent",
                "Chrys-Session-Id": "chrys",
            }
        )
        == "chrys"
    )


def test_extract_session_id_missing_or_blank_returns_none():
    assert extract_session_id(None) is None
    assert extract_session_id({}) is None
    assert extract_session_id({"User-Agent": "opencode"}) is None
    assert extract_session_id({"Chrys-Session-Id": "  "}) is None


def test_is_sticky_anchor_result_uses_substring_not_prefix():
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:cache_hit") is True
    assert AutoRouteAlgorithm.is_sticky_anchor_result("glm-5:complexity:5") is True
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:routing_failed:502:boom") is True
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:small_request_routing") is False
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:multimodal_bypass") is False
    assert AutoRouteAlgorithm.is_sticky_anchor_result(None) is False


@pytest.mark.django_db
def test_resolve_sticky_model_reuses_recent_anchor():
    model = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=model.id, base_url="http://target.example", is_online=True)
    _anchor_record("sess-1", model, "auto:complexity:5")

    chosen = AutoRouteAlgorithm()._resolve_sticky_model(_context("sess-1"))
    assert chosen == model


@pytest.mark.django_db
def test_resolve_sticky_model_skips_non_anchor_records():
    anchor_model = Model.objects.create(model_name="anchor", complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=anchor_model.id, base_url="http://anchor.example", is_online=True)
    small_model = Model.objects.create(model_name="small-routing", is_routing_model=True)
    Server.objects.create(model_id=small_model.id, base_url="http://small.example", is_online=True)

    # Newest record is a small-request shortcut and must not pin the session.
    _anchor_record("sess-1", small_model, "auto:small_request_routing")
    _anchor_record("sess-1", anchor_model, "auto:complexity:5")

    chosen = AutoRouteAlgorithm()._resolve_sticky_model(_context("sess-1"))
    assert chosen == anchor_model


@pytest.mark.django_db
def test_resolve_sticky_model_ignores_anchor_without_servers():
    model = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    _anchor_record("sess-1", model, "auto:complexity:5")

    assert AutoRouteAlgorithm()._resolve_sticky_model(_context("sess-1")) is None


@pytest.mark.django_db
def test_small_request_routing_precedes_sticky():
    anchor_model = Model.objects.create(model_name="anchor", complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=anchor_model.id, base_url="http://anchor.example", is_online=True)
    _anchor_record("sess-1", anchor_model, "auto:complexity:5")

    routing_model = Model.objects.create(model_name="routing", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://routing.example", is_online=True)

    parsed = type(
        "Parsed",
        (),
        {
            "estimated_full_body_tokens": 100,
            "body": b'{"model":"auto","messages":[{"role":"user","content":"hi"}]}',
            "model_name": "auto",
        },
    )()
    record = type("Record", (), {"save": lambda self, **kwargs: None})()
    decision = AutoRouteAlgorithm().resolve(parsed, record, _context("sess-1"), None, False)

    assert decision.model == routing_model
    assert decision.router_result == "auto:small_request_routing"


@pytest.mark.django_db
def test_same_session_stays_sticky_across_rounds(monkeypatch):
    target_model = Model.objects.create(model_name="target-model", auto=True, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    router_calls = []

    def fake_request(self_inner, method, url, **kwargs):
        data = json.loads(kwargs["data"].decode("utf-8"))
        if url == "http://router.example/chat/completions":
            router_calls.append(url)
            assert data["model"] == "router-model"
            upstream = type("Upstream", (), {})()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices":[{"message":{"content":"{\\"complexity\\":5}"}}]}'
            upstream.headers = {}
            return upstream
        assert url == "http://target.example/chat/completions"
        assert data["model"] == "target-model"
        upstream = type("Upstream", (), {})()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.content = b"{}"
        upstream.headers = {}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    client = Client()
    body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hello"}]})
    for _ in range(2):
        response = client.post(
            "/v1/chat/completions",
            data=body,
            content_type="application/json",
            HTTP_X_SESSION_ID="sess-1",
        )
        assert response.status_code == 200

    external_records = list(RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID).order_by("id"))
    assert len(external_records) == 2
    assert external_records[0].session == "sess-1"
    assert external_records[0].model_id == target_model.id
    assert external_records[0].router_result == "auto:complexity:5"
    assert external_records[1].session == "sess-1"
    assert external_records[1].model_id == target_model.id
    assert external_records[1].router_result == "auto:session-sticky"
    # The routing LLM is called only on the first round.
    assert len(router_calls) == 1
