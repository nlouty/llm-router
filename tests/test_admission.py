from datetime import timedelta

import pytest
from django.utils import timezone

from router.models import Department, UserIP, Whitelist
from router.services.admission import AdmissionService
from router.services.identity import RequestIdentity


def _identity(
    user_ip_id=0,
    employee_no="",
    department_id=None,
    user_charge="",
):
    return RequestIdentity(
        ip=None,
        user_ip_id=user_ip_id,
        employee_no=employee_no,
        department_id=department_id,
        is_vip=False,
        is_apikey=False,
        user_charge=user_charge,
    )


def _service(strict: bool = False) -> AdmissionService:
    service = AdmissionService()
    service.allow_missing_user_info = not strict
    return service


def _allowed_department() -> Department:
    return Department.objects.create(dept1="allowed-dept", is_allowed=1)


def _denied_department() -> Department:
    return Department.objects.create(dept1="denied-dept", is_allowed=0)


def _whitelist(employee_no="E001", user_name="", expire_time=None):
    return Whitelist.objects.create(
        employee_no=employee_no,
        user_name=user_name,
        is_allowed=1,
        expire_time=expire_time,
        update_time=timezone.now(),
    )


# --- complete identities: department chain ---


@pytest.mark.django_db
def test_complete_identity_with_allowed_department_passes():
    dept = _allowed_department()
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    assert AdmissionService().check_permission(identity).allowed


@pytest.mark.django_db
def test_complete_identity_with_allowed_department_passes_even_when_strict():
    dept = _allowed_department()
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    assert _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_complete_identity_with_denied_department_is_denied():
    dept = _denied_department()
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    result = AdmissionService().check_permission(identity)

    assert not result.allowed
    assert result.status_code == 403
    assert result.error_type == "permission_denied"


@pytest.mark.django_db
def test_whitelisted_employee_rescues_denied_department():
    dept = _denied_department()
    _whitelist(employee_no="E001")
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    assert AdmissionService().check_permission(identity).allowed


@pytest.mark.django_db
def test_user_charge_matching_whitelist_user_name_rescues_denied_department():
    dept = _denied_department()
    _whitelist(employee_no="E999", user_name="Alice")
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id, user_charge="Alice")

    assert AdmissionService().check_permission(identity).allowed


@pytest.mark.django_db
def test_expired_whitelist_employee_does_not_rescue_denied_department():
    dept = _denied_department()
    _whitelist(employee_no="E001", expire_time=timezone.now() - timedelta(hours=1))
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    assert not AdmissionService().check_permission(identity).allowed


@pytest.mark.django_db
def test_expired_whitelist_user_name_does_not_rescue_denied_department():
    dept = _denied_department()
    _whitelist(employee_no="E999", user_name="Alice", expire_time=timezone.now() - timedelta(hours=1))
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id, user_charge="Alice")

    assert not AdmissionService().check_permission(identity).allowed


@pytest.mark.django_db
def test_disallowed_whitelist_entry_does_not_rescue():
    dept = _denied_department()
    Whitelist.objects.create(employee_no="E001", is_allowed=0, update_time=timezone.now())
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    assert not AdmissionService().check_permission(identity).allowed


# --- incomplete identities: whitelist then the unified toggle ---


@pytest.mark.django_db
def test_ip_without_user_ips_row_follows_toggle():
    identity = _identity()

    assert AdmissionService().check_permission(identity).allowed
    assert not _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_row_without_employee_no_follows_toggle():
    identity = _identity(user_ip_id=1)

    assert AdmissionService().check_permission(identity).allowed
    assert not _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_row_without_department_follows_toggle():
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=None)

    assert AdmissionService().check_permission(identity).allowed
    assert not _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_row_with_unresolvable_department_zero_follows_toggle():
    # department_id = 0 is what the whitelist apikey bypass inserts; there is
    # no departments row with id 0, so it counts as unresolved.
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=0)

    assert AdmissionService().check_permission(identity).allowed
    assert not _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_strict_denial_uses_configured_message():
    service = _service(strict=True)
    service.missing_user_info_message = "Custom denial message"

    result = service.check_permission(_identity())

    assert result.status_code == 403
    assert result.error_type == "permission_denied"
    assert result.message == "Custom denial message"


@pytest.mark.django_db
def test_department_denial_keeps_default_message():
    dept = _denied_department()
    service = AdmissionService()
    service.missing_user_info_message = "Custom denial message"
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=dept.id)

    result = service.check_permission(identity)

    assert result.status_code == 403
    assert result.message == "Access denied, you do not have permission"


@pytest.mark.django_db
def test_whitelisted_user_charge_rescues_missing_department_when_strict():
    _whitelist(employee_no="E999", user_name="Alice")
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=None, user_charge="Alice")

    assert _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_whitelisted_employee_rescues_missing_employee_info_when_strict():
    _whitelist(employee_no="E001")
    identity = _identity(user_ip_id=1, employee_no="E001", department_id=None)

    assert _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_expired_whitelist_user_charge_does_not_rescue_when_strict():
    _whitelist(employee_no="E999", user_name="Alice", expire_time=timezone.now() - timedelta(hours=1))
    identity = _identity(user_ip_id=1, department_id=None, user_charge="Alice")

    assert not _service(strict=True).check_permission(identity).allowed


@pytest.mark.django_db
def test_whitelist_cannot_rescue_an_ip_without_user_ips_row():
    # No row means no employee_no and no user_charge to match against.
    _whitelist(employee_no="E001", user_name="Alice")
    identity = _identity()

    assert not _service(strict=True).check_permission(identity).allowed


# --- apikey registration verification (department, then whitelist) ---


def _user_ip(employee_no="E001", department_id=None, user_charge=""):
    return UserIP(
        ip_id=0,
        apikey="key-1",
        employee_no=employee_no,
        department_id=department_id,
        user_charge=user_charge,
    )


@pytest.mark.django_db
def test_verify_apikey_registration_allows_allowed_department():
    dept = _allowed_department()

    assert AdmissionService.verify_apikey_registration(_user_ip(department_id=dept.id))


@pytest.mark.django_db
def test_verify_apikey_registration_denies_denied_department():
    dept = _denied_department()

    assert not AdmissionService.verify_apikey_registration(_user_ip(department_id=dept.id))


@pytest.mark.django_db
def test_verify_apikey_registration_denies_missing_department():
    assert not AdmissionService.verify_apikey_registration(_user_ip(department_id=None))
    # An id no departments row uses (e.g. the whitelist-bypass 0) fails too.
    assert not AdmissionService.verify_apikey_registration(_user_ip(department_id=0))


@pytest.mark.django_db
def test_verify_apikey_registration_whitelist_employee_no_rescues():
    dept = _denied_department()
    _whitelist(employee_no="E001")

    assert AdmissionService.verify_apikey_registration(_user_ip(department_id=dept.id))


@pytest.mark.django_db
def test_verify_apikey_registration_whitelist_user_charge_rescues():
    # user_ips.user_charge is matched against whitelist.user_name.
    dept = _denied_department()
    _whitelist(employee_no="E999", user_name="Alice")

    assert AdmissionService.verify_apikey_registration(
        _user_ip(department_id=dept.id, user_charge="Alice")
    )


@pytest.mark.django_db
def test_verify_apikey_registration_expired_whitelist_does_not_rescue():
    dept = _denied_department()
    _whitelist(employee_no="E001", expire_time=timezone.now() - timedelta(hours=1))

    assert not AdmissionService.verify_apikey_registration(_user_ip(department_id=dept.id))
