"""Employee-scoped concurrency (issue #301).

Concurrency is no longer inflated per IP by ``ips.concurrent_multiplier``
(deprecated). It is scoped by the resolved identity:

- a VIP ``user_ips`` row means no limit at all, on any port;
- every other employee shares one bucket across all of his ``user_ips`` rows
  (apikey- or IP-backed) and his bound IPs — a request with both his apikey
  and from his IP counts once;
- anonymous traffic keeps the plain per-IP bucket.
"""
import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from django.test import Client
from django.utils import timezone

from router.models import Ips, Model, RequestRecord, Server, UserIP
from router.services.admission import AdmissionService
from router.services.identity import RequestIdentity


@pytest.fixture(autouse=True)
def _fixed_business_hours(monkeypatch):
    """Pin admission's clock to a weekday noon so the off-hours x4 boost never applies."""
    monkeypatch.setattr(
        "router.services.admission.timezone.localtime",
        lambda: datetime(2026, 6, 1, 12, 0, 0),  # Monday 12:00 -> no boost
    )


def _ip(addr):
    return Ips.objects.create(ip=addr, vip=False)


def _seed(model_id, ip_id=None, user_ip_id=0, vip=False, router_result=None):
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


def _identity(ip, user_ip_id=0, employee_no="", is_vip=False, is_apikey=False):
    return RequestIdentity(
        ip=ip,
        user_ip_id=user_ip_id,
        employee_no=employee_no,
        department_id=None,
        is_vip=is_vip,
        is_apikey=is_apikey,
    )


@pytest.mark.django_db
def test_vip_identity_has_no_concurrency_limit():
    ip = _ip("10.1.1.1")
    model = Model.objects.create(model_name="model-a", concurrent_limit=1)
    key_row = UserIP.objects.create(apikey="sk-vip", employee_no="E1", ip_id=0, vip=True, is_valid=True)

    # Base limit saturated by unrelated anonymous traffic on the same IP.
    for _ in range(3):
        _seed(model.id, ip_id=ip.id)

    result = AdmissionService().check_concurrency(
        ip, model, identity=_identity(ip, user_ip_id=key_row.id, employee_no="E1", is_vip=True, is_apikey=True)
    )

    assert result.allowed is True


@pytest.mark.django_db
def test_employee_apikey_and_bound_ip_share_one_bucket():
    nat = _ip("10.2.2.2")  # unbound NAT IP the apikey request arrives from
    bound = _ip("10.2.2.3")  # the employee's bound office IP
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    key_row = UserIP.objects.create(apikey="sk-e1", employee_no="E1", ip_id=0, is_valid=True)
    UserIP.objects.create(ip_id=bound.id, employee_no="E1", is_valid=True)

    # 2 in-flight via his apikey (from the NAT IP) + 1 in-flight from his
    # bound IP without a key: all three count against the same employee.
    _seed(model.id, ip_id=nat.id, user_ip_id=key_row.id)
    _seed(model.id, ip_id=nat.id, user_ip_id=key_row.id)
    _seed(model.id, ip_id=bound.id)

    result = AdmissionService().check_concurrency(
        nat, model, identity=_identity(nat, user_ip_id=key_row.id, employee_no="E1", is_apikey=True)
    )

    assert result.allowed is False
    assert result.current == 3
    assert result.limit == 3


@pytest.mark.django_db
def test_request_with_apikey_from_bound_ip_counts_once():
    nat = _ip("10.2.2.2")
    bound = _ip("10.2.2.3")
    model = Model.objects.create(model_name="model-a", concurrent_limit=2)
    key_row = UserIP.objects.create(apikey="sk-e1", employee_no="E1", ip_id=0, is_valid=True)
    UserIP.objects.create(ip_id=bound.id, employee_no="E1", is_valid=True)

    # One row matches BOTH scope sides (his apikey and his bound IP); the
    # other matches only the apikey side. Total must be 2, not 3.
    _seed(model.id, ip_id=bound.id, user_ip_id=key_row.id)
    _seed(model.id, ip_id=nat.id, user_ip_id=key_row.id)

    result = AdmissionService().check_concurrency(
        nat, model, identity=_identity(nat, user_ip_id=key_row.id, employee_no="E1", is_apikey=True)
    )

    assert result.allowed is False
    assert result.current == 2


