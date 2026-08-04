from types import SimpleNamespace

import pytest

from router.repositories.requests import RequestRepository
from router.services.proxy_response import (
    NON_UTF8_FAIL_REASON,
    finish_normal_success,
    finish_stream_success,
    request_non_utf8_fail_reason,
)


def _context(body: bytes):
    return SimpleNamespace(body=body, router_result=None)


def test_detector_flags_invalid_utf8_bytes():
    # 0xe9 is a 3-byte UTF-8 lead with no continuation bytes -> invalid.
    assert request_non_utf8_fail_reason(b'{"messages": "caf\xe9"}') == NON_UTF8_FAIL_REASON


def test_detector_accepts_valid_ascii():
    assert request_non_utf8_fail_reason(b'{"messages": []}') is None


def test_detector_accepts_valid_non_ascii_utf8():
    # Chinese, emoji and accented letters are valid UTF-8, not flagged.
    assert request_non_utf8_fail_reason("你好 🌍 café".encode("utf-8")) is None


def test_detector_accepts_empty_body():
    assert request_non_utf8_fail_reason(b"") is None


@pytest.mark.django_db
def test_finish_normal_success_marks_invalid_utf8_request():
    record = RequestRepository.create_processing(
        ip_id=1, model_id=7, is_stream=False, user_agent="pytest"
    )
    content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'

    finish_normal_success(
        record, content, None, _context(b'{"messages": "bad \xe9"}'),
        200, "OK", "http://upstream", 1,
    )

    record.refresh_from_db()
    assert record.task_status == "success"
    assert record.fail_reason == NON_UTF8_FAIL_REASON


@pytest.mark.django_db
def test_finish_normal_success_clean_when_request_is_valid_utf8():
    record = RequestRepository.create_processing(
        ip_id=1, model_id=7, is_stream=False, user_agent="pytest"
    )
    content = b'{"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'

    finish_normal_success(
        record, content, None, _context('{"messages": "你好"}'.encode("utf-8")),
        200, "OK", "http://upstream", 1,
    )

    record.refresh_from_db()
    assert record.task_status == "success"
    assert record.fail_reason is None


@pytest.mark.django_db
def test_finish_stream_success_marks_invalid_utf8_request():
    record = RequestRepository.create_processing(
        ip_id=1, model_id=7, is_stream=True, user_agent="pytest"
    )
    chunks = [
        b'data: {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}\n\n',
        b"data: [DONE]\n\n",
    ]

    finish_stream_success(
        record, 200, "OK", chunks, "http://upstream", None, 1,
        _context(b'{"messages": "bad \xe9"}'),
    )

    record.refresh_from_db()
    assert record.task_status == "success"
    assert record.fail_reason == NON_UTF8_FAIL_REASON
