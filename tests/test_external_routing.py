"""External-provider routing (issue #287).

Employees with an active ``external_routes`` row are forwarded to their
provider: the router records the request, rewrites the body's ``model`` via
the policy mapping, swaps the client credential for the per-employee provider
API key, and passes the response through. A provider circuit breaker (grouped
by ``base_url``) falls routing back to the internal pipeline when open.
"""

import json

import pytest
import requests
from django.test import Client
from django.utils import timezone

from router.models import (
    ExternalModelMapping,
    ExternalRoute,
    Ips,
    Model,
    RequestRecord,
    Server,
    UserIP,
)
from router.repositories.external import ExternalRouteRepository
from router.services.admission import AdmissionService
from router.services.external_route import ExternalRouteService

CLIENT_IP = "127.0.0.1"
EMPLOYEE = "E1001"
PROVIDER_URL = "http://ext.example/v1"
INTERNAL_URL = "http://internal.example/v1"
POLICY = 1


class FakeUpstream:
    def __init__(self, status_code=200, content=b"", headers=None, reason="OK", chunks=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}
        self.reason = reason
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks or []:
            yield chunk

    def close(self):
        self.closed = True


def ok_body(model="GLM-5.2"):
    return json.dumps({
        "id": "chatcmpl-1",
        "model": model,
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }).encode("utf-8")


def patch_upstream(monkeypatch, calls, response):
    """Patch the non-stream upstream client used by both the external and the
    internal path; every send is recorded in *calls*."""
    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return response

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )
    return calls


def seed_identity(employee_no=EMPLOYEE, vip=False):
    ip = Ips.objects.create(ip=CLIENT_IP, vip=vip)
    UserIP.objects.create(
        ip_id=ip.id,
        employee_no=employee_no,
        user_name="External Tester",
        vip=vip,
    )
    return ip


def seed_route(employee_no=EMPLOYEE, name="vendor-a", api_key="sk-emp-1", base_url=PROVIDER_URL):
    return ExternalRoute.objects.create(
        name=name,
        base_url=base_url,
        employee_no=employee_no,
        api_key=api_key,
        model_mapping_policy=POLICY,
    )


def seed_mapping(internal="glm-5.2", external="GLM-5.2", enabled=True):
    return ExternalModelMapping.objects.create(
        policy_id=POLICY,
        internal_model_name=internal,
        external_model_name=external,
        is_enabled=enabled,
    )


def seed_internal_model(name="glm-5.2", online=True, base_url=INTERNAL_URL, **extra):
    # max_tokens above the parser-injected default so the internal path's
    # max-token admission never interferes with routing assertions.
    extra.setdefault("max_tokens", 40000)
    model = Model.objects.create(model_name=name, **extra)
    Server.objects.create(model_id=model.id, base_url=base_url, is_online=online)
    return model


def post_chat(payload, **extra_meta):
    return Client().post(
        "/v1/chat/completions",
        data=json.dumps(payload),
        content_type="application/json",
        **extra_meta,
    )


