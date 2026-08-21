from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


@dataclass
class ServerSelectionContext:
    request_id: int
    ip_id: int | None
    model_id: int | None
    model_name: str | None
    path: str
    method: str
    is_stream: bool
    body: bytes
    headers: dict | None = None
    origin_model_name: str | None = None
    auto_model_selection: bool = False
    prefix_cache: float = 0.0
    last_match: int | None = None
    router_result: str | None = None
    session: str | None = None
    # Parsed JSON of ``body`` when the request is a JSON object; populated once
    # by RequestParser and kept in sync with ``body`` by model resolution, so
    # downstream consumers avoid re-parsing the same bytes.
    body_data: dict | None = None
    # Per-request cache of (body_bytes, prefix_text, prefix_hashes) computed by
    # the prefix-cache chooser so on_response reuses choose()'s hashing work.
    _prefix_cache_work: tuple | None = None


class ServerChooser(Protocol):
    def choose(
        self,
        candidates: Sequence[Any],
        context: ServerSelectionContext,
        attempted_server_ids: set[int],
    ) -> Any | None:
        ...
