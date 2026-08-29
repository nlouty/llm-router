"""p/n prefiller split selection matrix (issue #276).

Covers the cluster-scoped chooser: classification (cached / partial / cold),
the busy-holder switch, role-aware #247 escape, strict p-isolation for new
traffic, and the standalone (mixed / blank-group) path keeping pre-#276
behavior.

Ratio construction: on_response() writes entries at every block boundary
(multiples of prefix_block_chars=128) of the cached request's rendered text,
so a later request sharing that text matches up to the last common multiple
of 128. Cached ratio: 1024 common chars out of 1094 (~0.94 > 0.9); partial
ratio: 640 out of 902 (~0.71, between 0.5 and 0.9).
"""
from __future__ import annotations

from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.prefix_cache_preble import (
    PrefixCachePrebleServerChooser,
    _PrefixMatch,
)

from tests.test_pd_chooser import (
    _chooser_with_workload,
    make_body,
    make_context,
    make_server,
    mock_redis,
)


def _cached_request_body(tag: str = "cached") -> bytes:
    return make_body(f"{tag} " + "x" * 1024)


def _cache_hit_body(tag: str = "cached") -> bytes:
    # Same rendered prefix as _cached_request_body plus a short tail:
    # 1024 of 1094 common chars -> ratio ~0.94 (cached class).
    return make_body(f"{tag} " + "x" * 1024 + "y" * 64)


def _partial_request_body(tag: str = "partial") -> bytes:
    return make_body(f"{tag} " + "x" * 640)


def _partial_hit_body(tag: str = "partial") -> bytes:
    # 640 of 902 common chars -> ratio ~0.71 (partial class).
    return make_body(f"{tag} " + "x" * 640 + "y" * 256)


def _seed(chooser, server, body: bytes, request_id: int = 42):
    chooser.on_response(server, make_context(body, request_id=request_id), 200)


def _choose(chooser, candidates, body: bytes):
    return chooser.choose(candidates, make_context(body), set())


class TestCachedClass:
    def test_holders_preferred_local(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=1)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1], _cache_hit_body())
        assert selected.id == n1.id  # idle holder keeps the request locally

    def test_context_records_cluster_ratio_on_switch(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={}, new_prefill_targets={"http://n1"})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, n1, _cached_request_body())

        ctx = make_context(_cache_hit_body())
        selected = chooser.choose([n1, p1], ctx, set())
        assert selected.id == p1.id  # busy holder switched to same-cluster p
        # The switch target's own ratio is 0; the cluster ratio is what the
        # placement can actually reuse via RDMA.
        assert ctx.prefix_cache > 0.9

    def test_busy_holder_stays_when_cluster_has_no_p(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={}, new_prefill_targets={"http://n1"})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1], _cache_hit_body())
        assert selected.id == n1.id  # split disabled for the cluster: stay

    def test_attempted_p_falls_back_to_holder(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={}, new_prefill_targets={"http://n1"})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = chooser.choose(
            [n1, p1], make_context(_cache_hit_body()), {p1.id}
        )
        assert selected.id == n1.id  # p already attempted this request: stay

    def test_overload_escape_is_cluster_first(self, mock_redis):
        _, storage = mock_redis
        # Pre-#276 behavior escaped the overloaded holder to GLOBAL
        # least-loaded (the idle mixed server); the split keeps cached traffic
        # on a same-cluster p-prefiller instead.
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=10)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        m1 = make_server(3, "http://m1", role="mixed", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1, m1], _cache_hit_body())
        assert selected.id == p1.id

    def test_escape_spills_to_n_when_p_overloaded(self, mock_redis):
        _, storage = mock_redis
        # Maintainer decision #3: bigger weight on p-prefillers; if still
        # overloaded, use n-prefillers.
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=10)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=9)
        n2 = make_server(3, "http://n2", role="prefiller", group_id="g1", workload=4)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1, n2], _cache_hit_body())
        # holder 10 overloaded -> p 9 overloaded (9 > 2*4) -> n tier: n2 (4)
        assert selected.id == n2.id


