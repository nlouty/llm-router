from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from router.config import APP_CONFIG, BASE_DIR


_LOG_PATH_CACHE: Path | None = None
_REQUEST_LOG_FILE_CACHE: dict[int, Path] = {}
_VERBOSE_REQUEST_LOG_ENV = "LLM_ROUTER_VERBOSE_REQUEST_LOG"


def _resolve_log_path() -> Path:
    global _LOG_PATH_CACHE
    if _LOG_PATH_CACHE is None:
        log_path = Path(APP_CONFIG.get("log_path", "./logs/requests"))
        if not log_path.is_absolute():
            log_path = BASE_DIR / log_path
        _LOG_PATH_CACHE = log_path
    return _LOG_PATH_CACHE


def _current_log_time() -> datetime:
    return datetime.now()


def _request_log_file(request_id: int) -> Path:
    log_file = _REQUEST_LOG_FILE_CACHE.get(request_id)
    if log_file is None:
        now = _current_log_time()
        log_file = (
            _resolve_log_path()
            / f"{now.year:04d}"
            / f"{now.month:02d}"
            / f"{now.day:02d}"
            / f"{now.hour:02d}"
            / f"{now.minute:02d}"
            / f"{request_id}.log"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        _REQUEST_LOG_FILE_CACHE[request_id] = log_file
        if len(_REQUEST_LOG_FILE_CACHE) > 10000:
            _REQUEST_LOG_FILE_CACHE.pop(next(iter(_REQUEST_LOG_FILE_CACHE)))
    return log_file


def verbose_request_logging_enabled() -> bool:
    value = os.environ.get(_VERBOSE_REQUEST_LOG_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on", "verbose"}


def append_request_log(request_id: int, message: str) -> None:
    with _request_log_file(request_id).open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def append_error_log(request_id: int, message: str) -> None:
    append_request_log(request_id, message)


def append_verbose_request_log(request_id: int, body: bytes) -> None:
    if not verbose_request_logging_enabled():
        return
    try:
        request_body = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        request_body = body.decode("utf-8", errors="replace")
    message = json.dumps(
        {
            "event": "user_request",
            "request_id": request_id,
            "body": request_body,
        },
        ensure_ascii=False,
        indent=2,
    )
    append_request_log(request_id, message)


def log_server_attempt(request_id: int, attempt: int, server_id: int, base_url: str, model_id: int, result: str, retry: bool, status: int | None = None, reason: str | None = None) -> None:
    payload = {
        "event": "server_attempt",
        "request_id": request_id,
        "attempt": attempt,
        "server_id": server_id,
        "base_url": base_url,
        "model_id": model_id,
        "result": result,
        "retry": retry,
    }
    if status is not None:
        payload["status"] = status
    if reason:
        payload["reason"] = reason[:500]
    append_request_log(request_id, json.dumps(payload, ensure_ascii=False))


def log_multi_server_route(request_id: int, attempted_server_ids: list[int], final_server_id: int | None) -> None:
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


def log_upstream_error_detail(request_id: int, method: str, url: str, headers: dict, body: bytes, status_code: int, response_body: bytes) -> None:
    try:
        req_body_str = body.decode("utf-8") if body else ""
    except (UnicodeDecodeError, AttributeError):
        req_body_str = repr(body)[:2000]
    try:
        resp_body_str = response_body.decode("utf-8") if response_body else ""
    except (UnicodeDecodeError, AttributeError):
        resp_body_str = repr(response_body)[:2000]
    safe_headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "csb-token")}
    log_entry = json.dumps({
        "event": "upstream_error",
        "request_id": request_id,
        "method": method,
        "url": url,
        "request_headers": safe_headers,
        "request_body": req_body_str[:5000],
        "response_status": status_code,
        "response_body": resp_body_str[:5000],
    }, ensure_ascii=False)
    append_error_log(request_id, log_entry)


class RequestLogBuffer:
    def __init__(self):
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)

    def flush(self, request_id: int) -> None:
        with _request_log_file(request_id).open("a", encoding="utf-8") as handle:
            for message in self.messages:
                handle.write(message.rstrip() + "\n")
        self.messages.clear()
