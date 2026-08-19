"""
测试IP列表接口（包含用户和部门信息）
"""
import pytest
from django.utils import timezone
from router.models import Ips, UserIP, Department
from router.repositories.ips import IPRepository


class TestIPListWithUserInfo:
    """测试IP列表接口"""

    @pytest.fixture
    def sample_data(self, db):
        """创建测试数据"""
        now = timezone.now()

        # 创建部门
        dept1 = Department.objects.create(
            dept1="技术部",
            dept2="研发中心",
            dept3="后端组",
            dept4="平台研发",
            created_at=now,
            updated_at=now
        )

        dept2 = Department.objects.create(
            dept1="技术部",
            dept2="研发中心",
            dept3="前端组",
            dept4="",
            created_at=now,
            updated_at=now
        )

        # 创建IP
        ips = []
        for i in range(10):
            ip = Ips.objects.create(
                ip=f"192.168.1.{i+1}",
                concurrent_multiplier=1.0 + i * 0.5,
                vip=i % 3 == 0,
                created_at=now,
                updated_at=now
            )
            ips.append(ip)

        # 创建用户IP关联（只为前5个IP创建）
        for i in range(5):
            UserIP.objects.create(
                ip_id=ips[i].id,
                employee_no=f"EMP{i:03d}",
                user_name=f"测试用户{i}",
                user_charge=f"职位{i}",
                department_id=dept1.id if i % 2 == 0 else dept2.id,
                is_valid=True,
                created_at=now,
                updated_at=now
            )

        return {"ips": ips, "dept1": dept1, "dept2": dept2}

    def test_list_all_without_pagination(self, sample_data):
        """测试不分页返回全量数据"""
        data, total = IPRepository.list_with_user_info()

        # 验证返回所有数据
        assert total == 10
        assert len(data) == 10

        # 验证数据结构
        assert "id" in data[0]
        assert "ip" in data[0]
        assert "concurrent_multiplier" in data[0]
        assert "vip" in data[0]
        assert "employee_no" in data[0]
        assert "user_name" in data[0]
        assert "user_charge" in data[0]
        assert "dept1" in data[0]
        assert "dept2" in data[0]
        assert "dept3" in data[0]
        assert "dept4" in data[0]

    def test_list_with_pagination(self, sample_data):
        """测试分页查询"""
        # 第一页，每页3条
        data, total = IPRepository.list_with_user_info(page=1, page_size=3)

        assert total == 10
        assert len(data) == 3

        # 第二页
        data, total = IPRepository.list_with_user_info(page=2, page_size=3)

        assert total == 10
        assert len(data) == 3

    def test_filter_by_employee_no(self, sample_data):
        """测试按员工工号筛选"""
        # 精确匹配
        data, total = IPRepository.list_with_user_info(employee_no="EMP001")

        assert total == 1
        assert len(data) == 1
        assert data[0]["employee_no"] == "EMP001"

        # 模糊匹配
        data, total = IPRepository.list_with_user_info(employee_no="EMP")

        assert total == 5  # 只有前5个IP有关联用户
        assert len(data) == 5

    def test_filter_by_ip(self, sample_data):
        """测试按IP地址筛选（部分匹配）"""
        # "192.168.1.1" 是 "192.168.1.1" 和 "192.168.1.10" 的子串
        data, total = IPRepository.list_with_user_info(ip="192.168.1.1")

        assert total == 2
        assert len(data) == 2
        assert {item["ip"] for item in data} == {"192.168.1.1", "192.168.1.10"}

        # 模糊匹配
        data, total = IPRepository.list_with_user_info(ip="192.168.1")

        assert total == 10  # 所有IP都匹配
        assert len(data) == 10

    def test_filter_combined(self, sample_data):
        """测试组合筛选"""
        data, total = IPRepository.list_with_user_info(
            employee_no="EMP001",
            ip="192.168.1.2"
        )

        assert total == 1
        assert data[0]["ip"] == "192.168.1.2"
        assert data[0]["employee_no"] == "EMP001"

    def test_data_with_department_info(self, sample_data):
        """测试部门信息关联"""
        data, total = IPRepository.list_with_user_info(employee_no="EMP000")

        assert len(data) == 1
        item = data[0]

        # 验证部门信息
        assert item["dept1"] == "技术部"
        assert item["dept2"] == "研发中心"
        assert item["dept3"] == "后端组"
        assert item["dept4"] == "平台研发"

    def test_data_without_user_info(self, sample_data):
        """测试没有关联用户信息的IP"""
        # IP索引6-9没有关联用户
        data, total = IPRepository.list_with_user_info(ip="192.168.1.7")

        assert len(data) == 1
        item = data[0]

        # 验证IP信息存在
        assert item["ip"] == "192.168.1.7"
        assert item["concurrent_multiplier"] > 0

        # 验证用户信息为空
        assert item["employee_no"] == ""
        assert item["user_name"] == ""
        assert item["user_charge"] == ""
        assert item["dept1"] == ""
        assert item["dept2"] == ""

    def test_concurrent_multiplier_values(self, sample_data):
        """测试并发数返回正确（按并发数降序）"""
        data, total = IPRepository.list_with_user_info()

        # 并发数从高到低：1.0 + i * 0.5, i = 9..0
        expected = [round(1.0 + i * 0.5, 1) for i in range(9, -1, -1)]
        assert [item["concurrent_multiplier"] for item in data] == expected

    def test_vip_flag(self, sample_data):
        """测试VIP标志"""
        data, total = IPRepository.list_with_user_info()

        # 验证VIP标志存在
        for item in data:
            assert isinstance(item["vip"], bool)

    def test_pagination_with_filter(self, sample_data):
        """测试分页+筛选组合"""
        data, total = IPRepository.list_with_user_info(
            page=1,
            page_size=2,
            employee_no="EMP"
        )

        assert total == 5  # 总共5条匹配
        assert len(data) == 2  # 第一页返回2条
