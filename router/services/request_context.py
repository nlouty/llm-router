from __future__ import annotations

import contextvars

_request_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "request_id", default=None
)

_llm_choosing_deadline_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "llm_choosing_deadline", default=None
)


def set_request_id(request_id: int) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> int | None:
    return _request_id_var.get()


def clear_request_id() -> None:
    _request_id_var.set(None)


def set_llm_choosing_deadline(deadline: float) -> None:
    _llm_choosing_deadline_var.set(deadline)


def get_llm_choosing_deadline() -> float | None:
    return _llm_choosing_deadline_var.get()


def clear_llm_choosing_deadline() -> None:
    _llm_choosing_deadline_var.set(None)
