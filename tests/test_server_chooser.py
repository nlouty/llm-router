from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser
from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser


from unittest.mock import MagicMock, patch

@pytest.fixture(autouse=True)
def mock_redis():
    with patch("redis.Redis") as mock:
        client = MagicMock()
        mock.return_value = client
        # In-memory storage modelling each key as a Redis Hash (field -> value),
        # so per-server writes merge instead of overwrite (the bug being fixed).
        storage = {}
        queued_reads = []

        def mock_hset(key, field=None, value=None, mapping=None):
            store = storage.setdefault(key, {})
            if mapping:
                store.update(mapping)
            else:
                store[field] = value
            return True

        def mock_hgetall(key):
            return dict(storage.get(key, {}))

        def mock_expire(key, seconds):
            return True

        def pipe_hgetall(key):
            queued_reads.append(mock_hgetall(key))
            return True

        def pipe_execute():
            results = list(queued_reads)
            queued_reads.clear()
            return results

        client.hset.side_effect = mock_hset
        client.hgetall.side_effect = mock_hgetall
        client.expire.side_effect = mock_expire

        # Pipeline mock: writes apply immediately; reads are queued and returned
        # by execute(), so call_count assertions still work.
        pipe = MagicMock()
        client.pipeline.return_value = pipe
        pipe.hset.side_effect = mock_hset
        pipe.expire.side_effect = mock_expire
        pipe.hgetall.side_effect = pipe_hgetall
        pipe.execute.side_effect = pipe_execute

        PrefixCachePrebleServerChooser._redis_client = client
        yield client
        PrefixCachePrebleServerChooser._redis_client = None


@dataclass
class Server:
    id: int
    base_url: str
    model_id: int | None = None
    cache_time: int = 3600
    weight: int = 1


def make_server(server_id, base_url, cache_time=3600, weight=1):
    return Server(id=server_id, base_url=base_url, cache_time=cache_time, weight=weight)


def make_context(body: bytes = b"{}", request_id: int = 1):
    return ServerSelectionContext(
        request_id=request_id,
        ip_id=None,
        model_id=None,
        model_name="test-model",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
    )


def make_body(words):
    return json.dumps({"messages": [{"role": "user", "content": " ".join(words)}]}).encode("utf-8")


def test_least_connection_chooser_selects_server_with_fewest_processing_requests():
    chooser = LeastConnectionServerChooser(lambda targets: {"http://10.0.0.1:8000": 3, "http://10.0.0.2:8000": 1})
    candidates = [make_server(1, "http://10.0.0.1:8000"), make_server(2, "http://10.0.0.2:8000")]

    selected = chooser.choose(candidates, make_context(), set())

    assert selected.id == 2


def test_least_connection_chooser_randomly_selects_among_tied_least_loaded_servers(monkeypatch):
    chooser = LeastConnectionServerChooser(
        lambda targets: {
            "http://10.0.0.1:8000": 0,
            "http://10.0.0.2:8000": 0,
            "http://10.0.0.3:8000": 1,
        }
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
        make_server(3, "http://10.0.0.3:8000"),
    ]
    choices = []

    def choose(options):
        choices.append(list(options))
        return options[1]

    monkeypatch.setattr("router.route_algorithm.least_connection.random.choice", choose)

    selected = chooser.choose(candidates, make_context(), set())

    assert selected.id == 2
    assert [[server.id for server in options] for options in choices] == [[1, 2]]


def test_least_connection_chooser_skips_attempted_servers():
    chooser = LeastConnectionServerChooser(lambda targets: {"http://10.0.0.1:8000": 0, "http://10.0.0.2:8000": 1})
    candidates = [make_server(1, "http://10.0.0.1:8000"), make_server(2, "http://10.0.0.2:8000")]

    selected = chooser.choose(candidates, make_context(), {1})

    assert selected.id == 2


