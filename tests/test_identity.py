import json
from unittest.mock import MagicMock

import pytest
from django.test import Client

from router.config import APP_CONFIG
from router.models import Department, Ips, Model, RequestRecord, Server, UserIP
from router.services.admission import AdmissionResult
from router.services.cmdb import CMDBService
from router.services.identity import IdentityService, RequestIdentity


def _strict_missing_user_info(monkeypatch):
    monkeypatch.setitem(APP_CONFIG["admission"], "allow_when_user_info_missing", False)


def _cmdb_enabled(monkeypatch):
    monkeypatch.setitem(APP_CONFIG["cmdb"], "enabled", True)


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
def test_apikey_employee_no_wins_over_ip_row():
    # Issue #287: when both an apikey-backed and an IP-backed row exist, the
    # key row is the identity and its employee_no takes precedence.
    ip = Ips.objects.create(ip="10.0.0.1")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="KEY-EMP")

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    assert identity.is_apikey is True
    assert identity.employee_no == "KEY-EMP"


@pytest.mark.django_db
def test_apikey_row_without_employee_no_borrows_ip_row_employee_no():
    # Issue #287 field report: a key row stored without an employee_no borrows
    # the IP-backed row's so admission/whitelist/external routing resolve one.
    ip = Ips.objects.create(ip="10.0.0.2")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="")

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    assert identity.is_apikey is True
    # The key row is still the identity (user_ip_id points at it).
    assert identity.user_ip_id == UserIP.objects.get(apikey="key-1").id
    assert identity.employee_no == "IP-EMP"


@pytest.mark.django_db
def test_apikey_row_without_employee_no_and_no_ip_row():
    ip = Ips.objects.create(ip="10.0.0.3")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="")

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    assert identity.is_apikey is True
    assert identity.employee_no == ""
    assert identity.has_employee is False


@pytest.mark.django_db
def test_unknown_bearer_key_logs_the_ip_fallback(caplog):
    # An unknown key silently downgrading to the IP identity was hard to
    # diagnose in the field; the fallback still happens but is now logged.
    ip = Ips.objects.create(ip="10.0.0.4")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")

    with caplog.at_level("WARNING", logger="router.services.identity"):
        identity = IdentityService.resolve(_request_with_auth("Bearer no-such-key"), ip)

    assert identity.is_apikey is False
    assert identity.employee_no == "IP-EMP"
    assert any("unknown Bearer apikey" in message for message in caplog.messages)


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
    # The key is known but invalid: flagged for refusal, no IP fallback.
    assert identity.invalid_apikey is True


@pytest.mark.django_db
def test_resolve_invalid_apikey_does_not_fall_back_to_ip_identity():
    ip = Ips.objects.create(ip="10.0.0.6")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001", is_valid=False)

    identity = IdentityService.resolve(_request_with_auth("Bearer key-1"), ip)

    # Presenting a revoked key must not downgrade to the IP identity.
    assert identity.invalid_apikey is True
    assert identity.employee_no == ""
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


def _force_permission_denied(monkeypatch):
    """Make every permission check deny, and record whether it ran."""
    calls = []

    def fake_check_permission(self, identity):
        calls.append(identity)
        return AdmissionResult(False, 403, "permission_denied", "Access denied, you do not have permission")

    monkeypatch.setattr(
        "router.services.admission.AdmissionService.check_permission",
        fake_check_permission,
    )
    return calls


@pytest.mark.django_db
def test_valid_apikey_bypasses_admission(monkeypatch):
    # A valid apikey must go through even when the permission check would deny.
    permission_calls = _force_permission_denied(monkeypatch)
    Model.objects.create(model_name="m", max_tokens=65536)
    Server.objects.create(model_id=Model.objects.get(model_name="m").id, base_url="http://s.example", is_online=True)
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")
    _upstream_ok(monkeypatch)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer key-1",
        REMOTE_ADDR="10.0.0.20",
    )

    assert response.status_code == 200
    assert permission_calls == []
    # The IP is still recorded even though it drove no admission decision.
    assert Ips.objects.filter(ip="10.0.0.20").exists()


@pytest.mark.django_db
def test_non_apikey_identity_is_still_gated_by_admission(monkeypatch):
    # Without a valid apikey the permission check still applies and can deny.
    permission_calls = _force_permission_denied(monkeypatch)
    ip = Ips.objects.create(ip="10.0.0.21")
    UserIP.objects.create(ip_id=ip.id, employee_no="IP-EMP")

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        REMOTE_ADDR="10.0.0.21",
    )

    assert response.status_code == 403
    assert len(permission_calls) == 1


