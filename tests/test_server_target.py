"""Target-string qualification for servers sharing a base_url (issue #310).

Normal servers keep the historical bare base_url in ``requests.target_pod_ip``;
rows whose base_url is shared by another active row record
``base_url#s<server.id>`` so workload cleanup, active-token release and
workload reconciliation attribute processing rows to the exact server row.
"""
from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from router.models import Model, RequestRecord, Server
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.utils import target as target_utils
from router.utils.target import (
    TARGET_MAX_LENGTH,
    duplicated_base_urls,
    parse_server_target,
    plain_target,
    qualified_target,
    server_target,
)


def make_fake_server(server_id, base_url):
    return SimpleNamespace(id=server_id, base_url=base_url)


# ---------------------------------------------------------------- helpers


def _seed_servers_sharing_url(url="http://a/v1"):
    model = Model.objects.create(model_name="m-shared")
    first = Server.objects.create(model_id=model.id, base_url=url, api_key="sk-1", workload=0)
    second = Server.objects.create(model_id=model.id, base_url=url, api_key="sk-2", workload=0)
    return model, first, second


def _make_stale(target_pod_ip: str, model_id: int, task_status: str = "processing",
                input_token_cnt: int = 0) -> RequestRecord:
    record = RequestRepository.create_processing(ip_id=1, model_id=model_id, is_stream=False, user_agent="t")
    RequestRecord.objects.filter(id=record.id).update(
        send_time=timezone.now() - timedelta(minutes=30),
        target_pod_ip=target_pod_ip,
        task_status=task_status,
        input_token_cnt=input_token_cnt,
    )
    return record


# ------------------------------------------------- pure format functions


def test_plain_and_qualified_target_formats():
    server = make_fake_server(7, "http://a/v1")

    assert plain_target(server) == "http://a/v1"
    assert qualified_target(server) == "http://a/v1#s7"
    assert parse_server_target("http://a/v1") == ("http://a/v1", None)
    assert parse_server_target("http://a/v1#s7") == ("http://a/v1", 7)
    assert parse_server_target("") == ("", None)
    assert parse_server_target(None) == ("", None)


def test_qualified_target_truncates_to_column_width():
    server = make_fake_server(123, "x" * 600)

    qualified = qualified_target(server)
    plain = plain_target(server)

    assert len(qualified) == TARGET_MAX_LENGTH
    assert len(plain) == TARGET_MAX_LENGTH
    base_url, server_id = parse_server_target(qualified)
    assert server_id == 123
    assert base_url == "x" * (TARGET_MAX_LENGTH - len("#s123"))


def test_server_target_qualifies_only_duplicated_base_urls(monkeypatch):
    unique = make_fake_server(1, "http://unique/v1")
    shared = make_fake_server(2, "http://shared/v1")

    monkeypatch.setattr(
        target_utils, "_duplication_cache", (time.monotonic(), frozenset({"http://shared/v1"}))
    )

    assert server_target(unique) == "http://unique/v1"
    assert server_target(shared) == "http://shared/v1#s2"


# --------------------------------------------------------- duplication set


def test_duplicated_base_urls_detects_shared_active_rows():
    model, first, second = _seed_servers_sharing_url()
    unique = Server.objects.create(model_id=model.id, base_url="http://b/v1", api_key=None)

    duplicated = duplicated_base_urls()

    assert duplicated == frozenset({"http://a/v1"})
    # A soft-deleted duplicate must not force qualification.
    second.deleted_at = timezone.now()
    second.save(update_fields=["deleted_at"])
    target_utils.clear_duplication_cache()
    assert duplicated_base_urls() == frozenset()


def test_server_target_on_db_rows():
    model, first, second = _seed_servers_sharing_url()

    assert server_target(first) == f"http://a/v1#s{first.id}"
    assert server_target(second) == f"http://a/v1#s{second.id}"

    Server.objects.create(model_id=model.id, base_url="http://solo/v1")
    target_utils.clear_duplication_cache()
    solo = Server.objects.get(base_url="http://solo/v1")
    assert server_target(solo) == "http://solo/v1"


# ------------------------------------------------- workload attribution


def test_decrement_workload_by_targets_qualified_hits_owning_row_only():
    model, first, second = _seed_servers_sharing_url()
    Server.objects.filter(id=first.id).update(workload=5)
    Server.objects.filter(id=second.id).update(workload=5)

    ServerRepository.decrement_workload_by_targets({f"http://a/v1#s{second.id}": 3})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.workload == 5
    assert second.workload == 2


def test_decrement_workload_by_targets_plain_hits_all_rows_legacy():
    model, first, second = _seed_servers_sharing_url()
    Server.objects.filter(id=first.id).update(workload=5)
    Server.objects.filter(id=second.id).update(workload=1)

    # Plain target (written before the duplicate existed): today's behavior.
    ServerRepository.decrement_workload_by_targets({"http://a/v1": 2})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.workload == 3
    assert second.workload == 0


def test_release_active_tokens_by_targets_qualified_hits_owning_row():
    model, first, second = _seed_servers_sharing_url()
    Server.objects.filter(id=first.id).update(active_tokens=100.0)
    Server.objects.filter(id=second.id).update(active_tokens=100.0)

    ServerRepository.release_active_tokens_by_targets({f"http://a/v1#s{first.id}": 40.0})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.active_tokens == 60.0
    assert second.active_tokens == 100.0


def test_cleanup_stale_decoding_releases_owning_decoder_row():
    model, first, second = _seed_servers_sharing_url()
    Server.objects.filter(id=first.id).update(workload=3, active_tokens=100.0)
    Server.objects.filter(id=second.id).update(workload=3, active_tokens=100.0)

    _make_stale(
        f"P: http://a/v1#s{second.id} -- D: http://a/v1#s{second.id}",
        model_id=model.id,
        task_status="decoding",
        input_token_cnt=40,
    )

    updated = RequestRepository.cleanup_stale(threshold_minutes=20)

    assert updated == 1
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.workload == 3
    assert second.workload == 2
    assert first.active_tokens == 100.0
    assert second.active_tokens == 60.0


def test_recalculate_workload_counts_per_row_for_duplicated_urls():
    model, first, second = _seed_servers_sharing_url()
    Server.objects.filter(id=first.id).update(workload=0)
    Server.objects.filter(id=second.id).update(workload=0)

    _make_stale(f"http://a/v1#s{first.id}", model_id=model.id)
    _make_stale(f"http://a/v1#s{second.id}", model_id=model.id)
    _make_stale(f"http://a/v1#s{second.id}", model_id=model.id)

    changes, orphans = ServerRepository.recalculate_workload(apply=True)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.workload == 1
    assert second.workload == 2
    assert orphans == []
    assert len(changes) == 2
