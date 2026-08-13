from django.utils import timezone

from router.models import Model, RequestRecord, Server
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.services.proxy import _RetryState


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


class TestClusterDecoderMinLoad:
    def test_decoder_min_is_min_workload_per_cluster(self):
        servers = [
            _server("http://p1", role="prefiller", group_id="g1", workload=2),
            _server("http://p2", role="prefiller", group_id="g1", workload=5),
            _server("http://d1", role="decoder", group_id="g1", workload=3),
            _server("http://d2", role="decoder", group_id="g1", workload=8),
            _server("http://m1", role="mixed"),
        ]
        decoder_mins = ServerRepository.cluster_decoder_min_load(servers)
        assert decoder_mins == {"g1": 3.0}

    def test_cluster_without_decoder_is_omitted(self):
        servers = [
            _server("http://p1", role="prefiller", group_id="g1", workload=2),
            _server("http://d1", role="decoder", group_id="g2", workload=1),
        ]
        decoder_mins = ServerRepository.cluster_decoder_min_load(servers)
        # g1 has no decoder -> omitted
        assert decoder_mins == {"g2": 1.0}


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

    def test_visible_when_registered_with_model_id(self):
        # Regression: add_server always assigns a real model_id. The picker must
        # still find a decoder that carries one (previously list_by_model_id(None)
        # silently limited results to NULL-model servers).
        model = Model.objects.create(model_name="glm-4")
        _server(
            "http://d1", role="decoder", group_id="g1", model_id=model.id
        )
        d = ServerRepository.pick_least_tokens_decoder("g1")
        assert d is not None
        assert d.base_url == "http://d1"


class TestDecoderCircuitRecovery:
    def test_decode_success_closes_half_open_decoder(self):
        # Regression for #192: a decoder must recover from half_open on a
        # successful decode. Decode success previously only recorded success on
        # the prefiller, leaving the decoder stuck half_open indefinitely.
        from router.services.circuit_breaker import CircuitBreakerService

        decoder = _server(
            "http://d-rec", role="decoder", group_id="g1",
            circuit_state="half_open", consecutive_failures=3, cooldown_seconds=30,
        )
        cb = CircuitBreakerService()
        cb.record_success(decoder)
        decoder.refresh_from_db()
        assert decoder.circuit_state == "closed"
        assert decoder.consecutive_failures == 0

    def test_normal_decode_records_success_on_decoder(self, monkeypatch):
        # The actual fix lives here: on a terminal-success decode, PDForwardService
        # must call record_success on the decoder (not just the prefiller). Before
        # the fix this assertion failed because only _notify_chooser_response
        # (prefiller) ran.
        from router.services import proxy_pd_forward
        from router.services.proxy_pd_forward import PDForwardService

        prefiller = _server("http://p-rec", role="prefiller", group_id="g1")
        decoder = _server(
            "http://d-rec2", role="decoder", group_id="g1",
            circuit_state="half_open", consecutive_failures=3,
        )

        record = RequestRecord.objects.create(
            user_ip_id=1, ip_id=None, send_time=timezone.now(),
            model_id=1, task_status="processing",
        )

        fake_cb = type("CB", (), {"record_success": staticmethod(lambda s: None)})()
        fake_proxy = type(
            "P",
            (),
            {
                "_build_url": staticmethod(lambda base, path, qs: f"{base}/{path}"),
                "_notify_chooser_response": staticmethod(lambda s, ctx, code: None),
                "_after_finish": staticmethod(lambda vip, m: None),
                "_decrement_workload": staticmethod(lambda s: None),
                "circuit_breaker": fake_cb,
                "normal_timeout": 5,
            },
        )()

        svc = PDForwardService.__new__(PDForwardService)
        svc.proxy = fake_proxy
        svc.circuit_breaker = fake_cb

        called = []

        def fake_record_success(server):
            called.append(server.id)

        monkeypatch.setattr(fake_cb, "record_success", fake_record_success)

        def fake_post_decode(d, url, h, b):
            fake_response = type("R", (), {"reason": "OK", "headers": {}})()
            content = b'{"choices":[{"message":{"content":"x"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            return fake_response, content, 200

        monkeypatch.setattr(PDForwardService, "_post_decode", staticmethod(fake_post_decode))

        result = svc._normal_decode(
            "chat/completions", {}, b'{"messages":[]}', record,
            ServerSelectionContext(
                request_id=1, ip_id=None, model_id=1, model_name="m",
                path="chat/completions", method="POST", is_stream=False, body=b"{}",
            ),
            False, None,
            _RetryState(), prefiller, {}, 1, 0, "P: x",
        )

        assert result.response is not None  # terminal success, not a retry
        assert decoder.id in called  # decoder success recorded
