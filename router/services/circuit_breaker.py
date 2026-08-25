from __future__ import annotations

from router.config import APP_CONFIG
from router.models import Server
from router.repositories.servers import ServerRepository
from router.services.request_context import get_llm_choosing_deadline


class CircuitBreakerService:
    def __init__(self):
        cb_config = APP_CONFIG.get("load_balancer", {}).get("circuit_breaker", {})
        self.failure_threshold = int(cb_config.get("failure_threshold", 3))
        self.base_cooldown_seconds = int(cb_config.get("base_cooldown_seconds", 30))
        self.max_cooldown_seconds = int(cb_config.get("max_cooldown_seconds", 3000))
        self.half_open_probe_limit = max(1, int(cb_config.get("half_open_probe_limit", 1)))

    def record_failure(self, server: Server) -> None:
        """Record a failure. Opens the circuit if threshold is reached."""
        # llm-choosing (routing-model) requests are capped at
        # llm_choosing_timeout_seconds, so a routing server busy prefilling
        # large client requests fails the choosing call at the deadline
        # without being unhealthy. Their failures must not accumulate into
        # consecutive_failures, or a few slow probes open the circuit and
        # drop the server from every pool while normal traffic still works.
        # Successes still count (record_success below): a completed probe
        # proves the server alive and is the in-band recovery path for
        # routing servers whose only traffic is choosing requests.
        if get_llm_choosing_deadline() is not None:
            return
        ServerRepository.record_failure(
            server,
            failure_threshold=self.failure_threshold,
            base_cooldown_seconds=self.base_cooldown_seconds,
            max_cooldown_seconds=self.max_cooldown_seconds,
        )

    def record_success(self, server: Server) -> None:
        """Record a success. Resets failure counter and closes the circuit."""
        ServerRepository.record_success(server, base_cooldown_seconds=self.base_cooldown_seconds)