@pytest.mark.django_db
class TestExternalRouteServiceResolve:
    def test_matrix(self):
        seed_route()
        seed_mapping()
        service = ExternalRouteService()

        # mapped employee + mapped name -> resolved
        resolved = service.resolve(EMPLOYEE, "glm-5.2")
        assert resolved is not None
        route, mapping = resolved
        assert route.base_url == PROVIDER_URL
        assert mapping.external_model_name == "GLM-5.2"

        # unmapped employee
        assert service.resolve("E9999", "glm-5.2") is None
        # mapped employee, unmapped name (internal-only model)
        assert service.resolve(EMPLOYEE, "Qwen3.6-35B") is None
        # disabled mapping
        seed_mapping(internal="disabled-model", enabled=False)
        assert service.resolve(EMPLOYEE, "disabled-model") is None
        # no model name (e.g. auto entrance never reaches resolve directly)
        assert service.resolve(EMPLOYEE, None) is None

    def test_inactive_route_and_missing_key_return_none(self):
        route = seed_route()
        seed_mapping()
        service = ExternalRouteService()

        route.is_active = False
        route.save()
        assert service.resolve(EMPLOYEE, "glm-5.2") is None

        route.is_active = True
        route.api_key = ""
        route.save()
        assert service.resolve(EMPLOYEE, "glm-5.2") is None

    def test_open_circuit_blocks_until_cooldown_expires(self):
        route = seed_route()
        seed_mapping()
        now = timezone.now()
        route.circuit_state = "open"
        route.last_state_change_at = now
        route.cooldown_seconds = 30
        route.save()

        assert ExternalRouteRepository.is_routable(ExternalRoute.objects.get(id=route.id)) is False
        assert ExternalRouteService().resolve(EMPLOYEE, "glm-5.2") is None

        # Cooldown expired -> half-open probe is allowed and persists.
        route.last_state_change_at = now - timezone.timedelta(seconds=31)
        route.save()
        assert ExternalRouteRepository.is_routable(ExternalRoute.objects.get(id=route.id)) is True
        assert ExternalRoute.objects.get(id=route.id).circuit_state == "half_open"


@pytest.mark.django_db
class TestHookOneConcreteNames:
    def test_mapped_concrete_model_forwards_externally(self, monkeypatch):
        ip = seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body()))

        response = Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer client-own-key",
            HTTP_X_API_KEY="client-x-key",
        )

        assert response.status_code == 200
        assert len(calls) == 1
        call = calls[0]
        # URL: provider base_url + the incoming path
        assert call["url"] == f"{PROVIDER_URL}/chat/completions"
        # Body rewritten to the provider's model name
        assert json.loads(call["kwargs"]["data"])["model"] == "GLM-5.2"
        # Per-employee provider key injected, client credentials stripped
        headers = {k.lower(): v for k, v in call["kwargs"]["headers"].items()}
        assert headers["authorization"] == "Bearer sk-emp-1"
        assert "x-api-key" not in headers and "api-key" not in headers
        # Internal server untouched
        server = Server.objects.get(base_url=INTERNAL_URL)
        assert server.workload == 0

        record = RequestRecord.objects.last()
        assert record.task_status == "success"
        assert record.router_result == "external:vendor-a:glm-5.2"
        assert record.target_pod_ip == PROVIDER_URL
        assert record.attempt_count == 1
        assert record.model_id == Model.objects.get(model_name="glm-5.2").id
        assert record.user_ip_id == UserIP.objects.get(ip_id=ip.id).id
        assert record.input_token_cnt == 11
        assert record.output_token_cnt == 7
        assert record.model_choosing_latency is not None

    def test_provider_only_model_forwarded_externally(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping(internal="deepseek-v4-pro", external="deepseek-v4-pro")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body(model="deepseek-v4-pro")))

        response = post_chat({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert calls[0]["url"] == f"{PROVIDER_URL}/chat/completions"
        record = RequestRecord.objects.last()
        assert record.router_result == "external:vendor-a:deepseek-v4-pro"
        # No internal models row exists for a provider-only name.
        assert record.model_id == 0
        assert not Model.objects.filter(model_name="deepseek-v4-pro").exists()

    def test_unmapped_employee_provider_only_name_rejected(self, monkeypatch):
        ip = seed_identity(employee_no="E2002")
        seed_route(employee_no=EMPLOYEE)
        seed_mapping(internal="deepseek-v4-pro", external="deepseek-v4-pro")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream())

        response = post_chat({"model": "deepseek-v4-pro"})
        assert response.status_code == 400
        assert "not supported" in response.json()["error"]["message"]
        assert calls == []
        assert RequestRecord.objects.last().task_status == "failed"

    def test_unmapped_internal_model_serves_internally(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("Qwen3.6-35B")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body(model="Qwen3.6-35B")))

        response = Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "Qwen3.6-35B", "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer client-own-key",
        )
        assert response.status_code == 200
        assert calls[0]["url"] == f"{INTERNAL_URL}/chat/completions"
        # Internal path forwards the client's own credential untouched.
        headers = {k.lower(): v for k, v in calls[0]["kwargs"]["headers"].items()}
        assert headers["authorization"] == "Bearer client-own-key"
        # Body keeps the internal name.
        assert json.loads(calls[0]["kwargs"]["data"])["model"] == "Qwen3.6-35B"
        record = RequestRecord.objects.last()
        assert record.router_result is None
        assert record.target_pod_ip == INTERNAL_URL

    def test_concrete_auto_model_with_mapping_wins_over_auto(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        # auto=True would normally enter the auto pipeline; the mapping wins.
        seed_internal_model("glm-5.2", auto=True, complexity_min=1, complexity_max=10)

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body()))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert calls[0]["url"] == f"{PROVIDER_URL}/chat/completions"
        assert RequestRecord.objects.last().router_result == "external:vendor-a:glm-5.2"


