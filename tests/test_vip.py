from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone

from router.config import APP_CONFIG
from router.models import Model, RequestRecord, Server
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.services.proxy import ProxyService
from router.services.vip_channel import VIPChannelService
from router.views import _is_vip_channel


def make_model(name="m", vip=None, concurrent_limit=100):
    return Model.objects.create(model_name=name, vip=vip, concurrent_limit=concurrent_limit)


def make_server(model_id, base_url, *, vip=False, vip_cooldown=None, workload=0, is_online=True):
    return Server.objects.create(
        model_id=model_id,
        base_url=base_url,
        is_online=is_online,
        vip=vip,
        vip_cooldown=vip_cooldown,
        workload=workload,
    )


def add_vip_processing(model_id, target_pod_ip=None):
    record = RequestRepository.create_processing(
        ip_id=1, model_id=model_id, is_stream=False, user_agent="t", user_ip_id=2,
    )
    if target_pod_ip:
        RequestRecord.objects.filter(id=record.id).update(target_pod_ip=target_pod_ip)
    return record


# -------- ServerRepository VIP methods --------


class TestServerRepositoryVIP:
    def test_promote_flips_flag(self):
        s = make_server(1, "http://s.example", vip=False)

        assert ServerRepository.promote_to_vip(s) is True

        s.refresh_from_db()
        assert s.vip is True
        assert s.vip_cooldown is None

    def test_promote_returns_false_when_already_vip(self):
        s = make_server(1, "http://s.example", vip=True)

        assert ServerRepository.promote_to_vip(s) is False

    def test_demote_clears_state(self):
        s = make_server(1, "http://s.example", vip=True, vip_cooldown=timezone.now())

        ServerRepository.demote_to_normal(s)

        s.refresh_from_db()
        assert s.vip is False
        assert s.vip_cooldown is None

    def test_mark_vip_cooldown_idempotent(self):
        s = make_server(1, "http://s.example", vip=True)

        first = ServerRepository.mark_vip_cooldown(s)
        s.refresh_from_db()
        first_ts = s.vip_cooldown
        second = ServerRepository.mark_vip_cooldown(s)
        s.refresh_from_db()

        assert first is True
        assert second is False
        assert s.vip_cooldown == first_ts

    def test_mark_vip_cooldown_skips_non_vip_servers(self):
        s = make_server(1, "http://s.example", vip=False)

        assert ServerRepository.mark_vip_cooldown(s) is False
        s.refresh_from_db()
        assert s.vip_cooldown is None

    def test_cancel_vip_cooldown_clears_timestamp(self):
        s = make_server(1, "http://s.example", vip=True, vip_cooldown=timezone.now())

        assert ServerRepository.cancel_vip_cooldown(s) is True
        s.refresh_from_db()
        assert s.vip_cooldown is None

    def test_demote_expired_cooldowns_only_touches_old_ones(self):
        old = timezone.now() - timedelta(seconds=400)
        recent = timezone.now() - timedelta(seconds=100)
        expired = make_server(1, "http://expired.example", vip=True, vip_cooldown=old)
        fresh = make_server(1, "http://fresh.example", vip=True, vip_cooldown=recent)

        demoted = ServerRepository.demote_expired_cooldowns(300, model_id=1)

        expired.refresh_from_db()
        fresh.refresh_from_db()
        assert demoted == 1
        assert expired.vip is False
        assert expired.vip_cooldown is None
        assert fresh.vip is True
        assert fresh.vip_cooldown is not None

    def test_list_by_model_id_filters_vip(self):
        v = make_server(7, "http://v.example", vip=True)
        n = make_server(7, "http://n.example", vip=False)

        assert ServerRepository.list_by_model_id(7, vip=True) == [v]
        assert ServerRepository.list_by_model_id(7, vip=False) == [n]
        assert sorted(ServerRepository.list_by_model_id(7), key=lambda s: s.id) == [v, n]


# -------- count_vip_processing --------


class TestCountVIPProcessing:
    def test_filters_by_user_ip_and_model(self):
        add_vip_processing(model_id=7)
        add_vip_processing(model_id=7)
        # non-VIP request on same model:
        RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="t", user_ip_id=1)
        # VIP request on different model:
        add_vip_processing(model_id=8)

        assert RequestRepository.count_vip_processing(model_id=7) == 2
        assert RequestRepository.count_vip_processing(model_id=8) == 1

    def test_excludes_finished_records(self):
        record = add_vip_processing(model_id=7)
        RequestRepository.finish(record, 200, "OK")

        assert RequestRepository.count_vip_processing(model_id=7) == 0


# -------- VIPChannelService.is_vip_eligible --------


