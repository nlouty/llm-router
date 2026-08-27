from __future__ import annotations

from urllib.parse import urljoin

import requests

from router.config import APP_CONFIG
from router.models import Server
from router.repositories.servers import ServerRepository
from router.services.circuit_breaker import CircuitBreakerService
from router.utils.headers import build_upstream_headers


class ServerHealthService:
    def __init__(self):
        lb_config = APP_CONFIG.get("load_balancer", {})
        self.timeout = float(lb_config.get("health_check_timeout_seconds", 2))
        self.circuit_breaker = CircuitBreakerService()

    def mark_failure(self, server: Server, reason: str) -> None:
        self.circuit_breaker.record_failure(server)

    def check_once(self, server: Server, recover_offline: bool = False) -> bool:
        url = urljoin(server.base_url.rstrip("/") + "/", (server.health_path or "/healthy").lstrip("/"))
        try:
            response = requests.get(url, timeout=self.timeout, headers=build_upstream_headers({}, server))
        except requests.RequestException:
            ServerRepository.mark_checked(server)
            if server.is_online:
                self.circuit_breaker.record_failure(server)
            return False

        if 200 <= response.status_code < 300:
            if server.is_online:
                self.circuit_breaker.record_success(server)
            elif recover_offline:
                self.circuit_breaker.record_success(server)
            else:
                ServerRepository.mark_checked(server)
            return True

        ServerRepository.mark_checked(server)
        if server.is_online:
            self.circuit_breaker.record_failure(server)
        return False
