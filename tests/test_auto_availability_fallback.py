"""Availability-aware auto routing: complexity targets are filtered by live
server pools; a matched model with no healthy server either receives an
in-band half-open probe request or traffic diverts to a healthy survivor."""

import json
from unittest.mock import MagicMock

import pytest
from django.http import HttpResponse
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Model, RequestRecord, Server
from router.repositories.servers import ServerRepository
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext


BODY = b'{"model":"auto","messages":[{"role":"user","content":"plan a migration"}]}'
TWO_USER_BODY = (
    b'{"model":"auto","messages":[{"role":"user","content":"earlier"},'
    b'{"role":"user","content":"plan a migration"}]}'
)

FALLBACK_RESULT = "complexity:7:availability_fallback:targets unavailable (big-model)"


class _Chooser:
    def choose(self, candidates, context, attempted):
        return candidates[0]


class _RatioChooser(_Chooser):
    def __init__(self, ratios):
        self.ratios = ratios
        self.requested = []

    def get_all_model_prefix_ratios(self, body, model_names):
        self.requested.append(list(model_names))
        return {name: self.ratios.get(name, 0.0) for name in model_names}


def _choosing_response(complexity, status=200):
    content = json.dumps(
        {"choices": [{"message": {"content": json.dumps({"complexity": complexity})}}]}
    )
    return HttpResponse(content.encode("utf-8"), status=status)


class _ChoosingProxy:
    """forward_internal stub answering with a fixed complexity."""

    def __init__(self, complexity, status=200):
        self._complexity = complexity
        self._status = status

    def forward_internal(self, body, model, path="chat/completions"):
        return _choosing_response(self._complexity, status=self._status)


class _CountingProxy(_ChoosingProxy):
    """Counts real pool computations: like the production proxy, repeated
    lookups for the same model are served from a per-request memo."""

    def __init__(self, complexity):
        super().__init__(complexity)
        self.pool_queries = []
        self._pool_cache = {}

    def _pd_holders_cached(self, model_id, vip=None, min_context_window=0):
        if model_id not in self._pool_cache:
            self.pool_queries.append(model_id)
            self._pool_cache[model_id] = ServerRepository.list_pd_holders(
                model_id, vip=vip, min_context_window=min_context_window
            )
        return self._pool_cache[model_id]


def _target(name, complexity_min, complexity_max):
    return Model.objects.create(
        model_name=name,
        complexity_min=complexity_min,
        complexity_max=complexity_max,
    )


def _server(model, name, circuit_state="closed", is_online=True, workload=0):
    return Server.objects.create(
        model_id=model.id,
        base_url=f"http://{name}.example",
        is_online=is_online,
        circuit_state=circuit_state,
        workload=workload,
        cooldown_seconds=3000,
        last_state_change_at=timezone.now(),
    )


def _routing_setup(complexity, proxy_class=_ChoosingProxy):
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    return proxy_class(complexity)


def _context(body=BODY, session=None):
    return ServerSelectionContext(
        request_id=123,
        ip_id=None,
        model_id=None,
        model_name="auto",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=body,
        session=session,
    )


def _route(service, body=BODY, session=None):
    return service._get_auto_route_model(body, MagicMock(id=123), _context(body, session))


@pytest.mark.django_db
def test_healthy_match_unchanged():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    _server(big, "big")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == big
    assert router_result == "complexity:7"


@pytest.mark.django_db
def test_offline_matched_model_diverts_to_survivor():
    small = _target("small-model", 1, 5)
    _target("big-model", 6, 10)  # no servers at all
    _server(small, "small")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == small
    assert router_result == FALLBACK_RESULT


@pytest.mark.django_db
def test_half_open_matched_model_gets_probe_request():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    # Half-open with spare probe capacity: routable pool, no closed circuit.
    _server(big, "big", circuit_state="half_open", workload=0)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == big
    assert router_result == "complexity:7:availability_probe"


@pytest.mark.django_db
def test_half_open_at_probe_capacity_diverts():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    # workload >= half_open_probe_limit (default 1): not routable at all.
    _server(big, "big", circuit_state="half_open", workload=1)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == small
    assert router_result == FALLBACK_RESULT


@pytest.mark.django_db
def test_open_circuit_in_cooldown_diverts():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    # Cooldown just started (3000s): excluded from the routable pool.
    _server(big, "big", circuit_state="open")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == small
    assert router_result == FALLBACK_RESULT


@pytest.mark.django_db
def test_probe_disabled_diverts_even_with_probe_pool(monkeypatch):
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    _server(big, "big", circuit_state="half_open", workload=0)
    router_cfg = dict(APP_CONFIG.get("router", {}))
    router_cfg["availability_probe_enabled"] = False
    monkeypatch.setitem(APP_CONFIG, "router", router_cfg)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == small
    assert router_result == FALLBACK_RESULT


