from __future__ import annotations

from dataclasses import dataclass

from router.models import Ips, UserIP
from router.repositories.user_ips import UserIPRepository


@dataclass(frozen=True)
class RequestIdentity:
    """Resolved request identity.

    ``user_ip_id`` is the real ``user_ips.id`` backing this request, or ``0``
    when no identity could be resolved (no apikey, and the IP is not in
    ``user_ips``). ``employee_no`` is the resolved employee number, or empty.
    ``user_charge`` is the person-in-charge name from the ``user_ips`` row
    (matched against ``whitelist.user_name`` by admission), or empty.
    ``is_vip`` is True only when the backing ``user_ips`` row is VIP.
    ``invalid_apikey`` is True when the request presented a Bearer key that
    exists in ``user_ips`` but is no longer valid (``is_valid = false``);
    such keys are refused by the proxy instead of silently downgrading to
    the weaker IP-based admission.
    """

    ip: Ips
    user_ip_id: int
    employee_no: str
    department_id: int | None
    is_vip: bool
    is_apikey: bool
    user_charge: str = ""
    invalid_apikey: bool = False

    @property
    def has_employee(self) -> bool:
        return bool(self.employee_no)


def _extract_bearer(request) -> str | None:
    header = request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


class IdentityService:
    @staticmethod
    def resolve(request, ip: Ips) -> RequestIdentity:
        apikey = _extract_bearer(request)
        if apikey:
            user_ip = UserIPRepository.get_active_by_apikey(apikey)
            if user_ip is not None:
                return IdentityService._identity_from(ip, user_ip, is_apikey=True)
            if UserIPRepository.get_by_apikey(apikey) is not None:
                # Known but invalidated key (failed the department/whitelist
                # check at registration): refuse it below instead of falling
                # back to the IP identity, which a weaker path could allow.
                return RequestIdentity(
                    ip=ip,
                    user_ip_id=0,
                    employee_no="",
                    department_id=None,
                    is_vip=False,
                    is_apikey=False,
                    invalid_apikey=True,
                )

        user_ip = UserIPRepository.get_by_ip_id(ip.id)
        if user_ip is not None:
            return IdentityService._identity_from(ip, user_ip, is_apikey=False)

        return RequestIdentity(
            ip=ip,
            user_ip_id=0,
            employee_no="",
            department_id=None,
            is_vip=False,
            is_apikey=False,
        )

    @staticmethod
    def _identity_from(ip: Ips, user_ip: UserIP, *, is_apikey: bool) -> RequestIdentity:
        return RequestIdentity(
            ip=ip,
            user_ip_id=user_ip.id,
            employee_no=user_ip.employee_no or "",
            department_id=user_ip.department_id,
            is_vip=bool(user_ip.vip),
            is_apikey=is_apikey,
            user_charge=user_ip.user_charge or "",
        )
