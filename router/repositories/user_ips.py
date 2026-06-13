from __future__ import annotations

from django.utils import timezone

from router.models import UserIP


class UserIPRepository:
    @staticmethod
    def get_by_ip_id(ip_id: int) -> UserIP | None:
        return UserIP.objects.filter(ip_id=ip_id, is_valid=True, deleted_at__isnull=True).first()

    @staticmethod
    def create_or_update(
        ip_id: int,
        user_name: str = "",
        user_charge: str = "",
        employee_no: str = "",
        department_id: int | None = None,
    ) -> UserIP:
        now = timezone.now()
        obj, created = UserIP.objects.get_or_create(
            ip_id=ip_id,
            defaults={
                "user_name": user_name,
                "user_charge": user_charge,
                "employee_no": employee_no,
                "department_id": department_id,
                "is_valid": True,
                "created_at": now,
                "updated_at": now,
            },
        )
        if not created:
            obj.user_name = user_name
            obj.user_charge = user_charge
            obj.employee_no = employee_no
            obj.department_id = department_id
            obj.is_valid = True
            obj.updated_at = now
            obj.save(update_fields=["user_name", "user_charge", "employee_no", "department_id", "is_valid", "updated_at"])
        return obj