def test_least_connection_chooser_returns_none_when_all_attempted():
    chooser = LeastConnectionServerChooser(lambda targets: {})
    candidates = [make_server(1, "http://10.0.0.1:8000"), make_server(2, "http://10.0.0.2:8000")]

    assert chooser.choose(candidates, make_context(), {1, 2}) is None


def test_least_connection_chooser_compares_normalized_load_by_weight():
    # A: weight 3, workload 5 -> normalized 5/3 ~= 1.67
    # B: weight 1, workload 2 -> normalized 2.0  -> A wins despite higher raw workload.
    chooser = LeastConnectionServerChooser(lambda targets: {"http://10.0.0.1:8000": 5, "http://10.0.0.2:8000": 2})
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=3),
        make_server(2, "http://10.0.0.2:8000", weight=1),
    ]

    selected = chooser.choose(candidates, make_context(), set())

    assert selected.id == 1


def test_least_connection_chooser_ties_on_normalized_load(monkeypatch):
    # A: weight 2, workload 2 -> 1.0; B: weight 1, workload 1 -> 1.0 -> tie.
    chooser = LeastConnectionServerChooser(lambda targets: {"http://10.0.0.1:8000": 2, "http://10.0.0.2:8000": 1})
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=2),
        make_server(2, "http://10.0.0.2:8000", weight=1),
    ]
    choices = []

    def choose(options):
        choices.append(list(options))
        return options[1]

    monkeypatch.setattr("router.route_algorithm.least_connection.random.choice", choose)

    selected = chooser.choose(candidates, make_context(), set())

    assert selected.id == 2
    assert [[server.id for server in options] for options in choices] == [[1, 2]]


def test_prefix_cache_high_match_chooses_least_loaded_cached_server():
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 2, "http://10.0.0.2:8000": 0, "http://10.0.0.3:8000": 0},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
        make_server(3, "http://10.0.0.3:8000"),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)
    chooser.on_response(candidates[1], make_context(cached_body, request_id=102), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 2
    assert context.prefix_cache > 0.9
    assert context.last_match == 102


def test_prefix_cache_overloaded_cached_server_falls_back_to_least_loaded():
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 8, "http://10.0.0.2:8000": 0, "http://10.0.0.3:8000": 1},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
        make_server(3, "http://10.0.0.3:8000"),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    # Cached server 1 (workload 8 >= weight 1) is overloaded,
    # so fall back to the least loaded server 2.
    assert selected.id == 2


def test_prefix_cache_ratio_zero_on_overload_fallback_to_uncached_server():
    # Overload fallback selects a server with no cache entry; ratio must be 0.0.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 8, "http://10.0.0.2:8000": 0, "http://10.0.0.3:8000": 1},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
        make_server(3, "http://10.0.0.3:8000"),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 2
    assert context.prefix_cache == 0.0
    assert context.last_match is None


def test_prefix_cache_keeps_cached_server_below_suggested_workload():
    # All weight 4 (e.g. 910C): cached server 1 at workload 3 < 4 keeps affinity
    # even though a lighter server exists.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 3, "http://10.0.0.2:8000": 1, "http://10.0.0.3:8000": 2},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=4),
        make_server(2, "http://10.0.0.2:8000", weight=4),
        make_server(3, "http://10.0.0.3:8000", weight=4),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 1


def test_prefix_cache_escapes_cached_server_at_suggested_workload():
    # All weight 2 (e.g. 910B4, issue #247): cached server 1 at workload 2 >= 2
    # falls back to the least loaded server.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 2, "http://10.0.0.2:8000": 0, "http://10.0.0.3:8000": 1},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=2),
        make_server(2, "http://10.0.0.2:8000", weight=2),
        make_server(3, "http://10.0.0.3:8000", weight=2),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 2


def test_prefix_cache_high_weight_cached_server_escapes_at_own_weight():
    # Cached server 1: weight 4, workload 4 -> 4 >= 4 -> escape to idle server 2.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 4, "http://10.0.0.2:8000": 0},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=4),
        make_server(2, "http://10.0.0.2:8000", weight=1),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 2


