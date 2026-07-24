from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from router.models import Server

# Arbitrary key for the workload-recalculation advisory lock. Only one
# corrector run may hold it at a time so concurrent runs serialize.
_ADVISORY_LOCK_KEY = 8723461


class ServerRepository:
    @staticmethod
    def list_by_model_id(model_id: int | None, vip: bool | None = None, min_context_window: int = 0) -> list[Server]:
        """Return routable servers: online + (closed or cooldown-expired open/half_open).

        ``vip``: when ``True`` only VIP servers, when ``False`` only non-VIP servers,
        when ``None`` (default) both.
        ``min_context_window``: when > 0, only servers whose context window is
        strictly larger than this (NULL context window is treated as unlimited).
        Used to retry on a larger-window server after a real context overflow.
        """
        now = timezone.now()
        queryset = Server.objects.filter(deleted_at__isnull=True, is_online=True)
        if model_id:
            queryset = queryset.filter(model_id=model_id)
        else:
            queryset = queryset.filter(model_id__isnull=True)
        if vip is True:
            queryset = queryset.filter(vip=True)
        elif vip is False:
            queryset = queryset.filter(vip=False)

        if min_context_window > 0:
            queryset = queryset.filter(Q(context_window__gt=min_context_window) | Q(context_window__isnull=True))
            
        servers = list(queryset.order_by("id"))
        return ServerRepository._filter_routable(servers, now)

    @staticmethod
    def list_all_online() -> list[Server]:
        now = timezone.now()
        servers = list(Server.objects.filter(deleted_at__isnull=True, is_online=True).order_by("id"))
        return ServerRepository._filter_routable(servers, now)

    @staticmethod
    def _filter_routable(servers: list[Server], now) -> list[Server]:
        """Include closed servers always; include open/half_open only if cooldown expired (and transition to half_open)."""
        routable = []
        for s in servers:
            if s.circuit_state == "closed":
                routable.append(s)
            elif s.circuit_state == "open":
                if s.last_state_change_at and (now - s.last_state_change_at).total_seconds() >= s.cooldown_seconds:
                    ServerRepository.transition_to_half_open(s)
                    routable.append(s)
            elif s.circuit_state == "half_open":
                routable.append(s)
        return routable

    @staticmethod
    def list_all_active() -> list[Server]:
        return list(Server.objects.filter(deleted_at__isnull=True).order_by("id"))

    @staticmethod
    def mark_checked(server: Server) -> None:
        now = timezone.now()
        Server.objects.filter(id=server.id).update(
            last_checked_at=now,
            updated_at=now,
        )
        server.last_checked_at = now
        server.updated_at = now

    @staticmethod
    def record_failure(server: Server, failure_threshold: int, base_cooldown_seconds: int, max_cooldown_seconds: int) -> None:
        """Increment failure counter. If threshold reached, open the circuit (or re-open with doubled cooldown)."""
        now = timezone.now()
        Server.objects.filter(id=server.id).update(
            consecutive_failures=F("consecutive_failures") + 1,
            last_failure_at=now,
            updated_at=now,
        )
        server.consecutive_failures += 1
        server.last_failure_at = now
        server.updated_at = now

        if server.consecutive_failures >= failure_threshold:
            if server.circuit_state == "half_open":
                # Failed during probe: double cooldown
                new_cooldown = min(server.cooldown_seconds * 2, max_cooldown_seconds)
            else:
                new_cooldown = min(
                    base_cooldown_seconds * (2 ** (server.consecutive_failures - failure_threshold)),
                    max_cooldown_seconds,
                )
            update_fields = {
                "circuit_state": "open",
                "last_state_change_at": now,
                "cooldown_seconds": new_cooldown,
            }
            if server.vip:
                update_fields["vip"] = False
                update_fields["vip_cooldown"] = None
            Server.objects.filter(id=server.id).update(**update_fields)
            server.circuit_state = "open"
            server.last_state_change_at = now
            server.cooldown_seconds = new_cooldown
            if server.vip:
                server.vip = False
                server.vip_cooldown = None

    @staticmethod
    def record_success(server: Server, base_cooldown_seconds: int) -> None:
        """Reset failure counter and close the circuit."""
        now = timezone.now()
        Server.objects.filter(id=server.id).update(
            consecutive_failures=0,
            circuit_state="closed",
            last_state_change_at=now,
            cooldown_seconds=base_cooldown_seconds,
            last_checked_at=now,
            updated_at=now,
        )
        server.consecutive_failures = 0
        server.circuit_state = "closed"
        server.last_state_change_at = now
        server.cooldown_seconds = base_cooldown_seconds
        server.last_checked_at = now
        server.updated_at = now

    @staticmethod
    def increment_workload(server: Server) -> None:
        Server.objects.filter(id=server.id).update(workload=F("workload") + 1)
        server.workload = (server.workload or 0) + 1

    @staticmethod
    def decrement_workload(server: Server) -> None:
        Server.objects.filter(id=server.id, workload__gt=0).update(workload=F("workload") - 1)
        server.workload = max((server.workload or 0) - 1, 0)

    @staticmethod
    def decrement_workload_by_targets(target_counts: dict[str, int]) -> None:
        for base_url, count in target_counts.items():
            if not base_url or count <= 0:
                continue
            Server.objects.filter(base_url=base_url).update(
                workload=Greatest(F("workload") - count, Value(0))
            )

    # ------------------------------------------------------------------
    # PD disaggregation: decoder load accounting and cluster helpers.
    # active_tokens is a decoder-only load estimate, reserved when a decoder
    # is chosen (after prefill, using the prefiller's exact prompt_tokens)
    # and released when the decode phase finishes. Prefillers are chosen by
    # prefix-cache affinity / workload, never by active_tokens.
    # ------------------------------------------------------------------

    @staticmethod
    def reserve_active_tokens(server: Server, load: float) -> None:
        Server.objects.filter(id=server.id).update(active_tokens=F("active_tokens") + load)
        server.active_tokens = (server.active_tokens or 0.0) + load

    @staticmethod
    def release_active_tokens(server: Server, load: float) -> None:
        Server.objects.filter(id=server.id, active_tokens__gt=0).update(
            active_tokens=Greatest(F("active_tokens") - load, Value(0))
        )
        server.active_tokens = max((server.active_tokens or 0.0) - load, 0.0)

    @staticmethod
    def list_pd_holders(model_id: int | None, vip: bool | None = None, min_context_window: int = 0) -> list[Server]:
        """Routable servers eligible for prefix-cache selection.

        Returns only single-node (role='mixed') and prefiller (role='prefiller')
        servers. Decoders are never directly routable and hold no prefix cache.

        A prefiller is included only if its cluster still has at least one
        routable decoder; otherwise the cluster cannot serve a PD request and the
        prefiller is dropped here so the chooser never proposes it.
        """
        servers = ServerRepository.list_by_model_id(model_id, vip=vip, min_context_window=min_context_window)
        prefillers_by_group: dict[str, list[Server]] = {}
        result: list[Server] = []
        for server in servers:
            role = getattr(server, "role", "mixed") or "mixed"
            if role == "mixed":
                result.append(server)
            elif role == "prefiller":
                prefillers_by_group.setdefault(server.group_id or "", []).append(server)
            # decoders are skipped
        if not prefillers_by_group:
            return result
        # Keep a prefiller only when its cluster has a routable decoder.
        decoder_groups = {
            s.group_id
            for s in servers
            if (getattr(s, "role", "mixed") or "mixed") == "decoder"
        }
        for group_id, prefillers in prefillers_by_group.items():
            if group_id in decoder_groups:
                result.extend(prefillers)
        return result

    @staticmethod
    def cluster_bottleneck_load(servers: list[Server]) -> dict[str, float]:
        """Per-cluster bottleneck load: ``max(min(P workload), min(D workload))``.

        A cluster is represented by the more loaded of its two sides; whichever
        side (prefillers or decoders) has the higher least-loaded node decides
        the cluster's effective load. Mixed servers are ignored here.

        Returns a mapping of ``group_id`` -> bottleneck load. Clusters missing a
        routable side are omitted (they were already filtered out by
        ``list_pd_holders``).
        """
        prefillers: dict[str, list[int]] = {}
        decoders: dict[str, list[int]] = {}
        for server in servers:
            role = getattr(server, "role", "mixed") or "mixed"
            if role not in ("prefiller", "decoder") or not server.group_id:
                continue
            workload = int(getattr(server, "workload", 0) or 0)
            if role == "prefiller":
                prefillers.setdefault(server.group_id, []).append(workload)
            else:
                decoders.setdefault(server.group_id, []).append(workload)
        bottleneck: dict[str, float] = {}
        for group_id in prefillers.keys() & decoders.keys():
            bottleneck[group_id] = float(max(min(prefillers[group_id]), min(decoders[group_id])))
        return bottleneck

    @staticmethod
    def pick_least_tokens_decoder(group_id: str, attempted_ids: set[int] | None = None) -> Server | None:
        """Pick the least-active_tokens routable decoder in a cluster."""
        attempted_ids = attempted_ids or set()
        servers = ServerRepository.list_by_model_id(None)
        decoders = [
            s for s in servers
            if (getattr(s, "role", "mixed") or "mixed") == "decoder"
            and s.group_id == group_id
            and s.id not in attempted_ids
        ]
        if not decoders:
            return None
        return min(decoders, key=lambda s: (float(getattr(s, "active_tokens", 0.0) or 0.0)))

    @staticmethod
    def pick_least_workload_prefiller(group_id: str, attempted_ids: set[int] | None = None) -> Server | None:
        """Pick the least-workload routable prefiller in a cluster."""
        attempted_ids = attempted_ids or set()
        servers = ServerRepository.list_by_model_id(None)
        prefillers = [
            s for s in servers
            if (getattr(s, "role", "mixed") or "mixed") == "prefiller"
            and s.group_id == group_id
            and s.id not in attempted_ids
        ]
        if not prefillers:
            return None
        return min(prefillers, key=lambda s: (int(getattr(s, "workload", 0) or 0)))

    @staticmethod
    def recalculate_workload(
        include_offline: bool = False,
        apply: bool = False,
    ) -> tuple[list[dict], list[dict]]:
        """Reconcile ``Server.workload`` against in-flight processing requests.

        The authoritative workload for a server is the number of
        ``RequestRecord`` rows with ``task_status='processing'`` whose
        ``target_pod_ip`` equals ``server.base_url`` (requests are linked to
        servers by string, not a foreign key).

        Returns ``(changes, orphans)`` where ``changes`` lists every server
        whose stored workload differs from the expected count (with
        ``before``/``after`` values), and ``orphans`` lists ``target_pod_ip``
        values seen on processing records that match no non-deleted server.

        Set ``apply=True`` to persist the corrected values. Uses a PostgreSQL
        advisory lock (a no-op transaction on other backends) so concurrent
        runs or workload mutations during the run cannot interleave.

        NOTE: Does not support PD-Disaggregation (role=prefiller/decoder) servers.
        PD requests record ``target_pod_ip`` as ``"P: <prefiller> -- D: <decoder>"``
        rather than a single ``server.base_url``, so the exact-string match below
        will not reconcile workload for prefillers/decoders. Single-node (role=mixed)
        servers remain fully supported. PD workload reconciliation is deferred.
        """
        from django.db import connection, transaction

        from router.models import RequestRecord

        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        [_ADVISORY_LOCK_KEY],
                    )

            qs = RequestRecord.objects.filter(
                task_status="processing", target_pod_ip__isnull=False
            ).exclude(target_pod_ip="")
            expected = {
                row["target_pod_ip"]: row["count"]
                for row in qs.values("target_pod_ip").annotate(count=Count("id"))
            }

            servers = ServerRepository._active_servers(include_offline)
            changes: list[dict] = []
            for server in servers:
                target = expected.get(server.base_url, 0)
                if server.workload == target:
                    continue
                changes.append(
                    {
                        "server_id": server.id,
                        "base_url": server.base_url,
                        "before": server.workload,
                        "after": target,
                    }
                )
                if apply:
                    Server.objects.filter(id=server.id).update(workload=target)
                    server.workload = target

            known = {s.base_url for s in servers}
            orphans = [
                {"target_pod_ip": pod_ip, "count": count}
                for pod_ip, count in expected.items()
                if pod_ip not in known
            ]
            orphans.sort(key=lambda item: item["target_pod_ip"])
            return changes, orphans

    @staticmethod
    def _active_servers(include_offline: bool) -> list[Server]:
        qs = Server.objects.filter(deleted_at__isnull=True)
        if not include_offline:
            qs = qs.filter(is_online=True)
        return list(qs.order_by("id"))

    @staticmethod
    def transition_to_half_open(server: Server) -> None:
        now = timezone.now()
        Server.objects.filter(id=server.id).update(
            circuit_state="half_open",
            last_state_change_at=now,
            updated_at=now,
        )
        server.circuit_state = "half_open"
        server.last_state_change_at = now
        server.updated_at = now

    @staticmethod
    def promote_to_vip(server: Server) -> bool:
        """Atomically flip vip=False -> vip=True. Returns True if this caller did the flip."""
        now = timezone.now()
        updated = Server.objects.filter(id=server.id, vip=False).update(
            vip=True,
            vip_cooldown=None,
            updated_at=now,
        )
        if updated:
            server.vip = True
            server.vip_cooldown = None
            server.updated_at = now
        return bool(updated)

    @staticmethod
    def demote_to_normal(server: Server) -> None:
        now = timezone.now()
        Server.objects.filter(id=server.id).update(
            vip=False,
            vip_cooldown=None,
            updated_at=now,
        )
        server.vip = False
        server.vip_cooldown = None
        server.updated_at = now

    @staticmethod
    def mark_vip_cooldown(server: Server) -> bool:
        """Idempotent: only sets vip_cooldown when currently NULL. Returns True if this caller set it."""
        now = timezone.now()
        updated = Server.objects.filter(
            id=server.id, vip=True, vip_cooldown__isnull=True
        ).update(vip_cooldown=now, updated_at=now)
        if updated:
            server.vip_cooldown = now
            server.updated_at = now
        return bool(updated)

    @staticmethod
    def cancel_vip_cooldown(server: Server) -> bool:
        now = timezone.now()
        updated = Server.objects.filter(
            id=server.id, vip=True, vip_cooldown__isnull=False
        ).update(vip_cooldown=None, updated_at=now)
        if updated:
            server.vip_cooldown = None
            server.updated_at = now
        return bool(updated)

    @staticmethod
    def demote_expired_cooldowns(cooldown_seconds: int, model_id: int | None = None) -> int:
        """Demote any VIP servers whose cooldown started more than ``cooldown_seconds`` ago."""
        now = timezone.now()
        cutoff = now - timedelta(seconds=cooldown_seconds)
        queryset = Server.objects.filter(
            deleted_at__isnull=True,
            vip=True,
            vip_cooldown__isnull=False,
            vip_cooldown__lte=cutoff,
        )
        if model_id is not None:
            queryset = queryset.filter(model_id=model_id)
        return queryset.update(vip=False, vip_cooldown=None, updated_at=now)
