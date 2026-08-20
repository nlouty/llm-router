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
from router.route_algorithm.least_connection import (
    LeastConnectionServerChooser,
    effective_weight,
)
from router.services.request_log_handler import install_pd_handler
from router.services.request_logger import append_request_log

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
install_pd_handler(logger)


@dataclass
class _PrefixMatch:
    best_match_ratio: float = 0.0
    cached_matches: list[Any] = field(default_factory=list)
    server_match_ratios: dict[int, float] = field(default_factory=dict)
    server_match_request_ids: dict[int, Any] = field(default_factory=dict)


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
            2000000,
        )
        self.prefix_block_chars = self._positive_int_setting(
            prefix_block_chars,
            prefix_config.get("prefix_block_chars"),
            128,
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
            self._clear_prefix_context(context)
            return self._choose_least_loaded(available)

        cached_values = self._read_cached_prefixes(model_key, prefix_data)
        match = self._collect_prefix_matches(cached_values, prefix_data, request_chars, available_by_id)
        self._log_prefix_matches(model_key, available, match)
        selected = self._choose_from_prefix_match(available, match)
        self._apply_selected_prefix_context(context, selected, match)

        if context.request_id:
            if match.best_match_ratio > 0:
                append_request_log(context.request_id, json.dumps({
                    "event": "prefix_cache_match",
                    "model_key": model_key,
                    "best_match_ratio": round(match.best_match_ratio, 4),
                    "cached_match_count": len(match.cached_matches) if match.cached_matches else 0,
                    "available_count": len(available),
                }, ensure_ascii=False))
            if selected is not None:
                match_ratio = match.server_match_ratios.get(selected.id, 0.0)
                is_cache_hit = match_ratio > self.primary_match_threshold and selected in (match.cached_matches or [])
                append_request_log(context.request_id, json.dumps({
                    "event": "prefix_server_chosen",
                    "server_id": selected.id,
                    "base_url": selected.base_url,
                    "match_ratio": round(match_ratio, 4),
                    "reason": "cache_hit" if is_cache_hit else "least_loaded",
                    "role": getattr(selected, "role", "mixed") or "mixed",
                }, ensure_ascii=False))

        return selected

    def _apply_selected_prefix_context(
        self,
        context: ServerSelectionContext,
        selected: Any,
        match: _PrefixMatch,
    ) -> None:
        if selected is None:
            self._clear_prefix_context(context)
            return
        context.prefix_cache = match.server_match_ratios.get(selected.id, 0.0)
        context.last_match = match.server_match_request_ids.get(selected.id)

    @staticmethod
    def _clear_prefix_context(context: ServerSelectionContext) -> None:
        context.prefix_cache = 0.0
        context.last_match = None

    def _read_cached_prefixes(self, model_key: str, prefix_data: list[tuple[str, int]]):
        redis_keys = [self._cache_key(model_key, prefix_hash) for prefix_hash, _ in prefix_data]
        return self._read_cache_hashes(redis_keys, "[PrefixCachePreble] Redis HGETALL failed: %s")

    def _cache_key(self, model_key: str, prefix_hash: str) -> str:
        return f"{self._cache_key_namespace}:{model_key}:{prefix_hash}"

    def _read_cache_hashes(self, redis_keys: list[str], error_message: str):
        try:
            # transaction=False: no MULTI/EXEC wrapper. The batch is ~10-15k
            # commands per request; wrapping it in MULTI makes Redis execute it
            # atomically and buffer every reply, stalling the single-threaded
            # server (and every other client) for 10-20ms per request.
            pipe = self._redis_client.pipeline(transaction=False)
            for key in redis_keys:
                pipe.hgetall(key)
            return pipe.execute()
        except Exception as e:
            logger.error(error_message, e)
            return [{} for _ in redis_keys]

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
        match_ratio = prefix_len / request_len
        valid_servers = self._valid_servers_for_prefix(
            value,
            match_ratio,
            available_by_id,
            match.server_match_ratios,
            match.server_match_request_ids,
            now_ts,
        )
        if not valid_servers.found:
            return
        if match_ratio > match.best_match_ratio:
            match.best_match_ratio = match_ratio
        if valid_servers.primary:
            match.cached_matches = valid_servers.primary

    def _valid_servers_for_prefix(
        self,
        servers_data: dict[str, str],
        match_ratio: float,
        available_by_id: dict[int, Any],
        server_match_ratios: dict[int, float],
        server_match_request_ids: dict[int, Any],
        now_ts: float,
    ) -> _ValidPrefixServers:
        valid_servers = _ValidPrefixServers()
        for server_id_text, field_value in servers_data.items():
            try:
                entry = json.loads(field_value)
                expiry_ts = float(entry.get("exp", 0))
                request_id = entry.get("rid")
            except (TypeError, ValueError):
                continue
            if now_ts >= expiry_ts:
                continue
            server_id = int(server_id_text)
            valid_servers.found = True
            if match_ratio > server_match_ratios.get(server_id, 0.0):
                server_match_ratios[server_id] = match_ratio
                server_match_request_ids[server_id] = request_id
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
        cached_matches = match.cached_matches or [
            server for server in available
            if match.server_match_ratios.get(server.id, 0.0) > self.secondary_match_threshold
        ]
        if not cached_matches:
            return self._choose_least_loaded(available)

        load_counts = self._load_counts(available)
        cached = self._pick_least_loaded(cached_matches, load_counts)
        min_load = min(load_counts.get(server.id, 0) for server in available)
        if self._is_overloaded(cached, load_counts.get(cached.id, 0), min_load):
            logger.info(
                "[PrefixCachePreble] cached server_id=%s overloaded; "
                "falling back to least loaded server",
                cached.id,
            )
            return self._pick_least_loaded(available, load_counts)
        return cached

    def _is_overloaded(self, server: Any, load: float, min_load: float) -> bool:
        # weight doubles as the server's suggested workload (issue #247): escape
        # cache affinity only when the cached server is at or above its weight
        # AND a materially lighter server exists (load > 2x the lightest one).
        return load >= effective_weight(server) and load > min_load * 2

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

        field_value = json.dumps({"exp": expiry_ts, "rid": context.request_id})
        try:
            # transaction=False: same reason as _read_cache_hashes — the write
            # batch is 2x the block count and must not run as one atomic
            # MULTI/EXEC block that freezes Redis for every other request.
            pipe = self._redis_client.pipeline(transaction=False)
            for h, _ in prefix_data:
                key = self._cache_key(model_key, h)
                pipe.hset(key, str(server.id), field_value)
                pipe.expire(key, cache_time)
            pipe.execute()
        except Exception as e:
            logger.error("[PrefixCachePreble] Redis HSET failed: %s", e)

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

    def _load_counts(self, available: Sequence[Any]) -> dict[int, float]:
        """Workload per server, with PD prefillers reporting node + cluster load.

        Mixed servers report their own workload. A prefiller reports
        ``max(own workload, min decoder workload)`` so least-loaded selection
        still prefers an idle prefiller within the cluster while a decoder-bound
        cluster is never mistaken for an idle node. Decoders are never in the
        candidate set (they hold no prefix cache and are chosen later).
        """
        load_counts = super()._load_counts(available)
        prefillers = [s for s in available if getattr(s, "role", "mixed") == "prefiller"]
        if not prefillers:
            return load_counts
        decoder_mins = self._cluster_decoder_mins()
        for server in prefillers:
            own = int(getattr(server, "workload", 0) or 0)
            floor = decoder_mins.get(getattr(server, "group_id", None), 0)
            load_counts[server.id] = max(own, floor)
        return load_counts

    @staticmethod
    def _cluster_decoder_mins() -> dict[str, float]:
        from router.repositories.servers import ServerRepository

        return ServerRepository.cluster_decoder_min_load(ServerRepository.list_all_online())

    def _prefix_chars_from_body(self, body: bytes) -> str:
        text = self._text_from_body(body)
        return text[: self.max_prefix_chars]

    def _tokens_from_body(self, body: bytes) -> str:
        return self._prefix_chars_from_body(body)

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

        # Section order mirrors how serving-side chat templates lay out the
        # rendered prompt (GLM-5.2 / DeepSeek-V4-Flash / Qwen3.6): generation
        # options, then tool definitions, then the messages. vLLM prefix-cache
        # blocks are chained from token 0, so any drift the template renders
        # but this text omits produces a false high match ratio.
        sections = []
        options = {
            key: data[key]
            for key in ("reasoning_effort", "response_format", "chat_template_kwargs")
            if key in data
        }
        if options:
            sections.append("options: " + json.dumps(options, ensure_ascii=False))

        tools = data.get("tools")
        if isinstance(tools, list) and tools:
            tool_lines = [
                json.dumps(tool, ensure_ascii=False) if isinstance(tool, dict) else str(tool)
                for tool in tools
            ]
            sections.append("tools:\n" + "\n".join(tool_lines))

        messages = data.get("messages")
        if isinstance(messages, list):
            parts = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = message.get("role") or ""
                content = PrefixCachePrebleServerChooser._message_content_text(message.get("content"))
                tool_calls = PrefixCachePrebleServerChooser._tool_calls_text(message.get("tool_calls"))
                turn_text = content + tool_calls
                if turn_text:
                    parts.append(f"{role}: {turn_text}" if role else turn_text)
            if parts:
                sections.append("\n".join(parts))

        if sections:
            return "\n".join(sections)

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
                elif isinstance(item, dict) and item.get("type"):
                    # Non-text parts (images, audio, ...) still change the
                    # rendered prompt via template placeholders or reminders.
                    parts.append(f"<part:{item['type']}>")
            return "\n".join(parts)
        return ""

    @staticmethod
    def _tool_calls_text(tool_calls: Any) -> str:
        if not isinstance(tool_calls, list) or not tool_calls:
            return ""
        parts = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                function = tool_call
            name = function.get("name") or ""
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                args_text = json.dumps(arguments, ensure_ascii=False)
            elif isinstance(arguments, str):
                args_text = arguments
            else:
                args_text = ""
            if name or args_text:
                parts.append(f"<tool_call>{name}({args_text})")
        return "".join(parts)

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

        cached_values = self._read_cache_hashes(redis_keys, "[PrefixCachePreble] Multi-model Redis HGETALL failed: %s")
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
        if not PrefixCachePrebleServerChooser._has_valid_cached_server(value, now_ts):
            return

        model_name, prefix_index = mapped_key
        _, prefix_len = prefix_data[prefix_index]
        ratio = prefix_len / request_len
        if ratio > results[model_name]:
            results[model_name] = ratio

    @staticmethod
    def _has_valid_cached_server(servers_data: dict[str, str], now_ts: float) -> bool:
        for field_value in servers_data.values():
            try:
                entry = json.loads(field_value)
                if now_ts < float(entry.get("exp", 0)):
                    return True
            except (TypeError, ValueError):
                continue
        return False
