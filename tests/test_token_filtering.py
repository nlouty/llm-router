import pytest
from router.models import Server, Model
from router.repositories.servers import ServerRepository
from router.services.parser import RequestParser


@pytest.mark.django_db
def test_list_by_model_id_does_not_filter_by_estimate():
    # Issue #153: server selection must never pre-decide by estimated tokens.
    model = Model.objects.create(model_name="test-model", concurrent_limit=10)
    s_large = Server.objects.create(
        model_id=model.id, base_url="http://large.example", is_online=True, context_window=100000
    )
    s_small = Server.objects.create(
        model_id=model.id, base_url="http://small.example", is_online=True, context_window=1000
    )

    # A large estimate no longer excludes the small-window server.
    servers = ServerRepository.list_by_model_id(model.id)
    assert len(servers) == 2
    assert s_large in servers
    assert s_small in servers


@pytest.mark.django_db
def test_min_context_window_filters_for_retry():
    # On a real overflow the router retries on a strictly larger window.
    model = Model.objects.create(model_name="retry-model")
    s_smaller = Server.objects.create(
        model_id=model.id, base_url="http://s1.example", is_online=True, context_window=1000
    )
    s_larger = Server.objects.create(
        model_id=model.id, base_url="http://s2.example", is_online=True, context_window=100000
    )

    # min_context_window=1000 keeps only windows strictly larger than 1000.
    servers = ServerRepository.list_by_model_id(model.id, min_context_window=1000)
    assert servers == [s_larger]


@pytest.mark.django_db
def test_unlimited_context_window_always_eligible_for_retry():
    model = Model.objects.create(model_name="unlimited-model")
    s_unlimited = Server.objects.create(
        model_id=model.id, base_url="http://unlimited.example", is_online=True, context_window=None
    )
    s_limited = Server.objects.create(
        model_id=model.id, base_url="http://limited.example", is_online=True, context_window=1000
    )

    # NULL context window (unlimited) is always eligible, even beyond a floor.
    servers = ServerRepository.list_by_model_id(model.id, min_context_window=1000000)
    assert servers == [s_unlimited]


@pytest.mark.django_db
def test_list_pd_holders_min_context_window_gates_prefill_tier_only():
    # Issue #248: the overflow retry's larger-window query keeps larger
    # prefillers and mixed servers, drops smaller prefillers, and never
    # returns decoders regardless of their window.
    model = Model.objects.create(model_name="pd-model")
    p_small = Server.objects.create(
        model_id=model.id, base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", context_window=1000,
    )
    Server.objects.create(
        model_id=model.id, base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", context_window=None,
    )
    p_large = Server.objects.create(
        model_id=model.id, base_url="http://p2.example", is_online=True,
        role="prefiller", group_id="g2", context_window=100000,
    )
    Server.objects.create(
        model_id=model.id, base_url="http://d2.example", is_online=True,
        role="decoder", group_id="g2", context_window=None,
    )
    mixed = Server.objects.create(
        model_id=model.id, base_url="http://m.example", is_online=True,
        context_window=200000,
    )

    servers = ServerRepository.list_pd_holders(model.id, min_context_window=1000)
    assert p_large in servers
    assert mixed in servers
    assert p_small not in servers
    assert not any((s.role or "mixed") == "decoder" for s in servers)


@pytest.mark.django_db
def test_parser_estimates_tokens_and_storage():
    parser = RequestParser()
    body = b'{"model":"test-model","prompt":"Hello world, this is a test prompt to estimate tokens."}'
    parsed = parser.parse(body)

    assert parsed.estimated_full_body_tokens > 0
    # Estimate is prompt-bytes // 4, which is well above 5 for this body.
    assert parsed.estimated_full_body_tokens > 5
