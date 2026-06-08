from router.repositories.requests import RequestRepository
from router.models import ROUTER_RESULT_MAX_LENGTH


def test_record_attempt_persists_prefix_cache_and_last_match():
    record = RequestRepository.create_processing(
        ip_id=1,
        model_id=7,
        is_stream=False,
        user_agent="pytest",
    )

    RequestRepository.record_attempt(
        record,
        target_pod_ip="http://10.0.0.1:8000",
        attempt_count=1,
        prefix_cache=0.75,
        last_match=12345,
    )

    record.refresh_from_db()

    assert record.target_pod_ip == "http://10.0.0.1:8000"
    assert record.attempt_count == 1
    assert record.prefix_cache == 0.75
    assert record.last_match == 12345


def test_record_attempt_clears_last_match_when_no_match():
    record = RequestRepository.create_processing(
        ip_id=1,
        model_id=7,
        is_stream=False,
        user_agent="pytest",
    )
    record.last_match = 12345
    record.save(update_fields=["last_match"])

    RequestRepository.record_attempt(
        record,
        target_pod_ip="http://10.0.0.1:8000",
        attempt_count=1,
        prefix_cache=0.0,
        last_match=None,
    )

    record.refresh_from_db()

    assert record.last_match is None


def test_record_model_choosing_latency_persists_milliseconds():
    record = RequestRepository.create_processing(
        ip_id=1,
        model_id=7,
        is_stream=False,
        user_agent="pytest",
    )

    RequestRepository.record_model_choosing_latency(record, 123)

    record.refresh_from_db()
    assert record.model_choosing_latency == 123


def test_record_model_choosing_latency_clamps_negative_values():
    record = RequestRepository.create_processing(
        ip_id=1,
        model_id=7,
        is_stream=False,
        user_agent="pytest",
    )

    RequestRepository.record_model_choosing_latency(record, -5)

    record.refresh_from_db()
    assert record.model_choosing_latency == 0


def test_create_blocked_status_uses_rfc_phrase():
    record = RequestRepository.create_blocked(
        ip_id=1,
        model_id=7,
        is_stream=False,
        user_agent="pytest",
        status_code=429,
        fail_reason="concurrent limit exceeded",
    )

    assert record.status == "429 Too Many Requests"
    assert record.fail_reason == "concurrent limit exceeded"
    assert record.task_status == "failed"


def test_finish_status_ignores_upstream_reason_text():
    record = RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="pytest")

    RequestRepository.finish(record, 502, "upstream timed out after retries: connection refused by 10.0.0.1:8000")

    record.refresh_from_db()
    assert record.status == "502 Bad Gateway"
    assert record.fail_reason == "upstream timed out after retries: connection refused by 10.0.0.1:8000"


def test_finish_status_success_clears_fail_reason():
    record = RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="pytest")

    RequestRepository.finish(record, 200, "OK", input_tokens=10, output_tokens=20)

    record.refresh_from_db()
    assert record.status == "200 OK"
    assert record.task_status == "success"
    assert record.fail_reason is None


def test_finish_persists_final_prefix_cache():
    record = RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="pytest")

    RequestRepository.finish(record, 200, "OK", input_tokens=10, output_tokens=20, final_prefix_cache=1920)

    record.refresh_from_db()
    assert record.input_token_cnt == 10
    assert record.output_token_cnt == 20
    assert record.final_prefix_cache == 1920


def test_finish_persists_extended_router_result():
    record = RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="pytest")
    router_result = "r" * (ROUTER_RESULT_MAX_LENGTH + 50)

    RequestRepository.finish(record, 200, "OK", router_result=router_result)

    record.refresh_from_db()
    assert record.router_result == router_result[:ROUTER_RESULT_MAX_LENGTH]
    assert len(record.router_result) > 100


def test_finish_status_handles_client_closed_request():
    record = RequestRepository.create_processing(ip_id=1, model_id=7, is_stream=False, user_agent="pytest")

    RequestRepository.finish(record, 499, "Client Closed Request", task_status="agent_disconnected")

    record.refresh_from_db()
    assert record.status == "499 Client Closed Request"
