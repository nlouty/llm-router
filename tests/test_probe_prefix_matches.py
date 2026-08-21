"""Tests for the probe-based prefix-cache reads and per-request work reuse.

Covers Actions 3 (bounded binary-search reads), 4 (hash once per request),
5a (parse-once threading) and 5b (per-request DB memoization).
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser


class FakeServer:
    def __init__(self, server_id, base_url, cache_time=3600, weight=1, role="mixed", workload=0):
        self.id = server_id
        self.base_url = base_url
        self.cache_time = cache_time
        self.weight = weight
        self.role = role
        self.workload = workload


class FakePipe:
    def __init__(self, client):
        self._client = client
        self._reads = []

    def hgetall(self, key):
        self._reads.append(key)
        return self

    def hset(self, key, field, value):
        self._client.storage.setdefault(key, {})[field] = value
        return self

    def expire(self, key, seconds):
        return self

    def execute(self):
        results = [dict(self._client.storage.get(key, {})) for key in self._reads]
        self._client.hgetall_calls += len(self._reads)
        self._reads.clear()
        return results


class FakeRedis:
    def __init__(self):
        self.storage: dict[str, dict] = {}
        self.hgetall_calls = 0
        self.pipeline_calls = 0

    def hgetall(self, key):
        self.hgetall_calls += 1
        return dict(self.storage.get(key, {}))

    def pipeline(self, transaction=True):
        self.pipeline_calls += 1
        return FakePipe(self)


@pytest.fixture(autouse=True)
def fake_redis():
    client = FakeRedis()
    PrefixCachePrebleServerChooser._redis_client = client
    yield client
    PrefixCachePrebleServerChooser._redis_client = None


def _chooser(prefix_block_chars=4, **workload):
    return PrefixCachePrebleServerChooser(
        lambda targets: {t: workload.get(t, 0) for t in targets},
        prefix_block_chars=prefix_block_chars,
    )


def _prompt_body(text: str) -> bytes:
    """A body whose rendered prefix text is exactly *text*."""
    return json.dumps({"prompt": text}, ensure_ascii=False).encode("utf-8")


def _context(body, request_id=1, **extra):
    kwargs = dict(
        request_id=request_id,
        ip_id=None,
        model_id=1,
        model_name="test-model",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
    )
    kwargs.update(extra)
    return ServerSelectionContext(**kwargs)


def _seed(client, chooser, body, server_id, start, end, exp=None, rid=42):
    """Seed cache entries for block indices [start, end) of *body*."""
    text = chooser._prefix_chars_from_body(body)
    hashes = chooser._get_prefix_hashes(text)
    value = json.dumps({"exp": time.time() + 3600 if exp is None else exp, "rid": rid})
    for prefix_hash, _ in hashes[start:end]:
        key = chooser._cache_key("test-model", prefix_hash)
        client.storage.setdefault(key, {})[str(server_id)] = value


# --------------------------------------------------------------------------
# Action 3: probe-based reads
# --------------------------------------------------------------------------


def test_probe_read_finds_exact_per_server_ratios_with_log_probes():
    # 100 blocks of 4 chars. Server 1 cached through block 89, server 2
    # through block 49. Binary search must reproduce both ratios exactly and
    # stay far below the one-HGETALL-per-block scan (<= 2 * ceil(log2(100))).
    chooser = _chooser()
    client = PrefixCachePrebleServerChooser._redis_client
    body = _prompt_body("x" * 400)
    _seed(client, chooser, body, 1, 0, 90)
    _seed(client, chooser, body, 2, 0, 50, rid=77)

    context = _context(body)
    selected = chooser.choose(
        [FakeServer(1, "http://s1"), FakeServer(2, "http://s2")], context, set()
    )

    assert selected.id == 1
    assert abs(context.prefix_cache - 0.9) < 1e-9
    assert context.last_match == 42
    assert client.hgetall_calls <= 14


def test_probe_read_respects_expired_tail_blocks():
    # Server 1 cached the full body once, but blocks 80.. are already expired.
    # Only the first 80 blocks are valid: ratio must be exactly 0.8.
    chooser = _chooser()
    client = PrefixCachePrebleServerChooser._redis_client
    body = _prompt_body("x" * 400)
    _seed(client, chooser, body, 1, 0, 80)
    _seed(client, chooser, body, 1, 80, 100, exp=0.0, rid=99)

    context = _context(body)
    selected = chooser.choose([FakeServer(1, "http://s1")], context, set())

    assert selected.id == 1
    assert abs(context.prefix_cache - 0.8) < 1e-9
    assert context.last_match == 42
    assert client.hgetall_calls <= 7


def test_probe_read_no_match_stays_least_loaded():
    chooser = _chooser(**{"http://s2": 2})
    body = _prompt_body("hello world")
    context = _context(body)
    selected = chooser.choose(
        [FakeServer(1, "http://s1", workload=0), FakeServer(2, "http://s2", workload=2)],
        context,
        set(),
    )

    assert selected.id == 1
    assert context.prefix_cache == 0.0
    assert context.last_match is None


def test_probe_read_empty_redis_storage_is_a_full_miss():
    chooser = _chooser()
    body = _prompt_body("x" * 400)
    context = _context(body)
    selected = chooser.choose([FakeServer(1, "http://s1")], context, set())
    assert selected.id == 1
    assert context.prefix_cache == 0.0


def test_probe_read_survives_pipeline_failure_without_matches(monkeypatch):
    chooser = _chooser()
    client = PrefixCachePrebleServerChooser._redis_client

    def broken_execute(self):
        raise RuntimeError("redis down")

    monkeypatch.setattr(FakePipe, "execute", broken_execute)
    body = _prompt_body("x" * 400)
    context = _context(body)
    selected = chooser.choose([FakeServer(1, "http://s1")], context, set())
    assert selected.id == 1
    assert context.prefix_cache == 0.0
    assert client.hgetall_calls == 0  # failures are counted before execute


def test_get_all_model_prefix_ratios_uses_bounded_probes():
    chooser = _chooser()
    client = PrefixCachePrebleServerChooser._redis_client
    body = _prompt_body("x" * 400)
    text = chooser._prefix_chars_from_body(body)
    hashes = chooser._get_prefix_hashes(text)
    value = json.dumps({"exp": time.time() + 3600, "rid": 1})
    for model, end in (("m1", len(hashes)), ("m2", len(hashes) // 2)):
        for prefix_hash, _ in hashes[:end]:
            key = chooser._cache_key(model, prefix_hash)
            client.storage.setdefault(key, {})["1"] = value

    ratios = chooser.get_all_model_prefix_ratios(body, ["m1", "m2"])

    assert abs(ratios["m1"] - 1.0) < 1e-9
    assert abs(ratios["m2"] - 0.5) < 1e-9
    assert client.hgetall_calls <= 14  # 2 models * ceil(log2(100))


# --------------------------------------------------------------------------
# Action 4: hash once per request
# --------------------------------------------------------------------------


def test_choose_and_on_response_hash_the_body_once(monkeypatch):
    chooser = _chooser()
    body = _prompt_body("y" * 200)

    calls = {"hash": 0, "text": 0}
    original_hash = chooser._get_prefix_hashes
    original_text = chooser._text_from_body

    def counting_hash(text):
        calls["hash"] += 1
        return original_hash(text)

    def counting_text(body_bytes, parsed_data=None):
        calls["text"] += 1
        return original_text(body_bytes, parsed_data)

    monkeypatch.setattr(chooser, "_get_prefix_hashes", counting_hash)
    monkeypatch.setattr(chooser, "_text_from_body", counting_text)

    context = _context(body, request_id=7)
    server = FakeServer(1, "http://s1")
    chooser.choose([server], context, set())
    chooser.on_response(server, context, 200)

    assert calls["hash"] == 1
    assert calls["text"] == 1
    assert context._prefix_cache_work[0] == body


def test_context_prefix_work_invalidated_when_body_changes():
    chooser = _chooser()
    first = _prompt_body("aaaa")
    second = _prompt_body("aaaab")
    context = _context(first, request_id=1)

    chooser.choose([FakeServer(1, "http://s1")], context, set())
    assert context._prefix_cache_work[0] == first

    context.body = second
    context.body_data = None
    chooser.choose([FakeServer(1, "http://s1")], context, set())
    assert context._prefix_cache_work[0] == second


# --------------------------------------------------------------------------
# Action 5a: parse-once threading
# --------------------------------------------------------------------------


def test_text_from_body_reuses_parsed_data(monkeypatch):
    body = _prompt_body("hello world")
    parsed = json.loads(body.decode("utf-8"))
    expected = PrefixCachePrebleServerChooser._text_from_body(body)

    def boom(_text):
        raise AssertionError("json.loads must not run when parsed_data is provided")

    monkeypatch.setattr("router.route_algorithm.prefix_cache_preble.json.loads", boom)
    assert PrefixCachePrebleServerChooser._text_from_body(body, parsed) == expected


def test_text_from_data_matches_text_from_body_for_all_fallbacks():
    cases = [
        b'{"messages": [{"role": "user", "content": "hi"}]}',
        b'{"prompt": "raw prompt"}',
        b'{"prompt": ["a", "b"]}',
        b'{"stream": true}',  # nothing rendered -> raw text fallback
        b"{}",
    ]
    for body in cases:
        parsed = json.loads(body.decode("utf-8"))
        assert PrefixCachePrebleServerChooser._text_from_body(body, parsed) == \
            PrefixCachePrebleServerChooser._text_from_body(body), body


def test_update_body_model_reuses_parsed_data():
    from router.route_algorithm.auto import AutoRouteAlgorithm

    parsed = json.loads(b'{"model":"auto","stream":true}')
    body = AutoRouteAlgorithm.update_body_model(
        b'{"model":"auto","stream":true}', "target-model", parsed_data=parsed
    )
    data = json.loads(body.decode("utf-8"))
    assert data["model"] == "target-model"
    assert data["stream"] is True
    assert parsed["model"] == "target-model"  # the provided dict was reused


def test_apply_resolved_model_syncs_parsed_and_context_data():
    from router.route_algorithm.auto import AutoRouteAlgorithm

    service = AutoRouteAlgorithm()
    parsed = SimpleNamespace(
        body=b'{"model":"auto","messages":[]}',
        model_name="auto",
        data={"model": "auto", "messages": []},
    )
    record = SimpleNamespace(model_id=0, router_result=None, save=lambda **kwargs: None)
    context = _context(parsed.body)
    context.body_data = parsed.data
    model = SimpleNamespace(id=3, model_name="resolved-model")

    service._apply_resolved_model(parsed, record, context, model, "auto:cache_hit")

    assert parsed.model_name == "resolved-model"
    assert json.loads(parsed.body.decode("utf-8"))["model"] == "resolved-model"
    assert parsed.data["model"] == "resolved-model"
    assert context.body_data is parsed.data
    assert json.loads(context.body.decode("utf-8"))["model"] == "resolved-model"


def test_is_multimodal_uses_parsed_data(monkeypatch):
    from router.route_algorithm.auto import AutoRouteAlgorithm

    parsed = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://x"}}
    ]}]}
    real_loads = json.loads

    def boom(_text):
        raise AssertionError("json.loads must not run when parsed_data is provided")

    monkeypatch.setattr("router.route_algorithm.auto.json.loads", boom)
    assert AutoRouteAlgorithm()._is_multimodal(b"ignored", parsed) is True
    monkeypatch.setattr("router.route_algorithm.auto.json.loads", real_loads)


def test_user_prompt_count_uses_parsed_data(monkeypatch):
    from router.route_algorithm.auto import AutoRouteAlgorithm

    parsed = {"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}
    real_loads = json.loads

    def boom(_text):
        raise AssertionError("json.loads must not run when parsed_data is provided")

    monkeypatch.setattr("router.route_algorithm.auto.json.loads", boom)
    assert AutoRouteAlgorithm._user_prompt_count_from_body(b"ignored", parsed) == 2
    monkeypatch.setattr("router.route_algorithm.auto.json.loads", real_loads)


# --------------------------------------------------------------------------
# Action 5b: per-request DB memoization
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_sticky_resolution_calls_get_by_id_once_per_unique_model(monkeypatch):
    from django.utils import timezone

    from router.models import Model, RequestRecord, Server
    from router.repositories.models import ModelRepository
    from router.route_algorithm.auto import AutoRouteAlgorithm

    target_model = Model.objects.create(model_name="target", complexity_min=1, complexity_max=10)
    no_server_model = Model.objects.create(model_name="no-server", complexity_min=1, complexity_max=10)
    Server.objects.create(model_id=target_model.id, base_url="http://target.example", is_online=True)
    # Newest-first order: two server-less anchors, then the target anchor.
    RequestRecord.objects.create(
        user_ip_id=0,
        ip_id=1,
        send_time=timezone.now(),
        model_id=target_model.id,
        task_status="success",
        session="sess-memo",
        router_result="auto:complexity:5",
    )
    for _ in range(2):  # two anchors for the same server-less model
        RequestRecord.objects.create(
            user_ip_id=0,
            ip_id=1,
            send_time=timezone.now(),
            model_id=no_server_model.id,
            task_status="success",
            session="sess-memo",
            router_result="auto:complexity:5",
        )

    seen = []
    original = ModelRepository.get_by_id

    def counting_get_by_id(model_id):
        seen.append(model_id)
        return original(model_id)

    monkeypatch.setattr(ModelRepository, "get_by_id", counting_get_by_id)

    context = _context(b'{"model":"auto","messages":[]}')
    context.session = "sess-memo"
    chosen = AutoRouteAlgorithm()._resolve_sticky_model(context)

    assert chosen == target_model
    # Two records share the server-less model id: one lookup for it, one for the target.
    assert seen == [no_server_model.id, target_model.id]


@pytest.mark.django_db
def test_pd_holders_cached_reuses_one_query_per_request(monkeypatch):
    from router.repositories.servers import ServerRepository
    from router.services.proxy import ProxyService

    calls = []
    original = ServerRepository.list_pd_holders

    def counting_list_pd_holders(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(ServerRepository, "list_pd_holders", counting_list_pd_holders)
    service = ProxyService()

    first = service._pd_holders_cached(1)
    second = service._pd_holders_cached(1)
    third = service._pd_holders_cached(1, min_context_window=1000)

    assert first == second == []
    assert third == []
    assert len(calls) == 2  # (model, window=0) once, (model, window=1000) once
