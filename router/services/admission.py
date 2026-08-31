from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import ClassVar

from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Ips, Model
from router.repositories.departments import DepartmentRepository
from router.repositories.requests import RequestRepository
from router.repositories.whitelist import WhitelistRepository


@dataclass
class AdmissionResult:
    allowed: bool
    status_code: int = 200
    error_type: str | None = None
    message: str | None = None
    current: int | None = None
    limit: int | None = None


class AdmissionService:
    # model_id -> last_cleanup_timestamp
    _last_cleanup: ClassVar[dict[int, float]] = {}
    _cleanup_throttle_seconds: ClassVar[int] = 10

    def __init__(self):
        self.allow_missing_user_info = bool(APP_CONFIG.get("admission", {}).get("allow_when_user_info_missing", True))
        self.stale_minutes = int(APP_CONFIG.get("proxy", {}).get("stale_processing_minutes", 20))
        self.unknown_model_max_tokens = int(APP_CONFIG.get("proxy", {}).get("unknown_model_max_tokens", 20480))
        self.auto_max_tokens = int(APP_CONFIG.get("proxy", {}).get("auto_max_tokens", 40000))

    def check_permission(self, identity) -> AdmissionResult:
        """Permission decision for a non-apikey identity.

        Complete identities (``user_ips`` row with ``employee_no`` and a
        resolvable department) follow the department chain: an allowed
        department passes, otherwise the whitelist decides. Incomplete
        identities (no ``user_ips`` row, no ``employee_no``, or a department
        CMDB could not resolve — NULL or unknown, including the
        whitelist-bypass ``0``) are rescued only by the whitelist, then by
        ``admission.allow_when_user_info_missing``.
        """
        if self._user_info_incomplete(identity):
            if self._whitelist_allows(identity):
                return AdmissionResult(True)
            return AdmissionResult(True) if self.allow_missing_user_info else self._permission_denied()

        department = DepartmentRepository.get(identity.department_id)
        if department is not None and department.is_allowed == 1:
            return AdmissionResult(True)
        if self._whitelist_allows(identity):
            return AdmissionResult(True)
        return self._permission_denied()

    @staticmethod
    def _user_info_incomplete(identity) -> bool:
        if identity.user_ip_id == 0:
            # The IP has no user_ips row (CMDB does not know it).
            return True
        if not identity.has_employee:
            return True
        if identity.department_id is None:
            return True
        # A department_id that resolves to no departments row (0 from the
        # whitelist apikey bypass, or a stale id) counts as unresolved too.
        return DepartmentRepository.get(identity.department_id) is None

    @staticmethod
    def _whitelist_allows(identity) -> bool:
        if WhitelistRepository.is_allowed(identity.employee_no):
            return True
        return bool(identity.user_charge) and WhitelistRepository.is_allowed_user_name(identity.user_charge)

    def check_max_tokens(self, requested: int | None, model: Model | None, is_auto: bool = False) -> AdmissionResult:
        if requested is None:
            return AdmissionResult(True)
        if is_auto:
            # The target model is not known until auto routing resolves it, so
            # cap auto requests with a global limit instead of the entrance.
            maximum = self.auto_max_tokens
        else:
            maximum = model.max_tokens if model else self.unknown_model_max_tokens
        if requested > maximum:
            return AdmissionResult(
                False,
                400,
                "invalid_request_error",
                f"The request tries to generate too many tokens: requested {requested}, Max allowed is {maximum}. "
                "Please lower max_completion_tokens, max_tokens, or Max Output Tokens in your client.",
            )
        return AdmissionResult(True)

    def check_concurrency(self, ip: Ips, model: Model | None, is_auto: bool = False) -> AdmissionResult:
        if model is None and not is_auto:
            return AdmissionResult(True)

        if model is None:
            # Literal "auto" entrance. In-flight records keep model_id = 0
            # before resolution and "auto:..." prefix after, so both map here.
            limit_base = int(APP_CONFIG.get("router", {}).get("auto_concurrent_limit", 6))
            matches_entrance = self._entrance_is_auto
        else:
            # Concrete model by name (whether or not it is also auto=true):
            # the entrance is the requested model. Before resolution records
            # sit at this model_id with a NULL router_result; after resolution
            # they carry a "<name>:..." prefix. Either way they map here.
            limit_base = model.concurrent_limit
            name_cf = model.model_name.casefold()
            matches_entrance = lambda r: self._entrance_matches(r, name_cf, model.id)

        if limit_base is None:
            return AdmissionResult(True)

        # Concurrency cleanup is still keyed by the entrance model_id (0 for auto).
        model_id_for_cleanup = model.id if model else 0
        now = time.time()
        last_run = self._last_cleanup.get(model_id_for_cleanup, 0)
        if now - last_run > self._cleanup_throttle_seconds:
            RequestRepository.cleanup_stale(model_id=model_id_for_cleanup, threshold_minutes=self.stale_minutes)
            self._last_cleanup[model_id_for_cleanup] = now

        limit = self.compute_concurrent_limit(ip, limit_base)

        current = self._count_inflight(ip.id, matches_entrance)

        if current >= limit:
            cleaned = RequestRepository.cleanup_stale(model_id=model_id_for_cleanup, threshold_minutes=self.stale_minutes, ip_id=ip.id)
            if cleaned > 0:
                current = self._count_inflight(ip.id, matches_entrance)

        if current >= limit:
            return AdmissionResult(
                False,
                429,
                "concurrent_limit_exceeded",
                f"Current concurrency ({current}) has reached the limit ({limit})",
                current,
                limit,
            )
        return AdmissionResult(True)

    @staticmethod
    def off_peak_boost_active(beijing_time=None) -> bool:
        """True during the 4x concurrency boost window.

        23:00–08:00 Beijing time every day, Saturdays from 18:00, or all day
        Sunday. ``beijing_time`` defaults to the current local time (the
        project timezone) so callers can pin the clock in tests.
        """
        if beijing_time is None:
            beijing_time = timezone.localtime()
        wd = beijing_time.weekday()  # Monday=0 ... Sunday=6
        return (
            beijing_time.hour < 8
            or beijing_time.hour >= 23
            or (wd == 5 and beijing_time.hour >= 18)
            or wd == 6
        )

    @staticmethod
    def compute_concurrent_limit(ip: Ips, limit_base: int | None, beijing_time=None) -> int | None:
        """Effective per-IP concurrency ceiling for a model with ``limit_base``.

        The exact formula enforced by :meth:`check_concurrency`: the base
        limit scaled by the IP's ``concurrent_multiplier``, multiplied by 4
        during the off-peak boost window. ``None`` when no base limit is set
        (no ceiling). Shared with the capability endpoint so the advertised
        limit can never drift from what admission actually enforces.
        """
        if limit_base is None:
            return None
        limit = max(1, math.ceil(limit_base * (ip.concurrent_multiplier or 1.0)))
        if AdmissionService.off_peak_boost_active(beijing_time):
            limit *= 4
        return limit

    @staticmethod
    def _count_inflight(ip_id: int, predicate) -> int:
        return sum(1 for row in RequestRepository.list_processing_for_concurrency(ip_id) if predicate(row))

    @staticmethod
    def _entrance_name(router_result: str | None) -> str | None:
        if not router_result:
            return None
        return router_result.split(":", 1)[0].casefold()

    @classmethod
    def _entrance_is_auto(cls, row: dict) -> bool:
        prefix = cls._entrance_name(row.get("router_result"))
        if prefix is not None:
            return prefix == "auto"
        # Unresolved auto request: model_id is 0 until resolution.
        return row.get("model_id") == 0

    @classmethod
    def _entrance_matches(cls, row: dict, name_cf: str, model_id: int) -> bool:
        prefix = cls._entrance_name(row.get("router_result"))
        if prefix is not None:
            return prefix == name_cf
        # Unresolved direct request for this model: NULL router_result, its own model_id.
        return row.get("model_id") == model_id

    @staticmethod
    def _permission_denied() -> AdmissionResult:
        return AdmissionResult(False, 403, "permission_denied", "Access denied, you do not have permission")