class TestPartialClass:
    def test_partial_targets_n_prefiller_in_argmax_cluster(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=3)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(3, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, holder, _partial_request_body())

        selected = _choose(chooser, [holder, sibling, p1], _partial_hit_body())
        # New-style placement, but inside the cluster holding the partial KV;
        # least-loaded n-prefiller, never the p-prefiller.
        assert selected.id == sibling.id

    def test_cluster_ratio_beats_lower_standalone(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        m1 = make_server(2, "http://m1", role="mixed", workload=0)
        _seed(chooser, n1, _partial_request_body())
        # m1's own text is unrelated: ratio 0.

        selected = _choose(chooser, [n1, m1], _partial_hit_body())
        assert selected.id == n1.id  # cluster scope (0.71) wins over global


class TestColdClass:
    def test_cold_never_uses_idle_p_prefiller(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = _choose(chooser, [n1, p1], make_body("brand new request"))
        assert selected.id == n1.id  # strict: queue on busy n, keep p reserved

    def test_cold_uses_least_loaded_mixed(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        m1 = make_server(2, "http://m1", role="mixed", workload=0)

        selected = _choose(chooser, [n1, m1], make_body("brand new request"))
        assert selected.id == m1.id

    def test_all_p_pool_degrades_to_least_loaded(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_mins={})
        p1 = make_server(1, "http://p1", role="prefix-prefiller", group_id="g1", workload=3)
        p2 = make_server(2, "http://p2", role="prefix-prefiller", group_id="g1", workload=0)

        selected = _choose(chooser, [p1, p2], make_body("brand new request"))
        assert selected.id == p2.id


class TestScopes:
    def test_argmax_cluster_wins(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        n_g1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        n_g2 = make_server(2, "http://n2", role="prefiller", group_id="g2", workload=0)
        _seed(chooser, n_g2, _cached_request_body())
        _seed(chooser, n_g1, _partial_request_body())

        selected = _choose(chooser, [n_g1, n_g2], _cache_hit_body())
        # g2's cluster ratio ~0.94 (cached); g1 only ~0.71 (partial).
        assert selected.id == n_g2.id

    def test_better_standalone_beats_cluster(self):
        # Unit-level scope comparison (no Redis): a mixed server's own ratio
        # competes with cluster ratios; the strictly higher scope wins.
        chooser = _chooser_with_workload({}, decoder_mins={})
        servers = [
            make_server(1, "http://n1", role="prefiller", group_id="g1"),
            make_server(2, "http://m1", role="mixed"),
        ]
        match = _PrefixMatch(
            server_match_ratios={1: 0.6, 2: 0.7},
            cluster_ratios={"g1": 0.6},
        )
        assert chooser._winning_scope(servers, match) is None  # mixed 0.7 wins

        match = _PrefixMatch(
            server_match_ratios={1: 0.7, 2: 0.6},
            cluster_ratios={"g1": 0.7},
        )
        assert chooser._winning_scope(servers, match) == "g1"  # cluster 0.7 wins

    def test_mixed_group_id_is_not_a_cluster_member(self):
        # A mixed server with an accidentally set group_id is standalone.
        chooser = _chooser_with_workload({}, decoder_mins={})
        mixed = make_server(1, "http://m1", role="mixed", group_id="g1")
        assert not chooser._is_cluster_prefiller(mixed)
        match = _PrefixMatch(
            server_match_ratios={1: 0.6, 2: 0.4},
            cluster_ratios={},  # only prefiller roles feed cluster_ratios
        )
        assert chooser._winning_scope([mixed], match) is None

    def test_blank_group_prefiller_is_standalone(self):
        chooser = _chooser_with_workload({}, decoder_mins={})
        blank = make_server(1, "http://p1", role="prefiller", group_id=None)
        assert not chooser._is_cluster_prefiller(blank)


class TestStandalonePoolUnchanged:
    def test_primary_hit_on_mixed_unchanged(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        m1 = make_server(1, "http://m1", role="mixed", workload=0)
        _seed(chooser, m1, _cached_request_body())

        selected = _choose(chooser, [m1], _cache_hit_body())
        assert selected.id == m1.id

    def test_overloaded_mixed_escapes_globally(self, mock_redis):
        # Pre-#276 behavior for standalone scopes: #247 escape to global
        # least-loaded.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        m1 = make_server(1, "http://m1", role="mixed", workload=10)
        m2 = make_server(2, "http://m2", role="mixed", workload=0)
        _seed(chooser, m1, _cached_request_body())

        selected = _choose(chooser, [m1, m2], _cache_hit_body())
        assert selected.id == m2.id


class TestFreshPrefillNeverOnP:
    """The #276 invariant under load: however busy the live prefillers are,
    a request that must prefill from scratch never lands on a
    prefix-prefiller while a live prefiller exists; p-prefillers take one
    only when no live prefiller is left."""

    def test_unclassifiable_body_stays_off_p(self, mock_redis):
        # Non-UTF-8 body -> no extractable prefix text: choose() cannot
        # classify the request and used to fall back to plain least-loaded,
        # which happily picked the idle p-prefiller.
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = chooser.choose([n1, p1], make_context(b"\xff\xfe not utf8"), set())
        assert selected.id == n1.id

    def test_standalone_overload_escape_stays_off_p(self, mock_redis):
        # Cached on an overloaded standalone holder: the escape used to go
        # global least-loaded, straight onto an idle p that holds none of the
        # KV. It must queue on the busy live prefiller instead.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        m1 = make_server(1, "http://m1", role="mixed", workload=10)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, m1, _cached_request_body())

        selected = _choose(chooser, [m1, p1], _cache_hit_body())
        assert selected.id == m1.id

    def test_cached_cluster_exhausted_stays_off_foreign_p(self, mock_redis):
        # Winning cluster g1 exhausted (holder, p and sibling n all
        # overloaded); the idle p of g2 used to win the terminal global pick
        # and take a full new prefill.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_mins={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=10)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=9)
        n2 = make_server(3, "http://n2", role="prefiller", group_id="g1", workload=8)
        p2 = make_server(4, "http://p2", role="prefix-prefiller", group_id="g2", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1, n2, p2], _cache_hit_body())
        assert selected.id == n2.id
