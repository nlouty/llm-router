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

    Server-choosing diagnostics (INFO and below) are written to the per-request
    file only — they never reach the root logger / main log.  Errors (ERROR and
    above) are still surfaced to the main log via a console handler attached to
    *target_logger*, and also land in the per-request file.  ``propagate`` is set
    to ``False`` so records never travel up to the root logger.
    """
    file_handler = RequestFileHandler()
    file_handler.addFilter(_request_context_filter)
    file_handler.setLevel(logging.DEBUG)
    target_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    target_logger.addHandler(console_handler)

    target_logger.setLevel(logging.DEBUG)
    target_logger.propagate = False
