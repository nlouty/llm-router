from __future__ import annotations

import importlib
import json
import logging
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.utils import timezone
import requests

from router.services.request_context import (
    clear_llm_choosing_deadline,
    clear_request_id,
    get_llm_choosing_deadline,
    get_request_id,
    set_llm_choosing_deadline,
    set_request_id,
)
from router.services.request_logger import append_request_log

from router.config import APP_CONFIG
from router.repositories.requests import (
    LLM_CHOOSING_IP_ID,
    LLM_CHOOSING_USER_AGENT,
    RequestRepository,
)
from router.repositories.servers import ServerRepository
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.circuit_breaker import CircuitBreakerService
from router.services.disconnect import DisconnectWatcher
from router.services.opencode import OpencodeVersionService
from router.services.parser import ParsedRequest, RequestParser
from router.services import proxy_logging, proxy_response
from router.services.request_logger import append_verbose_request_log
from router.utils.token_count import count_tokens_with_latency
from router.services.vip_channel import VIPChannelService
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser
from router.utils.errors import error_payload, error_response, timeout_sse_event
from router.utils.headers import filter_request_headers
from router.utils.session import extract_session_id


logger = logging.getLogger(__name__)


@dataclass
class _DisconnectScope:
    disconnect_event: threading.Event
    stop_event: threading.Event
    upstream_client: CancellableUpstreamRequest | None = None
    watcher: DisconnectWatcher | None = None


@dataclass
class _RetryState:
    attempted_server_ids: set[int] = field(default_factory=set)
    attempts: int = 0
    last_server: Any = None
    last_status: int = 502
    last_reason: str = "Bad Gateway"
    # The display string of the most recent attempt's target ("P: <url>",
    # "P: <url> -- D: <url>", or a bare mixed-server url). Preserved across
    # terminal failure so finish_* records the same target that was shown while
    # the request was in flight, instead of regressing to a bare base_url.
    last_target_pod_ip: str | None = None
    # The most recent real upstream error response, captured so that when
    # retries are exhausted (all candidates tried or max attempts hit) the
    # caller gets the actual upstream body/status rather than a synthetic
    # 502. Stays None for timeout/connection failures, which have no body.
    last_upstream: Any = None
    last_content: bytes = b""
    last_fail_reason: str | None = None


@dataclass
class _RouteAttemptResult:
    response: Any = None
    should_retry: bool = False
    candidates: Any = None
    model: Any = None
    body: bytes | None = None


