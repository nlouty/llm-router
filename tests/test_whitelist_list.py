"""
测试白名单列表接口
"""
import pytest
from django.utils import timezone
from router.models import Whitelist
from router.repositories.whitelist import WhitelistRepository


class TestWhitelistList:
    """测试白名单列表接口"""

    @pytest.fixture
    def sample_whitelist_data(self, db):
        """创建测试数据"""
        now = timezone.now()
        items = []
        for i in range(15):
            item = Whitelist.objects.create(
                employee_no=f"EMP{i:03d}",
                user_name=f"测试用户{i}",
                is_allowed=1 if i % 2 == 0 else 0,
                update_time=now
            )
            items.append(item)
        return items

    def test_list_all_without_pagination(self, sample_whitelist_data):
        """测试不分页返回全量数据"""
        data, total = WhitelistRepository.list_all()

        # 验证返回所有数据
        assert total == 15
        assert len(data) == 15

        # 验证数据结构
        assert "id" in data[0]
        assert "employee_no" in data[0]
        assert "user_name" in data[0]
        assert "is_allowed" in data[0]
        assert "update_time" in data[0]

    def test_list_all_with_pagination(self, sample_whitelist_data):
        """测试分页查询"""
        # 第一页，每页5条
        data, total = WhitelistRepository.list_all(page=1, page_size=5)

        assert total == 15
        assert len(data) == 5

        # 第二页
        data, total = WhitelistRepository.list_all(page=2, page_size=5)

        assert total == 15
        assert len(data) == 5

        # 第三页
        data, total = WhitelistRepository.list_all(page=3, page_size=5)

        assert total == 15
        assert len(data) == 5

        # 第四页（超出范围）
        data, total = WhitelistRepository.list_all(page=4, page_size=5)

        assert total == 15
        assert len(data) == 0

    def test_list_all_empty_database(self, db):
        """测试空数据库"""
        data, total = WhitelistRepository.list_all()

        assert total == 0
        assert len(data) == 0

    def test_list_all_includes_expire_time(self, db):
        """测试返回数据包含到期时间"""
        Whitelist.objects.create(
            employee_no="E001",
            is_allowed=1,
            expire_time=timezone.datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.get_current_timezone()),
            update_time=timezone.now(),
        )

        data, _total = WhitelistRepository.list_all()

        assert data[0]["expire_time"] == "2026-12-31 23:59:59"

    def test_list_all_order(self, sample_whitelist_data):
        """测试数据按更新时间倒序排列"""
        # 更新一条记录的时间
        item = Whitelist.objects.get(employee_no="EMP005")
        item.update_time = timezone.now()
        item.save()

        data, total = WhitelistRepository.list_all()

        # 第一条应该是刚更新的记录
        assert data[0]["employee_no"] == "EMP005"

    def test_data_format(self, sample_whitelist_data):
        """测试返回数据格式"""
        data, total = WhitelistRepository.list_all(page=1, page_size=1)

        assert len(data) == 1
        item = data[0]

        # 验证字段类型
        assert isinstance(item["id"], int)
        assert isinstance(item["employee_no"], str)
        assert isinstance(item["user_name"], str)
        assert isinstance(item["is_allowed"], int) or item["is_allowed"] is None
        assert isinstance(item["update_time"], str) or item["update_time"] is None

        # 验证时间格式
        if item["update_time"]:
            # 应该是 YYYY-MM-DD HH:MM:SS 格式
            assert len(item["update_time"]) == 19
            assert item["update_time"][10] == " "
