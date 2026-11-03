from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import requests
from django.utils import timezone

from router.models import Model, RequestRecord, Server
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.services.proxy import ProxyService


class _ChooserOnce:
    def __init__(self, server):
        self._server = server

    def choose(self, candidates, context, attempted):
        if self._server.id in attempted:
            return None
        return self._server

    def on_response(self, server, context, status_code):
        return None


def _ctx(record_id: int = 1):
    return ServerSelectionContext(
        request_id=record_id,
        ip_id=None,
        model_id=None,
        model_name="m",
        path="chat/completions",
        method="POST",
        is_stream=False,
        body=b"{}",
    )


def test_increment_and_decrement_workload_are_atomic():
    server = Server.objects.create(base_url="http://w1.example", is_online=True)
    assert server.workload == 0

    ServerRepository.increment_workload(server)
    ServerRepository.increment_workload(server)
    server.refresh_from_db()
    assert server.workload == 2

    ServerRepository.decrement_workload(server)
    server.refresh_from_db()
    assert server.workload == 1


def test_decrement_workload_never_goes_below_zero():
    server = Server.objects.create(base_url="http://w2.example", is_online=True)

    ServerRepository.decrement_workload(server)

    server.refresh_from_db()
    assert server.workload == 0


def _make_upstream(status: int = 200, body: bytes = b"{}"):
    upstream = MagicMock()
    upstream.status_code = status
    upstream.reason = "OK" if status == 200 else "Bad"
    upstream.content = body
    upstream.headers = {}
    return upstream


