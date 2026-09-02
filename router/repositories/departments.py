from __future__ import annotations

from datetime import datetime
from django.utils import timezone

from router.models import Department


class DepartmentRepository:
    @staticmethod
    def get(department_id: int | None) -> Department | None:
        if department_id is None:
            return None
        return Department.objects.filter(id=department_id, deleted_at__isnull=True).first()

    @staticmethod
    def get_by_manager(manager: str) -> Department | None:
        return Department.objects.filter(manager=manager, deleted_at__isnull=True).first()

    @staticmethod
    def get_or_create(
        dept1: str = "",
        dept2: str = "",
        dept3: str = "",
        dept4: str = "",
    ) -> tuple[Department, bool]:
        now = timezone.now()
        return Department.objects.get_or_create(
            dept1=dept1,
            dept2=dept2,
            dept3=dept3,
            dept4=dept4,
            defaults={"created_at": now, "updated_at": now},
        )

    @staticmethod
    def get_cascade(
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        """
        获取部门级联数据。

        Args:
            start: 开始时间（可选，用于筛选有时间范围内访问记录的部门）
            end: 结束时间（可选，用于筛选有时间范围内访问记录的部门）

        Returns:
            级联部门数据，格式为：
            {
                "options": [
                    {
                        "value": "技术部",
                        "label": "技术部",
                        "children": [
                            {
                                "value": "研发中心",
                                "label": "研发中心",
                                "children": [...]
                            }
                        ]
                    }
                ]
            }
        """
        from router.models import UserIP, Ips
        from router.repositories.requests import RequestRepository

        # 获取所有有效部门
        queryset = Department.objects.filter(deleted_at__isnull=True)

        # 如果有时间范围，筛选有访问记录的部门
        if start and end:
            # 获取时间范围内有成功请求的 IP
            ip_ids_with_requests = (
                RequestRepository.external_requests()
                .filter(
                    send_time__gte=start,
                    send_time__lte=end,
                    task_status="success",
                    ip_id__isnull=False,
                    ip_id__gt=0,
                )
                .values_list("ip_id", flat=True)
                .distinct()
            )

            # 获取这些 IP 关联的部门
            dept_ids = UserIP.objects.filter(
                ip_id__in=ip_ids_with_requests,
                is_valid=True,
                deleted_at__isnull=True,
                department_id__isnull=False,
            ).values_list("department_id", flat=True).distinct()

            queryset = queryset.filter(id__in=dept_ids)

        # 构建级联数据结构
        departments = queryset.values("dept1", "dept2", "dept3", "dept4")

        # 使用字典构建级联树
        dept1_map = {}
        for dept in departments:
            d1 = dept["dept1"] or ""
            d2 = dept["dept2"] or ""
            d3 = dept["dept3"] or ""
            d4 = dept["dept4"] or ""

            if not d1:
                continue

            # 构建 dept1 层级
            if d1 not in dept1_map:
                dept1_map[d1] = {"value": d1, "label": d1, "children": {}}

            # 构建 dept2 层级
            if d2 and d2 not in dept1_map[d1]["children"]:
                dept1_map[d1]["children"][d2] = {"value": d2, "label": d2, "children": {}}

            # 构建 dept3 层级
            if d2 and d3 and d3 not in dept1_map[d1]["children"][d2]["children"]:
                dept1_map[d1]["children"][d2]["children"][d3] = {"value": d3, "label": d3, "children": []}

            # 构建 dept4 层级
            if d2 and d3 and d4:
                dept4_item = {"value": d4, "label": d4}
                # 检查是否已存在
                existing = dept1_map[d1]["children"][d2]["children"][d3]["children"]
                if dept4_item not in existing:
                    dept1_map[d1]["children"][d2]["children"][d3]["children"].append(dept4_item)

        # 转换为列表格式
        options = []
        for d1_key in sorted(dept1_map.keys()):
            d1_item = dept1_map[d1_key]
            d1_result = {"value": d1_key, "label": d1_key, "children": []}

            for d2_key in sorted(d1_item["children"].keys()):
                d2_item = d1_item["children"][d2_key]
                d2_result = {"value": d2_key, "label": d2_key, "children": []}

                for d3_key in sorted(d2_item["children"].keys()):
                    d3_item = d2_item["children"][d3_key]
                    d3_result = {"value": d3_key, "label": d3_key, "children": d3_item["children"]}
                    d2_result["children"].append(d3_result)

                # 如果 dept3 为空，则 children 为空列表
                if not d2_result["children"]:
                    d2_result["children"] = []
                d1_result["children"].append(d2_result)

            # 如果 dept2 为空，则 children 为空列表
            if not d1_result["children"]:
                d1_result["children"] = []
            options.append(d1_result)

        return {"options": options}
