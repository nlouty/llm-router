from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import redis

from router.config import APP_CONFIG
from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class _PrefixMatch:
    best_match_ratio: float = 0.0
    best_match_request_id: Any = None
    cached_matches: list[Any] = field(default_factory=list)
    server_match_ratios: dict[int, float] = field(default_factory=dict)


@dataclass
class _ValidPrefixServers:
    found: bool = False
    primary: list[Any] = field(default_factory=list)


class PrefixCachePrebleServerChooser(LeastConnectionServerChooser):
    _redis_client: redis.Redis | None = None
    _client_lock = threading.Lock()
    _cache_key_namespace = "prefix_chars"

    def __init__(
        self,
        count_provider: Callable[[list[str]], dict[str, int]] | None = None,
        primary_match_threshold: float | None = None,
        secondary_match_threshold: float | None = None,
        max_prefix_chars: int | None = None,
        prefix_block_chars: int | None = None,
    ):
        super().__init__(count_provider)
        prefix_config = APP_CONFIG.get("prefix_cache", {})
        self.primary_match_threshold = self._float_setting(primary_match_threshold, prefix_config.get("primary_match_threshold"), 0.9)
        self.secondary_match_threshold = self._float_setting(secondary_match_threshold, prefix_config.get("secondary_match_threshold"), 0.5)
        self.max_prefix_chars = self._positive_int_setting(
            max_prefix_chars,
            prefix_config.get("max_prefix_chars"),
            1000000,
        )
        self.prefix_block_chars = self._positive_int_setting(
            prefix_block_chars,
            prefix_config.get("prefix_block_chars"),
            8,
        )
        self._ensure_redis()

    def _ensure_redis(self):
        if PrefixCachePrebleServerChooser._redis_client is not None:
            return
        with PrefixCachePrebleServerChooser._client_lock:
            if PrefixCachePrebleServerChooser._redis_client is not None:
                return
            redis_cfg = APP_CONFIG.get("prefix_cache", {}).get("redis", {})
            try:
                PrefixCachePrebleServerChooser._redis_client = redis.Redis(
                    host=redis_cfg.get("host", "localhost"),
                    port=redis_cfg.get("port", 6379),
                    db=redis_cfg.get("db", 0),
                    password=redis_cfg.get("password"),
                    decode_responses=True,
                    socket_timeout=2.0,
                    socket_connect_timeout=2.0,
                )
            except Exception as e:
                logger.error("[PrefixCachePreble] Failed to connect to Redis: %s", e)

    def choose(
        self,
        candidates: Sequence[Any],
        context: ServerSelectionContext,
        attempted_server_ids: set[int],
    ) -> Any | None:
        available = [server for server in candidates if server.id not in attempted_server_ids]
        if not available:
            return None

        request_chars = self._prefix_chars_from_body(context.body)
        if not request_chars or self._redis_client is None:
            self._clear_prefix_context(context)
            return self._choose_least_loaded(available)

        model_key = context.model_name or str(context.model_id or "")
        available_by_id = {server.id: server for server in available}
        prefix_data = self._get_prefix_hashes(request_chars)
        if not prefix_data:
            return self._choose_least_loaded(available)

        cached_values = self._read_cached_prefixes(model_key, prefix_data)
        match = self._collect_prefix_matches(cached_values, prefix_data, request_chars, available_by_id)
        self._log_prefix_matches(model_key, available, match)
        context.prefix_cache = match.best_match_ratio
        context.last_match = match.best_match_request_id
        return self._choose_from_prefix_match(available, match)

    @staticmethod
    def _clear_prefix_context(context: ServerSelectionContext) -> None:
        context.prefix_cache = 0.0
        context.last_match = None

    def _read_cached_prefixes(self, model_key: str, prefix_data: list[tuple[str, int]]):
        redis_keys = [self._cache_key(model_key, prefix_hash) for prefix_hash, _ in prefix_data]
        return self._mget_cache_values(redis_keys, "[PrefixCachePreble] Redis MGET failed: %s")

    def _cache_key(self, model_key: str, prefix_hash: str) -> str:
        return f"{self._cache_key_namespace}:{model_key}:{prefix_hash}"

    def _mget_cache_values(self, redis_keys: list[str], error_message: str):
        try:
            return self._redis_client.mget(redis_keys)
        except Exception as e:
            logger.error(error_message, e)
            return [None] * len(redis_keys)

    def _collect_prefix_matches(
        self,
        cached_values,
        prefix_data: list[tuple[str, int]],
        request_chars: str,
        available_by_id: dict[int, Any],
    ) -> _PrefixMatch:
        match = _PrefixMatch()
        now_ts = time.time()
        for index, value in enumerate(cached_values):
            if not value:
                continue
            try:
                self._apply_cached_prefix_match(
                    match,
                    value,
                    prefix_data[index][1],
                    len(request_chars),
                    available_by_id,
                    now_ts,
                )
            except Exception as e:
                logger.error("[PrefixCachePreble] Failed to parse cached value: %s", e)
        return match

    def _apply_cached_prefix_match(
        self,
        match: _PrefixMatch,
        value,
        prefix_len: int,
        request_len: int,
        available_by_id: dict[int, Any],
        now_ts: float,
    ) -> None:
        data = json.loads(value)
        request_id = data.get("request_id")
        servers_data = data.get("servers", {})
        match_ratio = prefix_len / request_len
        valid_servers = self._valid_servers_for_prefix(
            servers_data,
            match_ratio,
            available_by_id,
            match.server_match_ratios,
            now_ts,
        )
        if not valid_servers.found:
            return
        if match_ratio > match.best_match_ratio:
            match.best_match_ratio = match_ratio
            match.best_match_request_id = request_id
        if valid_servers.primary:
            match.cached_matches = valid_servers.primary

    def _valid_servers_for_prefix(
        self,
        servers_data: dict[str, float],
        match_ratio: float,
        available_by_id: dict[int, Any],
        server_match_ratios: dict[int, float],
        now_ts: float,
    ) -> _ValidPrefixServers:
        valid_servers = _ValidPrefixServers()
        for server_id_text, expiry_ts in servers_data.items():
            if now_ts >= expiry_ts:
                continue
            server_id = int(server_id_text)
            valid_servers.found = True
            if match_ratio > server_match_ratios.get(server_id, 0.0):
                server_match_ratios[server_id] = match_ratio
            if match_ratio > self.primary_match_threshold:
                server = available_by_id.get(server_id)
                if server:
                    valid_servers.primary.append(server)
        return valid_servers

    @staticmethod
    def _log_prefix_matches(model_key: str, available: Sequence[Any], match: _PrefixMatch) -> None:
        logger.info(
            "[PrefixCachePreble] match_ratio per server (model=%s, best=%.4f):",
            model_key, match.best_match_ratio,
        )
        for server in available:
            ratio = match.server_match_ratios.get(server.id, 0.0)
            logger.info(
                "  server_id=%-6d base_url=%-40s match_ratio=%.4f",
                server.id, server.base_url, ratio,
            )

    def _choose_from_prefix_match(self, available: Sequence[Any], match: _PrefixMatch):
        if match.cached_matches:
            return self._choose_least_loaded(match.cached_matches)
        secondary_matches = [
            server for server in available
            if match.server_match_ratios.get(server.id, 0.0) > self.secondary_match_threshold
        ]
        if secondary_matches:
            return self._choose_least_loaded(secondary_matches)
        return self._choose_least_loaded(available)

    def on_response(self, server: Any, context: ServerSelectionContext, status_code: int) -> None:
        if not 200 <= status_code < 300 or self._redis_client is None:
            return
        request_chars = self._prefix_chars_from_body(context.body)
        if not request_chars:
            return

        model_key = context.model_name or str(context.model_id or "")
        raw_cache_time = getattr(server, "cache_time", 3600)
        cache_time = 3600 if raw_cache_time is None else int(raw_cache_time)
        expiry_ts = time.time() + cache_time

        # Generate hashes for blocks and full request
        prefix_data = self._get_prefix_hashes(request_chars)

        if not prefix_data:
            return

        try:
            pipe = self._redis_client.pipeline()
            for h, _ in prefix_data:
                data = {
                    "request_id": context.request_id,
                    "servers": {str(server.id): expiry_ts}
                }
                pipe.set(self._cache_key(model_key, h), json.dumps(data), ex=cache_time)
            pipe.execute()
        except Exception as e:
            logger.error("[PrefixCachePreble] Redis SET failed: %s", e)

    def _get_prefix_hashes(self, text: str) -> list[tuple[str, int]]:
        results = []
        h = hashlib.sha256()
        block_size = self.prefix_block_chars
        for i in range(0, len(text), block_size):
            block = text[i : i + block_size]
            if not block:
                break
            h.update(block.encode("utf-8"))
            results.append((h.hexdigest(), i + len(block)))
        
        # Ensure we always include the full text if not already included by block alignment
        if len(text) % block_size != 0:
            # This is slightly tricky with incremental hashing if we already updated it.
            # But the loop above already handles it!
            # If text length is 10 and block is 8:
            # i=0: block = text[0:8], i+len(block) = 8
            # i=8: block = text[8:10], i+len(block) = 10
            # So the full text IS included.
            pass
        return results

    def _log_connection_counts(self, available: Sequence[Any], load_counts: dict[int, int]) -> None:
        logger.info("[PrefixCachePreble] connection counts per server:")
        for server in available:
            count = load_counts.get(server.id, 0)
            logger.info(
                "  server_id=%-6d base_url=%-40s connections=%d",
                server.id, server.base_url, count,
            )

    def _prefix_chars_from_body(self, body: bytes) -> str:
        text = self._text_from_body(body)
        return text[: self.max_prefix_chars]

    @staticmethod
    def _text_from_body(body: bytes) -> str:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(data, dict):
            return text

        messages = data.get("messages")
        if isinstance(messages, list):
            parts = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = message.get("role") or ""
                content = PrefixCachePrebleServerChooser._message_content_text(message.get("content"))
                if content:
                    parts.append(f"{role}: {content}" if role else content)
            if parts:
                return "\n".join(parts)

        prompt = data.get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):
            return "\n".join(item for item in prompt if isinstance(item, str))
        return text

    @staticmethod
    def _message_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _float_setting(*values) -> float:
        default = float(values[-1])
        for value in values[:-1]:
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def _int_setting(*values) -> int:
        default = int(values[-1])
        for value in values[:-1]:
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def _positive_int_setting(*values) -> int:
        setting = PrefixCachePrebleServerChooser._int_setting(*values)
        return max(setting, 1)

    def get_all_model_prefix_ratios(self, body: bytes, model_names: list[str]) -> dict[str, float]:
        request_chars = self._prefix_chars_from_body(body)
        if not request_chars or self._redis_client is None or not model_names:
            return {name: 0.0 for name in model_names}

        prefix_data = self._get_prefix_hashes(request_chars)
        if not prefix_data:
            return {name: 0.0 for name in model_names}

        results = {name: 0.0 for name in model_names}
        redis_keys, key_map = self._model_prefix_cache_keys(model_names, prefix_data)
        if not redis_keys:
            return results

        cached_values = self._mget_cache_values(redis_keys, "[PrefixCachePreble] Multi-model Redis MGET failed: %s")
        self._apply_model_prefix_ratios(results, cached_values, key_map, prefix_data, len(request_chars))
        return results

    def _model_prefix_cache_keys(
        self,
        model_names: list[str],
        prefix_data: list[tuple[str, int]],
    ) -> tuple[list[str], list[tuple[str, int]]]:
        redis_keys = []
        key_map = []
        for model_name in model_names:
            for index, (prefix_hash, _) in enumerate(prefix_data):
                redis_keys.append(self._cache_key(model_name, prefix_hash))
                key_map.append((model_name, index))
        return redis_keys, key_map

    def _apply_model_prefix_ratios(
        self,
        results: dict[str, float],
        cached_values,
        key_map: list[tuple[str, int]],
        prefix_data: list[tuple[str, int]],
        request_len: int,
    ) -> None:
        now_ts = time.time()
        for index, value in enumerate(cached_values):
            if not value:
                continue
            try:
                self._apply_model_prefix_ratio(results, value, key_map[index], prefix_data, request_len, now_ts)
            except Exception:
                continue

    @staticmethod
    def _apply_model_prefix_ratio(
        results: dict[str, float],
        value,
        mapped_key: tuple[str, int],
        prefix_data: list[tuple[str, int]],
        request_len: int,
        now_ts: float,
    ) -> None:
        data = json.loads(value)
        servers_data = data.get("servers", {})
        if not PrefixCachePrebleServerChooser._has_valid_cached_server(servers_data, now_ts):
            return

        model_name, prefix_index = mapped_key
        _, prefix_len = prefix_data[prefix_index]
        ratio = prefix_len / request_len
        if ratio > results[model_name]:
            results[model_name] = ratio

    @staticmethod
    def _has_valid_cached_server(servers_data: dict[str, float], now_ts: float) -> bool:
        return any(now_ts < expiry_ts for expiry_ts in servers_data.values())