def test_workload_decremented_after_normal_success(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://norm.example", is_online=True)

    service = ProxyService(chooser=_ChooserOnce(server))

    def fake_request(self_inner, method, url, **kwargs):
        server.refresh_from_db()
        assert server.workload == 1
        return _make_upstream(200, b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}')

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None

    parsed = MagicMock(stream=False, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    service.forward(django_request, "chat/completions", parsed, None, None, None)

    server.refresh_from_db()
    assert server.workload == 0


def test_workload_decremented_after_normal_request_exception(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://normfail.example", is_online=True)

    service = ProxyService(chooser=_ChooserOnce(server))

    def boom(self_inner, method, url, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        boom,
    )

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None

    parsed = MagicMock(stream=False, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    service.forward(django_request, "chat/completions", parsed, None, None, None)

    server.refresh_from_db()
    assert server.workload == 0


def test_workload_decremented_after_stream_error_response(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://streamerr.example", is_online=True)

    service = ProxyService(chooser=_ChooserOnce(server))

    def fake_request(method, url, **kwargs):
        server.refresh_from_db()
        assert server.workload == 1
        upstream = _make_upstream(400, b'{"error": {"message": "bad"}}')
        return upstream

    monkeypatch.setattr("router.services.proxy.requests.request", fake_request)

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None

    parsed = MagicMock(stream=True, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    service.forward(django_request, "chat/completions", parsed, None, None, None)

    server.refresh_from_db()
    assert server.workload == 0


def test_workload_kept_until_stream_generator_completes(monkeypatch):
    Model.objects.create(model_name="m")
    server = Server.objects.create(model_id=None, base_url="http://streamok.example", is_online=True)

    service = ProxyService(chooser=_ChooserOnce(server))

    chunks = [b"data: hello\n\n", b"data: [DONE]\n\n"]

    def fake_request(method, url, **kwargs):
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.reason = "OK"
        upstream.headers = {}
        upstream.iter_content = lambda chunk_size=8192: iter(chunks)
        return upstream

    monkeypatch.setattr("router.services.proxy.requests.request", fake_request)

    django_request = MagicMock()
    django_request.method = "POST"
    django_request.headers = {}
    django_request.META = {"QUERY_STRING": ""}
    django_request.client_disconnect_tracker = None

    parsed = MagicMock(stream=True, body=b"{}", model_name="m", estimated_full_body_tokens=0)
    response = service.forward(django_request, "chat/completions", parsed, None, None, None)

    server.refresh_from_db()
    assert server.workload == 1

    list(response.streaming_content)

    server.refresh_from_db()
    assert server.workload == 0


def _make_stale_processing(target_pod_ip: str, model_id: int, minutes_ago: int = 30) -> RequestRecord:
    record = RequestRepository.create_processing(ip_id=1, model_id=model_id, is_stream=False, user_agent="t")
    stale_time = timezone.now() - timedelta(minutes=minutes_ago)
    RequestRecord.objects.filter(id=record.id).update(send_time=stale_time, target_pod_ip=target_pod_ip)
    record.refresh_from_db()
    return record


def test_cleanup_stale_releases_workload_for_stale_targets():
    server_a = Server.objects.create(base_url="http://a.example", is_online=True, workload=3)
    server_b = Server.objects.create(base_url="http://b.example", is_online=True, workload=1)

    _make_stale_processing("http://a.example", model_id=1)
    _make_stale_processing("http://a.example", model_id=1)
    _make_stale_processing("http://b.example", model_id=1)

    updated = RequestRepository.cleanup_stale(threshold_minutes=20)

    assert updated == 3
    server_a.refresh_from_db()
    server_b.refresh_from_db()
    assert server_a.workload == 1
    assert server_b.workload == 0


def test_cleanup_stale_clamps_workload_at_zero_when_counter_already_drained():
    server = Server.objects.create(base_url="http://drained.example", is_online=True, workload=0)
    _make_stale_processing("http://drained.example", model_id=1)
    _make_stale_processing("http://drained.example", model_id=1)

    RequestRepository.cleanup_stale(threshold_minutes=20)

    server.refresh_from_db()
    assert server.workload == 0


def test_cleanup_stale_ignores_records_without_target():
    server = Server.objects.create(base_url="http://t.example", is_online=True, workload=2)
    record = RequestRepository.create_processing(ip_id=1, model_id=1, is_stream=False, user_agent="t")
    RequestRecord.objects.filter(id=record.id).update(
        send_time=timezone.now() - timedelta(minutes=30),
        target_pod_ip=None,
    )

    updated = RequestRepository.cleanup_stale(threshold_minutes=20)

    assert updated == 1
    server.refresh_from_db()
    assert server.workload == 2


def test_cleanup_stale_only_releases_filtered_model():
    server = Server.objects.create(base_url="http://m.example", is_online=True, workload=2)
    _make_stale_processing("http://m.example", model_id=1)
    _make_stale_processing("http://m.example", model_id=2)

    RequestRepository.cleanup_stale(model_id=1, threshold_minutes=20)

    server.refresh_from_db()
    assert server.workload == 1


def test_recalculate_workload_no_drift_reports_no_changes():
    server = Server.objects.create(base_url="http://ok.example", is_online=True, workload=2)
    _make_stale_processing("http://ok.example", model_id=1)
    _make_stale_processing("http://ok.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload()

    assert changes == []
    assert orphans == []
    server.refresh_from_db()
    assert server.workload == 2


def test_recalculate_workload_detects_inflated_drift_without_applying():
    server = Server.objects.create(base_url="http://drift.example", is_online=True, workload=5)
    _make_stale_processing("http://drift.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload(apply=False)

    assert len(changes) == 1
    assert changes[0]["before"] == 5
    assert changes[0]["after"] == 1
    assert orphans == []
    server.refresh_from_db()
    assert server.workload == 5


def test_recalculate_workload_apply_resets_to_processing_count():
    inflated = Server.objects.create(base_url="http://inflated.example", is_online=True, workload=4)
    deflated = Server.objects.create(base_url="http://deflated.example", is_online=True, workload=0)
    _make_stale_processing("http://inflated.example", model_id=1)
    _make_stale_processing("http://inflated.example", model_id=1)
    _make_stale_processing("http://inflated.example", model_id=1)
    _make_stale_processing("http://deflated.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    changed_urls = {c["base_url"]: c for c in changes}
    assert changed_urls["http://inflated.example"]["after"] == 3
    assert changed_urls["http://deflated.example"]["after"] == 1
    assert orphans == []
    inflated.refresh_from_db()
    deflated.refresh_from_db()
    assert inflated.workload == 3
    assert deflated.workload == 1


def test_recalculate_workload_resets_idle_servers_to_zero():
    idle = Server.objects.create(base_url="http://idle.example", is_online=True, workload=3)

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    assert len(changes) == 1
    assert changes[0]["after"] == 0
    assert orphans == []
    idle.refresh_from_db()
    assert idle.workload == 0


def test_recalculate_workload_reports_orphan_targets():
    Server.objects.create(base_url="http://known.example", is_online=True, workload=0)
    _make_stale_processing("http://gone.example", model_id=1)
    _make_stale_processing("http://gone.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload(apply=False)

    assert changes == []
    assert orphans == [
        {"target_pod_ip": "http://gone.example", "count": 2, "kind": "processing"}
    ]


def test_recalculate_workload_default_skips_offline_servers():
    online = Server.objects.create(base_url="http://online.example", is_online=True, workload=5)
    offline = Server.objects.create(base_url="http://offline.example", is_online=False, workload=5)
    _make_stale_processing("http://online.example", model_id=1)
    _make_stale_processing("http://offline.example", model_id=1)
    _make_stale_processing("http://offline.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    changed_urls = {c["base_url"] for c in changes}
    assert "http://online.example" in changed_urls
    assert "http://offline.example" not in changed_urls
    # Offline server has processing records but is not reconciled by default.
    assert orphans == [
        {"target_pod_ip": "http://offline.example", "count": 2, "kind": "processing"}
    ]
    online.refresh_from_db()
    offline.refresh_from_db()
    assert online.workload == 1
    assert offline.workload == 5


def test_recalculate_workload_offline_flag_includes_offline_servers():
    offline = Server.objects.create(base_url="http://offline.example", is_online=False, workload=5)
    _make_stale_processing("http://offline.example", model_id=1)
    _make_stale_processing("http://offline.example", model_id=1)

    changes, orphans = ServerRepository.recalculate_workload(include_offline=True, apply=True)

    assert len(changes) == 1
    assert changes[0]["after"] == 2
    assert orphans == []
    offline.refresh_from_db()
    assert offline.workload == 2


def _make_inflight(
    target_pod_ip: str,
    model_id: int,
    task_status: str,
    input_tokens: int = 0,
) -> RequestRecord:
    record = RequestRepository.create_processing(ip_id=1, model_id=model_id, is_stream=False, user_agent="t")
    RequestRecord.objects.filter(id=record.id).update(
        target_pod_ip=target_pod_ip,
        task_status=task_status,
        input_token_cnt=input_tokens,
    )
    record.refresh_from_db()
    return record


def test_recalculate_workload_counts_prefilling_records_for_prefiller():
    prefiller = Server.objects.create(
        base_url="http://p1.example", is_online=True,
        role="prefix-prefiller", group_id="g1", workload=2,
    )
    _make_inflight("P: http://p1.example", model_id=1, task_status="prefilling")
    _make_inflight("P: http://p1.example", model_id=1, task_status="prefilling")

    changes, orphans = ServerRepository.recalculate_workload()

    assert changes == []
    assert orphans == []


def test_recalculate_workload_detects_prefiller_workload_drift():
    prefiller = Server.objects.create(
        base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", workload=5,
    )
    _make_inflight("P: http://p1.example", model_id=1, task_status="prefilling")

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    assert changes == [
        {
            "server_id": prefiller.id,
            "base_url": "http://p1.example",
            "before": 5,
            "after": 1,
        }
    ]
    assert orphans == []
    prefiller.refresh_from_db()
    assert prefiller.workload == 1


def test_recalculate_workload_counts_decoding_records_and_decoder_tokens():
    decoder = Server.objects.create(
        base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", workload=0, active_tokens=999.0,
    )
    _make_inflight(
        "P: http://p1.example -- D: http://d1.example",
        model_id=1, task_status="decoding", input_tokens=100,
    )
    _make_inflight(
        "P: http://p1.example -- D: http://d1.example -- KV_TRANS_FAIL",
        model_id=1, task_status="decoding", input_tokens=50,
    )

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    assert changes == [
        {
            "server_id": decoder.id,
            "base_url": "http://d1.example",
            "before": 0,
            "after": 2,
            "tokens_before": 999.0,
            "tokens_after": 150.0,
        }
    ]
    assert orphans == []
    decoder.refresh_from_db()
    assert decoder.workload == 2
    assert decoder.active_tokens == 150.0


def test_recalculate_workload_interphase_processing_pd_records_belong_to_no_server():
    prefiller = Server.objects.create(
        base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", workload=0,
    )
    # Prefill released, decoder not yet reserved: transient "processing" + "P: ".
    _make_inflight("P: http://p1.example", model_id=1, task_status="processing")

    changes, orphans = ServerRepository.recalculate_workload()

    assert changes == []
    assert orphans == []


def test_recalculate_workload_attributes_pd_targets_by_id_suffix():
    first = Server.objects.create(
        base_url="http://shared.example", is_online=True,
        role="decoder", group_id="g1", workload=0,
    )
    second = Server.objects.create(
        base_url="http://shared.example", is_online=True,
        role="decoder", group_id="g1", workload=0,
    )
    _make_inflight(
        f"P: http://x -- D: http://shared.example#s{first.id}",
        model_id=1, task_status="decoding", input_tokens=10,
    )
    _make_inflight(
        f"P: http://x -- D: http://shared.example#s{second.id}",
        model_id=1, task_status="decoding", input_tokens=20,
    )

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    changed = {change["server_id"]: change for change in changes}
    assert changed[first.id]["after"] == 1
    assert changed[first.id]["tokens_after"] == 10.0
    assert changed[second.id]["after"] == 1
    assert changed[second.id]["tokens_after"] == 20.0
    assert orphans == []
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.workload == 1
    assert second.workload == 1


def test_recalculate_workload_plain_pd_part_attributes_to_all_rows_on_base_url():
    first = Server.objects.create(
        base_url="http://shared.example", is_online=True,
        role="decoder", group_id="g1", workload=0,
    )
    second = Server.objects.create(
        base_url="http://shared.example", is_online=True,
        role="decoder", group_id="g1", workload=0,
    )
    _make_inflight(
        "P: http://x -- D: http://shared.example",
        model_id=1, task_status="decoding", input_tokens=30,
    )

    changes, orphans = ServerRepository.recalculate_workload()

    changed = {change["server_id"]: change for change in changes}
    # Legacy plain part cannot distinguish rows: every row on the base_url
    # owns it, mirroring decrement_workload_by_targets.
    assert changed[first.id]["after"] == 1
    assert changed[second.id]["after"] == 1
    assert changed[first.id]["tokens_after"] == 30.0
    assert changed[second.id]["tokens_after"] == 30.0
    assert orphans == []


def test_recalculate_workload_zeroes_stale_decoder_counters():
    decoder = Server.objects.create(
        base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", workload=3, active_tokens=500.0,
    )

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    assert changes == [
        {
            "server_id": decoder.id,
            "base_url": "http://d1.example",
            "before": 3,
            "after": 0,
            "tokens_before": 500.0,
            "tokens_after": 0.0,
        }
    ]
    assert orphans == []
    decoder.refresh_from_db()
    assert decoder.workload == 0
    assert decoder.active_tokens == 0.0


def test_recalculate_workload_mixed_and_pd_servers_reconcile_independently():
    Server.objects.create(base_url="http://m1.example", is_online=True, workload=0)
    Server.objects.create(
        base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", workload=0,
    )
    Server.objects.create(
        base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", workload=0,
    )
    _make_inflight("http://m1.example", model_id=1, task_status="processing")
    _make_inflight("P: http://p1.example", model_id=1, task_status="prefilling")
    _make_inflight(
        "P: http://p1.example -- D: http://d1.example",
        model_id=1, task_status="decoding", input_tokens=64,
    )

    changes, orphans = ServerRepository.recalculate_workload()

    after_by_url = {change["base_url"]: change["after"] for change in changes}
    assert after_by_url == {
        "http://m1.example": 1,
        "http://p1.example": 1,
        "http://d1.example": 1,
    }
    assert orphans == []


def test_recalculate_workload_reports_pd_orphans_with_phase_kind():
    Server.objects.create(
        base_url="http://p1.example", is_online=True,
        role="prefiller", group_id="g1", workload=0,
    )
    _make_inflight("P: http://gone.example", model_id=1, task_status="prefilling")
    _make_inflight(
        "P: http://p1.example -- D: http://gone-decoder.example",
        model_id=1, task_status="decoding", input_tokens=5,
    )

    changes, orphans = ServerRepository.recalculate_workload()

    assert changes == []
    assert orphans == [
        {
            "target_pod_ip": "http://gone-decoder.example",
            "count": 1,
            "kind": "decoding",
        },
        {
            "target_pod_ip": "http://gone.example",
            "count": 1,
            "kind": "prefilling",
        },
    ]


def test_correct_workload_command_check_reports_drift_and_exits_nonzero(capsys):
    from django.core.management import call_command

    server = Server.objects.create(base_url="http://cmd.example", is_online=True, workload=5)
    _make_stale_processing("http://cmd.example", model_id=1)

    with pytest.raises(SystemExit) as exc_info:
        call_command("correct_workload")

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "http://cmd.example" in output
    assert "5 -> 1" in output
    server.refresh_from_db()
    assert server.workload == 5


def test_correct_workload_command_fix_applies_changes_and_exits_zero(capsys):
    from django.core.management import call_command

    server = Server.objects.create(base_url="http://cmd.example", is_online=True, workload=5)
    _make_stale_processing("http://cmd.example", model_id=1)

    call_command("correct_workload", "--fix")

    output = capsys.readouterr().out
    assert "Corrected 1 server" in output
    server.refresh_from_db()
    assert server.workload == 1


def test_correct_workload_command_clean_state_exits_zero(capsys):
    from django.core.management import call_command

    Server.objects.create(base_url="http://clean.example", is_online=True, workload=0)

    call_command("correct_workload")

    output = capsys.readouterr().out
    assert "already match" in output


def test_correct_workload_command_rejects_fix_with_dry_run():
    from django.core.management import call_command

    with pytest.raises(SystemExit) as exc_info:
        call_command("correct_workload", "--fix", "--dry-run")

    assert exc_info.value.code == 1


def test_correct_workload_command_reports_decoder_active_tokens_drift(capsys):
    from django.core.management import call_command

    decoder = Server.objects.create(
        base_url="http://d1.example", is_online=True,
        role="decoder", group_id="g1", workload=0, active_tokens=512.0,
    )
    _make_inflight(
        "P: http://p1.example -- D: http://d1.example",
        model_id=1, task_status="decoding", input_tokens=256,
    )

    with pytest.raises(SystemExit) as exc_info:
        call_command("correct_workload")

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "active_tokens 512.0 -> 256.0" in output
    decoder.refresh_from_db()
    assert decoder.active_tokens == 512.0


def test_correct_workload_command_prints_phase_kind_orphans(capsys):
    from django.core.management import call_command

    _make_inflight("P: http://gone.example", model_id=1, task_status="prefilling")

    call_command("correct_workload")

    output = capsys.readouterr().out
    assert "orphan prefilling target 'http://gone.example'" in output
