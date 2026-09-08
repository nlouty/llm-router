from __future__ import annotations

from django.db.models import F, Value
from django.db.models.functions import Least
from django.utils import timezone

from router.models import ExternalModelMapping, ExternalRoute


class ExternalRouteRepository:
    """Lookup and circuit-breaker state for external routes (issue #287).

    Breaker fields live per row but are always updated for the whole
    ``base_url`` group (one provider = N employee rows), so every employee of
    a provider sees the same circuit state.
    """

    @staticmethod
    def get_active_route(employee_no: str) -> ExternalRoute | None:
        if not employee_no:
            return None
        return (
            ExternalRoute.objects.filter(
                employee_no=employee_no,
                is_active=True,
                deleted_at__isnull=True,
            )
            .order_by("id")
            .first()
        )

    @staticmethod
    def get_enabled_mapping(policy_id: int, internal_model_name: str) -> ExternalModelMapping | None:
        if not internal_model_name:
            return None
        return ExternalModelMapping.objects.filter(
            policy_id=policy_id,
            internal_model_name=internal_model_name,
            is_enabled=True,
            deleted_at__isnull=True,
        ).first()

    @staticmethod
    def list_enabled_mappings(policy_id: int) -> list[ExternalModelMapping]:
        return list(
            ExternalModelMapping.objects.filter(
                policy_id=policy_id,
                is_enabled=True,
                deleted_at__isnull=True,
            ).order_by("id")
        )

    @staticmethod
    def is_routable(route: ExternalRoute, now=None) -> bool:
        """True when the provider's circuit allows a send.

        Mirrors ServerRepository._filter_routable: closed always; an open
        circuit becomes a half-open probe once its cooldown expired; half-open
        accepts the probe. Unlike servers there is no workload counter, so a
        half-open probe is accepted without a limit — a failed probe re-opens
        the circuit with doubled cooldown.
        """
        if route.circuit_state == "closed":
            return True
        if route.circuit_state == "open":
            now = now or timezone.now()
            if route.last_state_change_at and (now - route.last_state_change_at).total_seconds() >= route.cooldown_seconds:
                ExternalRouteRepository.transition_to_half_open(route, now)
                return True
            return False
        return route.circuit_state == "half_open"

    @staticmethod
    def transition_to_half_open(route: ExternalRoute, now=None) -> None:
        now = now or timezone.now()
        # Compare-and-set on the whole group: only transition rows still open,
        # so a concurrent record_success (-> closed) is not clobbered.
        updated = ExternalRoute.objects.filter(
            base_url=route.base_url,
            circuit_state="open",
            deleted_at__isnull=True,
        ).update(
            circuit_state="half_open",
            last_state_change_at=now,
            updated_at=now,
        )
        if updated:
            route.circuit_state = "half_open"
            route.last_state_change_at = now

    @staticmethod
    def record_failure(
        route: ExternalRoute,
        failure_threshold: int,
        base_cooldown_seconds: int,
        max_cooldown_seconds: int,
    ) -> None:
        """Count a provider failure; open the circuit at the threshold.

        Cooldown escalation is per recovery cycle, not per failure: once the
        circuit is open, further failures are casualties of requests sent
        before the outage was detected and must not escalate the cooldown or
        re-stamp ``last_state_change_at`` (issue #300). Each transition is a
        compare-and-set on the group's current DB state, so stale in-memory
        snapshots cannot re-apply it.
        """
        now = timezone.now()
        group = ExternalRoute.objects.filter(base_url=route.base_url, deleted_at__isnull=True)
        group.update(consecutive_failures=F("consecutive_failures") + 1, updated_at=now)
        route.consecutive_failures = (route.consecutive_failures or 0) + 1

        if route.consecutive_failures < failure_threshold:
            return
        if route.circuit_state == "half_open":
            # A probe request failed: double the cooldown of this recovery cycle.
            new_cooldown = min(route.cooldown_seconds * 2, max_cooldown_seconds)
            updated = group.filter(circuit_state="half_open").update(
                circuit_state="open",
                last_state_change_at=now,
                cooldown_seconds=Least(F("cooldown_seconds") * 2, Value(max_cooldown_seconds)),
                updated_at=now,
            )
        else:
            # Threshold crossed from closed: open with the base cooldown. The
            # compare-and-set on "closed" skips groups already open or
            # half_open — a half_open group is only re-opened by its own
            # probe failing.
            new_cooldown = base_cooldown_seconds
            updated = group.filter(circuit_state="closed").update(
                circuit_state="open",
                last_state_change_at=now,
                cooldown_seconds=base_cooldown_seconds,
                updated_at=now,
            )
        if updated:
            route.circuit_state = "open"
            route.last_state_change_at = now
            route.cooldown_seconds = new_cooldown

    @staticmethod
    def record_success(route: ExternalRoute, base_cooldown_seconds: int) -> None:
        """Reset the provider's failure counter and close the circuit."""
        now = timezone.now()
        ExternalRoute.objects.filter(base_url=route.base_url, deleted_at__isnull=True).update(
            consecutive_failures=0,
            circuit_state="closed",
            last_state_change_at=now,
            cooldown_seconds=base_cooldown_seconds,
            updated_at=now,
        )
        route.consecutive_failures = 0
        route.circuit_state = "closed"
        route.last_state_change_at = now
        route.cooldown_seconds = base_cooldown_seconds