class TestVIPEligibility:
    def test_eligible_when_threshold_set(self):
        m = make_model("m", vip=3)
        assert VIPChannelService.is_vip_eligible(m) is True

    def test_not_eligible_when_zero(self):
        m = make_model("m", vip=0)
        assert VIPChannelService.is_vip_eligible(m) is False

    def test_not_eligible_when_none(self):
        m = make_model("m", vip=None)
        assert VIPChannelService.is_vip_eligible(m) is False

    def test_not_eligible_when_no_model(self):
        assert VIPChannelService.is_vip_eligible(None) is False


# -------- VIPChannelService.select_candidates (scale-up) --------


class TestVIPSelectCandidates:
    def test_zero_vip_promotes_least_loaded_normal_when_capacity_allows(self):
        m = make_model("m", vip=3)
        n1 = make_server(m.id, "http://n1.example", workload=2)
        n2 = make_server(m.id, "http://n2.example", workload=1)
        n3 = make_server(m.id, "http://n3.example", workload=5)

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        n2.refresh_from_db()
        assert served is True
        assert candidates == [n2]
        assert n2.vip is True

    def test_zero_vip_at_min_floor_returns_normals_unpromoted(self):
        m = make_model("m", vip=3)
        n1 = make_server(m.id, "http://n1.example")
        n2 = make_server(m.id, "http://n2.example")

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        n1.refresh_from_db()
        n2.refresh_from_db()
        assert served is False
        assert n1.vip is False and n2.vip is False
        assert {s.id for s in candidates} == {n1.id, n2.id}

    def test_all_vips_cooling_cancels_first_and_returns_just_it(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True, vip_cooldown=timezone.now())
        v2 = make_server(m.id, "http://v2.example", vip=True, vip_cooldown=timezone.now())

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert served is True
        assert candidates == [v1]
        assert v1.vip_cooldown is None
        assert v2.vip_cooldown is not None  # untouched

    def test_scale_up_cancels_cooling_when_projected_exceeds_threshold(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        v2 = make_server(m.id, "http://v2.example", vip=True, vip_cooldown=timezone.now())
        for _ in range(3):
            add_vip_processing(m.id)
        # active=[v1], projected=(3+1)/1=4 > 3 → cancel cooling

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        v2.refresh_from_db()
        assert served is True
        assert v2.vip_cooldown is None
        assert {s.id for s in candidates} == {v1.id, v2.id}

    def test_scale_up_promotes_when_no_cooling_and_normal_capacity(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        n1 = make_server(m.id, "http://n1.example", workload=1)
        n2 = make_server(m.id, "http://n2.example", workload=5)
        n3 = make_server(m.id, "http://n3.example", workload=2)
        for _ in range(4):
            add_vip_processing(m.id)
        # projected=(4+1)/1=5 > 3, no cooling, normals=3 > min 2 → promote n1

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        n1.refresh_from_db()
        assert served is True
        assert n1.vip is True
        assert n1 in candidates

    def test_scale_up_blocked_at_min_normals(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        n1 = make_server(m.id, "http://n1.example")
        n2 = make_server(m.id, "http://n2.example")
        for _ in range(4):
            add_vip_processing(m.id)

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        n1.refresh_from_db()
        n2.refresh_from_db()
        assert served is True
        assert n1.vip is False and n2.vip is False
        assert candidates == [v1]

    def test_no_scale_up_when_within_threshold(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        v2 = make_server(m.id, "http://v2.example", vip=True)
        n1 = make_server(m.id, "http://n1.example")
        n2 = make_server(m.id, "http://n2.example")
        n3 = make_server(m.id, "http://n3.example")
        for _ in range(4):
            add_vip_processing(m.id)
        # projected=(4+1)/2=2.5 ≤ 3 → no scale up

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        n1.refresh_from_db()
        assert served is True
        assert n1.vip is False
        assert {s.id for s in candidates} == {v1.id, v2.id}

    def test_demotes_expired_cooldowns_before_evaluating(self):
        m = make_model("m", vip=3)
        old = timezone.now() - timedelta(seconds=400)
        v1 = make_server(m.id, "http://v1.example", vip=True, vip_cooldown=old)
        v2 = make_server(m.id, "http://v2.example", vip=True)
        make_server(m.id, "http://n1.example")
        make_server(m.id, "http://n2.example")

        candidates, served, _logs = VIPChannelService().select_candidates(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert served is True
        assert v1.vip is False
        assert v2.vip is True
        assert candidates == [v2]


# -------- VIPChannelService.maybe_scale_down --------


class TestVIPScaleDown:
    def test_zero_load_marks_all_active_for_cooldown(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        v2 = make_server(m.id, "http://v2.example", vip=True)

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.vip_cooldown is not None
        assert v2.vip_cooldown is not None

    def test_load_with_all_cooling_logs_error(self, caplog):
        m = make_model("m", vip=3)
        make_server(m.id, "http://v1.example", vip=True, vip_cooldown=timezone.now())
        add_vip_processing(m.id)

        with caplog.at_level(logging.ERROR, logger="router.services.vip_channel"):
            VIPChannelService().maybe_scale_down(m)

        assert any("VIP scale-down" in rec.getMessage() for rec in caplog.records)

    def test_single_vip_with_load_does_not_mark(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        add_vip_processing(m.id)

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        assert v1.vip_cooldown is None

    def test_single_vip_zero_load_marks_for_cooldown(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        assert v1.vip_cooldown is not None

    def test_projected_below_threshold_marks_least_loaded_active(self):
        m = make_model("m", vip=5)
        v1 = make_server(m.id, "http://v1.example", vip=True, workload=1)
        v2 = make_server(m.id, "http://v2.example", vip=True, workload=2)
        for _ in range(4):
            add_vip_processing(m.id)
        # projected = 4 / (2-1) = 4 < 5

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.vip_cooldown is not None  # least workload
        assert v2.vip_cooldown is None

    def test_projected_above_threshold_does_not_mark(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        v2 = make_server(m.id, "http://v2.example", vip=True)
        for _ in range(7):
            add_vip_processing(m.id)
        # projected = 7/1 = 7 > 3

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.vip_cooldown is None
        assert v2.vip_cooldown is None

    def test_not_eligible_no_op(self):
        m = make_model("m", vip=None)
        v1 = make_server(m.id, "http://v1.example", vip=True)

        VIPChannelService().maybe_scale_down(m)

        v1.refresh_from_db()
        assert v1.vip_cooldown is None


# -------- spike-then-stop drain cycle --------


class TestSpikeStopDrain:
    def test_repeated_finishes_eventually_drain_all_vips(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v1.example", vip=True)
        v2 = make_server(m.id, "http://v2.example", vip=True)
        records = [add_vip_processing(m.id) for _ in range(5)]

        svc = VIPChannelService()
        for record in records:
            RequestRepository.finish(record, 200, "OK")
            svc.maybe_scale_down(m)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.vip_cooldown is not None
        assert v2.vip_cooldown is not None


# -------- _is_vip_channel --------


class TestVIPChannelDetection:
    def _request_with_port(self, port):
        rf = RequestFactory()
        request = rf.get("/v1/chat/completions")
        if port is None:
            request.META.pop("SERVER_PORT", None)
        else:
            request.META["SERVER_PORT"] = port
        return request

    def test_matches_configured_port(self, monkeypatch):
        monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
        assert _is_vip_channel(self._request_with_port("8008")) is True

    def test_other_port_is_not_vip(self, monkeypatch):
        monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
        assert _is_vip_channel(self._request_with_port("8001")) is False

    def test_missing_port_is_not_vip(self, monkeypatch):
        monkeypatch.setitem(APP_CONFIG.setdefault("server", {}), "vip_port", 8008)
        assert _is_vip_channel(self._request_with_port(None)) is False


# -------- release_vip_cooldowns command --------


class TestReleaseVIPCooldownsCommand:
    def test_demotes_only_expired(self):
        old = timezone.now() - timedelta(seconds=400)
        expired = make_server(1, "http://expired.example", vip=True, vip_cooldown=old)
        fresh = make_server(1, "http://fresh.example", vip=True, vip_cooldown=timezone.now())

        call_command("release_vip_cooldowns")

        expired.refresh_from_db()
        fresh.refresh_from_db()
        assert expired.vip is False
        assert fresh.vip is True


# -------- ProxyService VIP routing --------


class TestProxyVIPRouting:
    def test_normal_channel_excludes_vip_servers(self):
        m = make_model("m", vip=3)
        normal = make_server(m.id, "http://n.example", vip=False)
        make_server(m.id, "http://v.example", vip=True)

        candidates, served, _logs = ProxyService()._select_candidates("chat/completions", m, is_vip_channel=False)

        assert candidates == [normal]
        assert served is False

    def test_vip_channel_non_eligible_falls_back_to_normal_pool(self):
        m = make_model("m", vip=None)
        normal = make_server(m.id, "http://n.example", vip=False)
        make_server(m.id, "http://v.example", vip=True)

        candidates, served, _logs = ProxyService()._select_candidates("chat/completions", m, is_vip_channel=True)

        assert candidates == [normal]
        assert served is False

    def test_vip_channel_eligible_routes_through_vip_service(self):
        m = make_model("m", vip=3)
        v1 = make_server(m.id, "http://v.example", vip=True)
        make_server(m.id, "http://n.example", vip=False)

        candidates, served, _logs = ProxyService()._select_candidates("chat/completions", m, is_vip_channel=True)

        assert served is True
        assert v1 in candidates
