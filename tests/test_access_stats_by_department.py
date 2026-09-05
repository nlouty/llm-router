"""
测试access_stats_by_department接口：按 <employee_no, ip> 聚合token用量（issue #295）
"""
import csv
import io
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from router.api.stats import BEIJING_TZ, TIME_FORMAT
from router.models import Department, Ips, RequestRecord, UserIP
from router.repositories.requests import RequestRepository


def _make_request(ip_id, user_ip_id, *, status="success", when=None,
                  input_tokens=100, output_tokens=50, prefix_cache=0):
    return RequestRecord.objects.create(
        user_ip_id=user_ip_id,
        ip_id=ip_id,
        send_time=when or timezone.now(),
        end_time=when or timezone.now(),
        model_id=1,
        task_status=status,
        input_token_cnt=input_tokens,
        output_token_cnt=output_tokens,
        final_prefix_cache=prefix_cache,
    )


@pytest.fixture
def stats_data(db):
    """一个IP（EMP001名下）+ 一把EMP002的apikey + 一个无归属IP"""
    now = timezone.now()

    backend_dept = Department.objects.create(
        dept1="技术部", dept2="研发中心", dept3="后端组", dept4="",
        created_at=now, updated_at=now,
    )
    frontend_dept = Department.objects.create(
        dept1="技术部", dept2="研发中心", dept3="前端组", dept4="",
        created_at=now, updated_at=now,
    )

    ip_a = Ips.objects.create(ip="10.0.0.1", created_at=now, updated_at=now)
    ip_b = Ips.objects.create(ip="10.0.0.2", created_at=now, updated_at=now)

    ip_user = UserIP.objects.create(
        ip_id=ip_a.id, apikey="",
        employee_no="EMP001", user_name="张三", user_charge="王五",
        department_id=backend_dept.id, is_valid=True,
        created_at=now, updated_at=now,
    )
    key_emp002 = UserIP.objects.create(
        ip_id=0, apikey="sk-emp002",
        employee_no="EMP002", user_name="李四", user_charge="赵六",
        department_id=frontend_dept.id, is_valid=True,
        created_at=now, updated_at=now,
    )

    start = now - timedelta(hours=1)

    # EMP002 的 apikey 从 IP A 发出的两笔请求（工号取 apikey 的）
    _make_request(ip_a.id, key_emp002.id, when=start + timedelta(minutes=10),
                  input_tokens=300, output_tokens=80, prefix_cache=120)
    _make_request(ip_a.id, key_emp002.id, when=start + timedelta(minutes=20),
                  input_tokens=500, output_tokens=120, prefix_cache=80)

    # IP A 的裸请求 + 无凭证请求（user_ip_id=0）：都降级到 IP 的 EMP001 并归并
    _make_request(ip_a.id, ip_user.id, when=start + timedelta(minutes=30),
                  input_tokens=200, output_tokens=60, prefix_cache=40)
    _make_request(ip_a.id, 0, when=start + timedelta(minutes=40),
                  input_tokens=100, output_tokens=50)

    # 无归属 IP 上的无凭证请求：stale
    _make_request(ip_b.id, 0, when=start + timedelta(minutes=50),
                  input_tokens=70, output_tokens=30)

    # 失败请求与时间范围外的请求不计入
    _make_request(ip_a.id, ip_user.id, status="failed",
                  input_tokens=999, output_tokens=999)
    _make_request(ip_a.id, ip_user.id, when=now - timedelta(days=3),
                  input_tokens=888, output_tokens=888)

    return {"start": start, "end": now + timedelta(minutes=5)}


