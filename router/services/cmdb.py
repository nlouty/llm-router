from __future__ import annotations

import logging
import time

from router.config import APP_CONFIG
from router.repositories.ips import IPRepository

logger = logging.getLogger(__name__)


class CMDBService:
    """@brief Adapter between the router and the corporate CMDB source.

    The public implementation shipped here is a dummy: it only ensures ``ips``
    rows exist and never fetches real user data. Internal adapters override the
    lookup/persistence methods (``fetch_user_data``,
    ``fetch_user_data_by_employee_no``, ``fetch_and_save_apikey``) to talk to the
    real CMDB.
    """

    def __init__(self):
        self.interval = float(APP_CONFIG.get("cmdb", {}).get("refresh_interval_between_ips_seconds", 1))

    def fetch_and_save_user(self, ip: str) -> None:
        """@brief Ensure an ``ips`` row exists for the given address (dummy mode).

        Does not fetch user data; used for background IP provisioning at request
        time. A real CMDB adapter would also populate the matching IP-backed
        ``user_ips`` row here.

        @param ip Client IP address string, e.g. ``"10.0.0.1"``.
        @return None. Side effect: an ``ips`` row is created if it was missing.
        """
        try:
            IPRepository.get_or_create(ip)
        except Exception:
            logger.exception("dummy CMDB failed to ensure IP row for %s", ip)
            return
        logger.info("CMDB dummy mode: no user data fetched for %s", ip)

    def fetch_user_data(self, ip: str) -> dict | None:
        """@brief Look up a single IP-backed user in CMDB by IP address.

        Used to refresh IP-backed ``user_ips`` rows, where ``ip``/``ip_id`` are
        fixed and ``apikey`` is empty.

        @param ip Client IP address string to look up, e.g. ``"10.0.0.1"``.
        @retval dict User fields when the IP is known, shaped as::

                {
                    "user_name": str,       # display name, e.g. "Alice"
                    "user_charge": str,     # charge/role string, e.g. "lead"
                    "employee_no": str,     # employee number, e.g. "E0001"
                    "department_id": int | None,  # FK into departments, or None
                    "vip": bool,            # identity-based VIP flag
                }

        @retval None When the IP has no CMDB record.
        @throw NotImplementedError In the public dummy adapter; override in an
            internal adapter to enable IP-backed refresh.
        """
        raise NotImplementedError("fetch_user_data is not implemented in the public CMDB adapter")

    def fetch_user_data_by_employee_no(self, employee_no: str) -> dict | None:
        """@brief Look up an API-key-backed user in CMDB by employee number.

        Used to refresh API-key-backed ``user_ips`` rows, where ``apikey`` and
        ``employee_no`` are fixed and ``ip_id`` is ``0``. The lookup runs by
        employee number (the CMDB's natural key); the returned fields overwrite
        the mutable columns while ``apikey``/``employee_no`` stay unchanged.

        @param employee_no Employee number that owns the key, e.g. ``"E0001"``.
        @retval dict Same shape as ``fetch_user_data``: ``user_name``,
            ``user_charge``, ``employee_no``, ``department_id``, ``vip``.
        @retval None When the employee has no CMDB record.
        @throw NotImplementedError In the public dummy adapter; override in an
            internal adapter to enable API-key-backed refresh.
        """
        raise NotImplementedError("fetch_user_data_by_employee_no is not implemented in the public CMDB adapter")

    def fetch_and_save_apikey(self, apikey: str, employee_no: str) -> None:
        """@brief Register (or rotate) an employee API key.

        The internal adapter owns employee lookup, department data, VIP
        inheritance, idempotency, conflict handling, and key rotation; it is
        expected to persist a ``user_ips`` API-key-backed row directly.

        @param apikey Non-empty API key string (max 255 chars).
        @param employee_no Employee number that owns the key (max 50 chars).
        @return None. Side effect: a ``user_ips`` API-key-backed row
            (``ip_id = 0``) is created or updated.
        @throw NotImplementedError In the public dummy adapter.
        @throw LookupError When ``employee_no`` is not found in CMDB (raised by
            internal adapters; surfaced as HTTP 404 by the registration API).
        """
        raise NotImplementedError("API key registration is not implemented in the public CMDB adapter")

    def fetch_all_users(self) -> None:
        """@brief Ensure ``ips`` rows exist for every active IP (dummy mode).

        Iterates all non-deleted IPs and calls ``fetch_and_save_user`` on each,
        pausing ``cmdb.refresh_interval_between_ips_seconds`` between lookups.

        @return None.
        """
        for row in IPRepository.all_active():
            self.fetch_and_save_user(row.ip)
            time.sleep(self.interval)
