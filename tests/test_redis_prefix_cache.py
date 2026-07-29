import json
from unittest.mock import MagicMock, patch
import pytest

from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser
from router.route_algorithm.base import ServerSelectionContext


class MockServer:
    def __init__(self, server_id, base_url):
        self.id = server_id
        self.base_url = base_url
        self.cache_time = 3600


@pytest.fixture
def mock_redis():
    with patch("redis.Redis") as mock:
        client = MagicMock()
        mock.return_value = client
        # In-memory Redis Hash model: key -> {field: value}. Per-server writes
        # merge into the same key instead of overwriting the whole value.
        storage = {}
        queued_reads = []

        def hset(key, field=None, value=None, mapping=None):
            store = storage.setdefault(key, {})
            if mapping:
                store.update(mapping)
            else:
                store[field] = value
            return True

        def hgetall(key):
            return dict(storage.get(key, {}))

        client.hset.side_effect = hset
        client.hgetall.side_effect = hgetall
        client.expire.return_value = True

        pipe = MagicMock()
        client.pipeline.return_value = pipe
        pipe.hset.side_effect = hset
        pipe.expire.return_value = True
        pipe.hgetall.side_effect = lambda key: queued_reads.append(hgetall(key))
        pipe.execute.side_effect = lambda: (list(queued_reads), queued_reads.clear())[0]

        PrefixCachePrebleServerChooser._redis_client = client
        yield client
        PrefixCachePrebleServerChooser._redis_client = None


def _make_context(body, request_id=1):
    return ServerSelectionContext(
        request_id=request_id,
        ip_id=1,
        model_id=1,
        model_name="test-model",
        path="/v1/completions",
        method="POST",
        is_stream=False,
        body=body,
    )


def test_redis_prefix_cache_flow(mock_redis):
    chooser = PrefixCachePrebleServerChooser(
        count_provider=lambda targets: {t: 0 for t in targets},
        prefix_block_chars=4,
    )

    candidates = [MockServer(1, "http://server1"), MockServer(2, "http://server2")]
    body = b'{"prompt": "abcdefghij"}'

    # 1. on_response writes per-server hash fields. Character blocks:
    # "abcd", "abcdefgh", "abcdefghij" -> 3 prefix hashes.
    chooser.on_response(candidates[0], _make_context(body, request_id=1), 200)
    pipe = mock_redis.pipeline.return_value
    assert pipe.hset.call_count == 3

    # 2. choose reads them back. The new request shares the first two blocks
    # ("abcd", "abcdefgh") but differs in the tail, so the match ratio is 8/10.
    new_context = _make_context(b'{"prompt": "abcdefghXY"}', request_id=2)
    selected = chooser.choose(candidates, new_context, set())

    assert selected.id == 1
    assert new_context.prefix_cache == 0.8
    assert new_context.last_match == 1


def test_redis_prefix_cache_merges_servers_without_overwrite(mock_redis):
    # Two servers serve the same prompt: both must end up in each hash, not
    # just the last writer (issue #179).
    chooser = PrefixCachePrebleServerChooser(
        count_provider=lambda targets: {t: 0 for t in targets},
        prefix_block_chars=4,
    )
    candidates = [MockServer(1, "http://server1"), MockServer(2, "http://server2")]
    body = b'{"prompt": "abcdefghij"}'

    chooser.on_response(candidates[0], _make_context(body, request_id=1), 200)
    chooser.on_response(candidates[1], _make_context(body, request_id=2), 200)

    prefix_hashes = chooser._get_prefix_hashes(chooser._prefix_chars_from_body(body))
    for prefix_hash, _ in prefix_hashes:
        fields = mock_redis.hgetall(chooser._cache_key("test-model", prefix_hash))
        assert set(fields) == {"1", "2"}
        assert json.loads(fields["1"])["rid"] == 1
        assert json.loads(fields["2"])["rid"] == 2
