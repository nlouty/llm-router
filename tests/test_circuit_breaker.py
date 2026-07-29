from datetime import timedelta

from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Server
from router.repositories.servers import ServerRepository
from router.services.circuit_breaker import CircuitBreakerService


class TestCircuitBreakerFailureCounting:
    def test_single_failure_keeps_server_routable(self):
        server = Server.objects.create(base_url="http://s1.example", is_online=True)
        cb = CircuitBreakerService()

        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "closed"
        assert server.consecutive_failures == 1
        # Server should still appear in routing
        assert server in ServerRepository.list_all_online()

    def test_two_failures_keeps_server_routable(self):
        server = Server.objects.create(base_url="http://s2.example", is_online=True)
        cb = CircuitBreakerService()

        cb.record_failure(server)
        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "closed"
        assert server.consecutive_failures == 2
        assert server in ServerRepository.list_all_online()

    def test_third_failure_opens_circuit(self):
        server = Server.objects.create(base_url="http://s3.example", is_online=True)
        cb = CircuitBreakerService()

        cb.record_failure(server)
        cb.record_failure(server)
        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "open"
        assert server.consecutive_failures == 3
        # Server should NOT appear in routing (cooldown not expired)
        assert server not in ServerRepository.list_all_online()

    def test_success_resets_failure_counter_and_closes_circuit(self):
        server = Server.objects.create(
            base_url="http://s4.example",
            is_online=True,
            consecutive_failures=2,
            circuit_state="half_open",
        )
        cb = CircuitBreakerService()

        cb.record_success(server)

        server.refresh_from_db()
        assert server.consecutive_failures == 0
        assert server.circuit_state == "closed"


class TestCircuitBreakerAdminControl:
    def test_offline_server_never_routed_regardless_of_circuit_state(self):
        Server.objects.create(base_url="http://admin-off.example", is_online=False, circuit_state="closed")

        assert ServerRepository.list_all_online() == []

    def test_offline_server_with_open_circuit_not_routed(self):
        Server.objects.create(base_url="http://admin-off2.example", is_online=False, circuit_state="open")

        assert ServerRepository.list_all_online() == []


class TestCircuitBreakerInlineProbe:
    def test_open_server_with_expired_cooldown_becomes_routable_as_half_open(self):
        server = Server.objects.create(
            base_url="http://probe1.example",
            is_online=True,
            circuit_state="open",
            consecutive_failures=3,
            last_state_change_at=timezone.now() - timedelta(seconds=60),
            cooldown_seconds=30,
        )

        # Cooldown expired: server should be included and transitioned to half_open
        online = ServerRepository.list_all_online()
        assert server in online
        server.refresh_from_db()
        assert server.circuit_state == "half_open"

    def test_open_server_before_cooldown_expires_not_routable(self):
        server = Server.objects.create(
            base_url="http://probe2.example",
            is_online=True,
            circuit_state="open",
            consecutive_failures=3,
            last_state_change_at=timezone.now() - timedelta(seconds=10),  # only 10s ago
            cooldown_seconds=30,  # needs 30s
        )

        # Cooldown NOT expired: server excluded
        online = ServerRepository.list_all_online()
        assert server not in online
        server.refresh_from_db()
        assert server.circuit_state == "open"  # unchanged

    def test_half_open_failure_reopens_with_doubled_cooldown(self):
        server = Server.objects.create(
            base_url="http://probe3.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=2,
            cooldown_seconds=30,
        )
        cb = CircuitBreakerService()

        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "open"
        assert server.cooldown_seconds == 60  # doubled from 30

    def test_cooldown_capped_at_max(self):
        server = Server.objects.create(
            base_url="http://probe4.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=2,
            cooldown_seconds=2000,
        )
        cb = CircuitBreakerService()

        cb.record_failure(server)

        server.refresh_from_db()
        assert server.cooldown_seconds == 3000  # capped at max

    def test_half_open_success_closes_circuit(self):
        server = Server.objects.create(
            base_url="http://probe5.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            cooldown_seconds=60,
        )
        cb = CircuitBreakerService()

        cb.record_success(server)

        server.refresh_from_db()
        assert server.circuit_state == "closed"
        assert server.consecutive_failures == 0

    def test_half_open_server_stays_routable_without_restamp(self):
        """Already-half_open servers stay routable and don't re-stamp last_state_change_at."""
        original_timestamp = timezone.now() - timedelta(seconds=60)
        server = Server.objects.create(
            base_url="http://probe6.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            last_state_change_at=original_timestamp,
            cooldown_seconds=30,
        )

        online = ServerRepository.list_all_online()
        assert server in online

        server.refresh_from_db()
        assert server.circuit_state == "half_open"
        # Timestamp must NOT be re-stamped (this was the bug)
        assert abs((server.last_state_change_at - original_timestamp).total_seconds()) < 1

    def test_vip_server_demoted_when_circuit_opens(self):
        """When a VIP server's circuit opens, it is demoted to normal."""
        server = Server.objects.create(
            base_url="http://vip-fail.example",
            is_online=True,
            vip=True,
            vip_cooldown=timezone.now(),
        )
        cb = CircuitBreakerService()

        cb.record_failure(server)
        cb.record_failure(server)
        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "open"
        assert server.vip is False
        assert server.vip_cooldown is None

    def test_non_vip_server_unaffected_by_demote_logic(self):
        """Normal servers stay vip=False when circuit opens (no spurious update)."""
        server = Server.objects.create(
            base_url="http://normal-fail.example",
            is_online=True,
            vip=False,
        )
        cb = CircuitBreakerService()

        cb.record_failure(server)
        cb.record_failure(server)
        cb.record_failure(server)

        server.refresh_from_db()
        assert server.circuit_state == "open"
        assert server.vip is False
        assert server.cooldown_seconds == 30  # reset to base


