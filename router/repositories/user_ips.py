from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from router.models import UserIP


class UserIPRepository:
    @staticmethod
    def get_by_ip_id(ip_id: int) -> UserIP | None:
        return UserIP.objects.filter(ip_id=ip_id, is_valid=True, deleted_at__isnull=True).first()

    @staticmethod
    def get_by_employee_no(employee_no: str) -> UserIP | None:
        return UserIP.objects.filter(employee_no=employee_no, is_valid=True, deleted_at__isnull=True).first()

    @staticmethod
    def get_ip_backed_by_employee_no(employee_no: str) -> UserIP | None:
        return UserIP.objects.filter(
            employee_no=employee_no,
            ip_id__gt=0,
            is_valid=True,
            deleted_at__isnull=True,
        ).first()

    @staticmethod
    def exists_by_ip_id(ip_id: int) -> bool:
        return UserIP.objects.filter(ip_id=ip_id, deleted_at__isnull=True).exists()

    @staticmethod
    def all_active_apikeys() -> list[UserIP]:
        return list(
            UserIP.objects.filter(
                ip_id=0,
                is_valid=True,
                deleted_at__isnull=True,
            ).exclude(apikey="").order_by("id")
        )

    @staticmethod
    def get_active_apikey_by_employee_no(employee_no: str) -> UserIP | None:
        return UserIP.objects.filter(
            employee_no=employee_no,
            is_valid=True,
            deleted_at__isnull=True,
        ).filter(~Q(apikey="")).first()

    @staticmethod
    def get_active_by_apikey(apikey: str) -> UserIP | None:
        return UserIP.objects.filter(
            apikey=apikey,
            is_valid=True,
            deleted_at__isnull=True,
        ).first()

    @staticmethod
    def create_or_update_apikey(
        apikey: str,
        employee_no: str,
        user_name: str = "",
        user_charge: str = "",
        department_id: int | None = None,
        vip: bool = False,
    ) -> UserIP:
        now = timezone.now()
        obj, created = UserIP.objects.get_or_create(
            apikey=apikey,
            defaults={
                "ip_id": 0,
                "employee_no": employee_no,
                "user_name": user_name,
                "user_charge": user_charge,
                "department_id": department_id,
                "vip": vip,
                "is_valid": True,
                "created_at": now,
                "updated_at": now,
            },
        )
        if not created:
            obj.employee_no = employee_no
            obj.user_name = user_name
            obj.user_charge = user_charge
            obj.department_id = department_id
            obj.vip = vip
            obj.is_valid = True
            obj.updated_at = now
            obj.save(update_fields=["employee_no", "user_name", "user_charge", "department_id", "vip", "is_valid", "updated_at"])
        return obj

    @staticmethod
    def create_or_update(
        ip_id: int,
        user_name: str = "",
        user_charge: str = "",
        employee_no: str = "",
        department_id: int | None = None,
        vip: bool = False,
    ) -> UserIP:
        now = timezone.now()
        obj, created = UserIP.objects.get_or_create(
            ip_id=ip_id,
            defaults={
                "user_name": user_name,
                "user_charge": user_charge,
                "employee_no": employee_no,
                "department_id": department_id,
                "vip": vip,
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
            obj.vip = vip
            obj.is_valid = True
            obj.updated_at = now
            obj.save(update_fields=["user_name", "user_charge", "employee_no", "department_id", "vip", "is_valid", "updated_at"])
        return obj
