from __future__ import annotations

import logging
from typing import Any

from router.config import APP_CONFIG
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.route_algorithm.least_connection import LeastConnectionServerChooser

logger = logging.getLogger(__name__)


class VIPChannelService:
    def __init__(self):
        vip_config = APP_CONFIG.get("vip", {})
        self.cooldown_seconds = int(vip_config.get("cooldown_seconds", 300))
        self.min_normal_servers = int(vip_config.get("min_normal_servers", 2))
        self.workload_chooser = LeastConnectionServerChooser.for_server_workload()

    @staticmethod
    def is_vip_eligible(model) -> bool:
        if model is None:
            return False
        threshold = getattr(model, "vip", None)
        return threshold is not None and threshold > 0

    def select_candidates(self, model, estimate_tokens: int = 0) -> tuple[list[Any], bool]:
        """Pick server candidates for a VIP request and run scale-up.

        Returns ``(candidates, served_as_vip)``. ``served_as_vip`` is False only
        in the zero-VIP fallback when promoting would drop the normal pool below
        the configured floor.
        """
        ServerRepository.demote_expired_cooldowns(self.cooldown_seconds, model.id)

        threshold = int(model.vip or 0)
        vip_set = ServerRepository.list_by_model_id(model.id, vip=True, estimate_tokens=estimate_tokens)
        normal = ServerRepository.list_by_model_id(model.id, vip=False, estimate_tokens=estimate_tokens)

        if not vip_set:
            if len(normal) > self.min_normal_servers:
                promoted = self.workload_chooser.choose_least_loaded(normal)
                if ServerRepository.promote_to_vip(promoted):
                    return [promoted], True
                # Lost the race: re-list and continue.
                vip_set = ServerRepository.list_by_model_id(model.id, vip=True, estimate_tokens=estimate_tokens)
                normal = ServerRepository.list_by_model_id(model.id, vip=False, estimate_tokens=estimate_tokens)
                if not vip_set:
                    return normal, False
            else:
                return normal, False

        active = [s for s in vip_set if s.vip_cooldown is None]
        if not active:
            target = self.workload_chooser.choose_least_loaded(vip_set)
            ServerRepository.cancel_vip_cooldown(target)
            return [target], True

        total_load = RequestRepository.count_vip_processing(model.id)
        projected_avg = (total_load + 1) / len(active)

        if projected_avg > threshold:
            cooling = [s for s in vip_set if s.vip_cooldown is not None]
            if cooling:
                ServerRepository.cancel_vip_cooldown(cooling[0])
            elif len(normal) > self.min_normal_servers:
                promoted = self.workload_chooser.choose_least_loaded(normal)
                if ServerRepository.promote_to_vip(promoted):
                    vip_set.append(promoted)

        return vip_set, True

    def maybe_scale_down(self, model, estimate_tokens: int = 0) -> None:
        if not self.is_vip_eligible(model):
            return

        ServerRepository.demote_expired_cooldowns(self.cooldown_seconds, model.id)

        threshold = int(model.vip or 0)
        vip_set = ServerRepository.list_by_model_id(model.id, vip=True, estimate_tokens=estimate_tokens)
        if not vip_set:
            return

        total_load = RequestRepository.count_vip_processing(model.id)
        active = [s for s in vip_set if s.vip_cooldown is None]

        if total_load == 0:
            for server in active:
                ServerRepository.mark_vip_cooldown(server)
            return

        if not active:
            logger.error(
                "VIP scale-down: load=%d > 0 but every VIP server for model %s is cooling",
                total_load, model.id,
            )
            return

        if len(active) == 1:
            return

        projected = len(active) - 1
        if total_load / projected < threshold:
            ServerRepository.mark_vip_cooldown(self.workload_chooser.choose_least_loaded(active))
