from django.test import Client
from django.utils import timezone

from router import views
from router.models import Whitelist


def test_whitelist_update_create_noop_and_update():
    client = Client()

    created = client.post("/api/whitelist/update", {"employee_no": "E001", "is_allowed": "1"})
    unchanged = client.post("/api/whitelist/update", {"employee_no": "E001", "is_allowed": "1"})
    updated = client.post("/api/whitelist/update", {"employee_no": "E001", "is_allowed": "0"})

    assert created.status_code == 200
    assert created.json()["message"] == "创建成功"
    assert unchanged.status_code == 200
    assert unchanged.json()["message"] == "本次修改未生效"
    assert updated.status_code == 200
    assert updated.json()["message"] == "更新成功"


def test_whitelist_update_sets_expire_time():
    response = Client().post(
        "/api/whitelist/update",
        {"employee_no": "E001", "is_allowed": "1", "user_name": "Alice", "expire_time": "2026-12-31 23:59:59"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["expire_time"] == "2026-12-31 23:59:59"
    row = Whitelist.objects.get(employee_no="E001")
    assert timezone.is_aware(row.expire_time)
    assert row.expire_time.year == 2026


def test_whitelist_update_absent_expire_time_keeps_stored_value():
    client = Client()
    client.post(
        "/api/whitelist/update",
        {"employee_no": "E001", "is_allowed": "1", "expire_time": "2026-12-31 23:59:59"},
    )

    response = client.post("/api/whitelist/update", {"employee_no": "E001", "is_allowed": "1"})

    assert response.status_code == 200
    assert response.json()["message"] == "本次修改未生效"
    assert response.json()["data"]["expire_time"] == "2026-12-31 23:59:59"


def test_whitelist_update_empty_expire_time_clears_it():
    client = Client()
    client.post(
        "/api/whitelist/update",
        {"employee_no": "E001", "is_allowed": "1", "expire_time": "2026-12-31 23:59:59"},
    )

    response = client.post("/api/whitelist/update", {"employee_no": "E001", "is_allowed": "1", "expire_time": ""})

    assert response.status_code == 200
    assert response.json()["data"]["expire_time"] is None
    assert Whitelist.objects.get(employee_no="E001").expire_time is None


def test_whitelist_update_rejects_invalid_expire_time():
    response = Client().post(
        "/api/whitelist/update",
        {"employee_no": "E001", "is_allowed": "1", "expire_time": "not-a-datetime"},
    )

    assert response.status_code == 400
    assert "expire_time" in response.json()["error"]
    assert not Whitelist.objects.filter(employee_no="E001").exists()


def test_refresh_user_info_starts_background_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, daemon):
            started["target"] = target
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(views.threading, "Thread", FakeThread)
    monkeypatch.setitem(views.APP_CONFIG, "cmdb", {**views.APP_CONFIG.get("cmdb", {}), "enabled": True})

    response = Client().post("/api/refresh_user_info")

    assert response.status_code == 200
    assert response.json() == {"code": 200, "message": "用户信息刷新任务已启动"}
    assert started["daemon"] is True
    assert started["started"] is True
