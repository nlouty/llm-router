from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser


@dataclass
class Server:
    id: int
    base_url: str
    model_id: int | None = None
    cache_time: int = 3600
    weight: int = 1
    role: str = "mixed"
    group_id: str | None = None
    workload: int = 0


def make_server(server_id, base_url, role="mixed", group_id=None, workload=0, weight=1):
    return Server(
        id=server_id,
        base_url=base_url,
        role=role,
        group_id=group_id,
        workload=workload,
        weight=weight,
    )


def make_context(body: bytes, request_id: int = 1):
    return ServerSelectionContext(
        request_id=request_id,
        ip_id=None,
        model_id=1,
        model_name="test-model",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
    )


def make_body(text: str) -> bytes:
    return json.dumps({"messages": [{"role": "user", "content": text}]}).encode("utf-8")


@pytest.fixture
def mock_redis():
    with patch("redis.Redis") as mock:
        client = type("C", (), {})()
        # storage: key -> {field: value}, modelling a Redis Hash per key.
        storage: dict = {}
        queued_reads = []

        client.hgetall = lambda key: dict(storage.get(key, {}))
        pipe = type("P", (), {})()

        def hset(key, field=None, value=None, mapping=None):
            store = storage.setdefault(key, {})
            if mapping:
                store.update(mapping)
            else:
                store[field] = value
        pipe.hset = hset
        pipe.expire = lambda key, seconds: None
        pipe.hgetall = lambda key: queued_reads.append(dict(storage.get(key, {})))

        def pipe_execute():
            results = list(queued_reads)
            queued_reads.clear()
            return results
        pipe.execute = pipe_execute
        client.pipeline = lambda: pipe
        mock.return_value = client
        PrefixCachePrebleServerChooser._redis_client = client
        yield client, storage
        PrefixCachePrebleServerChooser._redis_client = None


def _chooser_with_workload(server_workloads: dict[int, int], decoder_mins: dict[str, float] | None = None):
    """Build a chooser where each server reports a fixed workload, and PD
    decoder-min loads are stubbed (no DB needed)."""
    c = PrefixCachePrebleServerChooser.__new__(PrefixCachePrebleServerChooser)
    # minimal init skipping redis/APP_CONFIG
    c.primary_match_threshold = 0.9
    c.secondary_match_threshold = 0.5
    c.max_prefix_chars = 1000000
    c.prefix_block_chars = 128
    c.count_provider = None
    c.server_count_provider = lambda servers: {
        s.id: server_workloads.get(s.id, getattr(s, "workload", 0)) for s in servers
    }
    c._ensure_redis = lambda: None
    if decoder_mins is not None:
        c._cluster_decoder_mins = staticmethod(lambda: decoder_mins)
    return c


class TestPrefillerLoadUsesDecoderFloor:
    def test_no_prefix_hit_prefiller_reports_decoder_floor(self, mock_redis):
        # cluster g1: min(D)=1; prefiller own workload=5 -> effective max(5,1)=5
        # mixed m1: workload 0 -> should win (0 < 5)
        candidates = [
            make_server(1, "http://m1", role="mixed", workload=0),
            make_server(2, "http://p1", role="prefiller", group_id="g1", workload=5),
        ]
        chooser = _chooser_with_workload({}, decoder_mins={"g1": 1.0})
        ctx = make_context(make_body("a fresh prompt with no cached match"))
        selected = chooser.choose(candidates, ctx, set())
        assert selected.id == 1  # mixed wins; prefiller's load is 5 not 0

    def test_prefix_hit_on_prefiller_overloaded_falls_back(self, mock_redis):
        # Seed Redis: prefix maps to prefiller (id=2), ratio 1.0
        _, storage = mock_redis
        body = make_body("shared prefix content")
        chooser = _chooser_with_workload({}, decoder_mins={"g1": 100.0})
        prefiller = make_server(2, "http://p1", role="prefiller", group_id="g1")
        chooser.on_response(prefiller, make_context(body, request_id=42), 200)
        assert storage, "on_response should have written prefix entries"

        # A fresh mixed server is idle (load 0); prefiller decoder floor=100.
        # Overload guard (load >= weight and load > 2x min) must fall back to
        # the mixed server.
        candidates = [
            make_server(1, "http://m1", role="mixed", workload=0),
            prefiller,
        ]
        chooser2 = _chooser_with_workload({}, decoder_mins={"g1": 100.0})
        selected = chooser2.choose(candidates, make_context(body), set())
        assert selected.id == 1


class TestPrefillerBalancingWithinCluster:
    def test_no_prefix_hit_prefers_idle_prefiller_over_busy_sibling(self, mock_redis):
        candidates = [
            make_server(1, "http://p1", role="prefiller", group_id="g1", workload=7),
            make_server(2, "http://p2", role="prefiller", group_id="g1", workload=0),
            make_server(3, "http://p3", role="prefiller", group_id="g1", workload=0),
        ]
        chooser = _chooser_with_workload({}, decoder_mins={"g1": 0.0})
        ctx = make_context(make_body("a fresh prompt with no cached match"))
        selected = chooser.choose(candidates, ctx, set())
        assert selected.id in (2, 3)

    def test_prefix_hit_on_busy_prefiller_falls_back_within_cluster(self, mock_redis):
        _, storage = mock_redis
        body = make_body("shared prefix content")
        busy = make_server(1, "http://p1", role="prefiller", group_id="g1", workload=7)
        idle = make_server(2, "http://p2", role="prefiller", group_id="g1", workload=0)
        chooser = _chooser_with_workload({}, decoder_mins={"g1": 3.0})
        chooser.on_response(busy, make_context(body, request_id=42), 200)
        assert storage, "on_response should have written prefix entries"

        candidates = [busy, idle]
        selected = chooser.choose(candidates, make_context(body), set())
        # busy effective = max(7,3)=7 >= weight 1 and 7 > 2*min(3)=6 -> escape
        # to least loaded: idle effective = max(0,3)=3.
        assert selected.id == 2


class TestOnResponseRecordsPrefiller:
    def test_on_response_writes_prefix_to_prefiller(self, mock_redis):
        _, storage = mock_redis
        chooser = _chooser_with_workload({})
        body = make_body("content to cache")
        prefiller = make_server(2, "http://p1", role="prefiller", group_id="g1")

        # In the real proxy flow on_response is only ever invoked with the
        # prefiller (the node that holds the prefix cache), never the decoder.
        chooser.on_response(prefiller, make_context(body, request_id=7), 200)

        assert storage, "on_response should have written prefix entries"
        for fields in storage.values():
            assert "2" in fields
            data = json.loads(fields["2"])
            assert data["rid"] == 7


class TestChooserIsPdUnaware:
    def test_chooser_does_not_filter_by_role(self, mock_redis):
        # The chooser is intentionally PD-unaware: it picks least-loaded from
        # whatever candidates it receives. Excluding decoders is the job of
        # ServerRepository.list_pd_holders (see test_pd_repository). Here we just
        # confirm the chooser does not crash on mixed/prefiller candidates.
        candidates = [
            make_server(1, "http://m1", role="mixed", workload=0),
            make_server(2, "http://p1", role="prefiller", group_id="g1"),
        ]
        chooser = _chooser_with_workload({})
        selected = chooser.choose(candidates, make_context(make_body("anything")), set())
        assert selected is not None
