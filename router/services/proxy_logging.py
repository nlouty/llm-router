from __future__ import annotations

import json

from router.services.request_logger import append_error_log, append_request_log


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


_SECRET_REQUEST_HEADERS = ("authorization", "csb-token")


def _safe_headers(headers) -> dict:
    if not headers:
        return {}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _SECRET_REQUEST_HEADERS
    }


def _redact_request_body(body) -> str:
    """Decode a request body for logging, dropping ``messages`` for privacy
    while keeping tools, mcp servers, and all other options (issue #225)."""
    try:
        text = body.decode("utf-8") if body else ""
    except (UnicodeDecodeError, AttributeError):
        return repr(body)[:2000]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text[:5000]
    if isinstance(data, dict):
        data.pop("messages", None)
        text = json.dumps(data, ensure_ascii=False)
    return text[:5000]


def log_request_context(request_id: int, method: str, url: str, headers, body) -> None:
    """Log the request context of a failed request: HTTP headers (with
    authorization/csb-token redacted) and the request body with ``messages``
    removed, so tools, mcp servers, and other options stay available for
    debugging without leaking user prompts (issue #225)."""
    payload = {
        "event": "request_context",
        "request_id": request_id,
        "method": method,
        "url": url,
        "request_headers": _safe_headers(headers),
        "request_body": _redact_request_body(body),
    }
    append_error_log(request_id, json.dumps(payload, ensure_ascii=False))


def log_request_context_for(context) -> None:
    """Log request context derived from a ServerSelectionContext (issue #225)."""
    log_request_context(
        context.request_id,
        getattr(context, "method", "") or "",
        f"/v1/{(getattr(context, 'path', '') or '').rstrip('/')}",
        getattr(context, "headers", None),
        getattr(context, "body", b""),
    )


def log_error_detail(
    request_id: int,
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    status_code: int,
    response_body: bytes,
) -> None:
    # Every failed request emits a uniform ``request_context`` event (issue
    # #225). The upstream-specific ``upstream_error`` event carries only the
    # response, so request context lives in exactly one place across all paths.
    log_request_context(request_id, method, url, headers, body)
    try:
        resp_body_str = response_body.decode("utf-8") if response_body else ""
    except (UnicodeDecodeError, AttributeError):
        resp_body_str = repr(response_body)[:2000]

    log_entry = json.dumps(
        {
            "event": "upstream_error",
            "request_id": request_id,
            "response_status": status_code,
            "response_body": resp_body_str[:5000],
        },
        ensure_ascii=False,
    )
    append_error_log(request_id, log_entry)


def decode_body_for_log(body: bytes) -> str:
    """Best-effort decode an upstream request/response body for log output."""
    if not body:
        return ""
    try:
        return body.decode("utf-8", "replace")
    except Exception:
        return repr(body)


def log_failure_response(
    request_id: int,
    target_pod_ip: str | None,
    status_code: int,
    response_body: bytes,
) -> None:
    """Log the upstream response body for a terminal failure.

    Used by disaggregated prefill/decode paths (and retry exhaustion) where the
    richer ``log_error_detail`` (request headers/body) is not available, but the
    server's error body must still be preserved for debugging.
    """
    payload = {
        "event": "failure_response",
        "request_id": request_id,
        "status_code": status_code,
        "response_body": decode_body_for_log(response_body)[:5000],
    }
    if target_pod_ip:
        payload["target_pod_ip"] = target_pod_ip
    safe_append_request_log(request_id, json.dumps(payload, ensure_ascii=False))


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
