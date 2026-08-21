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

        request_chars, prefix_data = self._prefix_work_for_context(context)
        if not request_chars or self._redis_client is None:
            self._clear_prefix_context(context)
            return self._choose_least_loaded(available)

        model_key = context.model_name or str(context.model_id or "")
        available_by_id = {server.id: server for server in available}
        if not prefix_data:
            self._clear_prefix_context(context)
            return self._choose_least_loaded(available)

        match = self._probe_prefix_matches(model_key, prefix_data, request_chars, available_by_id)
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

    def _cache_key(self, model_key: str, prefix_hash: str) -> str:
        return f"{self._cache_key_namespace}:{model_key}:{prefix_hash}"

    def _probe_prefix_matches(
        self,
        model_key: str,
        prefix_data: list[tuple[str, int]],
        request_chars: str,
        available_by_id: dict[int, Any],
    ) -> _PrefixMatch:
        """Find each available server's longest cached prefix via binary search.

        A successful request of length L writes a cache entry at every block
        boundary <= L with the same expiry, so for a fixed prefix text a
        server's entry validity is monotone in the block index: if block k is
        valid, every smaller block-multiple is valid too. The longest valid
        index per server is therefore found with ceil(log2(n)) probes instead
        of one HGETALL per block. All active servers share the same lattice
        and every binary search takes the same number of steps, so each round
        probes every unresolved server's midpoint in ONE pipeline.

        Semantics equal the old full scan for the routable set: per-server
        ratios, per-server originating request ids, primary (ratio > 0.9) and
        secondary (> 0.5) sets. The only divergence is best_match_ratio, which
        previously also counted entries of servers that are not currently
        available; it now reflects available servers only (it feeds the
        prefix_cache_match log event, not selection).
        """
        match = _PrefixMatch()
        now_ts = time.time()
        block_count = len(prefix_data)
        request_len = len(request_chars)

        lower = {server_id: 0 for server_id in available_by_id}
        upper = {server_id: block_count for server_id in available_by_id}
        best_index = {server_id: -1 for server_id in available_by_id}
        best_entry = {server_id: None for server_id in available_by_id}
        active = list(available_by_id)
        while active:
            pipe = self._redis_client.pipeline(transaction=False)
            probes: list[tuple[int, int]] = []
            for server_id in active:
                mid = (lower[server_id] + upper[server_id]) // 2
                pipe.hgetall(self._cache_key(model_key, prefix_data[mid][0]))
                probes.append((server_id, mid))
            values = self._execute_probe_pipeline(pipe, len(probes))
            next_active: list[int] = []
            for (server_id, mid), value in zip(probes, values):
                entry = self._valid_server_entry(value, server_id, now_ts)
                if entry is not None:
                    lower[server_id] = mid + 1
                    best_index[server_id] = mid
                    best_entry[server_id] = entry
                else:
                    upper[server_id] = mid
                if lower[server_id] < upper[server_id]:
                    next_active.append(server_id)
            active = next_active

        for server_id, index in best_index.items():
            if index < 0:
                continue
            match_ratio = prefix_data[index][1] / request_len
            match.server_match_ratios[server_id] = match_ratio
            entry = best_entry[server_id] or {}
            match.server_match_request_ids[server_id] = entry.get("rid")
            if match_ratio > match.best_match_ratio:
                match.best_match_ratio = match_ratio
            if match_ratio > self.primary_match_threshold:
                server = available_by_id.get(server_id)
                if server:
                    match.cached_matches.append(server)
        return match

    def _execute_probe_pipeline(self, pipe, probe_count: int):
        try:
            return pipe.execute()
        except Exception as e:
            logger.error("[PrefixCachePreble] Redis HGETALL failed: %s", e)
            return [{} for _ in range(probe_count)]

    @staticmethod
    def _valid_server_entry(value, server_id: int, now_ts: float) -> dict | None:
        """Parse and validate one server's cached entry at a probed block.

        Returns the parsed entry dict when the server has a valid (unexpired)
        entry at that block, None otherwise (missing, malformed, or expired).
        """
        if not isinstance(value, dict):
            return None
        field_value = value.get(str(server_id))
        if field_value is None:
            return None
        try:
            entry = json.loads(field_value)
            if now_ts < float(entry.get("exp", 0)):
                return entry
        except (TypeError, ValueError):
            return None
        return None

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
        # Reuse choose()'s extracted text and hashes for the same body instead
        # of re-parsing and re-hashing the whole request (Action 4).
        request_chars, prefix_data = self._prefix_work_for_context(context)
        if not request_chars or not prefix_data:
            return

        model_key = context.model_name or str(context.model_id or "")
        raw_cache_time = getattr(server, "cache_time", 3600)
        cache_time = 3600 if raw_cache_time is None else int(raw_cache_time)
        field_value = json.dumps({"exp": time.time() + cache_time, "rid": context.request_id})
        try:
            # transaction=False: same reason as the read probes — the write
            # batch is 2x the block count and must not run as one atomic
            # MULTI/EXEC block that freezes Redis for every other request.
            pipe = self._redis_client.pipeline(transaction=False)
            for prefix_hash, _ in prefix_data:
                key = self._cache_key(model_key, prefix_hash)
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

    def _prefix_work_for_context(
        self, context: ServerSelectionContext
    ) -> tuple[str, list[tuple[str, int]]]:
        """Extract and hash the prefix text for a request, exactly once.

        The (body, text, hashes) triple is cached on the context so
        on_response() — which runs after a possibly long stream on the same
        context — reuses choose()'s JSON parse and hash pass instead of
        repeating them. The body-bytes guard makes the cache safe across the
        retry loop: a rewritten body (model resolution, decode recompute)
        produces a cache miss and a fresh extraction.
        """
        cached = getattr(context, "_prefix_cache_work", None)
        if cached is not None and cached[0] == context.body:
            return cached[1], cached[2]
        request_chars = self._prefix_chars_for_context(context)
        prefix_data = self._get_prefix_hashes(request_chars) if request_chars else []
        context._prefix_cache_work = (context.body, request_chars, prefix_data)
        return request_chars, prefix_data

    def _prefix_chars_for_context(self, context: ServerSelectionContext) -> str:
        body_data = getattr(context, "body_data", None)
        if isinstance(body_data, dict):
            text = self._text_from_body(context.body, body_data)
        else:
            text = self._text_from_body(context.body)
        return text[: self.max_prefix_chars]

    @staticmethod
    def _text_from_body(body: bytes, parsed_data: dict | None = None) -> str:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        if isinstance(parsed_data, dict):
            rendered = PrefixCachePrebleServerChooser._text_from_data(parsed_data)
            return rendered if rendered is not None else text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(data, dict):
            return text
        rendered = PrefixCachePrebleServerChooser._text_from_data(data)
        return rendered if rendered is not None else text

    @staticmethod
    def _text_from_data(data: dict) -> str | None:
        """Render the prompt text from an already-parsed JSON object.

        Returns None when the object renders no section (callers fall back to
        the raw body text, preserving the pre-existing behavior).
        """
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
        return None

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
        # Per-model longest cached prefix via binary search: any valid server
        # entry at block k implies entries at every smaller block-multiple
        # (same monotonicity as the per-server search), so log2(n) probes per
        # model replace one HGETALL per block per model. All models share the
        # lattice, so one pipeline per round covers every unresolved model.
        now_ts = time.time()
        block_count = len(prefix_data)
        request_len = len(request_chars)
        lower = {name: 0 for name in model_names}
        upper = {name: block_count for name in model_names}
        best_index = {name: -1 for name in model_names}
        active = list(model_names)
        while active:
            pipe = self._redis_client.pipeline(transaction=False)
            probes: list[tuple[str, int]] = []
            for name in active:
                mid = (lower[name] + upper[name]) // 2
                pipe.hgetall(self._cache_key(name, prefix_data[mid][0]))
                probes.append((name, mid))
            values = self._execute_probe_pipeline(pipe, len(probes))
            next_active: list[str] = []
            for (name, mid), value in zip(probes, values):
                if PrefixCachePrebleServerChooser._has_valid_cached_server(value, now_ts):
                    lower[name] = mid + 1
                    best_index[name] = mid
                else:
                    upper[name] = mid
                if lower[name] < upper[name]:
                    next_active.append(name)
            active = next_active

        for name, index in best_index.items():
            if index < 0:
                continue
            results[name] = prefix_data[index][1] / request_len
        return results

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