class TestSummarizeSuccessByEmployeeAndIp:
    """测试 RequestRepository.summarize_success_by_employee_and_ip"""

    def _results(self, stats_data, **kwargs):
        return RequestRepository.summarize_success_by_employee_and_ip(
            start=stats_data["start"], end=stats_data["end"], **kwargs
        )

    def test_grouping_and_resolution_priority(self, stats_data):
        """apikey工号优先，无apikey降级到IP工号，均缺失用stale"""
        results = self._results(stats_data)
        by_key = {(r["employee_no"], r["ip"]): r for r in results}

        assert set(by_key) == {("EMP002", "10.0.0.1"), ("EMP001", "10.0.0.1"), ("stale", "10.0.0.2")}

        emp002 = by_key[("EMP002", "10.0.0.1")]
        assert emp002["access_count"] == 2
        assert emp002["input_token"] == 800
        assert emp002["output_token"] == 200
        assert emp002["prefix_cache"] == 200
        assert emp002["ip_employee_no"] == "EMP001"
        assert emp002["user_name"] == "李四"

        emp001 = by_key[("EMP001", "10.0.0.1")]
        assert emp001["access_count"] == 2  # 裸请求 + user_ip_id=0 归并
        assert emp001["input_token"] == 300
        assert emp001["prefix_cache"] == 40
        assert emp001["ip_employee_no"] == "EMP001"
        assert emp001["user_name"] == "张三"

        stale = by_key[("stale", "10.0.0.2")]
        assert stale["access_count"] == 1
        assert stale["ip_employee_no"] == ""
        assert stale["user_name"] == ""
        assert stale["dept1"] == ""

    def test_department_from_resolved_employee(self, stats_data):
        """dept1-4 取自解析后工号的用户，而非IP的用户"""
        results = self._results(stats_data)
        by_key = {(r["employee_no"], r["ip"]): r for r in results}

        # EMP002 来自IP A（IP归属后端组），但部门应是李四的前端组
        emp002 = by_key[("EMP002", "10.0.0.1")]
        assert emp002["dept1"] == "技术部"
        assert emp002["dept2"] == "研发中心"
        assert emp002["dept3"] == "前端组"

        assert by_key[("EMP001", "10.0.0.1")]["dept3"] == "后端组"

    def test_department_filter_uses_resolved_employee(self, stats_data):
        results = self._results(stats_data, dept3="前端组")
        assert [r["employee_no"] for r in results] == ["EMP002"]

    def test_employee_no_filter_matches_resolved(self, stats_data):
        assert [r["employee_no"] for r in self._results(stats_data, employee_no=["EMP002"])] == ["EMP002"]
        assert [r["employee_no"] for r in self._results(stats_data, employee_no=["stale"])] == ["stale"]

    def test_user_name_filter(self, stats_data):
        results = self._results(stats_data, user_name=["李四"])
        assert [r["employee_no"] for r in results] == ["EMP002"]

    def test_ip_filter(self, stats_data):
        results = self._results(stats_data, ip=["10.0.0.2"])
        assert [r["employee_no"] for r in results] == ["stale"]

    def test_sorted_by_access_count_desc(self, stats_data):
        results = self._results(stats_data)
        counts = [r["access_count"] for r in results]
        assert counts == sorted(counts, reverse=True)

    def test_empty_time_range(self, stats_data):
        results = self._results(
            {"start": stats_data["end"] + timedelta(days=1),
             "end": stats_data["end"] + timedelta(days=2)}
        )
        assert results == []


class TestAccessStatsByDepartmentView:
    """测试 /api/access_stats_by_department 视图"""

    def _query(self, stats_data, extra=""):
        start_str = stats_data["start"].astimezone(BEIJING_TZ).strftime(TIME_FORMAT)
        end_str = stats_data["end"].astimezone(BEIJING_TZ).strftime(TIME_FORMAT)
        url = f"/api/access_stats_by_department?start_time={start_str}&end_time={end_str}{extra}"
        return Client().get(url)

    def test_response_shape(self, stats_data):
        response = self._query(stats_data)
        assert response.status_code == 200

        data = response.json()
        assert data["code"] == 200
        assert data["total"] == len(data["data"]) == 3

        row = next(r for r in data["data"] if r["employee_no"] == "EMP002")
        for field in ("employee_no", "ip", "ip_employee_no", "access_count",
                      "prefix_cache", "input_token", "output_token",
                      "user_name", "user_charge", "dept1", "dept2", "dept3", "dept4"):
            assert field in row
        assert row["ip_employee_no"] == "EMP001"

    def test_employee_filter_via_query(self, stats_data):
        response = self._query(stats_data, "&employee_no=EMP002")
        data = response.json()
        assert data["total"] == 1
        assert data["data"][0]["employee_no"] == "EMP002"


class TestExportAccessStatsCsv:
    """测试 /api/access_stats_by_department/export 导出"""

    def _csv_rows(self, stats_data):
        start_str = stats_data["start"].astimezone(BEIJING_TZ).strftime(TIME_FORMAT)
        end_str = stats_data["end"].astimezone(BEIJING_TZ).strftime(TIME_FORMAT)
        url = f"/api/access_stats_by_department/export?start_time={start_str}&end_time={end_str}"
        response = Client().get(url)
        assert response.status_code == 200
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_csv_contains_new_columns(self, stats_data):
        rows = self._csv_rows(stats_data)
        header = rows[0]
        data_rows = rows[1:]

        assert header == [
            "IP地址", "访问次数", "输入Token", "输出Token", "用户姓名", "资产挂账人",
            "员工工号", "一级部门", "二级部门", "三级部门", "四级部门", "IP工号", "前缀缓存Token",
        ]
        assert len(data_rows) == 3

        emp002 = next(r for r in data_rows if r[header.index("员工工号")] == "EMP002")
        assert emp002[header.index("IP工号")] == "EMP001"
        assert emp002[header.index("前缀缓存Token")] == "200"
        assert emp002[header.index("三级部门")] == "前端组"
