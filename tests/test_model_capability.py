import json
from datetime import datetime

import pytest
from django.test import Client

from router.models import Ips, Model, RequestRecord, Server, UserIP

_server_url_counter = [0]


def _make_server(model, context_window=None, base_url=None):
    _server_url_counter[0] += 1
    return Server.objects.create(
        model_id=model.id,
        base_url=base_url or f"http://{model.model_name}-{_server_url_counter[0]}.example",
        is_online=True,
        context_window=context_window,
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Pin the local clock to a weekday noon so the off-hours x4 concurrency
    boost never applies unless a test moves the clock.

    The fake ``localtime`` must accept the optional value argument Django's
    template layer passes it: >=400 responses are logged through the debug
    error-page renderer, which calls ``localtime(value)``.
    """
    clock = {"now": datetime(2026, 6, 1, 12, 0, 0)}  # Monday 12:00 -> no boost

    def fake_localtime(value=None):
        return clock["now"]

    monkeypatch.setattr("django.utils.timezone.localtime", fake_localtime)
    return clock


def _entry_by_id(payload, model_id):
    return next(entry for entry in payload["data"] if entry["id"] == model_id)


def test_normal_port_capabilities():
    # model-a is an auto-routing target (complexity bounds set).
    model_a = Model.objects.create(
        model_name="model-a", concurrent_limit=3, max_tokens=20480,
        complexity_min=1, complexity_max=10,
    )
    _make_server(model_a, context_window=1000)
    _make_server(model_a, context_window=200000)
    # model-b is not an auto target (no complexity bounds).
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=10, max_tokens=8192)
    _make_server(model_b, context_window=None)  # unlimited

    response = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["ip"] == "10.0.0.1"
    assert "employee_no" not in payload
    # Gateway internals (port, vip channel, multiplier, boost window) are
    # intentionally not exposed to users.
    for hidden in ("port", "vip_channel", "concurrent_multiplier", "concurrent_boost_active"):
        assert hidden not in payload

    entry_a = _entry_by_id(payload, "model-a")
    assert entry_a["max_context"] == 200000  # max over all online servers
    assert entry_a["max_output_tokens"] == 20480
    assert entry_a["concurrent_limit"] == 3

    entry_b = _entry_by_id(payload, "model-b")
    assert entry_b["max_context"] is None  # every server unlimited
    assert entry_b["max_output_tokens"] == 8192
    assert entry_b["concurrent_limit"] == 10

    auto = _entry_by_id(payload, "auto")
    assert auto["max_output_tokens"] == 40000
    assert auto["max_context"] == 200000  # min over auto targets (only model-a)
    assert auto["concurrent_limit"] == 6


def test_auto_max_context_is_min_of_redirect_targets():
    # Two text targets and one multimodal target: auto advertises the
    # smallest max-context so users are never misled by a larger target's
    # window when auto picks a smaller one.
    model_a = Model.objects.create(
        model_name="model-a", concurrent_limit=3, max_tokens=20480,
        complexity_min=1, complexity_max=10,
    )
    _make_server(model_a, context_window=200000)
    model_b = Model.objects.create(
        model_name="model-b", concurrent_limit=3, max_tokens=20480,
        complexity_min=1, complexity_max=10,
    )
    _make_server(model_b, context_window=50000)
    model_c = Model.objects.create(
        model_name="model-c", concurrent_limit=3, max_tokens=20480, multimodal=True
    )
    _make_server(model_c, context_window=10000)

    payload = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.9").json()

    assert _entry_by_id(payload, "auto")["max_context"] == 10000


def test_auto_max_context_skips_unlimited_targets():
    # A target with an unlimited (NULL) window never drags the advertised
    # ceiling down; the min is over the finite windows only.
    model_a = Model.objects.create(
        model_name="model-a", concurrent_limit=3, max_tokens=20480,
        complexity_min=1, complexity_max=10,
    )
    _make_server(model_a, context_window=200000)
    model_b = Model.objects.create(
        model_name="model-b", concurrent_limit=3, max_tokens=20480,
        complexity_min=1, complexity_max=10,
    )
    _make_server(model_b, context_window=None)  # unlimited

    payload = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.10").json()

    assert _entry_by_id(payload, "auto")["max_context"] == 200000


def test_auto_max_context_none_without_targets():
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model, context_window=200000)

    payload = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.11").json()

    assert _entry_by_id(payload, "auto")["max_context"] is None


def test_deprecated_model_hidden_on_normal_port_but_visible_on_vip_port():
    model = Model.objects.create(
        model_name="legacy-model", concurrent_limit=3, max_tokens=20480, deprecation="retired"
    )
    _make_server(model)

    normal = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.1").json()
    assert "legacy-model" not in [entry["id"] for entry in normal["data"]]

    Ips.objects.create(ip="10.0.0.2", concurrent_multiplier=1.0, vip=True)
    vip = Client().get("/v1/models", SERVER_PORT="8008", REMOTE_ADDR="10.0.0.2").json()
    entry = _entry_by_id(vip, "legacy-model")
    assert entry["max_output_tokens"] == 20480


def test_vip_port_reports_unlimited_concurrency_and_skips_multiplier():
    Ips.objects.create(ip="10.0.0.3", concurrent_multiplier=2.5, vip=True)
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)

    response = Client().get("/v1/models", SERVER_PORT="8008", REMOTE_ADDR="10.0.0.3")

    assert response.status_code == 200
    payload = response.json()
    assert _entry_by_id(payload, "model-a")["concurrent_limit"] is None
    assert _entry_by_id(payload, "auto")["concurrent_limit"] is None


def test_non_vip_ip_blocked_on_vip_port():
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)

    response = Client().get("/v1/models", SERVER_PORT="8008", REMOTE_ADDR="10.0.0.4")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Port 8008 is closed, please use port 8001"


def test_concurrent_multiplier_scales_limit():
    Ips.objects.create(ip="10.0.0.5", concurrent_multiplier=2.5, vip=False)
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)

    payload = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.5").json()

    assert _entry_by_id(payload, "model-a")["concurrent_limit"] == 8  # ceil(3 * 2.5)


def test_off_peak_boost_window_quadruples_limit(_fixed_clock):
    Ips.objects.create(ip="10.0.0.6", concurrent_multiplier=1.0, vip=False)
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)

    _fixed_clock["now"] = datetime(2026, 6, 6, 20, 0, 0)  # Saturday 20:00 -> boost

    payload = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.6").json()

    assert _entry_by_id(payload, "model-a")["concurrent_limit"] == 12
    assert _entry_by_id(payload, "auto")["concurrent_limit"] == 24


def test_apikey_identity_included_and_no_request_record():
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)
    UserIP.objects.create(apikey="sk-test-1234", employee_no="E001", ip_id=0, is_valid=True)

    response = Client().get(
        "/v1/models",
        SERVER_PORT="8001",
        REMOTE_ADDR="10.0.0.7",
        HTTP_AUTHORIZATION="Bearer sk-test-1234",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["employee_no"] == "E001"
    # The local capability response is not a proxied request: no record row.
    assert RequestRecord.objects.count() == 0


def test_models_capability_json_serializable():
    model = Model.objects.create(model_name="model-a", concurrent_limit=3, max_tokens=20480)
    _make_server(model)

    response = Client().get("/v1/models", SERVER_PORT="8001", REMOTE_ADDR="10.0.0.8")

    assert response.status_code == 200
    json.dumps(response.json())  # must not raise
