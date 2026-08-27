from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Any

import requests
from django.http import StreamingHttpResponse
from django.utils import timezone

from router.config import APP_CONFIG
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.services import proxy_logging, proxy_response
from router.services.request_context import get_request_id
from router.services.request_log_handler import install_pd_handler
from router.services.request_logger import append_request_log, flush_request_log
from router.utils.errors import error_payload, timeout_sse_event
from router.utils.headers import build_upstream_headers
from router.utils.sse import parse_sse_usage

logger = logging.getLogger(__name__)
install_pd_handler(logger)

_KV_TRANSFER_FAIL_TAG = " -- KV_TRANS_FAIL"


def build_prefill_body(body: bytes) -> bytes:
    """Build the prefill-only request: max_tokens=1, non-stream, do_remote_decode."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    data["stream"] = False
    data["max_tokens"] = 1
    data["min_tokens"] = 1
    if "max_completion_tokens" in data:
        data["max_completion_tokens"] = 1
    data.pop("stream_options", None)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_decode_body(body: bytes, kv_transfer_params: dict) -> bytes:
    """Inject prefiller-returned kv_transfer_params into the original request."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if kv_transfer_params:
        data["kv_transfer_params"] = kv_transfer_params
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _extract_kv_params(prefill_json: dict) -> dict:
    params = prefill_json.get("kv_transfer_params")
    return params if isinstance(params, dict) and params else {}


def _parse_usage(usage_dict: dict | None) -> tuple[int, int, int]:
    """Return (prompt_tokens, completion_tokens, cached_tokens)."""
    if not isinstance(usage_dict, dict):
        return 0, 0, 0
    prompt_tokens = int(usage_dict.get("prompt_tokens") or 0)
    completion_tokens = int(usage_dict.get("completion_tokens") or 0)
    details = usage_dict.get("prompt_tokens_details")
    cached_tokens = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return prompt_tokens, completion_tokens, cached_tokens


def rewrite_json_cached_tokens(content: bytes, cached_tokens: int) -> bytes:
    """Rewrite cached_tokens in a JSON usage body to the prefiller's value.

    In disaggregated mode the decoder's reported cached_tokens does not reflect
    a real prefix hit (the KV was transferred, not recomputed). The prefiller's
    cached_tokens is authoritative, so the client-visible response is rewritten
    to match. Both the top-level ``usage.cached_tokens`` and the nested
    ``usage.prompt_tokens_details.cached_tokens`` are updated where present.
    Returns the original content unchanged if it is not valid JSON with usage.
    """
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    if not isinstance(data, dict):
        return content
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return content
    usage["cached_tokens"] = cached_tokens
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        details["cached_tokens"] = cached_tokens
    else:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def rewrite_sse_cached_tokens(chunks: list[bytes], cached_tokens: int) -> list[bytes]:
    """Rewrite cached_tokens in the final SSE usage chunk to the prefiller's value.

    SSE usage is reported in a trailing ``data: {..usage..}`` event. This scans
    only the usage-bearing events (mirrors parse_sse_usage) and rewrites the
    same fields as ``rewrite_json_cached_tokens``. Non-usage frames are passed
    through verbatim so token deltas/choices are untouched.
    """
    return [rewrite_sse_chunk_cached_tokens(chunk, cached_tokens) for chunk in chunks]


def rewrite_sse_chunk_cached_tokens(chunk: bytes, cached_tokens: int) -> bytes:
    """Rewrite cached_tokens in a single SSE chunk (usage frames only).

    Chunks without a usage frame are returned unchanged, so this is safe to call
    on every streamed chunk in flight.
    """
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return chunk
    if '"usage"' not in text:
        return chunk
    return _rewrite_sse_text_cached_tokens(text, cached_tokens).encode("utf-8")


def _rewrite_sse_text_cached_tokens(text: str, cached_tokens: int) -> str:
    """Rewrite cached_tokens in every usage-bearing SSE data line of ``text``."""
    out_lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("data:"):
            out_lines.append(line)
            continue
        payload = stripped[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            out_lines.append(line)
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("usage"), dict):
            out_lines.append(line)
            continue
        usage = obj["usage"]
        usage["cached_tokens"] = cached_tokens
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            details["cached_tokens"] = cached_tokens
        else:
            usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
        prefix = line[: len(line) - len(line.lstrip())]
        suffix = "\n" if line.endswith("\n") else ""
        out_lines.append(f"{prefix}data: {json.dumps(obj, ensure_ascii=False)}{suffix}")
    return "".join(out_lines)


