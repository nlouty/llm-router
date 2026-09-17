from __future__ import annotations

import random
from typing import Any, Callable, Sequence

from router.route_algorithm.base import ServerSelectionContext
from router.utils.target import server_target


def effective_weight(server: Any) -> int:
    """Server capacity multiplier. Falls back to 1 for missing/invalid values."""
    weight = getattr(server, "weight", 1)
    try:
        weight = int(weight)
    except (TypeError, ValueError):
        weight = 1
    return weight if weight >= 1 else 1


class LeastConnectionServerChooser:
    def __init__(
        self,
        count_provider: Callable[[list[str]], dict[str, int]] | None = None,
        server_count_provider: Callable[[Sequence[Any]], dict[int, int]] | None = None,
    ):
        self.count_provider = count_provider
        self.server_count_provider = server_count_provider

    @classmethod
    def for_server_workload(cls) -> "LeastConnectionServerChooser":
        return cls(server_count_provider=cls._server_workload_counts)

    def choose(
        self,
        candidates: Sequence[Any],
        context: ServerSelectionContext,
        attempted_server_ids: set[int],
    ) -> Any | None:
        return self.choose_least_loaded(candidates, attempted_server_ids)

    def choose_least_loaded(
        self,
        candidates: Sequence[Any],
        attempted_server_ids: set[int] | None = None,
    ) -> Any | None:
        attempted_server_ids = attempted_server_ids or set()
        available = [server for server in candidates if server.id not in attempted_server_ids]
        return self._choose_least_loaded(available)

    def on_response(self, server: Any, context: ServerSelectionContext, status_code: int) -> None:
        return None

    def _choose_least_loaded(self, available: Sequence[Any]) -> Any | None:
        if not available:
            return None
        load_counts = self._load_counts(available)
        self._log_connection_counts(available, load_counts)
        return self._pick_least_loaded(available, load_counts)

    @staticmethod
    def _normalized_load(server: Any, load_counts: dict[int, int]) -> float:
        return load_counts.get(server.id, 0) / effective_weight(server)

    @staticmethod
    def _pick_least_loaded(servers: Sequence[Any], load_counts: dict[int, int]) -> Any:
        if not servers:
            return None
        min_load = min(LeastConnectionServerChooser._normalized_load(s, load_counts) for s in servers)
        least_loaded = [
            server
            for server in servers
            if LeastConnectionServerChooser._normalized_load(server, load_counts) == min_load
        ]
        return random.choice(least_loaded)

    def _load_counts(self, available: Sequence[Any]) -> dict[int, int]:
        if self.server_count_provider:
            return self.server_count_provider(available)
        targets = {server.id: server_target(server) for server in available}
        processing_counts = self._count_processing(list(targets.values()))
        return {
            server.id: processing_counts.get(targets[server.id], 0)
            for server in available
        }

    @staticmethod
    def _server_workload_counts(servers: Sequence[Any]) -> dict[int, int]:
        return {
            server.id: int(getattr(server, "workload", 0) or 0)
            for server in servers
        }

    def _log_connection_counts(self, available: Sequence[Any], load_counts: dict[int, int]) -> None:
        return None

    def _count_processing(self, targets: list[str]) -> dict[str, int]:
        if self.count_provider:
            return self.count_provider(targets)
        from router.repositories.requests import RequestRepository

        return RequestRepository.count_processing_by_targets(targets)
