from __future__ import annotations

import logging

from router.models import ExternalModelMapping, ExternalRoute
from router.repositories.external import ExternalRouteRepository

logger = logging.getLogger(__name__)


class ExternalRouteService:
    """Decide whether a request is forwarded to an external provider (issue #287).

    A request is eligible when the employee has an active ``external_routes``
    row whose provider circuit is routable, and the requested internal model
    name has an enabled mapping under the route's policy. Anything missing
    returns ``None`` and the request stays on the internal pipeline: internal
    model names keep their normal handling, provider-only names fall to the
    standard unknown-model 400.
    """

    def resolve(self, employee_no: str, internal_model_name: str | None) -> tuple[ExternalRoute, ExternalModelMapping] | None:
        if not employee_no or not internal_model_name:
            return None
        route = ExternalRouteRepository.get_active_route(employee_no)
        if route is None:
            return None
        if not route.api_key:
            # Misconfiguration guard: never forward a request with the client's
            # own router credential to a provider.
            logger.error(
                "external route %r (employee %s) has no api_key; keeping the request internal",
                route.name,
                employee_no,
            )
            return None
        if not ExternalRouteRepository.is_routable(route):
            return None
        mapping = ExternalRouteRepository.get_enabled_mapping(route.model_mapping_policy, internal_model_name)
        if mapping is None:
            return None
        return route, mapping
