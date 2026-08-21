from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from router.config import APP_CONFIG, BASE_DIR


_LOG_PATH_CACHE: Path | None = None
_REQUEST_LOG_FILE_CACHE: dict[int, Path] = {}
_VERBOSE_REQUEST_LOG_ENV = "LLM_ROUTER_VERBOSE_REQUEST_LOG"

# Per-request append buffering: every event used to open/write/close its own
# file handle (~15-20 syscall pairs per request). Events for the same request
# now accumulate in memory and are flushed together — after the flush interval
# elapses, when the buffer registry grows too large, or explicitly at request
# end via flush_request_log(). Worst case on a crashed worker, at most the
# events appended within the last flush interval are lost.
_BUFFERED_MESSAGES: dict[int, list[str]] = {}
_BUFFERED_LAST_FLUSH: dict[int, float] = {}
_FLUSH_INTERVAL_SECONDS = 0.25
_MAX_BUFFERED_REQUESTS = 10000


def _resolve_log_path() -> Path:
    global _LOG_PATH_CACHE
    if _LOG_PATH_CACHE is None:
        log_path = Path(APP_CONFIG.get("log_path", "./logs/requests"))
        if not log_path.is_absolute():
            log_path = BASE_DIR / log_path
        _LOG_PATH_CACHE = log_path
    return _LOG_PATH_CACHE


def _current_log_time() -> datetime:
    # Bucket per-request files by Django's configured TIME_ZONE (Asia/Shanghai),
    # the same timezone the DB timestamps are shown in, so a request is findable
    # under the yyyy/mm/dd/hh/mm the user sees in the UI. datetime.now() would
    # use the server's OS timezone, which can drift from the DB by hours. Avoid
    # timezone.localtime() here: it calls timezone.now(), which code that mocks
    # the clock (tests) must not be surprised by.
    return datetime.now(timezone.get_current_timezone())


def _event_time_prefix(now: datetime) -> str:
    # Issue #262: each event line is prefixed with its time. Millisecond
    # precision because events within one request routinely share a second.
    return f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}]"


def _request_log_file(request_id: int, now: datetime | None = None) -> Path:
    log_file = _REQUEST_LOG_FILE_CACHE.get(request_id)
    if log_file is None:
        if now is None:
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
    # One clock read per event: the prefix and (on first write) the file's
    # minute bucket both derive from it.
    event_time = _current_log_time()
    message = f"{_event_time_prefix(event_time)} {message}"
    now = time.monotonic()
    last_flush = _BUFFERED_LAST_FLUSH.get(request_id)
    if last_flush is not None and now - last_flush < _FLUSH_INTERVAL_SECONDS:
        _BUFFERED_MESSAGES.setdefault(request_id, []).append(message)
        _evict_buffered_overflow()
        return
    buffered = _BUFFERED_MESSAGES.pop(request_id, [])
    _BUFFERED_LAST_FLUSH[request_id] = now
    with _request_log_file(request_id, event_time).open("a", encoding="utf-8") as handle:
        for buffered_message in buffered:
            handle.write(buffered_message.rstrip() + "\n")
        handle.write(message.rstrip() + "\n")


def flush_request_log(request_id: int) -> None:
    """Write any buffered events for *request_id* to its per-request log file."""
    messages = _BUFFERED_MESSAGES.pop(request_id, [])
    if not messages:
        return
    with _request_log_file(request_id).open("a", encoding="utf-8") as handle:
        for message in messages:
            handle.write(message.rstrip() + "\n")
    _BUFFERED_LAST_FLUSH.pop(request_id, None)


def clear_request_log_buffers() -> None:
    """Drop buffered (unflushed) events; used by tests between runs."""
    _BUFFERED_MESSAGES.clear()
    _BUFFERED_LAST_FLUSH.clear()


def _evict_buffered_overflow() -> None:
    # Flush the oldest buffered requests (dicts keep insertion order) so the
    # registry never grows unboundedly on a long-lived worker.
    while len(_BUFFERED_MESSAGES) > _MAX_BUFFERED_REQUESTS:
        flush_request_log(next(iter(_BUFFERED_MESSAGES)))


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