class ProxyService:
    def __init__(self, chooser=None):
        proxy_config = APP_CONFIG.get("proxy", {})
        lb_config = APP_CONFIG.get("load_balancer", {})
        self.max_attempts_per_request = int(lb_config.get("max_attempts_per_request", 3))
        self.chooser = chooser or self._load_chooser(str(lb_config.get("chooser_class", "router.route_algorithm.least_connection.LeastConnectionServerChooser")))
        self.auto_router = AutoRouteAlgorithm(self.chooser, proxy=self)
        self.stream_timeout = (
            float(proxy_config.get("stream_connect_timeout_seconds", 30)),
            float(proxy_config.get("stream_read_timeout_seconds", 900)),
        )
        self.normal_timeout = (
            float(proxy_config.get("normal_connect_timeout_seconds", 5)),
            float(proxy_config.get("normal_read_timeout_seconds", 900)),
        )
        self.llm_choosing_timeout = float(proxy_config.get("llm_choosing_timeout_seconds", 10))
        self.stream_total_timeout = float(proxy_config.get("stream_total_timeout_seconds", 900))
        self.client_disconnect_check_interval = float(proxy_config.get("client_disconnect_check_interval_seconds", 0.5))
        self.opencode_failure_delay = float(proxy_config.get("opencode_failure_delay_seconds", 30))
        self.circuit_breaker = CircuitBreakerService()
        self.vip_service = VIPChannelService()
        self.vip_port = int(APP_CONFIG.get("server", {}).get("vip_port", 8008))
        self.tokenizer_enabled = bool(APP_CONFIG.get("tokenizer", {}).get("enabled", False))
        self._active_chooser = None

    def forward(self, django_request, path: str, parsed, ip_id: int | None, model, user_agent: str | None, is_vip_channel: bool = False, user_ip_id: int = 0, is_identity_vip: bool = False, skip_auto_selection: bool = False):
        headers = filter_request_headers(dict(django_request.headers), django_request.method)
        normalized = path.rstrip("/")
        session = extract_session_id(dict(django_request.headers)) if normalized == "chat/completions" else None
        record = self._create_processing_record(ip_id, model, parsed, user_agent, user_ip_id=user_ip_id, session=session)
        append_verbose_request_log(record.id, django_request.body)
        # Open the per-request log for every proxied request, not only when a
        # server is chosen in _route_with_retry: failures before that point
        # (e.g. no candidates -> synthetic 502) would otherwise leave the DB
        # row without any <request_id>.log to diagnose.
        set_request_id(record.id)
        try:
            append_request_log(record.id, json.dumps({
                "event": "request_received",
                "request_id": record.id,
                "path": f"/v1/{normalized}",
                "model": (model.model_name if model else None) or parsed.model_name,
                "stream": bool(parsed.stream),
                "is_vip_channel": is_vip_channel,
                "user_ip_id": user_ip_id,
            }, ensure_ascii=False))
            if normalized == "chat/completions":
                return self._forward_chat(
                    django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel, is_identity_vip, skip_auto_selection
                )
            if normalized == "embeddings":
                return self._forward_embeddings(
                    django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel, is_identity_vip
                )
            return self._forward_default(
                django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel, is_identity_vip
            )
        except Exception as exc:
            # Any unhandled exception inside routing/forwarding must finish the
            # processing record this method created (exactly one row per
            # request) and record the full traceback in that record's
            # per-request log, rather than propagating to views.py, which would
            # create a second record and orphan this one.
            return self._finish_unhandled(record, exc)
        finally:
            clear_request_id()

    def forward_internal(self, body: bytes, model, path: str = "chat/completions") -> HttpResponse:
        """Route an internal (llm-choosing) request through the normal pipeline.

        Called from AutoRouteAlgorithm._query_routing_complexity instead of a
        direct upstream POST: the choosing call gets the exact same handling as
        a client request (PD-aware candidates via list_pd_holders, cluster-aware
        chooser, two-phase PD dispatch, retries, circuit breaker, one terminal
        record) while keeping the llm-choosing record conventions (ip_id = 0,
        user_agent = "llm-choosing"). Auto selection is skipped so a routing
        model with auto = TRUE can never re-enter the choosing algorithm.

        The whole choosing request is capped at
        proxy.llm_choosing_timeout_seconds (default 10): forward_internal sets
        the deadline in the request context, and every upstream attempt's
        socket timeouts are clamped to the remaining budget, so a hung routing
        server is disconnected at the deadline and the choosing call fails
        fast with a 504 instead of blocking for up to
        normal_read_timeout_seconds per attempt.
        """
        parsed = RequestParser(
            int(APP_CONFIG.get("proxy", {}).get("default_max_tokens", 28528))
        ).parse(body, path, is_vip=False)
        internal_request = HttpRequest()
        internal_request.method = "POST"
        internal_request.path = f"/v1/{path.rstrip('/')}"
        # Django 4.2's HttpRequest.body is read-only; set the backing field.
        internal_request._body = body
        internal_request.META = {
            "QUERY_STRING": "",
            "HTTP_CONTENT_TYPE": "application/json",
            "HTTP_USER_AGENT": LLM_CHOOSING_USER_AGENT,
        }
        # forward() sets/clears the request-id context; restore the outer
        # request's id afterwards so the caller's own PD-aware candidate
        # selection keeps logging under the outer record.
        previous_request_id = get_request_id()
        set_llm_choosing_deadline(time.monotonic() + self.llm_choosing_timeout)
        try:
            return self.forward(
                internal_request,
                path,
                parsed,
                LLM_CHOOSING_IP_ID,
                model,
                LLM_CHOOSING_USER_AGENT,
                skip_auto_selection=True,
            )
        finally:
            clear_llm_choosing_deadline()
            if previous_request_id is not None:
                set_request_id(previous_request_id)
            else:
                clear_request_id()

    def _finish_unhandled(self, record, exc: BaseException):
        """Finish the processing record after an otherwise-unhandled exception.

        Guarantees one terminal row per request: finishes the record forward()
        created (instead of leaving it orphaned), writes the exception and full
        traceback to that record's per-request log, and returns a generic 502.
        Must never raise, so the response still goes out.
        """
        fail_reason = f"unhandled {type(exc).__name__}: {exc}"[:200]
        logger.error("proxy unhandled %s request_id=%s: %s", type(exc).__name__, record.id, str(exc)[:200])
        try:
            proxy_logging.safe_append_request_log(
                record.id,
                f"proxy unhandled exception: {fail_reason}\n{traceback.format_exc()}",
            )
        except Exception:
            pass
        try:
            RequestRepository.finish(record, 502, fail_reason)
        except Exception:
            logger.exception("failed to finish processing record %s after unhandled exception", record.id)
        return error_response(502, "502 Bad Gateway", "server_error")

    def _forward_chat(self, django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel: bool, is_identity_vip: bool = False, skip_auto_selection: bool = False):
        auto_model_selection = False
        if not skip_auto_selection:
            auto_model_selection = self.auto_router.should_auto_select(
                parsed,
                model,
                is_vip_channel,
            )
        context = self._selection_context(
            record,
            ip_id,
            model,
            parsed,
            path,
            django_request.method,
            auto_model_selection,
            headers,
        )
        decision = self.auto_router.resolve(
            parsed,
            record,
            context,
            model,
            is_vip_channel,
        )
        model = decision.model

        self._count_tokens_after_selection(parsed, record, model)

        candidates, served_as_vip = self._select_candidates(path, model, is_vip_channel, is_identity_vip)
        if served_as_vip:
            record.vip = True
            record.save(update_fields=["vip"])

        if not candidates:
            return self._handle_no_candidates(record, user_agent, context, model)

        return self._route_with_retry(
            django_request, path, headers, parsed.body, record, user_agent,
            candidates, context, served_as_vip, model, parsed.stream
        )

    def _forward_embeddings(self, django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel: bool, is_identity_vip: bool = False):
        # Embeddings skip the chat-completions auto-routing algorithm entirely
        # and select a server by least-connection (random among least-loaded).
        # The request body is forwarded unchanged (no max_tokens / model rewrite).
        context = self._selection_context(
            record, ip_id, model, parsed, path, django_request.method, False, headers
        )
        context.router_result = path.rstrip("/")
        candidates, served_as_vip = self._select_candidates(path, model, is_vip_channel, is_identity_vip)
        if served_as_vip:
            record.vip = True
            record.save(update_fields=["vip"])
        if not candidates:
            return self._handle_no_candidates(record, user_agent, context, model)
        self._active_chooser = self.auto_router.workload_chooser
        try:
            return self._route_with_retry(
                django_request, path, headers, parsed.body, record, user_agent,
                candidates, context, served_as_vip, model, parsed.stream
            )
        finally:
            self._active_chooser = None

    def _forward_default(self, django_request, path, headers, record, ip_id, model, parsed, user_agent, is_vip_channel: bool, is_identity_vip: bool = False):
        # Non-chat, non-embeddings endpoints (e.g. /v1/models): no auto-routing;
        # record the endpoint path as the router_result.
        context = self._selection_context(
            record, ip_id, model, parsed, path, django_request.method, False, headers
        )
        context.router_result = path.rstrip("/")
        candidates, served_as_vip = self._select_candidates(path, model, is_vip_channel, is_identity_vip)
        if served_as_vip:
            record.vip = True
            record.save(update_fields=["vip"])
        if not candidates:
            return self._handle_no_candidates(record, user_agent, context, model)
        return self._route_with_retry(
            django_request, path, headers, parsed.body, record, user_agent,
            candidates, context, served_as_vip, model, parsed.stream
        )

    @staticmethod
    def _create_processing_record(ip_id: int | None, model, parsed, user_agent: str | None, user_ip_id: int = 0, session: str | None = None):
        return RequestRepository.create_processing(
            ip_id,
            model.id if model else 0,
            parsed.stream,
            user_agent,
            user_ip_id=user_ip_id,
            estimate_tokens=parsed.estimated_full_body_tokens,
            session=session,
        )

    def _count_tokens_after_selection(self, parsed, record, model):
        """Count tokens with the resolved model's tokenizer when enabled.

        Runs after model selection so a real tokenizer path is always known,
        avoiding the chicken-and-egg of needing a model to tokenize before one
        is selected. Skipped entirely when the toggle is off (default) or the
        resolved model has no ``model_path``.
        """
        if not self.tokenizer_enabled:
            return
        if model is None or not getattr(model, "model_path", None):
            return
        if not isinstance(parsed, ParsedRequest):
            return
        count, latency_ms, error = count_tokens_with_latency(
            model.model_path,
            parsed.body.decode("utf-8", errors="replace"),
        )
        parsed.estimated_full_body_tokens = count
        parsed.tokenizer_latency_ms = latency_ms
        parsed.tokenizer_error = error
        record.estimate_tokens = count
        record.save(update_fields=["estimate_tokens"])
        tokenizer_event = {
            "event": "tokenizer_count",
            "request_id": record.id,
            "tokens": count,
            "latency_ms": latency_ms,
        }
        if error:
            tokenizer_event["error"] = error
        append_request_log(record.id, json.dumps(tokenizer_event, ensure_ascii=False))

    @staticmethod
    def _selection_context(
        record,
        ip_id: int | None,
        model,
        parsed,
        path: str,
        method: str,
        auto_model_selection: bool,
        headers: dict | None = None,
    ) -> ServerSelectionContext:
        return ServerSelectionContext(
            request_id=record.id,
            ip_id=ip_id,
            model_id=model.id if model else 0,
            model_name=model.model_name if model else None,
            path=path,
            method=method,
            is_stream=parsed.stream,
            body=parsed.body,
            headers=headers,
            origin_model_name=parsed.model_name,
            auto_model_selection=auto_model_selection,
            session=getattr(record, "session", None),
        )

    def _build_url(self, base_url: str, path: str, query_string: str) -> str:
        url = base_url.rstrip("/") + "/" + path
        if query_string:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query_string}"
        return url

    def _candidates_for_request(self, path: str, model_id: int | None, vip: bool | None = None, min_context_window: int = 0):
        if path.rstrip("/") == "models" and model_id is None:
            candidates = ServerRepository.list_all_online()
            return [random.choice(candidates)] if candidates else []
        return ServerRepository.list_pd_holders(model_id, vip=vip, min_context_window=min_context_window)

    def _select_candidates(self, path: str, model, is_vip_channel: bool, is_identity_vip: bool = False, min_context_window: int = 0):
        model_id = model.id if model else None
        if path.rstrip("/") == "models" and model_id is None:
            return self._candidates_for_request(path, None), False

        if self.vip_service.is_vip_eligible(model):
            ServerRepository.demote_expired_cooldowns(self.vip_service.cooldown_seconds, model.id)

        if (is_vip_channel or is_identity_vip) and self.vip_service.is_vip_eligible(model):
            return self.vip_service.select_candidates(model)
        return self._candidates_for_request(path, model_id, vip=False, min_context_window=min_context_window), False

    def larger_window_candidates(self, path, model, served_as_vip, failed_context_window: int) -> list:
        """Same-model candidates whose context window is strictly larger than
        failed_context_window (NULL context_window counts as unlimited). Shared
        by the single-node and PD-prefill context-overflow retries."""
        candidates, _ = self._select_candidates(
            path, model, served_as_vip, min_context_window=failed_context_window,
        )
        return candidates

    def _after_finish(self, served_as_vip: bool, model) -> None:
        if served_as_vip and model is not None:
            self.vip_service.maybe_scale_down(model)

    def _handle_no_candidates(self, record, user_agent, context: ServerSelectionContext, model):
        reason = "no available server"
        if model is not None:
            reason = f"no available server for model {model.model_name}"
        append_request_log(record.id, json.dumps({
            "event": "no_candidates",
            "request_id": record.id,
            "reason": reason,
            "model": model.model_name if model else None,
        }, ensure_ascii=False))
        proxy_logging.log_request_context_for(context)
        proxy_response.finish_no_candidates(record, reason, context, model)
        self._maybe_delay_opencode_failure(user_agent, 502)
        return HttpResponse(
            json.dumps(error_payload("502 Bad Gateway", "server_error")),
            status=502,
            content_type="application/json",
        )

    def _route_with_retry(self, django_request, path, headers, body, record, user_agent, candidates, context, served_as_vip, model, is_stream):
        disconnect_scope = self._open_disconnect_scope(django_request, is_stream)
        state = _RetryState()
        set_request_id(record.id)
        try:
            append_request_log(record.id, json.dumps({
                "event": "route_start",
                "candidates": [
                    {"id": s.id, "base_url": s.base_url, "role": getattr(s, "role", "mixed") or "mixed"}
                    for s in candidates
                ],
                "max_attempts": self.max_attempts_per_request,
                "stream": is_stream,
            }, ensure_ascii=False))
            # Absolute timeout budget: once the applicable timeout has elapsed,
            # the request must end as a 504 regardless of the last failure.
            # Without this, an upstream that drops the connection after ~900s
            # looks like a ConnectionError (which #198 retries) and a retry
            # storm of 3 x ~900s ends in a synthetic "502 Bad Gateway" instead
            # of the 504 the client should see.
            request_started = time.monotonic()
            # llm-choosing requests carry a short absolute deadline set by
            # forward_internal in the request context; each attempt's socket
            # timeouts are clamped to the remaining budget (see
            # _deadline_timeout) so a hung routing server is disconnected at
            # the deadline.
            llm_choosing_deadline = get_llm_choosing_deadline()
            if llm_choosing_deadline is not None:
                deadline = llm_choosing_deadline
            else:
                budget = self.stream_total_timeout if is_stream else self.normal_timeout[1]
                deadline = request_started + budget
            while state.attempts < self.max_attempts_per_request:
                if llm_choosing_deadline is not None and time.monotonic() >= deadline:
                    state.last_status = 504
                    state.last_reason = "Gateway Timeout"
                    append_request_log(record.id, json.dumps({
                        "event": "timeout_budget_exceeded",
                        "elapsed_ms": int((time.monotonic() - request_started) * 1000),
                        "attempts": state.attempts,
                    }, ensure_ascii=False))
                    break
                server = self._effective_chooser().choose(candidates, context, state.attempted_server_ids)
                if server is None:
                    append_request_log(record.id, json.dumps({
                        "event": "no_server_available",
                        "attempts_exhausted": True,
                    }, ensure_ascii=False))
                    break

                append_request_log(record.id, json.dumps({
                    "event": "server_chosen",
                    "server_id": server.id,
                    "base_url": server.base_url,
                    "role": getattr(server, "role", "mixed") or "mixed",
                    "group_id": getattr(server, "group_id", None),
                    "workload": int(getattr(server, "workload", 0) or 0),
                    "active_tokens": float(getattr(server, "active_tokens", 0.0) or 0.0),
                    "attempt": state.attempts + 1,
                }, ensure_ascii=False))

                # PD disaggregation: a chosen prefiller is handed to the two-phase
                # PD forward service (prefill -> pick decoder -> decode). Mixed
                # servers take the existing single-node path.
                if getattr(server, "role", "mixed") == "prefiller":
                    append_request_log(record.id, json.dumps({
                        "event": "pd_path_selected",
                        "prefiller_id": server.id,
                        "prefiller_url": server.base_url,
                        "group_id": getattr(server, "group_id", None),
                    }, ensure_ascii=False))
                    result = self._pd_forward_service().forward(
                        django_request, path, headers, body, record, user_agent, context,
                        served_as_vip, model, is_stream, disconnect_scope, state, server,
                    )
                else:
                    result = self._route_single_attempt(
                        django_request,
                        path,
                        headers,
                        body,
                        record,
                        user_agent,
                        context,
                        served_as_vip,
                        model,
                        is_stream,
                        disconnect_scope,
                        state,
                        server,
                    )

                if result.response is not None:
                    return result.response
                candidates = result.candidates if result.candidates is not None else candidates
                model = result.model if result.model is not None else model
                body = result.body if result.body is not None else body
                if result.should_retry:
                    now = time.monotonic()
                    if now >= deadline:
                        elapsed_ms = int((now - request_started) * 1000)
                        state.last_status = 504
                        state.last_reason = "Gateway Timeout"
                        append_request_log(record.id, json.dumps({
                            "event": "timeout_budget_exceeded",
                            "elapsed_ms": elapsed_ms,
                            "attempts": state.attempts,
                        }, ensure_ascii=False))
                        break
                    continue
                break

            return self._retry_failure_response(record, state, served_as_vip, model, user_agent, context)
        finally:
            self._close_disconnect_scope(disconnect_scope)
            clear_request_id()

    def _open_disconnect_scope(self, django_request, is_stream: bool) -> _DisconnectScope:
        scope = _DisconnectScope(threading.Event(), threading.Event())
        if is_stream:
            return scope

        scope.upstream_client = CancellableUpstreamRequest()
        tracker = getattr(django_request, "client_disconnect_tracker", None)
        if tracker:
            scope.watcher = DisconnectWatcher(
                tracker,
                scope.disconnect_event,
                scope.stop_event,
                scope.upstream_client.cancel,
                self.client_disconnect_check_interval,
            )
            scope.watcher.start()
        return scope

    @staticmethod
    def _close_disconnect_scope(scope: _DisconnectScope) -> None:
        scope.stop_event.set()
        if scope.upstream_client:
            scope.upstream_client.close()
        if scope.watcher:
            scope.watcher.join(timeout=0.1)

    def _route_single_attempt(
        self,
        django_request,
        path,
        headers,
        body,
        record,
        user_agent,
        context,
        served_as_vip,
        model,
        is_stream,
        disconnect_scope,
        state,
        server,
    ):
        upstream_url, target_pod_ip = self._start_attempt(django_request, path, record, context, state, server)
        if record.model_choosing_latency is None:
            # Elapsed time from request receipt to the first upstream send.
            # ttft minus this value is the LLM-side time to first token.
            RequestRepository.record_model_choosing_latency(
                record,
                int((timezone.now() - record.send_time).total_seconds() * 1000),
            )
        workload_handed_off = False
        try:
            upstream = self._perform_request(
                django_request,
                server,
                upstream_url,
                headers,
                body,
                is_stream,
                disconnect_scope.upstream_client,
            )
            result = self._handle_upstream_response(
                django_request,
                upstream,
                server,
                upstream_url,
                headers,
                body,
                record,
                user_agent,
                context,
                served_as_vip,
                model,
                is_stream,
                disconnect_scope,
                state,
                target_pod_ip,
            )
            workload_handed_off = result.response is not None and is_stream and state.last_status < 400
            return result
        except requests.exceptions.ReadTimeout:
            return self._handle_read_timeout(record, server, disconnect_scope, state, served_as_vip, model)
        except requests.RequestException as exc:
            return self._handle_request_exception(record, server, exc, disconnect_scope, state, served_as_vip, model)
        except Exception:
            if disconnect_scope.disconnect_event.is_set():
                # The client disconnected while this attempt was in flight, so
                # whatever blew up (e.g. the cancel path racing the upstream
                # socket write and surfacing as a raw AttributeError) is a
                # symptom of that, not an upstream failure. Finish as 499
                # instead of leaking a 502.
                return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))
            raise
        finally:
            if not workload_handed_off:
                self._decrement_workload(server)

    def _start_attempt(self, django_request, path, record, context, state: _RetryState, server):
        state.last_server = server
        state.attempted_server_ids.add(server.id)
        state.attempts += 1
        upstream_url = self._build_url(server.base_url, path, django_request.META.get("QUERY_STRING", ""))
        target_pod_ip = self._target_identifier(server)
        state.last_target_pod_ip = target_pod_ip
        RequestRepository.record_attempt(
            record,
            target_pod_ip,
            state.attempts,
            getattr(context, "prefix_cache", None),
            getattr(context, "last_match", None),
        )
        self._increment_workload(server)
        return upstream_url, target_pod_ip

    def _handle_upstream_response(
        self,
        django_request,
        upstream,
        server,
        upstream_url,
        headers,
        body,
        record,
        user_agent,
        context,
        served_as_vip,
        model,
        is_stream,
        disconnect_scope,
        state,
        target_pod_ip,
    ):
        content = upstream.content if not is_stream else b""
        if disconnect_scope.disconnect_event.is_set():
            return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))

        status_code = upstream.status_code
        reason = upstream.reason or ""
        state.last_status = status_code
        state.last_reason = reason
        self._record_upstream_status(record, state, server, user_agent, context, status_code, is_stream)

        if status_code >= 400:
            return self._handle_upstream_error(
                django_request,
                upstream,
                upstream_url,
                headers,
                body,
                content,
                record,
                context,
                served_as_vip,
                model,
                is_stream,
                status_code,
                reason,
                target_pod_ip,
                state.attempts,
                server,
                state,
            )

        if not is_stream:
            return self._normal_success_response(
                upstream,
                content,
                record,
                model,
                context,
                status_code,
                reason,
                target_pod_ip,
                state.attempts,
                served_as_vip,
            )

        response = self._stream_success(
            django_request,
            upstream,
            record,
            server,
            context.model_name,
            status_code,
            reason,
            target_pod_ip,
            state.attempts,
            context,
            served_as_vip,
            model,
        )
        return _RouteAttemptResult(response=response)

    def _record_upstream_status(self, record, state: _RetryState, server, user_agent, context, status_code: int, is_stream: bool) -> None:
        proxy_logging.log_attempt(
            record.id,
            state.attempts,
            server,
            "status",
            False,
            status=status_code,
        )
        if status_code >= 500:
            self._mark_unhealthy(server)
        proxy_logging.maybe_log_multi_server_route(
            record.id,
            state.attempted_server_ids,
            server.id,
        )
        self._maybe_delay_opencode_failure(user_agent, status_code)
        # For streams, the status code is known only at header time and the body
        # still has to stream. Recording a circuit-breaker success now would reset
        # consecutive_failures before a mid-stream timeout/disconnect (a real
        # failure) can be counted, so a chronically slow server never trips the
        # circuit. The chooser on_response hook still runs (prefix cache etc.);
        # the success record is deferred to _stream_success on full completion.
        self._notify_chooser_response(server, context, status_code, record_circuit=not is_stream)

    def _handle_upstream_error(
        self,
        django_request,
        upstream,
        upstream_url,
        headers,
        body,
        content,
        record,
        context,
        served_as_vip,
        model,
        is_stream,
        status_code,
        reason,
        target_pod_ip,
        attempts,
        server,
        state: _RetryState,
    ):
        if is_stream:
            # Drain the error body and close defensively: a broken connection
            # can make content access or close() raise, and that must not skip
            # finish_upstream_error (which would orphan a 'processing' record
            # whose workload was already handed back in the attempt finally).
            try:
                content = upstream.content
            except Exception:
                content = b""
            try:
                upstream.close()
            except Exception:
                pass

        fail_reason = proxy_response.extract_fail_reason(content, reason)
        failed_context_window = getattr(server, "context_window", None)

        # Capture the real upstream error so that if retries are later
        # exhausted the caller receives this body/status rather than a
        # synthetic 502. Stream bodies have already been drained above.
        state.last_upstream = upstream
        state.last_content = content
        state.last_fail_reason = fail_reason

        # On a real context overflow, retry on a same-model server with a
        # strictly larger context window (the chooser excludes already-tried
        # servers). The router never switches to a different model on overflow
        # (issue #224); if no larger-window same-model server exists the real
        # upstream error surfaces. Issue #153: never pre-decide by estimated tokens.
        if (
            self.auto_router.check_context_overflow(status_code, failed_context_window, fail_reason)
            and failed_context_window
        ):
            higher_candidates = self.larger_window_candidates(
                context.path, model, served_as_vip, failed_context_window,
            )
            if higher_candidates:
                return _RouteAttemptResult(
                    should_retry=True,
                    candidates=higher_candidates,
                    model=model,
                    body=body,
                )

        proxy_response.finish_upstream_error(
            record,
            status_code,
            fail_reason,
            target_pod_ip,
            model,
            attempts,
            context,
        )
        self._after_finish(served_as_vip, model)
        proxy_logging.log_error_detail(
            record.id,
            django_request.method,
            upstream_url,
            headers,
            body,
            status_code,
            content,
        )
        return _RouteAttemptResult(
            response=proxy_response.response_from_upstream(upstream, content, status_code)
        )

    def _normal_success_response(self, upstream, content, record, model, context, status_code, reason, target_pod_ip, attempts, served_as_vip):
        proxy_response.finish_normal_success(
            record,
            content,
            model,
            context,
            status_code,
            reason,
            target_pod_ip,
            attempts,
        )
        self._after_finish(served_as_vip, model)
        return _RouteAttemptResult(
            response=proxy_response.response_from_upstream(upstream, content, status_code)
        )

    def _handle_read_timeout(self, record, server, disconnect_scope, state: _RetryState, served_as_vip, model):
        if disconnect_scope.disconnect_event.is_set():
            return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))
        state.last_status = 504
        state.last_reason = "Gateway Timeout"
        # A read timeout is a real upstream failure (the request was accepted but
        # the server never answered). It must count toward the circuit breaker,
        # otherwise a chronically slow server returns 504 forever without its
        # consecutive_failures ever accumulating and the circuit never opens.
        self._mark_unhealthy(server)
        proxy_logging.log_attempt(
            record.id,
            state.attempts,
            server,
            "read_timeout",
            False,
            reason="ReadTimeout",
        )
        return _RouteAttemptResult()

    def _handle_request_exception(self, record, server, exc, disconnect_scope, state: _RetryState, served_as_vip, model):
        if disconnect_scope.disconnect_event.is_set():
            return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))
        state.last_status = 502
        state.last_reason = "Bad Gateway"
        retry = self._is_connection_failure(exc) and state.attempts < self.max_attempts_per_request
        self._mark_unhealthy(server)
        proxy_logging.log_attempt(
            record.id,
            state.attempts,
            server,
            exc.__class__.__name__,
            retry,
            reason=str(exc),
        )
        return _RouteAttemptResult(should_retry=retry)

    def _retry_failure_response(self, record, state: _RetryState, served_as_vip, model, user_agent, context):
        proxy_logging.log_request_context_for(context)
        final_server_id = state.last_server.id if state.last_server else None
        proxy_logging.maybe_log_multi_server_route(
            record.id,
            state.attempted_server_ids,
            final_server_id,
        )
        final_target_pod_ip = state.last_target_pod_ip or self._target_identifier(state.last_server)
        # Retries exhausted (all candidates tried or max attempts hit). If the
        # last failure was a real upstream error, return that response so the
        # caller sees the actual upstream status/body. Only synthesize a 502/504
        # for failures with no body, i.e. read timeouts or connection errors.
        if state.last_upstream is not None and state.last_status >= 400:
            # This branch is only reached when the deadline broke after a retry
            # was scheduled (the normal terminal path returns a response and
            # never gets here), so the upstream body for this attempt was never
            # logged — capture it now.
            proxy_logging.log_failure_response(
                record.id, final_target_pod_ip, state.last_status, state.last_content
            )
            proxy_response.finish_upstream_error(
                record,
                state.last_status,
                state.last_fail_reason or state.last_reason,
                final_target_pod_ip,
                model,
                state.attempts,
                context,
            )
            self._after_finish(served_as_vip, model)
            self._maybe_delay_opencode_failure(user_agent, state.last_status)
            return proxy_response.response_from_upstream(
                state.last_upstream, state.last_content, state.last_status
            )

        status = 504 if state.last_status == 504 else 502
        message = "request timeout" if status == 504 else "502 Bad Gateway"
        proxy_response.finish_retry_failure(
            record,
            status,
            message,
            final_target_pod_ip,
            state.attempts,
            context,
        )
        self._after_finish(served_as_vip, model)
        error_type = "gateway_timeout_error" if status == 504 else "server_error"
        # The opencode failure delay exists to backpressure retry storms on
        # fast failures. After a 504 the client has already waited the full
        # timeout budget; sleeping another 180s here only postpones the timeout
        # response (and lets an upstream proxy cut it off, surfacing as a
        # spurious 502 instead of the 504 we decided).
        if status != 504:
            self._maybe_delay_opencode_failure(user_agent, status)
        return HttpResponse(json.dumps(error_payload(message, error_type)), status=status, content_type="application/json")

    @staticmethod
    def _deadline_timeout(base: tuple[float, float]) -> tuple[float, float]:
        """Clamp (connect, read) socket timeouts to the remaining llm-choosing
        budget from the request context (set by forward_internal; absent for
        normal client requests). When the read timeout fires, urllib3 drops
        the connection instead of returning it to the pool, so the routing
        server is disconnected at the deadline rather than after
        normal_read_timeout_seconds."""
        deadline = get_llm_choosing_deadline()
        if deadline is None:
            return base
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            remaining = 0.001
        return (min(base[0], remaining), remaining)

    def _handle_normal(self, django_request, server, upstream_url, headers, body, upstream_client):
        req_headers = {**headers}
        if server.csb_token:
            req_headers["csb-token"] = server.csb_token
        return upstream_client.request(
            django_request.method,
            upstream_url,
            headers=req_headers,
            data=body if django_request.method.upper() not in {"GET", "HEAD"} else None,
            timeout=self._deadline_timeout(self.normal_timeout),
        )

    def _handle_stream(self, django_request, server, upstream_url, headers, body):
        req_headers = {**headers}
        if server.csb_token:
            req_headers["csb-token"] = server.csb_token
        return requests.request(
            django_request.method,
            upstream_url,
            headers=req_headers,
            data=body,
            stream=True,
            timeout=self.stream_timeout,
        )

    def _stream_success(self, django_request, upstream, record, server, model_name, status_code, reason, target_pod_ip, attempts, context, served_as_vip, model):
        request_start = time.monotonic()

        def generate():
            chunks: list[bytes] = []
            first_chunk_at = None
            try:
                deadline = request_start + self.stream_total_timeout
                for chunk in upstream.iter_content(chunk_size=8192):
                    if time.monotonic() > deadline:
                        self._mark_unhealthy(server)
                        proxy_logging.log_request_context_for(context)
                        yield timeout_sse_event()
                        proxy_response.finish_stream_total_timeout(
                            record,
                            target_pod_ip,
                            attempts,
                        )
                        return
                    tracker = getattr(django_request, "client_disconnect_tracker", None)
                    if tracker and tracker.client_disconnected():
                        proxy_response.finish_stream_client_disconnected(
                            record,
                            target_pod_ip,
                            attempts,
                        )
                        return
                    if chunk:
                        if first_chunk_at is None:
                            first_chunk_at = timezone.now()
                        chunks.append(chunk)
                        yield chunk
                self._notify_chooser_response(server, context, status_code)
                ttft = (
                    int((first_chunk_at - record.send_time).total_seconds() * 1000)
                    if first_chunk_at is not None
                    else None
                )
                proxy_response.finish_stream_success(
                    record,
                    status_code,
                    reason,
                    chunks,
                    target_pod_ip,
                    model_name,
                    attempts,
                    context,
                    ttft,
                )
            except requests.exceptions.ReadTimeout:
                proxy_logging.log_request_context_for(context)
                self._mark_unhealthy(server)
                yield timeout_sse_event()
                proxy_response.finish_stream_read_timeout(
                    record,
                    target_pod_ip,
                    attempts,
                    model,
                    context,
                )
            except requests.RequestException:
                proxy_logging.log_request_context_for(context)
                message = "502 Bad Gateway"
                payload = error_payload(message, "server_error")
                yield f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                self._mark_unhealthy(server)
                proxy_response.finish_stream_request_exception(
                    record,
                    message,
                    target_pod_ip,
                    attempts,
                    model,
                    context,
                )
            except Exception as exc:
                # Scenario-2 guard: any non-requests exception during streaming
                # (e.g. a client disconnect surfacing as OSError on yield, or a
                # failure inside finish_*). Finish the record, log the full
                # traceback to its per-request log, and emit a terminal error.
                # The finally below still runs cleanup; this must not raise.
                fail_reason = f"unhandled stream {type(exc).__name__}: {exc}"[:200]
                logger.error("proxy stream unhandled %s request_id=%s: %s", type(exc).__name__, record.id, str(exc)[:200])
                try:
                    proxy_logging.safe_append_request_log(
                        record.id,
                        f"proxy stream unhandled exception: {fail_reason}\n{traceback.format_exc()}",
                    )
                except Exception:
                    pass
                try:
                    proxy_response.finish_stream_request_exception(
                        record, "502 Bad Gateway", target_pod_ip, attempts, model, context
                    )
                except Exception:
                    logger.exception("failed to finish streaming record %s after unhandled exception", record.id)
                try:
                    yield f"data: {json.dumps(error_payload('502 Bad Gateway', 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                except Exception:
                    pass
            finally:
                # Decrement workload before closing the upstream: a close() that
                # raises on a broken connection must not skip the decrement, and
                # finish_* has already run so cleanup_stale cannot reclaim it.
                self._decrement_workload(server)
                try:
                    upstream.close()
                except Exception:
                    pass
                self._after_finish(served_as_vip, model)

        response = StreamingHttpResponse(generate(), status=200, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def _client_closed_response(self, record, served_as_vip: bool = False, model=None):
        proxy_response.finish_client_closed(record)
        self._after_finish(served_as_vip, model)
        return HttpResponse(status=499)

    def _maybe_delay_opencode_failure(self, user_agent: str | None, status_code: int) -> None:
        if self.opencode_failure_delay > 0 and OpencodeVersionService.should_delay_failure(user_agent, status_code):
            time.sleep(self.opencode_failure_delay)

    @staticmethod
    def _is_connection_failure(exc: BaseException) -> bool:
        # A connection failure guarantees the request body never reached the
        # upstream, so retrying on another server is safe even for non-idempotent
        # POST. ReadTimeout and other RequestException subclasses mean the
        # connection was established (the server may have started processing),
        # so they must not trigger a retry. ConnectTimeout subclasses
        # ConnectionError and is therefore treated as a connection failure.
        return isinstance(exc, requests.exceptions.ConnectionError)

    @staticmethod
    def _load_chooser(path: str):
        try:
            module_name, class_name = path.rsplit(".", 1)
            chooser_class = getattr(importlib.import_module(module_name), class_name)
            return chooser_class()
        except (ImportError, AttributeError, ValueError, TypeError):
            return LeastConnectionServerChooser()

    def _effective_chooser(self):
        return self._active_chooser or self.chooser

    def _notify_chooser_response(self, server, context, status_code: int, *, record_circuit: bool = True) -> None:
        if record_circuit and 200 <= status_code < 300:
            self.circuit_breaker.record_success(server)
        hook = getattr(self._effective_chooser(), "on_response", None)
        if not hook:
            return
        try:
            hook(server, context, status_code)
        except Exception as exc:
            proxy_logging.log_chooser_response_hook_error(
                context,
                server,
                status_code,
                exc,
            )

    def _mark_unhealthy(self, server) -> None:
        if server.id != 0:
            self.circuit_breaker.record_failure(server)

    def _increment_workload(self, server) -> None:
        if server and getattr(server, "id", 0) != 0:
            ServerRepository.increment_workload(server)

    def _decrement_workload(self, server) -> None:
        if server and getattr(server, "id", 0) != 0:
            ServerRepository.decrement_workload(server)

    def _pd_forward_service(self):
        # Lazily imported to avoid pulling the PD forward path (and its requests
        # usage) into deployments that only run single-node servers.
        from router.services.proxy_pd_forward import PDForwardService

        return PDForwardService(self)

    @staticmethod
    def _target_identifier(server) -> str | None:
        if not server:
            return None
        return server.base_url[:500]

    def _perform_request(self, django_request, server, upstream_url, headers, body, is_stream, upstream_client):
        if is_stream:
            return self._handle_stream(django_request, server, upstream_url, headers, body)
        return self._handle_normal(django_request, server, upstream_url, headers, body, upstream_client)