def test_prefix_cache_high_weight_cached_server_kept_below_own_weight():
    # Cached server 1: weight 4, workload 3 -> 3 < 4 -> keep cache affinity
    # despite the idle weight-1 server.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 3, "http://10.0.0.2:8000": 0},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=4),
        make_server(2, "http://10.0.0.2:8000", weight=1),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 1


def test_prefix_cache_keeps_cached_server_without_materially_lighter_server():
    # Cached server 1: weight 4, workload 4 -> at its suggested workload, but the
    # lightest candidate is 2 and 4 > 2*2 is false -> keep cache affinity.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 4, "http://10.0.0.2:8000": 2, "http://10.0.0.3:8000": 3},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=4),
        make_server(2, "http://10.0.0.2:8000", weight=4),
        make_server(3, "http://10.0.0.3:8000", weight=4),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)

    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 1


def test_prefix_cache_medium_match_prefers_cached_server_below_suggested_workload():
    # Weight-2 pool: cached server 2 at workload 1 < 2 keeps its medium-match
    # affinity even though idle servers exist.
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 0, "http://10.0.0.2:8000": 1, "http://10.0.0.3:8000": 0},
        prefix_block_chars=1,
    )
    candidates = [
        make_server(1, "http://10.0.0.1:8000", weight=2),
        make_server(2, "http://10.0.0.2:8000", weight=2),
        make_server(3, "http://10.0.0.3:8000", weight=2),
    ]
    chooser.on_response(candidates[1], make_context(make_body([str(i) for i in range(60)]), request_id=201), 200)

    context = make_context(make_body([str(i) for i in range(60)] + [f"new-{i}" for i in range(20)]))
    selected = chooser.choose(candidates, context, set())

    assert selected.id == 2
    assert context.prefix_cache > 0.5
    assert context.last_match == 201


def test_prefix_cache_last_match_tracks_best_match_request_id():
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    candidates = [make_server(1, "http://10.0.0.1:8000")]
    chooser.on_response(candidates[0], make_context(make_body(["a", "b", "x"]), request_id=301), 200)
    chooser.on_response(candidates[0], make_context(make_body(["a", "b", "c", "d"]), request_id=302), 200)

    context = make_context(make_body(["a", "b", "c", "new"]))
    chooser.choose(candidates, context, set())

    assert context.prefix_cache > 0.5
    assert context.last_match == 302


def test_prefix_cache_last_match_is_none_without_common_prefix():
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    candidates = [make_server(1, "http://10.0.0.1:8000")]
    chooser.on_response(candidates[0], make_context(b"hello world", request_id=401), 200)

    context = make_context(b"goodbye world")
    chooser.choose(candidates, context, set())

    assert context.prefix_cache == 0.0
    assert context.last_match is None


def test_prefix_cache_concurrent_writes_merge_servers_not_overwrite():
    # Two different servers successfully serve the same prefix concurrently.
    # The cached affinity set must keep BOTH servers (issue #179 regression).
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)
    chooser.on_response(candidates[1], make_context(cached_body, request_id=102), 200)

    # Every prefix key must record both servers, not just the last writer.
    client = PrefixCachePrebleServerChooser._redis_client
    prefix_hashes = chooser._get_prefix_hashes(chooser._prefix_chars_from_body(cached_body))
    model_key = "test-model"
    for prefix_hash, _ in prefix_hashes:
        fields = client.hgetall(chooser._cache_key(model_key, prefix_hash))
        assert set(fields.keys()) == {"1", "2"}


def test_prefix_cache_surviving_server_still_reported_after_overwrite_bug():
    # The reporter's AAA / AAAB scenario: server A caches AAA, then server B
    # caches AAAB. The shared-prefix keys must retain A so a later request that
    # lands on A still reports a non-zero ratio instead of a spurious 0.0.
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
    ]
    # A serves the shorter prefix; B serves the same prefix with an extra char.
    chooser.on_response(
        candidates[0],
        make_context(make_body([str(i) for i in range(100)]), request_id=101),
        200,
    )
    chooser.on_response(
        candidates[1],
        make_context(make_body([str(i) for i in range(100)] + ["x"]), request_id=102),
        200,
    )

    # A request sharing the first 100 chars: server 1's ratio must survive.
    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    chooser.choose(candidates, context, set())
    assert context.prefix_cache > 0.0


