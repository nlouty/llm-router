from router.models import Server
from router.repositories.servers import ServerRepository


def _server(base_url, role="mixed", group_id=None, workload=0, active_tokens=0.0, **kwargs):
    return Server.objects.create(
        base_url=base_url,
        role=role,
        group_id=group_id,
        workload=workload,
        active_tokens=active_tokens,
        **kwargs,
    )


class TestActiveTokensAccounting:
    def test_reserve_and_release(self):
        s = _server("http://d1", role="decoder", group_id="g1", active_tokens=10.0)
        ServerRepository.reserve_active_tokens(s, 5.0)
        s.refresh_from_db()
        assert s.active_tokens == 15.0

        ServerRepository.release_active_tokens(s, 5.0)
        s.refresh_from_db()
        assert s.active_tokens == 10.0

    def test_release_floors_at_zero(self):
        s = _server("http://d2", role="decoder", group_id="g1", active_tokens=3.0)
        ServerRepository.release_active_tokens(s, 10.0)
        s.refresh_from_db()
        assert s.active_tokens == 0.0


class TestListPdHolders:
    def test_returns_mixed_and_prefillers_with_routable_decoders(self):
        _server("http://m1", role="mixed")
        _server("http://p1", role="prefiller", group_id="g1")
        _server("http://d1", role="decoder", group_id="g1")
        # cluster g2 has a prefiller but no routable decoder -> prefiller dropped
        _server("http://p2", role="prefiller", group_id="g2")
        # a standalone decoder is never returned
        _server("http://d3", role="decoder", group_id="g3")

        holders = ServerRepository.list_pd_holders(None)
        urls = sorted(s.base_url for s in holders)
        assert urls == ["http://m1", "http://p1"]

    def test_prefiller_dropped_when_cluster_decoder_open(self):
        p = _server("http://p1", role="prefiller", group_id="g1")
        # circuit open with no last_state_change_at -> not routable
        _server("http://d1", role="decoder", group_id="g1", circuit_state="open")
        holders = ServerRepository.list_pd_holders(None)
        assert p not in holders
        assert holders == []


class TestClusterBottleneckLoad:
    def test_bottleneck_is_max_of_min_per_side(self):
        servers = [
            _server("http://p1", role="prefiller", group_id="g1", workload=2),
            _server("http://p2", role="prefiller", group_id="g1", workload=5),
            _server("http://d1", role="decoder", group_id="g1", workload=3),
            _server("http://d2", role="decoder", group_id="g1", workload=8),
            _server("http://m1", role="mixed"),
        ]
        bottleneck = ServerRepository.cluster_bottleneck_load(servers)
        # min(P)=2, min(D)=3 -> max=3
        assert bottleneck == {"g1": 3.0}

    def test_cluster_missing_side_is_omitted(self):
        servers = [
            _server("http://p1", role="prefiller", group_id="g1", workload=2),
            _server("http://d1", role="decoder", group_id="g2", workload=1),
        ]
        bottleneck = ServerRepository.cluster_bottleneck_load(servers)
        # g1 has no decoder, g2 has no prefiller -> both omitted
        assert bottleneck == {}


class TestPickDecoder:
    def test_picks_least_active_tokens(self):
        _server("http://d1", role="decoder", group_id="g1", active_tokens=10.0)
        _server("http://d2", role="decoder", group_id="g1", active_tokens=2.0)
        d = ServerRepository.pick_least_tokens_decoder("g1")
        assert d is not None
        assert d.base_url == "http://d2"

    def test_excludes_attempted(self):
        d1 = _server("http://d1", role="decoder", group_id="g1", active_tokens=1.0)
        _server("http://d2", role="decoder", group_id="g1", active_tokens=2.0)
        d = ServerRepository.pick_least_tokens_decoder("g1", attempted_ids={d1.id})
        assert d.base_url == "http://d2"