@pytest.mark.django_db
class TestHookTwoAutoResolution:
    def _seed_session_sticky(self, session="session-1"):
        ip = seed_identity()
        seed_route()
        seed_mapping()
        glm = seed_internal_model("glm-5.2", complexity_min=1, complexity_max=10)
        # A recent committed auto choice anchors the session to glm-5.2, so
        # the auto pipeline resolves deterministically without an LLM call.
        RequestRecord.objects.create(
            user_ip_id=0,
            ip_id=ip.id,
            send_time=timezone.now(),
            model_id=glm.id,
            task_status="success",
            is_stream=False,
            user_agent="tester",
            session=session,
            router_result="auto:complexity:7",
        )
        return glm

    def test_auto_entrance_resolves_then_diverts(self, monkeypatch):
        glm = self._seed_session_sticky()

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body()))

        response = Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
            HTTP_X_SESSION_ID="session-1",
        )

        assert response.status_code == 200
        assert calls[0]["url"] == f"{PROVIDER_URL}/chat/completions"
        assert json.loads(calls[0]["kwargs"]["data"])["model"] == "GLM-5.2"
        record = RequestRecord.objects.last()
        assert record.router_result == "external:vendor-a:glm-5.2"
        assert record.model_id == glm.id
        assert record.target_pod_ip == PROVIDER_URL

    def test_auto_entrance_unmapped_target_serves_internally(self, monkeypatch):
        ip = seed_identity()
        seed_route()
        seed_mapping()
        qwen = seed_internal_model("Qwen3.6-35B", complexity_min=1, complexity_max=10)

        RequestRecord.objects.create(
            user_ip_id=0,
            ip_id=ip.id,
            send_time=timezone.now(),
            model_id=qwen.id,
            task_status="success",
            is_stream=False,
            user_agent="tester",
            session="session-2",
            router_result="auto:complexity:3",
        )

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body(model="Qwen3.6-35B")))

        response = Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
            HTTP_X_SESSION_ID="session-2",
        )
        assert response.status_code == 200
        assert calls[0]["url"] == f"{INTERNAL_URL}/chat/completions"
        record = RequestRecord.objects.last()
        assert record.router_result == "auto:session-sticky"
        assert record.model_id == qwen.id


@pytest.mark.django_db
class TestVipGating:
    def test_vip_port_never_goes_external(self, monkeypatch):
        seed_identity(vip=True)
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body(model="glm-5.2")))

        response = post_chat(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
            SERVER_PORT="8008",
        )
        assert response.status_code == 200
        assert calls[0]["url"] == f"{INTERNAL_URL}/chat/completions"
        record = RequestRecord.objects.last()
        assert record.router_result is None

    def test_vip_identity_on_normal_port_goes_external(self, monkeypatch):
        seed_identity(vip=True)
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body()))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert calls[0]["url"] == f"{PROVIDER_URL}/chat/completions"
        assert RequestRecord.objects.last().router_result == "external:vendor-a:glm-5.2"


