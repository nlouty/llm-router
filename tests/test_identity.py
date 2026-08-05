import json
from unittest.mock import MagicMock

import pytest
from django.test import Client

from router.models import Ips, Model, RequestRecord, Server, UserIP
from router.services.identity import IdentityService, RequestIdentity


def _request_with_auth(authorization: str | None):
    request = MagicMock()
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    request.headers = headers
    return request


@pytest.mark.django_db
def test_resolve_apikey_identity():
    ip = Ips.objects.create(ip="10.0.0.1")
    user_ip = UserIP.objects.create(
        ip_id=0, apikey="key-1", employee_no="E001", department_id=7, vip=True
    )

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    assert identity.is_apikey is True
    assert identity.user_ip_id == user_ip.id
    assert identity.employee_no == "E001"
    assert identity.department_id == 7
    assert identity.is_vip is True
    assert identity.has_employee is True


@pytest.mark.django_db
def test_resolve_bearer_prefix_is_case_insensitive():
    ip = Ips.objects.create(ip="10.0.0.1")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")

    identity = IdentityService.resolve(_request_with_auth("bearer key-1"), ip)

    assert identity.is_apikey is True
    assert identity.employee_no == "E001"


@pytest.mark.django_db
def test_resolve_unknown_bearer_falls_back_to_ip_identity():
    ip = Ips.objects.create(ip="10.0.0.1")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")

    identity = IdentityService.resolve(_request_with_auth("Bearer no-such-key"), ip)

    # Unknown apikey must not reject; falls back to the IP identity.
    assert identity.is_apikey is False
    assert identity.employee_no == "IP-EMP"


@pytest.mark.django_db
def test_resolve_ip_backed_identity_without_apikey():
    ip = Ips.objects.create(ip="10.0.0.2")
    user_ip = UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP", vip=False)

    identity = IdentityService.resolve(_request_with_auth(None), ip)

    assert identity.is_apikey is False
    assert identity.user_ip_id == user_ip.id
    assert identity.employee_no == "IP-EMP"
    assert identity.is_vip is False


@pytest.mark.django_db
def test_resolve_unresolved_identity_uses_zero_user_ip_id():
    ip = Ips.objects.create(ip="10.0.0.3")

    identity = IdentityService.resolve(_request_with_auth(None), ip)

    assert identity.user_ip_id == 0
    assert identity.employee_no == ""
    assert identity.has_employee is False
    assert identity.is_vip is False


@pytest.mark.django_db
def test_malformed_authorization_header_falls_back_to_ip():
    ip = Ips.objects.create(ip="10.0.0.4")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")

    # Not a Bearer scheme -> ignored, IP identity used.
    identity = IdentityService.resolve(_request_with_auth("Basic abc123"), ip)

    assert identity.is_apikey is False
    assert identity.employee_no == "IP-EMP"


@pytest.mark.django_db
def test_invalid_apikey_user_row_is_skipped():
    ip = Ips.objects.create(ip="10.0.0.5")
    # An invalidated apikey row must not match.
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001", is_valid=False)

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    assert identity.is_apikey is False
    assert identity.user_ip_id == 0


# --- end-to-end: requests.user_ip_id / vip populated through the proxy ---


def _upstream_ok(monkeypatch):
    def fake_request(self, method, url, **kwargs):
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


@pytest.mark.django_db
def test_apikey_request_records_real_user_ip_id(monkeypatch):
    model = Model.objects.create(model_name="m", max_tokens=65536)
    Server.objects.create(model_id=model.id, base_url="http://s.example", is_online=True)
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")
    _upstream_ok(monkeypatch)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer key-1",
        REMOTE_ADDR="10.0.0.9",
    )

    assert response.status_code == 200
    user_ip = UserIP.objects.get(apikey="key-1")
    record = RequestRecord.objects.exclude(ip_id=0).get()
    assert record.user_ip_id == user_ip.id
    assert record.ip_id == Ips.objects.get(ip="10.0.0.9").id
    assert record.vip is False


@pytest.mark.django_db
def test_ip_request_without_user_ips_row_records_zero(monkeypatch):
    model = Model.objects.create(model_name="m", max_tokens=65536)
    Server.objects.create(model_id=model.id, base_url="http://s.example", is_online=True)
    _upstream_ok(monkeypatch)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        REMOTE_ADDR="10.0.0.10",
    )

    assert response.status_code == 200
    record = RequestRecord.objects.exclude(ip_id=0).get()
    assert record.user_ip_id == 0
    assert record.ip_id == Ips.objects.get(ip="10.0.0.10").id


@pytest.mark.django_db
def test_blocked_request_records_resolved_user_ip_id(monkeypatch):
    # Unknown model -> 400 blocked path; identity must still be written.
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "missing-model"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer key-1",
        REMOTE_ADDR="10.0.0.11",
    )

    assert response.status_code == 400
    user_ip = UserIP.objects.get(apikey="key-1")
    record = RequestRecord.objects.exclude(ip_id=0).get()
    assert record.user_ip_id == user_ip.id
