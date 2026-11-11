"""p/n prefiller split selection matrix (issue #276).

Covers the cluster-scoped chooser: classification (cached / partial / cold),
the busy-holder switch, role-aware #247 escape, strict p-isolation for new
traffic (with the small-request lane exception), and the standalone (mixed /
blank-group) path keeping pre-#276 behavior.

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
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=1)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1], _cache_hit_body())
        assert selected.id == n1.id  # idle holder keeps the request locally

    def test_context_records_cluster_ratio_on_switch(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, new_prefill_targets={"http://n1"})
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
        chooser = _chooser_with_workload({}, decoder_avgs={}, new_prefill_targets={"http://n1"})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1], _cache_hit_body())
        assert selected.id == n1.id  # split disabled for the cluster: stay

    def test_attempted_p_falls_back_to_holder(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, new_prefill_targets={"http://n1"})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        m1 = make_server(2, "http://m1", role="mixed", workload=0)
        _seed(chooser, n1, _partial_request_body())
        # m1's own text is unrelated: ratio 0.

        selected = _choose(chooser, [n1, m1], _partial_hit_body())
        assert selected.id == n1.id  # cluster scope (0.71) wins over global


class TestColdClass:
    def test_cold_never_uses_idle_p_prefiller(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = _choose(chooser, [n1, p1], make_body("brand new request"))
        assert selected.id == n1.id  # strict: queue on busy n, keep p reserved

    def test_cold_uses_least_loaded_mixed(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        m1 = make_server(2, "http://m1", role="mixed", workload=0)

        selected = _choose(chooser, [n1, m1], make_body("brand new request"))
        assert selected.id == m1.id

    def test_all_p_pool_degrades_to_least_loaded(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        p1 = make_server(1, "http://p1", role="prefix-prefiller", group_id="g1", workload=3)
        p2 = make_server(2, "http://p2", role="prefix-prefiller", group_id="g1", workload=0)

        selected = _choose(chooser, [p1, p2], make_body("brand new request"))
        assert selected.id == p2.id


class TestSmallRequestLane:
    """Small requests (0 < estimate_tokens < SMALL_REQUEST_TOKEN_LIMIT) share
    the p-prefiller tier with cached traffic: short prompts must not queue
    behind long new prefills on n-prefillers. Cached-class requests keep
    holder-first placement, and the lane yields to the pre-existing
    partial/cold placement whenever no healthy p-prefiller exists."""

    LIMIT = PrefixCachePrebleServerChooser.SMALL_REQUEST_TOKEN_LIMIT

    def _choose_small(self, chooser, candidates, body, estimate, attempted=set()):
        return chooser.choose(
            candidates, make_context(body, estimate_tokens=estimate), attempted
        )

    def test_small_cold_takes_least_loaded_p(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=1)
        p2 = make_server(3, "http://p2", role="prefix-prefiller", group_id="g2", workload=0)

        selected = self._choose_small(
            chooser, [n1, p1, p2], make_body("brand new request"), 8000
        )
        # Even an idle n-prefiller is bypassed: the lane is p-tier only.
        assert selected.id == p2.id

    def test_small_partial_takes_p_tier(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=3)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(3, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, holder, _partial_request_body())

        selected = self._choose_small(
            chooser, [holder, sibling, p1], _partial_hit_body(), 8000
        )
        # Same shape as test_partial_targets_n_prefiller_in_argmax_cluster,
        # but small: the p-tier lane wins over the in-cluster n-prefiller.
        assert selected.id == p1.id

    def test_small_cached_keeps_holder(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = self._choose_small(
            chooser, [n1, p1], _cache_hit_body(), 8000
        )
        # Cached class keeps holder-first placement: local DRAM beats the lane.
        assert selected.id == n1.id

    def test_lane_ranks_p_on_decode_floor(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={"g1": 10.0})
        p1 = make_server(1, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        p2 = make_server(2, "http://p2", role="prefix-prefiller", group_id="g2", workload=0)

        selected = self._choose_small(chooser, [p1, p2], make_body("cold"), 8000)
        # g1's decode floor (10) makes its idle p the busier placement.
        assert selected.id == p2.id

    def test_overloaded_p_falls_back_to_new(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=5)

        selected = self._choose_small(chooser, [n1, p1], make_body("cold"), 8000)
        # p at 5x the pool minimum is #247-overloaded: the lane yields and the
        # request keeps its regular cold placement.
        assert selected.id == n1.id

    def test_attempted_p_falls_back_to_new(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = self._choose_small(
            chooser, [n1, p1], make_body("cold"), 8000, attempted={p1.id}
        )
        assert selected.id == n1.id

    def test_no_p_tier_falls_back_to_new(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        m1 = make_server(2, "http://m1", role="mixed", workload=0)

        selected = self._choose_small(chooser, [n1, m1], make_body("cold"), 8000)
        assert selected.id == m1.id

    def test_limit_is_exclusive(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = self._choose_small(chooser, [n1, p1], make_body("cold"), self.LIMIT)
        assert selected.id == n1.id  # exactly at the limit: not small

    def test_unknown_estimate_stays_off_p(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = self._choose_small(chooser, [n1, p1], make_body("cold"), 0)
        assert selected.id == n1.id  # 0 = unknown: keep p reserved

    def test_cold_placement_records_truthful_ratio(self, mock_redis):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        ctx = make_context(make_body("brand new request"), estimate_tokens=8000)
        selected = chooser.choose([n1, p1], ctx, set())
        assert selected.id == p1.id
        # The lane changes placement, not the dispatch-time classification:
        # prefix_cache stays the truthful match ratio (0 for a cold request).
        assert ctx.prefix_cache == 0.0
        assert ctx.last_match is None


class TestOverloadEscapeCoversClusterPaths:
    """#247 must cover every prefix-affinity path, not just the idle-holder
    one: a cluster whose least-loaded prefiller is at/above its weight and
    more than 2x the pool minimum sheds partials and busy-holder requests to
    the global least-loaded pool instead of queueing them."""

    def test_overloaded_partial_escapes_globally(self, mock_redis):
        # pd-1 prefiller at workload 5 (weight 2) vs mixed at 2 (weight 4):
        # 5 >= 2 and 5 > 2*2 -> escape the partial affinity.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={"g1": 0.0})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5, weight=2)
        m1 = make_server(2, "http://m1", role="mixed", workload=2, weight=4)
        _seed(chooser, n1, _partial_request_body(), request_id=7)
        # m1 holds only a short own prefix (128 of 910 chars -> ~0.14).
        _seed(chooser, m1, make_body("partial " + "x" * 128), request_id=9)

        ctx = make_context(_partial_hit_body())
        selected = chooser.choose([n1, m1], ctx, set())
        assert selected.id == m1.id
        # The escape left the winning cluster, so the row records the
        # escaping server's own entry, not the scope's partial ratio (#346).
        assert 0 < ctx.prefix_cache < 0.5
        assert ctx.last_match == 9

    def test_loaded_partial_stays_in_cluster(self, mock_redis):
        # Control: below the weight threshold the RDMA partial-KV pull still
        # wins over the lighter mixed server.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={"g1": 0.0})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1, weight=2)
        m1 = make_server(2, "http://m1", role="mixed", workload=2, weight=4)
        _seed(chooser, n1, _partial_request_body())

        selected = _choose(chooser, [n1, m1], _partial_hit_body())
        assert selected.id == n1.id  # 1 < weight 2: keep the affinity

    def test_overloaded_busy_holder_escapes_when_no_p(self, mock_redis):
        # Busy holder (>0.9 match, new prefill in flight), cluster has no
        # p-prefiller, holder at 2.5x the pool minimum: escape globally.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={"g1": 0.0}, new_prefill_targets={"http://n1"}
        )
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5, weight=2)
        m1 = make_server(2, "http://m1", role="mixed", workload=2, weight=4)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, m1], _cache_hit_body())
        assert selected.id == m1.id

    def test_loaded_busy_holder_stays_when_no_p(self, mock_redis):
        # Control: busy but below the weight threshold — staying on the
        # holder is still cheaper than a full recompute elsewhere.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={"g1": 0.0}, new_prefill_targets={"http://n1"}
        )
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1, weight=2)
        m1 = make_server(2, "http://m1", role="mixed", workload=2, weight=4)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, m1], _cache_hit_body())
        assert selected.id == n1.id


class TestNonSharedPoolCluster:
    """Composition rule: a group without a prefix-prefiller has no
    intra-cluster KV transfer, so its prefillers are standalone scopes.
    Cached and partial requests must stick to the server that actually
    holds the prefix — placing them on a sibling (the shared-pool RDMA
    assumption) silently degenerates to a full re-prefill."""

    def test_cached_holder_keeps_request_with_idle_sibling(self, mock_redis):
        # The incident shape: conversation turn N prefilled on prefiller-3,
        # turn N+1 arrives while it is busy and a sibling sits idle.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, shared_groups=set())
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1, weight=8)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0, weight=8)
        _seed(chooser, holder, _cached_request_body())

        ctx = make_context(_cache_hit_body())
        selected = chooser.choose([holder, sibling], ctx, set())
        assert selected.id == holder.id
        assert ctx.prefix_cache > 0.9  # the holder's own entry

    def test_busy_holder_with_new_prefill_stays(self, mock_redis):
        # No p-prefiller to switch to, and switching would be wrong anyway:
        # the sibling cannot pull the holder's KV.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, shared_groups=set(), new_prefill_targets={"http://n1"}
        )
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1, weight=8)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0, weight=8)
        _seed(chooser, holder, _cached_request_body())

        selected = _choose(chooser, [holder, sibling], _cache_hit_body())
        assert selected.id == holder.id

    def test_partial_prefers_holder_over_idle_sibling(self, mock_redis):
        # A partial prefix is only reusable on the holder itself; the
        # shared-pool behavior (least-loaded in-cluster n-prefiller + RDMA
        # pull) must not apply without the p/n split.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, shared_groups=set())
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=3, weight=8)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0, weight=8)
        _seed(chooser, holder, _partial_request_body())

        ctx = make_context(_partial_hit_body())
        selected = chooser.choose([holder, sibling], ctx, set())
        assert selected.id == holder.id
        assert 0.5 < ctx.prefix_cache <= 0.9

    def test_overloaded_holder_still_escapes(self, mock_redis):
        # Standalone scopes keep the #247 gate: at/above weight and >2x the
        # pool minimum, affinity sheds to global least-loaded.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, shared_groups=set())
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=10, weight=2)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0)
        mixed = make_server(3, "http://m1", role="mixed", workload=0)
        _seed(chooser, holder, _cached_request_body())

        selected = _choose(chooser, [holder, sibling, mixed], _cache_hit_body())
        assert selected.id in (sibling.id, mixed.id)

    def test_split_cluster_keeps_shared_placement(self, mock_redis):
        # Control: the same pool with the p/n split deployed (shared_groups
        # contains g1) keeps the cluster-scoped placement — partial goes to
        # the least-loaded in-cluster prefiller, KV pulled via RDMA.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={}, shared_groups={"g1"})
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=3)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0)
        p1 = make_server(3, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, holder, _partial_request_body())

        ctx = make_context(_partial_hit_body())
        selected = chooser.choose([holder, sibling, p1], ctx, set())
        assert selected.id == sibling.id
        assert 0.5 < ctx.prefix_cache <= 0.9  # scope ratio, reused via RDMA


class TestScopes:
    def test_argmax_cluster_wins(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
        servers = [
            make_server(1, "http://n1", role="prefiller", group_id="g1"),
            make_server(2, "http://m1", role="mixed"),
        ]
        match = _PrefixMatch(
            server_match_ratios={1: 0.6, 2: 0.7},
            cluster_ratios={"g1": 0.6},
        )
        assert chooser._winning_scope(servers, match, {"g1"}) is None  # mixed 0.7 wins

        match = _PrefixMatch(
            server_match_ratios={1: 0.7, 2: 0.6},
            cluster_ratios={"g1": 0.7},
        )
        assert chooser._winning_scope(servers, match, {"g1"}) == "g1"  # cluster 0.7 wins

    def test_tied_clusters_win_randomly(self, monkeypatch):
        # Issue #318: two same-scale clusters with the same best ratio must
        # share the win instead of the first-inserted cluster always winning.
        chooser = _chooser_with_workload({}, decoder_avgs={})
        servers = [
            make_server(1, "http://n1", role="prefiller", group_id="g1"),
            make_server(2, "http://n2", role="prefiller", group_id="g2"),
        ]
        match = _PrefixMatch(
            server_match_ratios={1: 0.7, 2: 0.7},
            cluster_ratios={"g1": 0.7, "g2": 0.7},
        )
        choices = []

        def choose(options):
            choices.append(list(options))
            return options[1]

        monkeypatch.setattr(
            "router.route_algorithm.prefix_cache_preble.random.choice", choose
        )

        assert chooser._winning_scope(servers, match, {"g1", "g2"}) == "g2"
        assert choices == [["g1", "g2"]]

    def test_mixed_group_id_is_not_a_cluster_member(self):
        # A mixed server with an accidentally set group_id is standalone.
        chooser = _chooser_with_workload({}, decoder_avgs={})
        mixed = make_server(1, "http://m1", role="mixed", group_id="g1")
        assert not chooser._is_cluster_prefiller(mixed, {"g1"})
        match = _PrefixMatch(
            server_match_ratios={1: 0.6, 2: 0.4},
            cluster_ratios={},  # only prefiller roles feed cluster_ratios
        )
        assert chooser._winning_scope([mixed], match, {"g1"}) is None

    def test_blank_group_prefiller_is_standalone(self):
        chooser = _chooser_with_workload({}, decoder_avgs={})
        blank = make_server(1, "http://p1", role="prefiller", group_id=None)
        assert not chooser._is_cluster_prefiller(blank, {"g1"})

    def test_prefiller_without_split_is_standalone(self):
        # Composition rule: a group with no prefix-prefiller has no shared
        # KV pool, so its members are standalone scopes even with group_id.
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1")
        assert chooser._is_cluster_prefiller(n1, {"g1"})
        assert not chooser._is_cluster_prefiller(n1, set())


class TestStandalonePoolUnchanged:
    def test_primary_hit_on_mixed_unchanged(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={})
        m1 = make_server(1, "http://m1", role="mixed", workload=0)
        _seed(chooser, m1, _cached_request_body())

        selected = _choose(chooser, [m1], _cache_hit_body())
        assert selected.id == m1.id

    def test_overloaded_mixed_escapes_globally(self, mock_redis):
        # Pre-#276 behavior for standalone scopes: #247 escape to global
        # least-loaded.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)

        selected = chooser.choose([n1, p1], make_context(b"\xff\xfe not utf8"), set())
        assert selected.id == n1.id

    def test_standalone_overload_escape_stays_off_p(self, mock_redis):
        # Cached on an overloaded standalone holder: the escape used to go
        # global least-loaded, straight onto an idle p that holds none of the
        # KV. It must queue on the busy live prefiller instead.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={})
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
        chooser = _chooser_with_workload({}, decoder_avgs={})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=10)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=9)
        n2 = make_server(3, "http://n2", role="prefiller", group_id="g1", workload=8)
        p2 = make_server(4, "http://p2", role="prefix-prefiller", group_id="g2", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p1, n2, p2], _cache_hit_body())
        assert selected.id == n2.id


class TestRecordedPairCoherence:
    """Issue #346: (prefix_cache, last_match) must describe ONE entry.

    The scope ratio (cluster argmax) and the selected server's own entry rid
    used to be recorded together — producing impossible rows like ratio 0.99
    paired with a 26k-token request. The pair now follows the placement: the
    scope argmax entry while the request stays in the winning cluster, the
    selected server's own entry once the escape ladder leaves it.
    """

    def test_escape_records_selected_servers_own_entry(self, mock_redis):
        # The incident shape: cached class on an exhausted cluster (holder
        # overloaded, no p-prefiller) escapes globally to an out-of-scope
        # server holding only a short prefix of its own.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={"g1": 0.0})
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=5, weight=2)
        m1 = make_server(2, "http://m1", role="mixed", workload=2, weight=4)
        _seed(chooser, n1, _cached_request_body(), request_id=7)
        # m1 holds only the first 128 of 1101 chars (~0.12).
        _seed(chooser, m1, make_body("cached " + "x" * 128), request_id=9)

        ctx = make_context(_cache_hit_body())
        selected = chooser.choose([n1, m1], ctx, set())
        assert selected.id == m1.id  # "cached cluster exhausted" -> global
        assert 0 < ctx.prefix_cache < 0.5  # m1's own ratio, not the scope 0.93
        assert ctx.last_match == 9

    def test_switch_records_scope_argmax_rid(self, mock_redis):
        # In-scope busy-holder switch: the recorded rid must be the entry
        # that produced the scope ratio (the holder's), not the switch
        # target's own — the p-prefiller holds none of the KV itself.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://n1"}
        )
        n1 = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=1)
        p1 = make_server(2, "http://p1", role="prefix-prefiller", group_id="g1", workload=0)
        _seed(chooser, n1, _cached_request_body(), request_id=7)

        ctx = make_context(_cache_hit_body())
        selected = chooser.choose([n1, p1], ctx, set())
        assert selected.id == p1.id
        assert ctx.prefix_cache > 0.9  # cluster ratio reused via RDMA
        assert ctx.last_match == 7

    def test_partial_in_cluster_records_scope_argmax_rid(self, mock_redis):
        # Partial class: the least-loaded in-cluster sibling has no own
        # entry at all, yet the recorded pair must still name the holder's
        # entry whose KV the placement pulls via RDMA.
        _, storage = mock_redis
        chooser = _chooser_with_workload({}, decoder_avgs={})
        holder = make_server(1, "http://n1", role="prefiller", group_id="g1", workload=3)
        sibling = make_server(2, "http://n2", role="prefiller", group_id="g1", workload=0)
        _seed(chooser, holder, _partial_request_body(), request_id=7)

        ctx = make_context(_partial_hit_body())
        selected = chooser.choose([holder, sibling], ctx, set())
        assert selected.id == sibling.id
        assert 0.5 < ctx.prefix_cache <= 0.9
        assert ctx.last_match == 7


class TestPPrefillerPoolAffinity:
    """The p-prefiller tier of the cached-class escape ranks by kv-pool
    affinity, not plain least-loaded: kv pools can be finer than the
    group (e.g. one pool per host — ``http://aaa:10x0/v1`` share one,
    ``http://bbb:10x0/v1`` another), so the switch target must be the
    p-prefiller sharing the longest base_url prefix with the origin (the
    writer of the scope's argmax entry), least-loaded among ties.

    Weights: p-prefillers get weight 10 so a nonzero workload stays below
    the #247 overload gate (the docs' advice for p-prefillers).
    """

    def test_busy_holder_switches_to_same_pool_p(self, mock_redis):
        # aaa holder busy; idle bbb p must lose to the loaded aaa p.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://aaa:1030/v1"}
        )
        n1 = make_server(1, "http://aaa:1030/v1", role="prefiller", group_id="g1", workload=1)
        p_aaa = make_server(2, "http://aaa:1010/v1", role="prefix-prefiller", group_id="g1", workload=2, weight=10)
        p_bbb = make_server(3, "http://bbb:1010/v1", role="prefix-prefiller", group_id="g1", workload=0, weight=10)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p_aaa, p_bbb], _cache_hit_body())
        assert selected.id == p_aaa.id  # shares "http://aaa:10"; p_bbb only "http://"

    def test_equal_prefix_tie_breaks_on_load(self, mock_redis):
        # Both aaa p's share 13 chars with the origin; the lighter one wins,
        # and the prefix rule still overrides the globally idle bbb p.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://aaa:1030/v1"}
        )
        n1 = make_server(1, "http://aaa:1030/v1", role="prefiller", group_id="g1", workload=1, weight=10)
        p_aaa1 = make_server(2, "http://aaa:1010/v1", role="prefix-prefiller", group_id="g1", workload=3, weight=10)
        p_aaa2 = make_server(3, "http://aaa:1020/v1", role="prefix-prefiller", group_id="g1", workload=1, weight=10)
        p_bbb = make_server(4, "http://bbb:1010/v1", role="prefix-prefiller", group_id="g1", workload=0, weight=10)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p_aaa1, p_aaa2, p_bbb], _cache_hit_body())
        assert selected.id == p_aaa2.id

    def test_overloaded_same_pool_p_falls_to_n_tier(self, mock_redis):
        # The affinity pick is still gated by #247: an overloaded aaa p is
        # skipped and the ladder continues to the same-cluster n-prefillers.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://aaa:1030/v1"}
        )
        n1 = make_server(1, "http://aaa:1030/v1", role="prefiller", group_id="g1", workload=5, weight=10)
        p_aaa = make_server(2, "http://aaa:1010/v1", role="prefix-prefiller", group_id="g1", workload=20)
        n2 = make_server(3, "http://aaa:1040/v1", role="prefiller", group_id="g1", workload=0)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p_aaa, n2], _cache_hit_body())
        assert selected.id == n2.id

    def test_origin_host_without_p_degrades_to_least_loaded(self, mock_redis):
        # Confirmed fallback: when no p shares a longer prefix (origin on a
        # host without p-prefillers), the p-tier keeps its plain
        # least-loaded cluster behavior.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://ccc:1030/v1"}
        )
        n1 = make_server(1, "http://ccc:1030/v1", role="prefiller", group_id="g1", workload=1)
        p_aaa = make_server(2, "http://aaa:1010/v1", role="prefix-prefiller", group_id="g1", workload=1, weight=10)
        p_bbb = make_server(3, "http://bbb:1010/v1", role="prefix-prefiller", group_id="g1", workload=0, weight=10)
        _seed(chooser, n1, _cached_request_body())

        selected = _choose(chooser, [n1, p_aaa, p_bbb], _cache_hit_body())
        # Both p's tie at "http://": least loaded wins.
        assert selected.id == p_bbb.id

    def test_origin_is_argmax_entry_writer_not_busy_holder(self, mock_redis):
        # Two holders, both cached-class: aaa holds the longer prefix (the
        # argmax entry, rid 100), the lighter bbb holder is the one busy
        # with a new prefill. Affinity must anchor on the argmax writer
        # (last_match's server), so the switch goes to the aaa p — not to
        # the busy holder's own pool.
        _, storage = mock_redis
        chooser = _chooser_with_workload(
            {}, decoder_avgs={}, new_prefill_targets={"http://bbb:1030/v1"}
        )
        n_aaa = make_server(1, "http://aaa:1030/v1", role="prefiller", group_id="g1", workload=1)
        n_bbb = make_server(2, "http://bbb:1030/v1", role="prefiller", group_id="g1", workload=0)
        p_aaa = make_server(3, "http://aaa:1010/v1", role="prefix-prefiller", group_id="g1", workload=2, weight=10)
        p_bbb = make_server(4, "http://bbb:1010/v1", role="prefix-prefiller", group_id="g1", workload=0, weight=10)
        # Rendered text is "user: " + content: aaa caches 1408 chars
        # (1408/1415 ~ 0.995), bbb 1280 (1280/1415 ~ 0.90) — both holders.
        _seed(chooser, n_aaa, make_body("x" * 1408), request_id=100)
        _seed(chooser, n_bbb, make_body("x" * 1280), request_id=200)

        ctx = make_context(make_body("x" * 1408 + "y"))
        selected = chooser.choose([n_aaa, n_bbb, p_aaa, p_bbb], ctx, set())
        assert selected.id == p_aaa.id
        assert ctx.prefix_cache > 0.99  # the argmax entry is aaa's
        assert ctx.last_match == 100
