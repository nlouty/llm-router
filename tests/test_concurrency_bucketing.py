import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from django.test import Client
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Ips, Model, RequestRecord, Server
from router.services.admission import AdmissionService


@pytest.fixture(autouse=True)
def _fixed_business_hours(monkeypatch):
    """Pin admission's clock to a weekday noon so the off-hours x4 boost never applies.

    Keeps per-test limits equal to the configured concurrent_limit.
    """
    monkeypatch.setattr(
        "router.services.admission.timezone.localtime",
        lambda: datetime(2026, 6, 1, 12, 0, 0),  # Monday 12:00 -> no boost
    )


def _make_ip(concurrent_multiplier=1.0):
    return Ips.objects.create(ip="10.20.30.40", concurrent_multiplier=concurrent_multiplier, vip=False)


def _seed_processing(ip_id, model_id, router_result=None, user_ip_id=1, vip=False):
    # send_time must be recent so the throttled cleanup_stale run inside
    # check_concurrency does not sweep these seed rows away before counting.
    return RequestRecord.objects.create(
        user_ip_id=user_ip_id,
        vip=vip,
        ip_id=ip_id,
        send_time=timezone.now(),
        model_id=model_id,
        task_status="processing",
        is_stream=False,
        user_agent="seed",
        router_result=router_result,
    )


# --- predicate unit tests (precise bucketing logic) ---


def test_entrance_is_auto_predicate():
    cls = AdmissionService
    assert cls._entrance_is_auto({"model_id": 0, "router_result": None}) is True
    assert cls._entrance_is_auto({"model_id": 99, "router_result": "auto:complexity:5"}) is True
    assert cls._entrance_is_auto({"model_id": 99, "router_result": "AUTO:complexity:5"}) is True
    assert cls._entrance_is_auto({"model_id": 5, "router_result": None}) is False
    assert cls._entrance_is_auto({"model_id": 99, "router_result": "source-model:complexity:1"}) is False


def test_entrance_matches_predicate():
    cls = AdmissionService
    name_cf = "model-b"
    assert cls._entrance_matches({"model_id": 7, "router_result": None}, name_cf, 7) is True
    assert cls._entrance_matches({"model_id": 99, "router_result": "model-b:complexity:5"}, name_cf, 7) is True
    assert cls._entrance_matches({"model_id": 7, "router_result": "auto:complexity:5"}, name_cf, 7) is False
    assert cls._entrance_matches({"model_id": 8, "router_result": None}, name_cf, 7) is False
    assert cls._entrance_matches({"model_id": 99, "router_result": "other:complexity:5"}, name_cf, 7) is False


# --- auto entrance: counts both unresolved (model_id=0) and resolved (prefix auto) ---


@pytest.mark.django_db
def test_auto_entrance_counts_unresolved_and_resolved_auto_requests(monkeypatch):
    ip = _make_ip()
    target = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    monkeypatch.setitem(APP_CONFIG.setdefault("router", {}), "auto_concurrent_limit", 2)

    _seed_processing(ip.id, 0, router_result=None)
    _seed_processing(ip.id, target.id, router_result="auto:complexity:5")

    result = AdmissionService().check_concurrency(ip, None, is_auto=True)

    assert result.allowed is False
    assert result.current == 2
    assert result.limit == 2


@pytest.mark.django_db
def test_auto_entrance_case_insensitive_prefix_counts_as_auto(monkeypatch):
    ip = _make_ip()
    target = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    monkeypatch.setitem(APP_CONFIG.setdefault("router", {}), "auto_concurrent_limit", 1)

    _seed_processing(ip.id, target.id, router_result="AUTO:complexity:5")

    result = AdmissionService().check_concurrency(ip, None, is_auto=True)

    assert result.allowed is False
    assert result.current == 1


@pytest.mark.django_db
def test_auto_entrance_excludes_direct_requests_for_other_models(monkeypatch):
    ip = _make_ip()
    target = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    monkeypatch.setitem(APP_CONFIG.setdefault("router", {}), "auto_concurrent_limit", 1)

    # A direct request to a different model must not be counted as auto.
    _seed_processing(ip.id, 555, router_result=None)

    result = AdmissionService().check_concurrency(ip, None, is_auto=True)

    assert result.allowed is True


# --- direct entrance: only unresolved direct requests for that model count ---


@pytest.mark.django_db
def test_direct_model_counts_only_own_unresolved_requests():
    ip = _make_ip()
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=1)

    # The single own request reaches the limit of 1 -> blocked, current==1.
    _seed_processing(ip.id, model_b.id, router_result=None)

    result = AdmissionService().check_concurrency(ip, model_b, is_auto=False)

    assert result.allowed is False
    assert result.current == 1
    assert result.limit == 1


@pytest.mark.django_db
def test_direct_model_excludes_resolved_auto_and_other_entrance_requests():
    ip = _make_ip()
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=1)

    # auto->b and source->b records share model_b.id but carry a prefix;
    # they must NOT count toward model-b's direct bucket.
    _seed_processing(ip.id, model_b.id, router_result="auto:complexity:5")
    _seed_processing(ip.id, model_b.id, router_result="source-model:complexity:5")

    result = AdmissionService().check_concurrency(ip, model_b, is_auto=False)

    assert result.allowed is True


# --- concrete auto-flagged model by name: counts under the requested name, not "auto" ---


@pytest.mark.django_db
def test_auto_flagged_model_by_name_counts_under_requested_name():
    ip = _make_ip()
    source_model = Model.objects.create(model_name="source-model", auto=True, concurrent_limit=1)

    # A source-model request resolved to another model still counts under source-model.
    _seed_processing(ip.id, 999, router_result="source-model:complexity:1")

    result = AdmissionService().check_concurrency(ip, source_model, is_auto=False)

    assert result.allowed is False
    assert result.current == 1


@pytest.mark.django_db
def test_auto_flagged_model_by_name_excludes_unrelated_auto_requests():
    ip = _make_ip()
    source_model = Model.objects.create(model_name="source-model", auto=True, concurrent_limit=1)

    # An unrelated literal-auto request must not count under source-model.
    _seed_processing(ip.id, 0, router_result=None)

    result = AdmissionService().check_concurrency(ip, source_model, is_auto=False)

    assert result.allowed is True


# --- VIP rows are excluded from the count ---


@pytest.mark.django_db
def test_vip_rows_excluded_from_concurrency_count():
    ip = _make_ip()
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=1)

    _seed_processing(ip.id, model_b.id, router_result=None, vip=True)

    result = AdmissionService().check_concurrency(ip, model_b, is_auto=False)

    assert result.allowed is True


# --- end-to-end: a resolved auto->b request does not block direct model-b users ---


@pytest.mark.django_db
def test_resolved_auto_request_does_not_inflate_direct_model_b_concurrency(monkeypatch):
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=1, max_tokens=65536, complexity_min=1, complexity_max=10)
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=model_b.id, base_url="http://b.example", is_online=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    ip = _make_ip()
    monkeypatch.setattr("router.route_algorithm.auto.AutoRouteAlgorithm.SMALL_REQUEST_ROUTING_TOKEN_LIMIT", 0)

    # Seed one auto->b request already in flight and resolved onto model_b.
    _seed_processing(ip.id, model_b.id, router_result="auto:complexity:5")

    def fake_request(self_inner, method, url, **kwargs):
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
        data=json.dumps({"model": "model-b", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        REMOTE_ADDR="10.20.30.40",
    )

    assert response.status_code == 200