@pytest.mark.django_db
class TestCircuitBreaker:
    def test_open_circuit_falls_back_to_internal(self, monkeypatch):
        seed_identity()
        route = seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")
        route.circuit_state = "open"
        route.last_state_change_at = timezone.now()
        route.save()

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body(model="glm-5.2")))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert calls[0]["url"] == f"{INTERNAL_URL}/chat/completions"

    def test_open_circuit_provider_only_name_returns_400(self, monkeypatch):
        seed_identity()
        route = seed_route()
        seed_mapping(internal="deepseek-v4-pro", external="deepseek-v4-pro")
        route.circuit_state = "open"
        route.last_state_change_at = timezone.now()
        route.save()

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream())

        response = post_chat({"model": "deepseek-v4-pro"})
        assert response.status_code == 400
        assert calls == []

    def test_500_trips_breaker_for_whole_provider_group(self, monkeypatch):
        seed_identity()
        route = seed_route()
        # Two employees share the provider; the group must trip together.
        seed_route(employee_no="E2002", api_key="sk-emp-2")
        seed_mapping()
        seed_internal_model("glm-5.2")
        # One failure away from the threshold (3).
        ExternalRoute.objects.filter(base_url=PROVIDER_URL).update(consecutive_failures=2)

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(status_code=500, content=b'{"error": {"message": "upstream exploded"}}', reason="Internal Server Error"))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 500
        assert response.json()["error"]["message"] == "upstream exploded"

        routes = {r.employee_no: r for r in ExternalRoute.objects.all()}
        assert all(r.circuit_state == "open" for r in routes.values())
        assert all(r.consecutive_failures == 3 for r in routes.values())
        record = RequestRecord.objects.last()
        assert record.task_status == "failed"
        assert record.fail_reason == "upstream exploded"

    def test_401_passthrough_does_not_trip_breaker(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(status_code=401, content=b'{"error": {"message": "bad key"}}', reason="Unauthorized"))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 401
        route = ExternalRoute.objects.get(employee_no=EMPLOYEE)
        assert route.circuit_state == "closed"
        assert route.consecutive_failures == 0

    def test_success_closes_half_open_circuit(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")
        ExternalRoute.objects.filter(base_url=PROVIDER_URL).update(
            circuit_state="half_open", consecutive_failures=3, cooldown_seconds=30
        )

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=ok_body()))

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        route = ExternalRoute.objects.get(employee_no=EMPLOYEE)
        assert route.circuit_state == "closed"
        assert route.consecutive_failures == 0

    def test_connection_error_returns_502_and_counts_failure(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []

        def fake_request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, "kwargs": kwargs})
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr(
            "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
            fake_request,
        )

        response = post_chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 502
        route = ExternalRoute.objects.get(employee_no=EMPLOYEE)
        assert route.consecutive_failures == 1
        record = RequestRecord.objects.last()
        assert record.task_status == "failed"
        assert "external provider unreachable" in record.fail_reason