@pytest.mark.django_db
def test_no_healthy_target_keeps_legacy_matched_model():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    # Nothing healthy anywhere: the matched model still receives the request,
    # exactly like before the availability filter existed.
    _server(small, "small", circuit_state="open")
    _server(big, "big", circuit_state="open")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == big
    assert router_result == "complexity:7"


@pytest.mark.django_db
def test_range_gap_falls_back_to_default_model():
    small = _target("small-model", 1, 3)
    _target("big-model", 8, 10)
    _server(small, "small")
    Model.objects.create(model_name=AutoRouteAlgorithm.fallback_model_name())
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(5))

    model, router_result = _route(service)

    assert model.model_name == AutoRouteAlgorithm.fallback_model_name()
    assert router_result.startswith("routing_failed:no_model_for_complexity")


@pytest.mark.django_db
def test_overlap_resolved_by_health():
    # Both ranges cover 5; only one model is alive, so the survivor wins
    # instead of degrading to the multiple-match fallback model.
    small = _target("small-model", 1, 5)
    _target("mid-model", 4, 7)
    _server(small, "small")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(5))

    model, router_result = _route(service)

    assert model == small
    assert router_result == "complexity:5"


@pytest.mark.django_db
def test_cache_hit_skips_unhealthy_model():
    _target("small-model", 1, 5)  # would be a 0.99 prefix hit, but has no servers
    big = _target("big-model", 6, 10)
    _server(big, "big")
    chooser = _RatioChooser({"small-model": 0.99, "big-model": 0.0})
    service = AutoRouteAlgorithm(chooser, proxy=_routing_setup(7))

    model, router_result = _route(service, body=TWO_USER_BODY)

    # The dead model never enters the prefix-ratio query.
    assert chooser.requested == [["big-model"]]
    assert model == big
    assert router_result == "complexity:7"


def _anchor_record(session, model, router_result):
    return RequestRecord.objects.create(
        user_ip_id=0,
        ip_id=1,
        send_time=timezone.now(),
        model_id=model.id,
        task_status="success",
        session=session,
        router_result=router_result,
    )


@pytest.mark.django_db
def test_sticky_anchor_on_unavailable_model_falls_through_and_diverts():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    _server(big, "big", circuit_state="open")
    _anchor_record("s1", big, "auto:complexity:7")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service, session="s1")

    assert model == small
    assert router_result == FALLBACK_RESULT


@pytest.mark.django_db
def test_sticky_anchor_on_probe_tier_model_probes():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    _server(big, "big", circuit_state="half_open", workload=0)
    _anchor_record("s1", big, "auto:complexity:7")
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service, session="s1")

    assert model == big
    assert router_result == "complexity:7:availability_probe"


def test_availability_results_are_sticky_anchors():
    # Per design decision: a diverted session keeps its fallback model until
    # that model stops serving (availability results count as anchors).
    assert AutoRouteAlgorithm.is_sticky_anchor_result(
        "auto:complexity:7:availability_fallback:targets unavailable (big-model)"
    )
    assert AutoRouteAlgorithm.is_sticky_anchor_result("auto:complexity:7:availability_probe")


@pytest.mark.django_db
def test_multiple_unhealthy_matches_probe_first_by_id():
    small = _target("small-model", 1, 5)
    mid = _target("mid-model", 6, 8)
    _target("big-model", 7, 10)
    _server(small, "small")
    _server(mid, "mid", circuit_state="half_open", workload=0)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == mid
    assert router_result == "complexity:7:availability_probe"


@pytest.mark.django_db
def test_divert_picks_least_loaded_survivor():
    small = _target("small-model", 1, 3)
    mid = _target("mid-model", 4, 6)
    _target("big-model", 7, 10)  # offline
    _server(small, "small", workload=5)
    _server(mid, "mid", workload=0)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_routing_setup(7))

    model, router_result = _route(service)

    assert model == mid
    assert router_result == "complexity:7:availability_fallback:targets unavailable (big-model)"


@pytest.mark.django_db
def test_classifier_failure_keeps_default_model():
    # Per design decision: classifier failures keep the classic blind
    # fallback-model behavior; no availability logic runs.
    _target("small-model", 1, 5)
    fallback = Model.objects.create(model_name=AutoRouteAlgorithm.fallback_model_name())
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)
    service = AutoRouteAlgorithm(_Chooser(), proxy=_ChoosingProxy(7, status=500))

    model, router_result = _route(service)

    assert model == fallback
    assert router_result.startswith("routing_failed")


@pytest.mark.django_db
def test_availability_checks_share_candidate_memo():
    small = _target("small-model", 1, 5)
    big = _target("big-model", 6, 10)
    _server(small, "small")
    proxy = _routing_setup(7, proxy_class=_CountingProxy)
    service = AutoRouteAlgorithm(_Chooser(), proxy=proxy)

    model, router_result = _route(service)

    assert model == small
    assert router_result == FALLBACK_RESULT
    # Each model's pool is queried exactly once: health checks, sticky
    # resolution and survivor scoring all reuse the per-request memo.
    assert proxy.pool_queries.count(small.id) == 1
    assert proxy.pool_queries.count(big.id) == 1
