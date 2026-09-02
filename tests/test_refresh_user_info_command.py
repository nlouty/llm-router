import pytest
from django.core.management import call_command
from io import StringIO
from router.config import APP_CONFIG
from router.repositories.ips import IPRepository

@pytest.mark.django_db
def test_refresh_user_info_dry_run(monkeypatch):
    # Enable CMDB in config for the test
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    # Mock fetch_user_data
    from router.services.cmdb import CMDBService
    def mock_fetch(self, ip):
        return {
            "user_name": f"user_{ip.replace('.', '_')}",
            "user_charge": "default_charge",
            "employee_no": f"E{ip.split('.')[-1].zfill(5)}",
            "department_id": 1,
            "vip": True,
        }
    monkeypatch.setattr(CMDBService, "fetch_user_data", mock_fetch, raising=False)

    # Create an IP to refresh
    IPRepository.get_or_create("127.0.0.1")

    out = StringIO()
    call_command("refresh_user_info", "--dry-run", stdout=out)

    output = out.getvalue()
    assert "-- GENERATED SQL COMMANDS --" in output
    assert "INSERT INTO user_ips" in output
    assert "ON CONFLICT (ip_id) WHERE ip_id > 0" in output
    assert "vip = EXCLUDED.vip" in output
    assert "127_0_0_1" in output
    assert "To run these commands manually against the database:" in output
    assert "psql -h <db_host>" in output

@pytest.mark.django_db
def test_refresh_user_info_actual_update(monkeypatch):
    # Enable CMDB in config for the test
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    # Mock fetch_user_data
    from router.services.cmdb import CMDBService
    def mock_fetch(self, ip):
        return {
            "user_name": f"user_{ip.replace('.', '_')}",
            "user_charge": "default_charge",
            "employee_no": f"E{ip.split('.')[-1].zfill(5)}",
            "department_id": 1,
            "vip": True,
        }
    monkeypatch.setattr(CMDBService, "fetch_user_data", mock_fetch, raising=False)

    # Create an IP
    ip_row, _ = IPRepository.get_or_create("192.168.1.1")

    out = StringIO()
    call_command("refresh_user_info", stdout=out)

    from router.models import UserIP
    user_ip = UserIP.objects.get(ip_id=ip_row.id)
    assert user_ip.user_name == "user_192_168_1_1"
    assert user_ip.vip is True
    assert "Successfully refreshed 192.168.1.1" in out.getvalue()

@pytest.mark.django_db
def test_refresh_user_info_reports_unimplemented_adapter(monkeypatch):
    # The public CMDB adapter leaves fetch_user_data unimplemented.
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    IPRepository.get_or_create("10.0.0.1")

    out = StringIO()
    call_command("refresh_user_info", stdout=out)

    output = out.getvalue()
    assert "does not implement 'fetch_user_data(ip) -> dict'" in output
    assert "Implement it in 'router/services/cmdb.py'" in output

    from router.models import UserIP
    assert UserIP.objects.filter(ip_id__gt=0).count() == 0

@pytest.mark.django_db
def test_refresh_user_info_apikey_actual_update(monkeypatch):
    # No active IPs, so the IP-backed pass is skipped; only the apikey pass runs.
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001", user_name="old")

    def mock_fetch_by_emp(self, employee_no):
        return {
            "user_name": "Alice",
            "user_charge": "lead",
            "employee_no": employee_no,
            "department_id": 7,
            "vip": True,
        }
    monkeypatch.setattr(CMDBService, "fetch_user_data_by_employee_no", mock_fetch_by_emp, raising=False)

    out = StringIO()
    call_command("refresh_user_info", stdout=out)

    user_ip = UserIP.objects.get(apikey="key-1")
    assert user_ip.user_name == "Alice"
    assert user_ip.user_charge == "lead"
    assert user_ip.department_id == 7
    assert user_ip.vip is True
    # apikey and employee_no stay fixed
    assert user_ip.apikey == "key-1"
    assert user_ip.employee_no == "E0001"
    assert "Successfully refreshed apikey for E0001" in out.getvalue()

@pytest.mark.django_db
def test_refresh_user_info_apikey_dry_run(monkeypatch):
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001")

    def mock_fetch_by_emp(self, employee_no):
        return {
            "user_name": "Alice",
            "user_charge": "lead",
            "employee_no": employee_no,
            "department_id": 7,
            "vip": True,
        }
    monkeypatch.setattr(CMDBService, "fetch_user_data_by_employee_no", mock_fetch_by_emp, raising=False)

    out = StringIO()
    call_command("refresh_user_info", "--dry-run", stdout=out)

    output = out.getvalue()
    assert "ON CONFLICT (apikey) WHERE apikey <> ''" in output
    assert "vip = EXCLUDED.vip" in output
    assert "'key-1'" in output