def test_prefix_cache_request_id_tracked_per_server():
    # Each server keeps its own originating request id in the merged entry;
    # last_match reflects the selected server's request id, not a global writer.
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    candidates = [
        make_server(1, "http://10.0.0.1:8000"),
        make_server(2, "http://10.0.0.2:8000"),
    ]
    cached_body = make_body([str(i) for i in range(100)])
    chooser.on_response(candidates[0], make_context(cached_body, request_id=101), 200)
    chooser.on_response(candidates[1], make_context(cached_body, request_id=102), 200)

    # Force selection of server 1 by loading server 2 heavily.
    chooser_loaded = PrefixCachePrebleServerChooser(
        lambda targets: {"http://10.0.0.1:8000": 0, "http://10.0.0.2:8000": 100},
        prefix_block_chars=1,
    )
    context = make_context(make_body([str(i) for i in range(99)] + ["new"]))
    selected = chooser_loaded.choose(candidates, context, set())
    assert selected.id == 1
    assert context.last_match == 101


def test_prefix_cache_filters_lazily_expired_entries():
    # Entries past their expiry must be ignored at read time even if not
    # physically removed from Redis (lazy expiry via the stored timestamp).
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    client = PrefixCachePrebleServerChooser._redis_client
    body = make_body(["hello", "world"])
    prefix_hashes = chooser._get_prefix_hashes(chooser._prefix_chars_from_body(body))
    model_key = "test-model"

    # Seed an already-expired entry directly into the hash storage.
    for prefix_hash, _ in prefix_hashes:
        client.hset(
            chooser._cache_key(model_key, prefix_hash),
            "1",
            json.dumps({"exp": 0.0, "rid": 999}),
        )

    context = make_context(body)
    chooser.choose([make_server(1, "http://10.0.0.1:8000")], context, set())
    assert context.prefix_cache == 0.0
    assert context.last_match is None


def test_prefix_cache_response_hook_only_marks_successful_responses():
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=1)
    server = make_server(1, "http://10.0.0.1:8000")
    context = make_context(make_body(["hello", "world"]))

    chooser.on_response(server, context, 500)
    # Check that nothing was saved to Redis
    pipe = PrefixCachePrebleServerChooser._redis_client.pipeline.return_value
    assert pipe.hset.call_count == 0

    chooser.on_response(server, context, 200)
    assert pipe.hset.call_count > 0


def test_prefix_cache_max_prefix_chars_default():
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, max_prefix_chars=10)

    assert chooser.max_prefix_chars == 10


def test_prefix_cache_chunks_chinese_text_by_character():
    chooser = PrefixCachePrebleServerChooser(lambda targets: {}, prefix_block_chars=2)
    prefix_chars = chooser._prefix_chars_from_body(
        json.dumps({"prompt": "你好，世界。再"}, ensure_ascii=False).encode("utf-8")
    )

    assert prefix_chars == "你好，世界。再"
    hashes = chooser._get_prefix_hashes(prefix_chars)
    # Block size 2, text length 7
    # 0:2 -> "你好" (2)
    # 2:4 -> "你好，世" (4)
    # 4:6 -> "你好，世界。" (6)
    # 6:7 -> "你好，世界。再" (7)
    assert [length for _, length in hashes] == [2, 4, 6, 7]


def test_prefix_cache_uses_renamed_threshold_arguments():
    chooser = PrefixCachePrebleServerChooser(
        lambda targets: {},
        primary_match_threshold=0.91,
        secondary_match_threshold=0.41,
    )

    assert chooser.primary_match_threshold == 0.91
    assert chooser.secondary_match_threshold == 0.41


# Trie-specific memory and pruning tests removed.
