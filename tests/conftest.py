import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("USE_SQLITE_FOR_TESTS", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "router_project.settings")

import django
import pytest
from django.db import connection


django.setup()

from router.models import Ips, Model, RequestRecord, Server, Whitelist, ServerOperation, MrLiveReview, DailyMrReview, UserIP, Department, CodehubReview
from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser


@pytest.fixture(scope="session", autouse=True)
def api_test_tables(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        existing_tables = connection.introspection.table_names()
        with connection.schema_editor() as schema_editor:
            if UserIP._meta.db_table in existing_tables and (
                not has_column("user_ips", "apikey") or not has_column("user_ips", "vip")
            ):
                schema_editor.delete_model(UserIP)
                schema_editor.create_model(UserIP)
            for model in (Ips, Model, RequestRecord, Server, Whitelist, ServerOperation, MrLiveReview, DailyMrReview, UserIP, Department, CodehubReview):
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)
            if Ips._meta.db_table in connection.introspection.table_names() and not has_column("ips", "vip"):
                schema_editor.add_field(Ips, Ips._meta.get_field("vip"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "last_match"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("last_match"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "final_prefix_cache"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("final_prefix_cache"))
            # Circuit breaker columns
            for col in ("circuit_state", "consecutive_failures", "last_state_change_at", "cooldown_seconds", "workload"):
                if Server._meta.db_table in connection.introspection.table_names() and not has_column("servers", col):
                    schema_editor.add_field(Server, Server._meta.get_field(col))
            # VIP columns
            for col in ("vip", "vip_cooldown"):
                if Server._meta.db_table in connection.introspection.table_names() and not has_column("servers", col):
                    schema_editor.add_field(Server, Server._meta.get_field(col))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "vip"):
                schema_editor.add_field(Model, Model._meta.get_field("vip"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "deprecation"):
                schema_editor.add_field(Model, Model._meta.get_field("deprecation"))
            # New router fields
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "is_routing_model"):
                schema_editor.add_field(Model, Model._meta.get_field("is_routing_model"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "auto"):
                schema_editor.add_field(Model, Model._meta.get_field("auto"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "complexity_min"):
                schema_editor.add_field(Model, Model._meta.get_field("complexity_min"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "complexity_max"):
                schema_editor.add_field(Model, Model._meta.get_field("complexity_max"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "multimodal"):
                schema_editor.add_field(Model, Model._meta.get_field("multimodal"))
            if Model._meta.db_table in connection.introspection.table_names() and not has_column("models", "model_path"):
                schema_editor.add_field(Model, Model._meta.get_field("model_path"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "router_result"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("router_result"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "estimate_tokens"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("estimate_tokens"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "model_choosing_latency"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("model_choosing_latency"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "ttft"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("ttft"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "vip"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("vip"))
            if RequestRecord._meta.db_table in connection.introspection.table_names() and not has_column("requests", "session"):
                schema_editor.add_field(RequestRecord, RequestRecord._meta.get_field("session"))
            if Server._meta.db_table in connection.introspection.table_names() and not has_column("servers", "context_window"):
                schema_editor.add_field(Server, Server._meta.get_field("context_window"))
            # PD disaggregation columns
            for col in ("role", "group_id", "active_tokens"):
                if Server._meta.db_table in connection.introspection.table_names() and not has_column("servers", col):
                    schema_editor.add_field(Server, Server._meta.get_field(col))
        yield


def has_column(table, column):
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table)
    return column in {item.name for item in description}


@pytest.fixture(autouse=True)
def _reset_request_log_buffers():
    # Buffered per-request log events (request_logger) must not leak from one
    # test into the next test's log_path assertions.
    yield
    from router.services import request_logger

    request_logger.clear_request_log_buffers()


@pytest.fixture(autouse=True)
def fake_prefix_cache_redis():
    # The prod chooser dials Redis (prefix cache) on every proxied request.
    # Tests run without a Redis server, and redis-py's default retry policy
    # (10 retries with exponential backoff) turns each dead-socket command
    # into a ~4s stall (issue #269). Install an in-memory fake client so the
    # chooser exercises its normal code path instantly; storage starts empty,
    # so probes return no matches and selection falls back to least-loaded,
    # exactly like prod against an empty Redis. redis.Redis is patched as a
    # safety net so no test can ever construct a real client.
    with patch("redis.Redis") as mock:
        client = MagicMock()
        mock.return_value = client
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

        # Pipeline mock: writes apply immediately; reads are queued and
        # returned by execute(), so call_count assertions still work.
        pipe = MagicMock()
        client.pipeline.return_value = pipe
        pipe.hset.side_effect = mock_hset
        pipe.expire.side_effect = mock_expire
        pipe.hgetall.side_effect = pipe_hgetall
        pipe.execute.side_effect = pipe_execute

        PrefixCachePrebleServerChooser._redis_client = client
        yield client
        PrefixCachePrebleServerChooser._redis_client = None


@pytest.fixture(autouse=True)
def clean_api_tables(api_test_tables):
    # Background daemon threads (e.g. CMDB IP provisioning at views.py proxy
    # entry) may briefly hold the sqlite file when a test ends; retry the
    # cleanup so the next test does not fail with "database table is locked".
    import time
    from django.db import OperationalError

    for attempt in range(3):
        try:
            RequestRecord.objects.all().delete()
            Ips.objects.all().delete()
            Server.objects.all().delete()
            Whitelist.objects.all().delete()
            Model.objects.all().delete()
            ServerOperation.objects.all().delete()
            MrLiveReview.objects.all().delete()
            DailyMrReview.objects.all().delete()
            UserIP.objects.all().delete()
            Department.objects.all().delete()
            CodehubReview.objects.all().delete()
            break
        except OperationalError:
            if attempt == 2:
                raise
            time.sleep(0.2)
