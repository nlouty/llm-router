from __future__ import annotations

from router.config import APP_CONFIG
from router.models import Ips
from router.repositories.external import ExternalRouteRepository
from router.repositories.models import ModelRepository
from router.repositories.servers import ServerRepository
from router.services.admission import AdmissionService


class ModelCatalogService:
    """Per-user, per-port model capability payload served at ``GET /v1/models``.

    The response is OpenAI-compatible (``object: "list"`` with per-model
    entries) plus extra capability fields per model:

    - ``max_context``: the largest ``servers.context_window`` among the
      model's online servers (``null`` = unlimited).
    - ``max_output_tokens``: the output-token ceiling admission enforces
      for the model (``models.max_tokens``).
    - ``concurrent_limit``: the per-IP concurrency ceiling admission
      enforces right now (base ``concurrent_limit`` scaled by the IP's
      ``concurrent_multiplier`` and the off-peak 4x boost). ``null`` on the
      VIP port, where the concurrency check is skipped.

    A synthetic ``auto`` entry reports the auto-routing entrance: the global
    ``auto_max_tokens`` cap, a context ceiling every possible auto target can
    honor, and the auto concurrency ceiling. Because auto picks its target at
    request time, advertising one target's window could mislead users when
    another target is chosen, so the entry advertises the smallest max-context
    among all models auto may redirect to.

    The payload exposes only the model list, the caller's IP, and per-model
    capabilities — gateway internals (port, VIP channel state, multiplier,
    boost window) are intentionally not included.
    """

    def __init__(self):
        proxy_config = APP_CONFIG.get("proxy", {})
        self.auto_max_tokens = int(proxy_config.get("auto_max_tokens", 40000))
        self.auto_concurrent_limit = int(
            APP_CONFIG.get("router", {}).get("auto_concurrent_limit", 6)
        )

    def capabilities(
        self,
        ip: Ips,
        is_vip_channel: bool,
        employee_no: str | None = None,
    ) -> dict:
        # Deprecation is an access-control word on the normal port: deprecated
        # models are blocked there but still served to VIP users / auto routing.
        models = [
            model
            for model in ModelRepository.list_online()
            if is_vip_channel or not model.deprecation
        ]
        data = [self._model_entry(model, ip, is_vip_channel) for model in models]
        data.append(self._auto_entry(ip, is_vip_channel))
        # Issue #287: a mapped employee's effective catalog on the normal port
        # includes their provider's mapped model names. VIP-port requests never
        # go external, so the VIP-port catalog stays internal-only.
        if employee_no and not is_vip_channel:
            data = self._merge_external_entries(data, employee_no)

        payload = {
            "object": "list",
            "data": data,
            "ip": ip.ip,
        }
        if employee_no:
            payload["employee_no"] = employee_no
        return payload

    @staticmethod
    def _merge_external_entries(data: list[dict], employee_no: str) -> list[dict]:
        """Overlay the employee's provider mappings on the internal list.

        A mapped name shadows the internal entry (the mapping wins at request
        time); internal models without a mapping stay listed because the
        router still serves them as fallback. The router enforces no limits
        for external calls, so mapped entries advertise null capabilities.
        """
        route = ExternalRouteRepository.get_active_route(employee_no)
        if route is None:
            return data
        mappings = ExternalRouteRepository.list_enabled_mappings(route.model_mapping_policy)
        if not mappings:
            return data
        mapped_names = {m.internal_model_name.casefold() for m in mappings}
        data = [
            entry
            for entry in data
            if entry["id"] == "auto" or entry["id"].casefold() not in mapped_names
        ]
        for m in mappings:
            data.append(
                {
                    "id": m.internal_model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": f"external:{route.name}",
                    "max_context": None,
                    "max_output_tokens": None,
                    "concurrent_limit": None,
                }
            )
        return data

    def _model_entry(self, model, ip: Ips, is_vip_channel: bool) -> dict:
        return {
            "id": model.model_name,
            "object": "model",
            "created": 0,
            "owned_by": "gateway",
            "max_context": self._max_context(ServerRepository.list_by_model_id(model.id)),
            "max_output_tokens": model.max_tokens,
            "concurrent_limit": None
            if is_vip_channel
            else AdmissionService.compute_concurrent_limit(ip, model.concurrent_limit),
        }

    def _auto_entry(self, ip: Ips, is_vip_channel: bool) -> dict:
        return {
            "id": "auto",
            "object": "model",
            "created": 0,
            "owned_by": "gateway",
            "max_context": self._auto_max_context(),
            "max_output_tokens": self.auto_max_tokens,
            "concurrent_limit": None
            if is_vip_channel
            else AdmissionService.compute_concurrent_limit(ip, self.auto_concurrent_limit),
        }

    def _auto_max_context(self) -> int | None:
        """Smallest max-context among models auto routing may redirect to.

        Auto picks its target at request time from the auto-selectable models
        (``complexity_min``/``complexity_max`` set) or the multimodal model
        for image requests, so the advertised ceiling is the minimum of their
        per-model maxima — a value every possible target can honor. ``None``
        when every target is unlimited or there are no targets.
        """
        targets = list(ModelRepository.list_auto_selectable_models())
        multimodal = ModelRepository.get_multimodal_model()
        if multimodal is not None:
            targets.append(multimodal)
        maxima = [
            self._max_context(ServerRepository.list_by_model_id(model.id))
            for model in targets
        ]
        finite = [value for value in maxima if value is not None]
        return min(finite) if finite else None

    @staticmethod
    def _max_context(servers) -> int | None:
        """Largest ``context_window`` among servers; ``None`` when all are unlimited."""
        context_windows = [s.context_window for s in servers if s.context_window is not None]
        return max(context_windows) if context_windows else None
