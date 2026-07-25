from __future__ import annotations

import logging

from router.services.request_context import get_request_id
from router.services.request_logger import append_request_log


class RequestContextFilter(logging.Filter):
    """Only passes records when a request_id is in context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return get_request_id() is not None


class RequestFileHandler(logging.Handler):
    """Routes log records to per-request files when a request_id is in context."""

    def emit(self, record: logging.LogRecord) -> None:
        request_id = get_request_id()
        if request_id is None:
            return
        try:
            msg = self.format(record)
            append_request_log(request_id, msg)
        except Exception:
            self.handleError(record)


_request_context_filter = RequestContextFilter()


def install_pd_handler(target_logger: logging.Logger) -> None:
    """Attach a request-aware file handler to *target_logger*.

    Log records are only written to the per-request file when ``set_request_id``
    has been called for the current context (thread / asyncio task).  When no
    request is active the handler silently does nothing, so startup and
    healthcheck noise does not leak into request files.
    """
    handler = RequestFileHandler()
    handler.addFilter(_request_context_filter)
    handler.setLevel(logging.DEBUG)
    target_logger.addHandler(handler)