class TestCircuitBreakerTransitionRace:
    def test_transition_to_half_open_does_not_clobber_closed(self):
        """If another thread already closed the circuit, transition is a no-op."""
        server = Server.objects.create(
            base_url="http://race-closed.example",
            is_online=True,
            circuit_state="closed",
            consecutive_failures=0,
            cooldown_seconds=30,
        )

        ServerRepository.transition_to_half_open(server)

        server.refresh_from_db()
        assert server.circuit_state == "closed"

    def test_transition_to_half_open_is_noop_when_already_half_open(self):
        """A second concurrent transition must not re-stamp last_state_change_at."""
        original_timestamp = timezone.now() - timedelta(seconds=60)
        server = Server.objects.create(
            base_url="http://race-half.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            last_state_change_at=original_timestamp,
            cooldown_seconds=30,
        )

        ServerRepository.transition_to_half_open(server)

        server.refresh_from_db()
        assert server.circuit_state == "half_open"
        # Not re-stamped: the compare-and-set found it already half_open.
        assert abs((server.last_state_change_at - original_timestamp).total_seconds()) < 1

    def test_transition_to_half_open_only_transitions_open_servers(self):
        server = Server.objects.create(
            base_url="http://race-ok.example",
            is_online=True,
            circuit_state="open",
            consecutive_failures=3,
            last_state_change_at=timezone.now() - timedelta(seconds=60),
            cooldown_seconds=30,
        )

        ServerRepository.transition_to_half_open(server)

        server.refresh_from_db()
        assert server.circuit_state == "half_open"


class TestCircuitBreakerHalfOpenProbeLimit:
    def test_half_open_server_routable_when_below_probe_limit(self):
        server = Server.objects.create(
            base_url="http://probe-below.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            workload=0,
            cooldown_seconds=30,
        )
        # default probe_limit=1, workload 0 < 1 -> routable
        assert server in ServerRepository.list_all_online()

    def test_half_open_server_excluded_at_probe_limit(self):
        """A half_open server with an in-flight probe is excluded until it completes."""
        server = Server.objects.create(
            base_url="http://probe-full.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            workload=1,  # probe in flight, probe_limit default = 1
            cooldown_seconds=30,
        )
        assert server not in ServerRepository.list_all_online()

    def test_closed_server_routable_regardless_of_workload(self):
        """Closed servers must not be gated by the probe limit."""
        server = Server.objects.create(
            base_url="http://probe-closed.example",
            is_online=True,
            circuit_state="closed",
            workload=100,
        )
        assert server in ServerRepository.list_all_online()

    def test_probe_limit_respects_config_override(self, monkeypatch):
        monkeypatch.setitem(
            APP_CONFIG["load_balancer"]["circuit_breaker"],
            "half_open_probe_limit",
            3,
        )
        server = Server.objects.create(
            base_url="http://probe-override.example",
            is_online=True,
            circuit_state="half_open",
            consecutive_failures=3,
            workload=2,  # < 3 (overridden limit)
            cooldown_seconds=30,
        )
        assert server in ServerRepository.list_all_online()
