"""Pod-identity strings recorded in ``requests.target_pod_ip`` (issue #310).

The request→server link is this string (there is no foreign key). Rows that
share a ``base_url`` — one endpoint dispatching each ``api_key`` to a
different backend — must be told apart, so their targets carry a
self-describing ``#s<server.id>`` suffix. Normal servers keep the bare
``base_url``, byte-identical to the historical format.

Readers parse the suffix (``parse_server_target``); they never re-derive the
duplication set, so writes and reads stay consistent even when rows are added
or soft-deleted. A plain target whose base_url later gains a second row is
handled by the legacy fallback (attribute to every row on that base_url).
"""
from __future__ import annotations

import re
import time

from router.models import Server

TARGET_MAX_LENGTH = 500
_DUPLICATION_CACHE_TTL_SECONDS = 30.0
_ID_SUFFIX_RE = re.compile(r"#s(\d+)$")

_duplication_cache: tuple[float, frozenset[str]] | None = None


def duplicated_base_urls() -> frozenset[str]:
    """base_urls used by more than one non-deleted server row.

    TTL-cached: the set only decides whether a freshly written target needs
    the row-id suffix, and readers parse the suffix instead of consulting
    this set — a stale cache can only produce a plain target, which the
    legacy fallback handles.
    """
    global _duplication_cache
    now = time.monotonic()
    if _duplication_cache is not None and now - _duplication_cache[0] < _DUPLICATION_CACHE_TTL_SECONDS:
        return _duplication_cache[1]
    from django.db.models import Count

    urls = frozenset(
        Server.objects.filter(deleted_at__isnull=True)
        .values("base_url")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .values_list("base_url", flat=True)
    )
    _duplication_cache = (now, urls)
    return urls


def clear_duplication_cache() -> None:
    global _duplication_cache
    _duplication_cache = None


def plain_target(server) -> str:
    """Historical format: the bare base_url, truncated to the column width."""
    return (getattr(server, "base_url", "") or "")[:TARGET_MAX_LENGTH]


def qualified_target(server) -> str:
    """Row-qualified format for base_urls shared by several active rows."""
    base = plain_target(server)
    server_id = getattr(server, "id", None)
    if not base or server_id is None:
        return base
    suffix = f"#s{server_id}"
    return base[: TARGET_MAX_LENGTH - len(suffix)] + suffix


def server_target(server) -> str:
    """The target to record: qualified only when the base_url is duplicated."""
    base = plain_target(server)
    if base and base in duplicated_base_urls():
        return qualified_target(server)
    return base


def parse_server_target(target: str | None) -> tuple[str, int | None]:
    """Split a recorded target into ``(base_url, server_id | None)``.

    The ``#s<id>`` suffix self-describes the owning row; targets without it
    are plain/legacy strings attributed by base_url.
    """
    if not target:
        return "", None
    match = _ID_SUFFIX_RE.search(target)
    if match:
        return target[: match.start()], int(match.group(1))
    return target, None
