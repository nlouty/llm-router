import json
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from router.models import UserIP, Whitelist
from router.repositories.user_ips import UserIPRepository
from router.services.cmdb import CMDBService


def _post(client, apikey="key-1", employee_no="E001"):
    return client.post(
        "/api/apikey",
        data=json.dumps({"apikey": apikey, "employee_no": employee_no}),
        content_type="application/json",
    )


def _get(client, employee_no="E001"):
    return client.get(f"/api/apikey?employee_no={employee_no}")


def _invalidate(client, employee_no="E001"):
    return client.post(
        "/api/apikey/invalidate",
        data=json.dumps({"employee_no": employee_no}),
        content_type="application/json",
    )


@pytest.mark.django_db
def test_public_cmdb_adapter_returns_404(client):
    response = _post(client)

    assert response.status_code == 404
    assert response.json()["error"] == "API key registration is not implemented"


@pytest.mark.django_db
def test_register_apikey_delegates_write_to_cmdb(client, monkeypatch):
    calls = []

    def fetch_and_save(self, apikey, employee_no):
        calls.append((apikey, employee_no))
        UserIP.objects.create(ip_id=0, apikey=apikey, employee_no=employee_no)

    monkeypatch.setattr(CMDBService, "fetch_and_save_apikey", fetch_and_save)

    response = _post(client)

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "message": "success",
        "data": {"employee_no": "E001"},
    }
    assert "key-1" not in response.content.decode()
    assert calls == [("key-1", "E001")]
    assert UserIP.objects.filter(apikey="key-1", employee_no="E001").exists()


@pytest.mark.django_db
def test_register_apikey_maps_cmdb_lookup_failure_to_404(client, monkeypatch):
    def fail(self, apikey, employee_no):
        raise LookupError(employee_no)

    monkeypatch.setattr(CMDBService, "fetch_and_save_apikey", fail)

    response = _post(client)

    assert response.status_code == 404
    assert response.json()["error"] == "employee_no not found"


@pytest.mark.django_db
def test_register_apikey_maps_cmdb_failure_to_502(client, monkeypatch):
    def fail(self, apikey, employee_no):
        raise RuntimeError("CMDB unavailable")

    monkeypatch.setattr(CMDBService, "fetch_and_save_apikey", fail)

    response = _post(client)

    assert response.status_code == 502


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"apikey": "", "employee_no": "E001"},
        {"apikey": "key-1", "employee_no": ""},
        {"apikey": 123, "employee_no": "E001"},
    ],
)
def test_register_apikey_validates_body(client, body):
    response = client.post("/api/apikey", data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_user_ip_credential_constraints():
    UserIP.objects.create(ip_id=10, employee_no="IP-1")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")
    UserIP.objects.create(ip_id=0, apikey="key-2", employee_no="E002")

    with pytest.raises(IntegrityError), transaction.atomic():
        UserIP.objects.create(ip_id=10, employee_no="IP-2")

    with pytest.raises(IntegrityError), transaction.atomic():
        UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E003")

    with pytest.raises(IntegrityError), transaction.atomic():
        UserIP.objects.create(ip_id=11, apikey="both", employee_no="E004")

    with pytest.raises(IntegrityError), transaction.atomic():
        UserIP.objects.create(ip_id=0, apikey="key-3", employee_no="E001")


@pytest.mark.django_db
def test_register_apikey_rejects_duplicate_employee(client):
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")

    response = _post(client, apikey="key-2", employee_no="E001")

    assert response.status_code == 409
    assert response.json()["error"] == "employee already has an active apikey"


# --- whitelist bypass (issue #281) ---


@pytest.mark.django_db
def test_whitelisted_employee_registers_without_cmdb(client, monkeypatch):
    def fail(self, apikey, employee_no):
        pytest.fail("CMDB must not be called for a whitelisted employee")

    monkeypatch.setattr(CMDBService, "fetch_and_save_apikey", fail)
    Whitelist.objects.create(
        employee_no="E001", user_name="Alice", is_allowed=1, update_time=timezone.now()
    )

    response = _post(client)

    assert response.status_code == 200
    assert response.json() == {"code": 200, "message": "success", "data": {"employee_no": "E001"}}
    row = UserIP.objects.get(apikey="key-1")
    assert row.employee_no == "E001"
    assert row.ip_id == 0
    assert row.department_id == 0
    assert row.user_name == "Alice"
    assert row.is_valid is True


@pytest.mark.django_db
def test_expired_whitelist_entry_falls_back_to_cmdb(client):
    Whitelist.objects.create(
        employee_no="E001",
        is_allowed=1,
        expire_time=timezone.now() - timedelta(hours=1),
        update_time=timezone.now(),
    )

    response = _post(client)

    assert response.status_code == 404
    assert response.json()["error"] == "API key registration is not implemented"


@pytest.mark.django_db
def test_disallowed_whitelist_entry_falls_back_to_cmdb(client):
    Whitelist.objects.create(employee_no="E001", is_allowed=0, update_time=timezone.now())

    response = _post(client)

    assert response.status_code == 404


@pytest.mark.django_db
def test_whitelisted_employee_still_rejects_duplicate(client):
    Whitelist.objects.create(employee_no="E001", is_allowed=1, update_time=timezone.now())
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")

    response = _post(client, apikey="key-2")

    assert response.status_code == 409


@pytest.mark.django_db
def test_get_apikey_returns_masked_preview(client):
    UserIP.objects.create(ip_id=0, apikey="abcdEFGHwxyz", employee_no="E001")

    response = _get(client, "E001")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["employee_no"] == "E001"
    assert data["apikey_preview"] == "abcd…wxyz"
    assert data["is_valid"] is True
    assert "abcdEFGHwxyz" not in response.content.decode()


@pytest.mark.django_db
def test_get_apikey_requires_employee_no(client):
    response = client.get("/api/apikey")

    assert response.status_code == 400


@pytest.mark.django_db
def test_get_apikey_missing_returns_404(client):
    response = _get(client, "E999")

    assert response.status_code == 404


@pytest.mark.django_db
def test_invalidate_apikey_deletes_row(client):
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")

    response = _invalidate(client, "E001")

    assert response.status_code == 200
    assert response.json()["data"]["employee_no"] == "E001"
    assert UserIPRepository.get_active_apikey_by_employee_no("E001") is None
    assert not UserIP.objects.filter(apikey="key-1").exists()


@pytest.mark.django_db
def test_invalidate_apikey_missing_returns_404(client):
    response = _invalidate(client, "E999")

    assert response.status_code == 404


@pytest.mark.django_db
def test_invalidate_then_rotate(client, monkeypatch):
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E001")
    _invalidate(client, "E001")

    def fetch_and_save(self, apikey, employee_no):
        UserIP.objects.create(ip_id=0, apikey=apikey, employee_no=employee_no)

    monkeypatch.setattr(CMDBService, "fetch_and_save_apikey", fetch_and_save)

    response = _post(client, apikey="key-2", employee_no="E001")

    assert response.status_code == 200
    assert UserIP.objects.filter(apikey="key-2", employee_no="E001").exists()