def _origin_max_tokens(body: bytes) -> int | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        # vLLM: max_completion_tokens takes precedence over max_tokens.
        if data.get("max_completion_tokens") is not None:
            return int(data.get("max_completion_tokens"))
        return int(data.get("max_tokens"))
    except (TypeError, ValueError):
        return None


def _extend_decode_body(body: bytes, generated_content: str) -> bytes:
    """Append already-generated content to the prompt for a re-prefill handoff."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    messages = data.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        base = messages[0].get("content", "") or ""
        messages[0]["content"] = (base + generated_content) if base else generated_content
    else:
        prompt = data.get("prompt", "") or ""
        data["prompt"] = (prompt + generated_content) if prompt else generated_content
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _PrefillHttpError(Exception):
    def __init__(self, status_code: int, reason: str, response, content: bytes):
        self.status_code = status_code
        self.reason = reason
        self.response = response
        self.content = content


class PDForwardService:
    """Two-phase PD disaggregation forwarding: prefill then decode.

    The chooser hands us a prefiller (role='prefiller'). We prefill on it, read
    the exact prompt_tokens + cached_tokens + kv_transfer_params, then pick a
    least-active_tokens decoder in the same cluster and stream/forward the
    decode phase. On a KV-transfer miss (stop_reason='recomputed') we reuse the
    same prefiller (it still holds the prefix) and pick a fresh decoder.

    Resource accounting mirrors ProxyService: prefiller workload is reserved on
    entry and released on exit; decoder workload + active_tokens are reserved
    when the decoder is chosen (after prefill, using exact prompt_tokens) and
    released when the decode phase ends.
    """

    def __init__(self, proxy):
        self.proxy = proxy
        self.circuit_breaker = proxy.circuit_breaker
        self.normal_timeout = proxy.normal_timeout
        self.stream_timeout = proxy.stream_timeout
        self.stream_total_timeout = proxy.stream_total_timeout
        pd_config = APP_CONFIG.get("pd", {})
        self.recompute_max = int(pd_config.get("recompute_max", 3))
        self.prefill_timeout = (
            float(pd_config.get("prefill_connect_timeout_seconds", 5)),
            float(pd_config.get("prefill_read_timeout_seconds", 300)),
        )

    # ------------------------------------------------------------------
    # Entry point — called from ProxyService._route_with_retry
    # ------------------------------------------------------------------

    def forward(
        self, django_request, path, headers, body, record, user_agent, context,
        served_as_vip, model, is_stream, disconnect_scope, state, prefiller,
    ) -> Any:
        from router.services.proxy import _RouteAttemptResult

        state.last_server = prefiller
        state.attempted_server_ids.add(prefiller.id)
        state.attempts += 1
        prefiller_url = self.proxy._build_url(
            prefiller.base_url, path, django_request.META.get("QUERY_STRING", "")
        )
        target_pod_ip = f"P: {prefiller.base_url}"
        state.last_target_pod_ip = target_pod_ip
        RequestRepository.record_attempt(
            record, target_pod_ip, state.attempts,
            getattr(context, "prefix_cache", None), getattr(context, "last_match", None),
        )
        self.proxy._increment_workload(prefiller)
        record.task_status = "prefilling"
        record.save(update_fields=["task_status"])

        # Phase 1: prefill (eager, synchronous).
        prefill_start = time.monotonic()
        append_request_log(record.id, json.dumps({
            "event": "pd_prefill_start",
            "prefiller_id": prefiller.id,
            "prefiller_url": prefiller.base_url,
            "group_id": getattr(prefiller, "group_id", None),
        }, ensure_ascii=False))
        if record.model_choosing_latency is None:
            # First upstream send for this request: elapsed time from request
            # receipt to the prefill dispatch. ttft minus this value is the
            # LLM-side time to first token.
            RequestRepository.record_model_choosing_latency(
                record,
                int((timezone.now() - record.send_time).total_seconds() * 1000),
            )
        try:
            prefill_json, prompt_tokens, cached_tokens = self._do_prefill(
                prefiller, prefiller_url, headers, body
            )
        except requests.exceptions.ReadTimeout:
            logger.warning("PD prefill read timeout on %s", prefiller.base_url)
            record.task_status = "processing"
            record.save(update_fields=["task_status"])
            append_request_log(record.id, json.dumps({
                "event": "pd_prefill_error",
                "error_type": "read_timeout",
                "prefiller_url": prefiller.base_url,
                "elapsed_ms": int((time.monotonic() - prefill_start) * 1000),
            }, ensure_ascii=False))
            self._release_prefiller(prefiller)
            self.circuit_breaker.record_failure(prefiller)
            state.last_status = 504
            state.last_reason = "Gateway Timeout"
            return _RouteAttemptResult()
        except requests.RequestException as exc:
            is_connection = self.proxy._is_connection_failure(exc)
            logger.warning("PD prefill %s on %s: %s",
                           "connection error" if is_connection else "request error",
                           prefiller.base_url, exc)
            record.task_status = "processing"
            record.save(update_fields=["task_status"])
            append_request_log(record.id, json.dumps({
                "event": "pd_prefill_error",
                "error_type": "connection" if is_connection else "request_error",
                "prefiller_url": prefiller.base_url,
                "elapsed_ms": int((time.monotonic() - prefill_start) * 1000),
                "reason": str(exc)[:500],
            }, ensure_ascii=False))
            self._release_prefiller(prefiller)
            self.circuit_breaker.record_failure(prefiller)
            state.last_status = 502
            state.last_reason = "Bad Gateway"
            return _RouteAttemptResult(should_retry=is_connection)
        except _PrefillHttpError as exc:
            logger.warning("PD prefill HTTP %s on %s", exc.status_code, prefiller.base_url)
            append_request_log(record.id, json.dumps({
                "event": "pd_prefill_error",
                "error_type": "http",
                "prefiller_url": prefiller.base_url,
                "status_code": exc.status_code,
                "elapsed_ms": int((time.monotonic() - prefill_start) * 1000),
                "response_body": proxy_logging.decode_body_for_log(exc.content)[:5000],
            }, ensure_ascii=False))
            proxy_logging.log_request_context_for(context)
            self._release_prefiller(prefiller)
            fail_reason = proxy_response.extract_fail_reason(exc.content, exc.reason)
            state.last_status = exc.status_code
            state.last_reason = exc.reason
            state.last_upstream = exc.response
            state.last_content = exc.content
            state.last_fail_reason = fail_reason

            # On a real context overflow, retry on a same-model server with a
            # strictly larger context window (the chooser excludes already-tried
            # servers). The window only ever gates the prefill tier: candidates
            # come from list_pd_holders (mixed/prefiller roles only), so
            # decoders are never selected here (issue #248).
            failed_context_window = getattr(prefiller, "context_window", None)
            if (
                self.proxy.auto_router.check_context_overflow(exc.status_code, failed_context_window, fail_reason)
                and failed_context_window
            ):
                higher_candidates = self.proxy.larger_window_candidates(
                    path, model, served_as_vip, failed_context_window,
                )
                if higher_candidates:
                    record.task_status = "processing"
                    record.save(update_fields=["task_status"])
                    append_request_log(record.id, json.dumps({
                        "event": "pd_prefill_context_overflow_retry",
                        "prefiller_url": prefiller.base_url,
                        "failed_context_window": failed_context_window,
                        "candidate_ids": [s.id for s in higher_candidates],
                    }, ensure_ascii=False))
                    return _RouteAttemptResult(
                        should_retry=True,
                        candidates=higher_candidates,
                        model=model,
                        body=body,
                    )

            self.circuit_breaker.record_failure(prefiller)
            proxy_response.finish_upstream_error(
                record, exc.status_code, fail_reason, target_pod_ip, model, state.attempts, context
            )
            self.proxy._after_finish(served_as_vip, model)
            return _RouteAttemptResult(
                response=proxy_response.response_from_upstream(exc.response, exc.content, exc.status_code)
            )

        kv_params = _extract_kv_params(prefill_json)

        # The prefill probe generates exactly one token (max_tokens=1), so its
        # completion is the request's first-token moment.
        prefill_ttft = int((timezone.now() - record.send_time).total_seconds() * 1000)

        append_request_log(record.id, json.dumps({
            "event": "pd_prefill_success",
            "prefiller_url": prefiller.base_url,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "prefix_cache_ratio": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0.0,
            "has_kv_params": bool(kv_params),
            "elapsed_ms": int((time.monotonic() - prefill_start) * 1000),
            "ttft_ms": prefill_ttft,
        }, ensure_ascii=False))

        # Prefill succeeded: release prefiller and transition to neutral "processing".
        # cached_tokens is authoritative from the prefiller and fixed at this point, so
        # persist final_prefix_cache now alongside input_token_cnt rather than at decode end.
        record.task_status = "processing"
        record.input_token_cnt = prompt_tokens
        record.final_prefix_cache = cached_tokens
        record.save(update_fields=["task_status", "input_token_cnt", "final_prefix_cache"])
        self._release_prefiller(prefiller)

        if is_stream:
            from router.services.proxy import _RouteAttemptResult

            return _RouteAttemptResult(
                response=self._stream_decode(
                    django_request, path, headers, body, record, context, served_as_vip, model,
                    state, prefiller, kv_params, prompt_tokens, cached_tokens, target_pod_ip,
                )
            )
        return self._normal_decode(
            path, headers, body, record, context, served_as_vip, model,
            state, prefiller, kv_params, prompt_tokens, cached_tokens, target_pod_ip, prefill_ttft,
        )

    # ------------------------------------------------------------------
    # Phase 1: prefill
    # ------------------------------------------------------------------

    def _do_prefill(self, prefiller, prefiller_url, headers, body) -> tuple[dict, int, int]:
        prefill_body = build_prefill_body(body)
        req_headers = build_upstream_headers(headers, prefiller)
        response = requests.post(
            prefiller_url,
            headers=req_headers,
            data=prefill_body,
            timeout=self.proxy._deadline_timeout(self.prefill_timeout),
        )
        if response.status_code >= 400:
            raise _PrefillHttpError(response.status_code, response.reason or "", response, response.content)
        try:
            prefill_json = response.json()
        except (ValueError, json.JSONDecodeError):
            prefill_json = {}
        prompt_tokens, _completion, cached_tokens = _parse_usage(prefill_json.get("usage"))
        return prefill_json, prompt_tokens, cached_tokens

    # ------------------------------------------------------------------
    # Phase 2 (non-stream): eager recompute loop
    # ------------------------------------------------------------------

    def _normal_decode(
        self, path, headers, body, record, context, served_as_vip, model,
        state, prefiller, kv_params, prompt_tokens, cached_tokens, target_pod_ip, prefill_ttft,
    ):
        from router.services.proxy import _RouteAttemptResult

        decode_body = build_decode_body(body, kv_params)
        generated = ""
        completion_tokens = 0
        recompute_count = 0
        attempted_decoder_ids: set[int] = set()
        origin_max_tokens = _origin_max_tokens(body)

        while True:
            decoder = ServerRepository.pick_least_tokens_decoder(prefiller.group_id, attempted_decoder_ids)
            if decoder is None:
                logger.error("No routable decoder in cluster %s for PD request", prefiller.group_id)
                append_request_log(record.id, json.dumps({
                    "event": "pd_no_decoder",
                    "group_id": prefiller.group_id,
                    "attempted_decoder_ids": sorted(attempted_decoder_ids),
                    "recompute_count": recompute_count,
                }, ensure_ascii=False))
                state.last_status = 502
                state.last_reason = "no routable decoder in cluster"
                return _RouteAttemptResult()
            attempted_decoder_ids.add(decoder.id)
            append_request_log(record.id, json.dumps({
                "event": "pd_decoder_chosen",
                "decoder_id": decoder.id,
                "decoder_url": decoder.base_url,
                "active_tokens": float(getattr(decoder, "active_tokens", 0.0) or 0.0),
                "recompute_count": recompute_count,
            }, ensure_ascii=False))
            state.last_server = decoder
            ServerRepository.increment_workload(decoder)
            ServerRepository.reserve_active_tokens(decoder, float(prompt_tokens or 0))
            record.task_status = "decoding"
            record.save(update_fields=["task_status"])
            current_target = f"{target_pod_ip} -- D: {decoder.base_url}"
            if recompute_count > 0:
                current_target += _KV_TRANSFER_FAIL_TAG
            state.last_target_pod_ip = current_target
            RequestRepository.record_attempt(
                record, current_target, state.attempts,
                getattr(context, "prefix_cache", None), getattr(context, "last_match", None),
            )
            decoder_url = self.proxy._build_url(decoder.base_url, path, "")

            try:
                response, content, status_code = self._post_decode(decoder, decoder_url, headers, decode_body)
            except requests.exceptions.ReadTimeout:
                append_request_log(record.id, json.dumps({
                    "event": "pd_decode_error",
                    "error_type": "read_timeout",
                    "decoder_url": decoder.base_url,
                    "recompute_count": recompute_count,
                }, ensure_ascii=False))
                self._release_decoder(decoder, prompt_tokens)
                self.circuit_breaker.record_failure(decoder)
                state.last_status = 504
                state.last_reason = "Gateway Timeout"
                return _RouteAttemptResult()
            except requests.RequestException as exc:
                is_connection = self.proxy._is_connection_failure(exc)
                logger.warning("PD decode %s on %s: %s",
                               "connection error" if is_connection else "request error",
                               decoder.base_url, exc)
                append_request_log(record.id, json.dumps({
                    "event": "pd_decode_error",
                    "error_type": "connection" if is_connection else "request_error",
                    "decoder_url": decoder.base_url,
                    "reason": str(exc)[:500],
                    "recompute_count": recompute_count,
                }, ensure_ascii=False))
                self._release_decoder(decoder, prompt_tokens)
                self.circuit_breaker.record_failure(decoder)
                state.last_status = 502
                state.last_reason = "Bad Gateway"
                return _RouteAttemptResult(should_retry=is_connection)

            if status_code >= 400:
                # 5xx: record failure on the node, NO retry — return error.
                append_request_log(record.id, json.dumps({
                    "event": "pd_decode_response",
                    "decoder_url": decoder.base_url,
                    "status_code": status_code,
                    "recompute_count": recompute_count,
                    "is_error": True,
                    "response_body": proxy_logging.decode_body_for_log(content)[:5000],
                }, ensure_ascii=False))
                proxy_logging.log_request_context_for(context)
                self._release_decoder(decoder, prompt_tokens)
                self.circuit_breaker.record_failure(decoder)
                fail_reason = proxy_response.extract_fail_reason(content, response.reason or "")
                state.last_status = status_code
                state.last_reason = response.reason or ""
                state.last_upstream = response
                state.last_content = content
                state.last_fail_reason = fail_reason
                proxy_response.finish_upstream_error(
                    record, status_code, fail_reason, current_target, model, state.attempts, context
                )
                self.proxy._after_finish(served_as_vip, model)
                return _RouteAttemptResult(
                    response=proxy_response.response_from_upstream(response, content, status_code)
                )

            data = self._json_or_none(content)
            choice = self._first_choice(data)
            generated = self._accumulate_content(data, generated)
            _prompt, usage_completion, _cached = _parse_usage(data.get("usage") if data else None)
            completion_tokens += usage_completion

            if data and choice and self._is_recomputed(choice):
                recompute_count += 1
                append_request_log(record.id, json.dumps({
                    "event": "pd_recompute",
                    "decoder_url": decoder.base_url,
                    "recompute_count": recompute_count,
                    "recompute_max": self.recompute_max,
                }, ensure_ascii=False))
                self._release_decoder(decoder, prompt_tokens)
                record.task_status = "processing"
                record.save(update_fields=["task_status"])
                if recompute_count > self.recompute_max:
                    logger.error("PD recompute limit (%s) exceeded for request %s", self.recompute_max, record.id)
                    append_request_log(record.id, json.dumps({
                        "event": "pd_recompute_limit_exceeded",
                        "recompute_count": recompute_count,
                        "recompute_max": self.recompute_max,
                    }, ensure_ascii=False))
                    state.last_status = 502
                    state.last_reason = "PD recompute limit exceeded"
                    return _RouteAttemptResult()
                decode_body = self._extend_for_recompute(
                    body, generated, origin_max_tokens, completion_tokens, recompute_count
                )
                continue

            # Terminal success: completion_tokens from the decode usage.
            self.circuit_breaker.record_success(decoder)
            self.proxy._notify_chooser_response(prefiller, context, status_code)
            append_request_log(record.id, json.dumps({
                "event": "pd_decode_success",
                "decoder_url": decoder.base_url,
                "status_code": status_code,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "recompute_count": recompute_count,
                "ttft_ms": prefill_ttft,
            }, ensure_ascii=False))
            _prompt, output_tokens, _cached = _parse_usage(data.get("usage") if data else None)
            RequestRepository.finish(
                record, status_code, response.reason or "", prompt_tokens, output_tokens,
                current_target, model.id if model else None,
                attempt_count=state.attempts, final_prefix_cache=cached_tokens,
                router_result=proxy_response.router_result(context),
                ttft=prefill_ttft,
                success_note=proxy_response.request_non_utf8_fail_reason(getattr(context, "body", b"")),
            )
            self._release_decoder(decoder, prompt_tokens)
            self.proxy._after_finish(served_as_vip, model)
            client_content = rewrite_json_cached_tokens(content, cached_tokens)
            final_response = proxy_response.response_from_upstream(response, client_content, status_code)
            # Prefix-cache write after the client has received the response.
            final_response = self.proxy._attach_chooser_response_hook(
                final_response, prefiller, context, status_code
            )
            return _RouteAttemptResult(response=final_response)

    def _post_decode(self, decoder, decoder_url, headers, decode_body):
        req_headers = build_upstream_headers(headers, decoder)
        response = requests.post(
            decoder_url, headers=req_headers, data=decode_body,
            timeout=self.proxy._deadline_timeout(self.normal_timeout),
        )
        return response, response.content, response.status_code

    # ------------------------------------------------------------------
    # Phase 2 (stream): generator owns the recompute loop
    # ------------------------------------------------------------------

    def _stream_decode(
        self, django_request, path, headers, body, record, context, served_as_vip, model,
        state, prefiller, kv_params, prompt_tokens, cached_tokens, target_pod_ip,
    ):
        decode_body = build_decode_body(body, kv_params)
        origin_max_tokens = _origin_max_tokens(body)

        def generate():
            nonlocal decode_body
            generated = ""
            completion_tokens = 0
            recompute_count = 0
            attempted_decoder_ids: set[int] = set()
            request_start = time.monotonic()
            current_decoder = None
            current_target = target_pod_ip
            ttft = None
            decoder_released = True

            def release_current_decoder():
                nonlocal current_decoder, decoder_released
                if current_decoder is not None and not decoder_released:
                    ServerRepository.decrement_workload(current_decoder)
                    ServerRepository.release_active_tokens(current_decoder, float(prompt_tokens or 0))
                    decoder_released = True

            try:
                while True:
                    decoder = ServerRepository.pick_least_tokens_decoder(
                        prefiller.group_id, attempted_decoder_ids
                    )
                    if decoder is None:
                        logger.error("No routable decoder in cluster %s for PD request", prefiller.group_id)
                        message = "No routable decoder in PD cluster"
                        append_request_log(record.id, json.dumps({
                            "event": "pd_no_decoder",
                            "group_id": prefiller.group_id,
                            "attempted_decoder_ids": sorted(attempted_decoder_ids),
                            "recompute_count": recompute_count,
                        }, ensure_ascii=False))
                        proxy_logging.log_request_context_for(context)
                        yield f"data: {json.dumps(error_payload(message, 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                        proxy_response.finish_stream_request_exception(
                            record, message, current_target, state.attempts, model, context
                        )
                        self.proxy._after_finish(served_as_vip, model)
                        return
                    attempted_decoder_ids.add(decoder.id)
                    append_request_log(record.id, json.dumps({
                        "event": "pd_decoder_chosen",
                        "decoder_id": decoder.id,
                        "decoder_url": decoder.base_url,
                        "active_tokens": float(getattr(decoder, "active_tokens", 0.0) or 0.0),
                        "recompute_count": recompute_count,
                    }, ensure_ascii=False))
                    current_decoder = decoder
                    decoder_released = False
                    state.last_server = decoder
                    ServerRepository.increment_workload(decoder)
                    ServerRepository.reserve_active_tokens(decoder, float(prompt_tokens or 0))
                    record.task_status = "decoding"
                    record.save(update_fields=["task_status"])
                    current_target = f"{target_pod_ip} -- D: {decoder.base_url}"
                    if recompute_count > 0:
                        current_target += _KV_TRANSFER_FAIL_TAG
                    state.last_target_pod_ip = current_target
                    RequestRepository.record_attempt(
                        record, current_target, state.attempts,
                        getattr(context, "prefix_cache", None), getattr(context, "last_match", None),
                    )
                    decoder_url = self.proxy._build_url(decoder.base_url, path, "")

                    req_headers = build_upstream_headers(headers, decoder)
                    try:
                        upstream = requests.request(
                            "POST", decoder_url, headers=req_headers, data=decode_body,
                            stream=True, timeout=self.stream_timeout,
                        )
                    except requests.RequestException as exc:
                        logger.warning("PD decode connection error on %s: %s", decoder.base_url, exc)
                        append_request_log(record.id, json.dumps({
                            "event": "pd_decode_error",
                            "error_type": "connection",
                            "decoder_url": decoder.base_url,
                            "reason": str(exc)[:500],
                            "recompute_count": recompute_count,
                        }, ensure_ascii=False))
                        proxy_logging.log_request_context_for(context)
                        self.circuit_breaker.record_failure(decoder)
                        message = "502 Bad Gateway"
                        yield f"data: {json.dumps(error_payload(message, 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                        proxy_response.finish_stream_request_exception(
                            record, message, current_target, state.attempts, model, context
                        )
                        release_current_decoder()
                        self.proxy._after_finish(served_as_vip, model)
                        return

                    if upstream.status_code >= 400:
                        try:
                            content = upstream.content
                        except Exception:
                            content = b""
                        append_request_log(record.id, json.dumps({
                            "event": "pd_decode_response",
                            "decoder_url": decoder.base_url,
                            "status_code": upstream.status_code,
                            "recompute_count": recompute_count,
                            "is_error": True,
                            "response_body": proxy_logging.decode_body_for_log(content)[:5000],
                        }, ensure_ascii=False))
                        proxy_logging.log_request_context_for(context)
                        fail_reason = proxy_response.extract_fail_reason(content, upstream.reason or "")
                        self.circuit_breaker.record_failure(decoder)
                        if content:
                            yield content
                        proxy_response.finish_upstream_error(
                            record, upstream.status_code, fail_reason, current_target, model, state.attempts, context
                        )
                        upstream.close()
                        release_current_decoder()
                        self.proxy._after_finish(served_as_vip, model)
                        return

                    recomputed = False
                    chunks: list[bytes] = []
                    deadline = request_start + self.stream_total_timeout
                    try:
                        for chunk in upstream.iter_content(chunk_size=8192):
                            if time.monotonic() > deadline:
                                proxy_logging.log_request_context_for(context)
                                yield timeout_sse_event()
                                proxy_response.finish_stream_total_timeout(record, current_target, state.attempts)
                                release_current_decoder()
                                self.proxy._after_finish(served_as_vip, model)
                                return
                            tracker = getattr(django_request, "client_disconnect_tracker", None)
                            if tracker and tracker.client_disconnected():
                                proxy_response.finish_stream_client_disconnected(record, current_target, state.attempts)
                                release_current_decoder()
                                self.proxy._after_finish(served_as_vip, model)
                                return
                            if chunk:
                                if ttft is None:
                                    ttft = int((timezone.now() - record.send_time).total_seconds() * 1000)
                                chunks.append(chunk)
                                generated, completion_tokens, recomputed = self._scan_chunk(
                                    chunk, generated, completion_tokens
                                )
                                if not recomputed:
                                    yield rewrite_sse_chunk_cached_tokens(chunk, cached_tokens)
                            if recomputed:
                                break
                    except requests.exceptions.ReadTimeout:
                        upstream.close()
                        release_current_decoder()
                        self.circuit_breaker.record_failure(decoder)
                        proxy_logging.log_request_context_for(context)
                        yield timeout_sse_event()
                        proxy_response.finish_stream_read_timeout(
                            record, current_target, state.attempts, model, context
                        )
                        self.proxy._after_finish(served_as_vip, model)
                        return
                    except requests.RequestException:
                        upstream.close()
                        release_current_decoder()
                        self.circuit_breaker.record_failure(decoder)
                        proxy_logging.log_request_context_for(context)
                        message = "502 Bad Gateway"
                        yield f"data: {json.dumps(error_payload(message, 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                        proxy_response.finish_stream_request_exception(
                            record, message, current_target, state.attempts, model, context
                        )
                        self.proxy._after_finish(served_as_vip, model)
                        return
                    finally:
                        try:
                            upstream.close()
                        except Exception:
                            pass

                    if recomputed:
                        recompute_count += 1
                        append_request_log(record.id, json.dumps({
                            "event": "pd_recompute",
                            "decoder_url": decoder.base_url,
                            "recompute_count": recompute_count,
                            "recompute_max": self.recompute_max,
                        }, ensure_ascii=False))
                        release_current_decoder()
                        record.task_status = "processing"
                        record.save(update_fields=["task_status"])
                        if recompute_count > self.recompute_max:
                            logger.error("PD recompute limit (%s) exceeded for request %s", self.recompute_max, record.id)
                            append_request_log(record.id, json.dumps({
                                "event": "pd_recompute_limit_exceeded",
                                "recompute_count": recompute_count,
                                "recompute_max": self.recompute_max,
                            }, ensure_ascii=False))
                            proxy_logging.log_request_context_for(context)
                            message = "PD recompute limit exceeded"
                            yield f"data: {json.dumps(error_payload(message, 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                            proxy_response.finish_stream_request_exception(
                                record, message, current_target, state.attempts, model, context
                            )
                            self.proxy._after_finish(served_as_vip, model)
                            return
                        decode_body = self._extend_for_recompute(
                            body, generated, origin_max_tokens, completion_tokens, recompute_count
                        )
                        continue

                    # Terminal success: final_prefix_cache from the prefiller.
                    self.circuit_breaker.record_success(decoder)
                    self.proxy._notify_chooser_response(prefiller, context, 200)
                    append_request_log(record.id, json.dumps({
                        "event": "pd_decode_success",
                        "decoder_url": decoder.base_url,
                        "status_code": 200,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cached_tokens": cached_tokens,
                        "recompute_count": recompute_count,
                        "ttft_ms": ttft,
                    }, ensure_ascii=False))
                    proxy_response.finish_stream_success(
                        record, 200, "OK", chunks, current_target, context.model_name,
                        state.attempts, context, ttft,
                    )
                    record.refresh_from_db()
                    if record.final_prefix_cache != cached_tokens:
                        record.final_prefix_cache = cached_tokens
                        record.save(update_fields=["final_prefix_cache"])
                    release_current_decoder()
                    self.proxy._after_finish(served_as_vip, model)
                    return

            except Exception as exc:
                # Scenario-2 guard: any non-requests exception during PD
                # streaming (e.g. a client disconnect surfacing as OSError on a
                # yield, or a DB error inside finish_*/record.save). Finish the
                # record, log the full traceback to its per-request log, release
                # the decoder, and emit a terminal error. Best-effort; never raise.
                fail_reason = f"unhandled pd stream {type(exc).__name__}: {exc}"[:200]
                logger.error("PD stream unhandled %s request_id=%s: %s", type(exc).__name__, record.id, str(exc)[:200])
                try:
                    proxy_logging.safe_append_request_log(
                        record.id,
                        f"PD stream unhandled exception: {fail_reason}\n{traceback.format_exc()}",
                    )
                except Exception:
                    pass
                try:
                    proxy_response.finish_stream_request_exception(
                        record, "502 Bad Gateway", current_target, state.attempts, model, context
                    )
                except Exception:
                    logger.exception("failed to finish PD streaming record %s after unhandled exception", record.id)
                try:
                    yield f"data: {json.dumps(error_payload('502 Bad Gateway', 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                except Exception:
                    pass
            finally:
                release_current_decoder()
                flush_request_log(record.id)

        response = StreamingHttpResponse(generate(), status=200, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        # Prefix-cache write after the last streamed chunk has been delivered.
        return self.proxy._attach_chooser_response_hook(response, prefiller, context, 200)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _json_or_none(content: bytes) -> dict | None:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _first_choice(data: dict | None) -> dict | None:
        if not data:
            return None
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return choices[0]
        return None

    @staticmethod
    def _is_recomputed(choice: dict) -> bool:
        return choice.get("stop_reason") == "recomputed" or choice.get("finish_reason") == "recomputed"

    @staticmethod
    def _accumulate_content(data: dict | None, generated: str) -> str:
        choice = PDForwardService._first_choice(data)
        if not choice:
            return generated
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            content = choice.get("text")
        if isinstance(content, str):
            return generated + content
        return generated

    @staticmethod
    def _scan_chunk(chunk: bytes, generated: str, completion_tokens: int) -> tuple[str, int, bool]:
        """Parse one SSE chunk: accumulate content, detect recomputed."""
        try:
            text = chunk.decode("utf-8").strip()
        except UnicodeDecodeError:
            return generated, completion_tokens, False
        recomputed = False
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if not content:
                    message = choice.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                if not content:
                    content = choice.get("text")
                if isinstance(content, str):
                    generated += content
                    completion_tokens += 1
                if PDForwardService._is_recomputed(choice):
                    recomputed = True
        return generated, completion_tokens, recomputed

    @staticmethod
    def _extend_for_recompute(
        body: bytes, generated: str, origin_max_tokens: int | None, completion_tokens: int, recompute_count: int
    ) -> bytes:
        body = _extend_decode_body(body, generated)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if not isinstance(data, dict):
            return body
        if origin_max_tokens is not None:
            # vLLM: adjust the field the decoder will actually honor.
            token_key = (
                "max_completion_tokens"
                if data.get("max_completion_tokens") is not None
                else "max_tokens"
            )
            data[token_key] = max(1, origin_max_tokens - completion_tokens + recompute_count)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _release_prefiller(self, prefiller) -> None:
        self.proxy._decrement_workload(prefiller)

    def _release_decoder(self, decoder, prompt_tokens: int) -> None:
        self.proxy._decrement_workload(decoder)
        ServerRepository.release_active_tokens(decoder, float(prompt_tokens or 0))