@pytest.mark.django_db
def test_invalid_apikey_request_is_refused(monkeypatch):
    # A key stored with is_valid = false (e.g. it failed the department and
    # whitelist check at registration) is refused outright with 403 instead
    # of silently downgrading to IP-based admission.
    def fail(self, method, url, **kwargs):
        pytest.fail("upstream must not be reached for an invalid apikey")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fail,
    )
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001", is_valid=False)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer key-1",
        REMOTE_ADDR="10.0.0.30",
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "invalid_apikey"
    assert response.json()["error"]["message"] == "API key is invalid or revoked"


@pytest.mark.django_db
def test_invalid_apikey_refusal_is_independent_of_allow_when_user_info_missing(monkeypatch):
    # Even with allow_when_user_info_missing = false the revoked key gets the
    # specific invalid_apikey refusal, not the generic permission denial.
    _strict_missing_user_info(monkeypatch)
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001", is_valid=False)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer key-1",
        REMOTE_ADDR="10.0.0.31",
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "invalid_apikey"


@pytest.mark.django_db
def test_unknown_bearer_key_from_unknown_ip_denied_when_strict(monkeypatch):
    # The toggle governs keyless traffic: with it false, a Bearer key with no
    # user_ips row at all falls back to the (unknown) IP identity and is denied.
    _strict_missing_user_info(monkeypatch)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer no-such-key",
        REMOTE_ADDR="10.0.0.32",
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_denied"


# --- first-time IP: blocking CMDB fetch happens before identity resolution ---


def _cmdb_provisioning(department_id, employee_no="E001"):
    """Fake internal adapter: insert the IP-backed user_ips row on lookup."""

    def fetch_and_save(self, ip):
        ip_row = Ips.objects.get(ip=ip)
        UserIP.objects.create(
            ip_id=ip_row.id,
            employee_no=employee_no,
            department_id=department_id,
        )

    return fetch_and_save


def _chat(remote_addr):
    return Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        REMOTE_ADDR=remote_addr,
    )


@pytest.mark.django_db
def test_first_time_ip_identity_resolved_by_blocking_cmdb(monkeypatch):
    # The CMDB fetch runs inline, so the very first request from an IP already
    # resolves the CMDB identity (department verified) instead of starting as
    # "missing user info".
    _cmdb_enabled(monkeypatch)
    dept = Department.objects.create(dept1="allowed-dept", is_allowed=1)
    monkeypatch.setattr(CMDBService, "fetch_and_save_user", _cmdb_provisioning(dept.id))
    Model.objects.create(model_name="m", max_tokens=65536)
    Server.objects.create(model_id=Model.objects.get(model_name="m").id, base_url="http://s.example", is_online=True)
    _upstream_ok(monkeypatch)

    response = _chat("10.0.0.40")

    assert response.status_code == 200
    user_ip = UserIP.objects.get(ip_id=Ips.objects.get(ip="10.0.0.40").id)
    record = RequestRecord.objects.exclude(ip_id=0).get()
    assert record.user_ip_id == user_ip.id


@pytest.mark.django_db
def test_first_time_ip_denied_department_blocked_on_first_request(monkeypatch):
    # With the blocking fetch, a denied department is refused on the first
    # request even while allow_when_user_info_missing is true (default):
    # the identity is complete, so the toggle no longer applies.
    _cmdb_enabled(monkeypatch)
    dept = Department.objects.create(dept1="denied-dept", is_allowed=0)
    monkeypatch.setattr(CMDBService, "fetch_and_save_user", _cmdb_provisioning(dept.id))

    response = _chat("10.0.0.41")

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_denied"
    # The CMDB row was still persisted for the record.
    assert UserIP.objects.filter(ip_id=Ips.objects.get(ip="10.0.0.41").id).exists()


@pytest.mark.django_db
def test_first_time_ip_cmdb_failure_degrades_to_incomplete_identity(monkeypatch):
    # A CMDB outage must not fail the request; it falls back to the
    # incomplete-identity path (allowed while the toggle is true).
    _cmdb_enabled(monkeypatch)

    def fetch_and_save(self, ip):
        raise RuntimeError("CMDB unavailable")

    monkeypatch.setattr(CMDBService, "fetch_and_save_user", fetch_and_save)
    Model.objects.create(model_name="m", max_tokens=65536)
    Server.objects.create(model_id=Model.objects.get(model_name="m").id, base_url="http://s.example", is_online=True)
    _upstream_ok(monkeypatch)

    response = _chat("10.0.0.42")

    assert response.status_code == 200
    record = RequestRecord.objects.exclude(ip_id=0).get()
    assert record.user_ip_id == 0
