from __future__ import annotations

import logging
from dataclasses import dataclass

from router.models import Ips, UserIP
from router.repositories.user_ips import UserIPRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestIdentity:
    """Resolved request identity.

    ``user_ip_id`` is the real ``user_ips.id`` backing this request, or ``0``
    when no identity could be resolved (no apikey, and the IP is not in
    ``user_ips``). ``employee_no`` is the resolved employee number, or empty.
    For an apikey identity it comes from the API-key-backed row, borrowing the
    IP-backed row's value when the key row has none (issue #287).
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
        apikey_user_ip = None
        if apikey:
            apikey_user_ip = UserIPRepository.get_active_by_apikey(apikey)
            if apikey_user_ip is None:
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
                # Unknown key: fall back to the IP identity (unchanged), but
                # leave a trace — a client presenting a credential the router
                # does not know is almost always a misconfiguration worth
                # surfacing (issue #287 field report).
                logger.warning(
                    "unknown Bearer apikey presented; falling back to IP identity (ip=%s)",
                    ip.ip,
                )

        if apikey_user_ip is not None:
            # Issue #287: the API-key row is the identity and its employee_no
            # takes precedence. A key row stored without an employee_no borrows
            # the IP-backed row's so admission, whitelist matching and external
            # routing still resolve one.
            employee_no = apikey_user_ip.employee_no or ""
            if not employee_no.strip():
                ip_user_ip = UserIPRepository.get_by_ip_id(ip.id)
                if ip_user_ip is not None:
                    employee_no = ip_user_ip.employee_no or ""
            return RequestIdentity(
                ip=ip,
                user_ip_id=apikey_user_ip.id,
                employee_no=employee_no,
                department_id=apikey_user_ip.department_id,
                is_vip=bool(apikey_user_ip.vip),
                is_apikey=True,
                user_charge=apikey_user_ip.user_charge or "",
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
