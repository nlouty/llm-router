from __future__ import annotations

from django.utils import timezone

from router.models import Ips


class IPRepository:
    @staticmethod
    def get_or_create(ip: str) -> tuple[Ips, bool]:
        now = timezone.now()
        return Ips.objects.get_or_create(
            ip=ip,
            defaults={"concurrent_multiplier": 1.0, "vip": False, "created_at": now, "updated_at": now},
        )

    @staticmethod
    def all_active() -> list[Ips]:
        return list(Ips.objects.filter(deleted_at__isnull=True).order_by("id"))

    @staticmethod
    def get_by_ip(ip: str) -> Ips | None:
        return Ips.objects.filter(ip=ip, deleted_at__isnull=True).first()

    @staticmethod
    def update_concurrent_multiplier(ip_id: int, multiplier: float) -> Ips:
        # TODO(#301 phase 2): deprecated — admission ignores the column this
        # writes. Remove together with the column drop.
        now = timezone.now()
        ip = Ips.objects.get(id=ip_id)
        ip.concurrent_multiplier = multiplier
        ip.updated_at = now
        ip.save(update_fields=["concurrent_multiplier", "updated_at"])
        return ip

    @staticmethod
    def list_with_user_info(
        page: int | None = None,
        page_size: int | None = None,
        employee_no: str | None = None,
        ip: str | None = None,
    ) -> tuple[list[dict], int]:
        """
        查询IP列表及关联的用户和部门信息，支持分页和筛选。

        Args:
            page: 页码（从1开始），None表示返回全量数据
            page_size: 每页条数，None表示返回全量数据
            employee_no: 员工工号筛选条件，None表示不筛选
            ip: IP地址筛选条件，None表示不筛选

        Returns:
            (数据列表, 总记录数)
            结果按并发数(concurrent_multiplier)从高到低排序
        """
        from router.models import UserIP, Department

        # 基础查询：从ips表开始
        queryset = Ips.objects.filter(deleted_at__isnull=True)

        # IP筛选
        if ip:
            queryset = queryset.filter(ip__icontains=ip)

        # 如果有employee_no筛选，需要先找到对应的ip_id
        if employee_no:
            user_ips = UserIP.objects.filter(
                employee_no__icontains=employee_no,
                is_valid=True,
                deleted_at__isnull=True
            ).values_list('ip_id', flat=True)
            queryset = queryset.filter(id__in=user_ips)

        # 获取总数
        total = queryset.count()

        # 排序：按并发数从高到低
        queryset = queryset.order_by('-concurrent_multiplier')

        # 如果提供了分页参数，应用分页
        if page is not None and page_size is not None:
            offset = (page - 1) * page_size
            queryset = queryset[offset:offset + page_size]

        # 获取所有ip_id
        ip_ids = [item.id for item in queryset]

        # 查询关联的user_ips
        user_ips_map = {}
        if ip_ids:
            user_ips_query = UserIP.objects.filter(
                ip_id__in=ip_ids,
                is_valid=True,
                deleted_at__isnull=True
            )
            for user_ip in user_ips_query:
                user_ips_map[user_ip.ip_id] = {
                    "employee_no": user_ip.employee_no,
                    "user_name": user_ip.user_name,
                    "user_charge": user_ip.user_charge,
                    "department_id": user_ip.department_id,
                }

        # 查询所有涉及的部门
        dept_ids = [info["department_id"] for info in user_ips_map.values() if info["department_id"]]
        departments_map = {}
        if dept_ids:
            departments_query = Department.objects.filter(id__in=dept_ids, deleted_at__isnull=True)
            for dept in departments_query:
                departments_map[dept.id] = {
                    "dept1": dept.dept1,
                    "dept2": dept.dept2,
                    "dept3": dept.dept3,
                    "dept4": dept.dept4,
                }

        # 组装结果
        data = []
        for item in queryset:
            user_info = user_ips_map.get(item.id, {})
            department_id = user_info.get("department_id")
            dept_info = departments_map.get(department_id, {}) if department_id else {}

            data.append({
                "id": item.id,
                "ip": item.ip,
                "concurrent_multiplier": item.concurrent_multiplier,
                "vip": item.vip,
                "employee_no": user_info.get("employee_no", ""),
                "user_name": user_info.get("user_name", ""),
                "user_charge": user_info.get("user_charge", ""),
                "dept1": dept_info.get("dept1", ""),
                "dept2": dept_info.get("dept2", ""),
                "dept3": dept_info.get("dept3", ""),
                "dept4": dept_info.get("dept4", ""),
            })

        return data, total