@pytest.mark.django_db
def test_refresh_user_info_apikey_reports_unimplemented(monkeypatch):
    # IP-backed lookup is implemented (returns no data); apikey-backed lookup is not.
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    monkeypatch.setattr(CMDBService, "fetch_user_data", lambda self, ip: None, raising=False)

    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001")

    out = StringIO()
    call_command("refresh_user_info", stdout=out)

    output = out.getvalue()
    assert "does not implement 'fetch_user_data_by_employee_no(employee_no) -> dict'" in output
    assert "Implement it in 'router/services/cmdb.py'" in output

@pytest.mark.django_db
def test_refresh_user_info_apikey_flag_skips_ip_pass(monkeypatch):
    # --apikey refreshes only API-key-backed rows, even with active IPs present.
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    def mock_fetch_by_emp(self, employee_no):
        return {
            "user_name": "Alice",
            "user_charge": "lead",
            "employee_no": employee_no,
            "department_id": 7,
            "vip": True,
        }

    monkeypatch.setattr(CMDBService, "fetch_user_data_by_employee_no", mock_fetch_by_emp, raising=False)

    IPRepository.get_or_create("192.168.1.1")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001", user_name="old")

    out = StringIO()
    call_command("refresh_user_info", "--apikey", stdout=out)

    # If the IP pass had run, the dummy adapter's unimplemented fetch_user_data
    # would abort the command before the apikey row was refreshed.
    assert UserIP.objects.get(apikey="key-1").user_name == "Alice"
    assert UserIP.objects.filter(ip_id__gt=0).count() == 0

@pytest.mark.django_db
def test_refresh_user_info_ip_flag_refreshes_all_ips_only(monkeypatch):
    # Bare --ip refreshes every IP-backed row and skips the API-key pass.
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    def mock_fetch(self, ip):
        return {
            "user_name": f"user_{ip.replace('.', '_')}",
            "user_charge": "default_charge",
            "employee_no": f"E{ip.split('.')[-1].zfill(5)}",
            "department_id": 1,
            "vip": True,
        }

    monkeypatch.setattr(CMDBService, "fetch_user_data", mock_fetch, raising=False)

    IPRepository.get_or_create("192.168.1.1")
    IPRepository.get_or_create("192.168.1.2")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001", user_name="old")

    out = StringIO()
    call_command("refresh_user_info", "--ip", stdout=out)

    # If the apikey pass had run, the dummy adapter's unimplemented
    # fetch_user_data_by_employee_no would abort the command.
    assert "Successfully refreshed 192.168.1.1" in out.getvalue()
    assert "Successfully refreshed 192.168.1.2" in out.getvalue()
    assert UserIP.objects.filter(ip_id__gt=0).count() == 2
    assert UserIP.objects.get(apikey="key-1").user_name == "old"

@pytest.mark.django_db
def test_refresh_user_info_ip_flag_single_address(monkeypatch):
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService

    def mock_fetch(self, ip):
        return {
            "user_name": f"user_{ip.replace('.', '_')}",
            "user_charge": "default_charge",
            "employee_no": f"E{ip.split('.')[-1].zfill(5)}",
            "department_id": 1,
            "vip": True,
        }

    monkeypatch.setattr(CMDBService, "fetch_user_data", mock_fetch, raising=False)

    ip_row, _ = IPRepository.get_or_create("192.168.1.1")
    IPRepository.get_or_create("192.168.1.2")

    out = StringIO()
    call_command("refresh_user_info", "--ip", "192.168.1.1", stdout=out)

    from router.models import UserIP
    assert UserIP.objects.get(ip_id=ip_row.id).user_name == "user_192_168_1_1"
    assert UserIP.objects.filter(ip_id__gt=0).count() == 1
    assert "Successfully refreshed 192.168.1.1" in out.getvalue()

@pytest.mark.django_db
def test_refresh_user_info_ip_and_apikey_flags_run_both_passes(monkeypatch):
    monkeypatch.setitem(APP_CONFIG, "cmdb", {"enabled": True, "dummy": True})

    from router.services.cmdb import CMDBService
    from router.models import UserIP

    def mock_fetch(self, ip):
        return {
            "user_name": "Bob",
            "user_charge": "lead",
            "employee_no": "E0002",
            "department_id": 3,
            "vip": False,
        }

    def mock_fetch_by_emp(self, employee_no):
        return {
            "user_name": "Alice",
            "user_charge": "lead",
            "employee_no": employee_no,
            "department_id": 7,
            "vip": True,
        }

    monkeypatch.setattr(CMDBService, "fetch_user_data", mock_fetch, raising=False)
    monkeypatch.setattr(CMDBService, "fetch_user_data_by_employee_no", mock_fetch_by_emp, raising=False)

    ip_row, _ = IPRepository.get_or_create("192.168.1.1")
    UserIP.objects.create(ip_id=0, apikey="key-1", employee_no="E0001", user_name="old")

    out = StringIO()
    call_command("refresh_user_info", "--ip", "--apikey", stdout=out)

    assert UserIP.objects.get(ip_id=ip_row.id).user_name == "Bob"
    assert UserIP.objects.get(apikey="key-1").user_name == "Alice"