@pytest.mark.django_db
class TestStreaming:
    def test_stream_forwarded_without_models_row_pollution(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        sse_chunks = [
            b'data: {"id":"c1","choices":[{"delta":{"content":"he"}}]}\n\n',
            b'data: {"id":"c1","choices":[{"delta":{"content":"llo"}}]}\n\n',
            b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ]
        calls = []

        def fake_stream_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "kwargs": kwargs})
            return FakeUpstream(chunks=sse_chunks)

        monkeypatch.setattr(
            "router.services.external_proxy.requests.request",
            fake_stream_request,
        )

        response = Client().post(
            "/v1/chat/completions",
            data=json.dumps({"model": "glm-5.2", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response["Content-Type"] == "text/event-stream"
        body = b"".join(response.streaming_content)
        assert b"he" in body and b"llo" in body

        assert calls[0]["url"] == f"{PROVIDER_URL}/chat/completions"
        assert json.loads(calls[0]["kwargs"]["data"])["model"] == "GLM-5.2"
        headers = {k.lower(): v for k, v in calls[0]["kwargs"]["headers"].items()}
        assert headers["authorization"] == "Bearer sk-emp-1"

        record = RequestRecord.objects.last()
        assert record.task_status == "success"
        assert record.is_stream is True
        assert record.router_result == "external:vendor-a:glm-5.2"
        assert record.input_token_cnt == 5
        assert record.output_token_cnt == 2
        assert record.ttft is not None
        # The provider's response model name must not create a models row.
        assert not Model.objects.filter(model_name="GLM-5.2").exists()


@pytest.mark.django_db
class TestAccounting:
    def test_external_inflight_rows_invisible_to_concurrency(self, monkeypatch):
        # Pin the off-peak boost off so the limit stays at 1 regardless of
        # when the suite runs.
        monkeypatch.setattr(
            AdmissionService, "off_peak_boost_active", staticmethod(lambda beijing_time=None: False)
        )
        ip = seed_identity(employee_no="E3003")
        glm = Model.objects.create(model_name="glm-5.2", concurrent_limit=1)
        common = dict(
            user_ip_id=0,
            vip=False,
            ip_id=ip.id,
            send_time=timezone.now(),
            model_id=glm.id,
            task_status="processing",
            is_stream=True,
            user_agent="tester",
            input_token_cnt=0,
            output_token_cnt=0,
            attempt_count=1,
            prefix_cache=0.0,
            final_prefix_cache=0,
            estimate_tokens=0,
        )
        # An in-flight external request must not consume the model bucket...
        RequestRecord.objects.create(router_result="external:vendor-a:glm-5.2", **common)
        assert AdmissionService().check_concurrency(ip, glm).allowed is True
        # ...while a normal internal in-flight row does (negative control).
        RequestRecord.objects.filter(router_result="external:vendor-a:glm-5.2").update(router_result=None)
        assert AdmissionService().check_concurrency(ip, glm).allowed is False

    def test_embeddings_stay_internal(self, monkeypatch):
        seed_identity()
        seed_route()
        seed_mapping()
        seed_internal_model("glm-5.2")

        calls = []
        patch_upstream(monkeypatch, calls, FakeUpstream(content=b'{"data": []}'))

        response = Client().post(
            "/v1/embeddings",
            data=json.dumps({"model": "glm-5.2", "input": "hi"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert calls[0]["url"] == f"{INTERNAL_URL}/embeddings"
        assert json.loads(calls[0]["kwargs"]["data"])["model"] == "glm-5.2"


@pytest.mark.django_db
class TestModelsCapability:
    def test_mapped_employee_catalog_merges_provider_models(self):
        seed_identity()
        seed_route()
        seed_mapping()  # glm-5.2 -> GLM-5.2 (shadows the internal entry)
        seed_mapping(internal="deepseek-v4-pro", external="deepseek-v4-pro")
        seed_internal_model("glm-5.2")
        seed_internal_model("Qwen3.6-35B", base_url="http://internal-qwen.example/v1")

        payload = Client().get("/v1/models").json()
        ids = {entry["id"]: entry for entry in payload["data"]}

        assert payload["employee_no"] == EMPLOYEE
        # Mapped name present exactly once, owned by the provider.
        assert len([e for e in payload["data"] if e["id"] == "glm-5.2"]) == 1
        assert ids["glm-5.2"]["owned_by"] == "external:vendor-a"
        assert ids["glm-5.2"]["concurrent_limit"] is None
        # Provider-only name listed.
        assert ids["deepseek-v4-pro"]["owned_by"] == "external:vendor-a"
        # Unmapped internal model and auto stay.
        assert ids["Qwen3.6-35B"]["owned_by"] == "gateway"
        assert "auto" in ids

    def test_unmapped_employee_catalog_unchanged(self):
        seed_identity(employee_no="E2002")
        seed_route(employee_no=EMPLOYEE)
        seed_mapping()
        seed_internal_model("glm-5.2")

        payload = Client().get("/v1/models").json()
        ids = {entry["id"] for entry in payload["data"]}
        assert ids == {"glm-5.2", "auto"}
        assert payload.get("employee_no") == "E2002"
