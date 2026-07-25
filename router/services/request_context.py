from __future__ import annotations

import contextvars

_request_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "request_id", default=None
)


def set_request_id(request_id: int) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> int | None:
    return _request_id_var.get()


def clear_request_id() -> None:
    _request_id_var.set(None)
