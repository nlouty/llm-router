from __future__ import annotations

import json

from django.http import HttpResponse

from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.utils.headers import filter_response_headers
from router.utils.sse import parse_sse_usage


def router_result(context) -> str | None:
    return getattr(context, "router_result", None)


def response_content_bytes(response) -> bytes:
    content = getattr(response, "content", b"")
    if isinstance(content, str):
        return content.encode("utf-8", errors="replace")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    text = getattr(response, "text", "")
    if isinstance(text, str):
        return text.encode("utf-8", errors="replace")
    return b""


def response_reason(response) -> str:
    reason = getattr(response, "reason", "")
    if isinstance(reason, str):
        return reason
    text = getattr(response, "text", "")
    return text if isinstance(text, str) else ""


def response_from_upstream(upstream, content: bytes, status_code: int):
    response = HttpResponse(content, status=status_code)
    for key, value in filter_response_headers(dict(upstream.headers)).items():
        response[key] = value
    return response


def parse_json_usage(content: bytes) -> tuple[int, int, int]:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0, 0, 0
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    cached_tokens = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return prompt_tokens, completion_tokens, cached_tokens


def extract_fail_reason(content: bytes, http_reason: str) -> str:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return http_reason
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            msg = error.get("message", "")
            err_type = error.get("type", "")
            if msg:
                return f"{err_type}: {msg}" if err_type else msg
    return http_reason


def ensure_model_after_success(model_name: str | None, status_code: int) -> int | None:
    if model_name and 200 <= status_code < 300:
        model, _ = ModelRepository.get_or_create(model_name)
        return model.id
    return None


def finish_no_candidates(record, reason: str, context, model) -> None:
    RequestRepository.finish(
        record,
        502,
        reason,
        model_id=model.id if model else None,
        attempt_count=0,
        router_result=router_result(context),
    )


def finish_upstream_error(
    record,
    status_code: int,
    fail_reason: str,
    target_pod_ip: str | None,
    model,
    attempts: int,
    context,
) -> None:
    RequestRepository.finish(
        record,
        status_code,
        fail_reason,
        0,
        0,
        target_pod_ip,
        model.id if model else None,
        attempt_count=attempts,
        router_result=router_result(context),
    )


def finish_normal_success(
    record,
    content: bytes,
    model,
    context,
    status_code: int,
    reason: str,
    target_pod_ip: str | None,
    attempts: int,
) -> None:
    input_tokens, output_tokens, cached_tokens = parse_json_usage(content)
    RequestRepository.finish(
        record,
        status_code,
        reason,
        input_tokens,
        output_tokens,
        target_pod_ip,
        model.id if model else None,
        attempt_count=attempts,
        final_prefix_cache=cached_tokens,
        router_result=router_result(context),
    )


def finish_retry_failure(
    record,
    status: int,
    message: str,
    target_pod_ip: str | None,
    attempts: int,
    context,
) -> None:
    RequestRepository.finish(
        record,
        status,
        message,
        target_pod_ip=target_pod_ip,
        attempt_count=attempts,
        router_result=router_result(context),
    )


def finish_stream_success(
    record,
    status_code: int,
    reason: str,
    chunks: list[bytes],
    target_pod_ip: str | None,
    model_name: str | None,
    attempts: int,
    context,
) -> None:
    input_tokens, output_tokens, cached_tokens = parse_sse_usage(chunks)
    final_model_id = ensure_model_after_success(model_name, status_code)
    RequestRepository.finish(
        record,
        status_code,
        reason,
        input_tokens,
        output_tokens,
        target_pod_ip,
        final_model_id,
        attempt_count=attempts,
        final_prefix_cache=cached_tokens,
        router_result=router_result(context),
    )


def finish_stream_total_timeout(record, target_pod_ip: str | None, attempts: int) -> None:
    RequestRepository.finish(
        record,
        504,
        "request timeout, please try again later",
        target_pod_ip=target_pod_ip,
        attempt_count=attempts,
    )


def finish_stream_client_disconnected(record, target_pod_ip: str | None, attempts: int) -> None:
    RequestRepository.finish(
        record,
        499,
        "Client Closed Request",
        target_pod_ip=target_pod_ip,
        task_status="agent_disconnected",
        attempt_count=attempts,
    )


def finish_stream_read_timeout(record, target_pod_ip: str | None, attempts: int, model, context) -> None:
    RequestRepository.finish(
        record,
        504,
        "request timeout, please try again later",
        target_pod_ip=target_pod_ip,
        attempt_count=attempts,
        model_id=model.id if model else None,
        router_result=router_result(context),
    )


def finish_stream_request_exception(
    record,
    message: str,
    target_pod_ip: str | None,
    attempts: int,
    model,
    context,
) -> None:
    RequestRepository.finish(
        record,
        502,
        message,
        target_pod_ip=target_pod_ip,
        attempt_count=attempts,
        model_id=model.id if model else None,
        router_result=router_result(context),
    )


def finish_client_closed(record) -> None:
    RequestRepository.finish(
        record,
        499,
        "Client Closed Request",
        task_status="agent_disconnected",
    )