@pytest.mark.django_db
def test_two_employees_on_shared_nat_ip_have_independent_limits():
    nat = _ip("10.2.2.9")
    model = Model.objects.create(model_name="model-a", concurrent_limit=1)
    key_e1 = UserIP.objects.create(apikey="sk-e1", employee_no="E1", ip_id=0, is_valid=True)
    key_e2 = UserIP.objects.create(apikey="sk-e2", employee_no="E2", ip_id=0, is_valid=True)

    _seed(model.id, ip_id=nat.id, user_ip_id=key_e1.id)  # E1's slot is used

    result_e1 = AdmissionService().check_concurrency(
        nat, model, identity=_identity(nat, user_ip_id=key_e1.id, employee_no="E1", is_apikey=True)
    )
    result_e2 = AdmissionService().check_concurrency(
        nat, model, identity=_identity(nat, user_ip_id=key_e2.id, employee_no="E2", is_apikey=True)
    )

    assert result_e1.allowed is False  # his own in-flight request blocks him
    assert result_e2.allowed is True  # E2 keeps his own independent quota


@pytest.mark.django_db
def test_apikey_row_without_employee_no_borrows_ip_row_scope():
    foreign = _ip("10.2.2.4")  # where the key request arrives from
    bound = _ip("10.2.2.5")  # bound to E1 via the IP-backed row
    model = Model.objects.create(model_name="model-a", concurrent_limit=2)
    # Key row without employee_no: identity resolution borrows E1 from the
    # IP-backed row, so both rows and the bound IP must form one bucket.
    key_row = UserIP.objects.create(apikey="sk-e1", employee_no="", ip_id=0, is_valid=True)
    UserIP.objects.create(ip_id=bound.id, employee_no="E1", is_valid=True)

    _seed(model.id, ip_id=foreign.id, user_ip_id=key_row.id)  # via his apikey
    _seed(model.id, ip_id=bound.id)  # anonymously from his bound IP

    result = AdmissionService().check_concurrency(
        foreign, model, identity=_identity(foreign, user_ip_id=key_row.id, employee_no="E1", is_apikey=True)
    )

    assert result.allowed is False
    assert result.current == 2


@pytest.mark.django_db
def test_anonymous_ip_keeps_per_ip_bucket():
    ip = _ip("10.3.3.3")
    model = Model.objects.create(model_name="model-a", concurrent_limit=2)

    _seed(model.id, ip_id=ip.id)
    _seed(model.id, ip_id=ip.id)

    result = AdmissionService().check_concurrency(ip, model)  # identity=None

    assert result.allowed is False
    assert result.current == 2
    assert result.limit == 2


@pytest.mark.django_db
def test_vip_apikey_identity_bypasses_concurrency_on_normal_port(monkeypatch):
    # End-to-end: a VIP apikey sails through the normal port even while the
    # base limit is saturated by other traffic from the same IP.
    model = Model.objects.create(model_name="model-b", concurrent_limit=1, max_tokens=65536)
    Server.objects.create(model_id=model.id, base_url="http://b.example", is_online=True)
    ip = _ip("10.4.4.4")
    UserIP.objects.create(apikey="sk-vip-2", employee_no="E9", ip_id=0, vip=True, is_valid=True)

    _seed(model.id, ip_id=ip.id)  # saturates the per-IP base limit

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
        HTTP_AUTHORIZATION="Bearer sk-vip-2",
        REMOTE_ADDR="10.4.4.4",
    )

    assert response.status_code == 200
