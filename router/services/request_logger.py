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
