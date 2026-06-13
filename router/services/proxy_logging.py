from __future__ import annotations

import json

from router.services.request_logger import append_request_log


def safe_append_request_log(request_id: int, message: str) -> None:
    try:
        append_request_log(request_id, message)
    except Exception:
        pass


def log_attempt(
    request_id: int,
    attempt: int,
    server,
    result: str,
    retry: bool,
    status: int | None = None,
    reason: str | None = None,
) -> None:
    payload = {
        "event": "server_attempt",
        "request_id": request_id,
        "attempt": attempt,
        "server_id": server.id,
        "base_url": server.base_url,
        "model_id": server.model_id,
        "result": result,
        "retry": retry,
    }
    if status is not None:
        payload["status"] = status
    if reason:
        payload["reason"] = reason[:500]
    append_request_log(request_id, json.dumps(payload, ensure_ascii=False))


def maybe_log_multi_server_route(
    request_id: int,
    attempted_server_ids: set[int],
    final_server_id: int | None,
) -> None:
    if len(attempted_server_ids) <= 1:
        return
    payload = {
        "event": "multi_server_route",
        "request_id": request_id,
        "server_ids": sorted(attempted_server_ids),
        "final_server_id": final_server_id,
        "reason": "retried_after_failure",
    }
    append_request_log(request_id, json.dumps(payload, ensure_ascii=False))


def log_error_detail(
    request_id: int,
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    status_code: int,
    response_body: bytes,
) -> None:
    try:
        req_body_str = body.decode("utf-8") if body else ""
    except (UnicodeDecodeError, AttributeError):
        req_body_str = repr(body)[:2000]
    try:
        resp_body_str = response_body.decode("utf-8") if response_body else ""
    except (UnicodeDecodeError, AttributeError):
        resp_body_str = repr(response_body)[:2000]

    safe_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in ("authorization", "csb-token")
    }
    log_entry = json.dumps(
        {
            "event": "upstream_error",
            "request_id": request_id,
            "method": method,
            "url": url,
            "request_headers": safe_headers,
            "request_body": req_body_str[:5000],
            "response_status": status_code,
            "response_body": resp_body_str[:5000],
        },
        ensure_ascii=False,
    )
    append_request_log(request_id, log_entry)


def log_chooser_response_hook_error(context, server, status_code: int, exc: Exception) -> None:
    append_request_log(
        context.request_id,
        json.dumps(
            {
                "event": "chooser_response_hook_error",
                "server_id": getattr(server, "id", None),
                "status_code": status_code,
                "reason": str(exc)[:500],
            },
            ensure_ascii=False,
        ),
    )


def log_context_overflow_switch(request_id: int, fail_reason: str, model_name: str) -> None:
    append_request_log(
        request_id,
        f"Context overflow detected ({fail_reason}), switching to {model_name}",
    )
