from __future__ import annotations

import importlib
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpResponse, StreamingHttpResponse
import requests

from router.config import APP_CONFIG
from router.models import ROUTER_RESULT_MAX_LENGTH
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.circuit_breaker import CircuitBreakerService
from router.services.disconnect import DisconnectWatcher
from router.services.opencode import OpencodeVersionService
from router.services.request_logger import append_request_log, append_error_log, append_verbose_request_log
from router.services.vip_channel import VIPChannelService
from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser
from router.utils.errors import error_payload, timeout_sse_event
from router.utils.headers import filter_request_headers, filter_response_headers
from router.utils.sse import parse_sse_usage


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


@dataclass
class _RouteAttemptResult:
    response: Any = None
    should_retry: bool = False
    candidates: Any = None
    model: Any = None
    body: bytes | None = None


class ProxyService:
    SMALL_REQUEST_ROUTING_TOKEN_LIMIT = 3000

    def __init__(self, chooser=None):
        proxy_config = APP_CONFIG.get("proxy", {})
        lb_config = APP_CONFIG.get("load_balancer", {})
        self.max_attempts_per_request = int(lb_config.get("max_attempts_per_request", 3))
        self.chooser = chooser or self._load_chooser(str(lb_config.get("chooser_class", "router.route_algorithm.least_connection.LeastConnectionServerChooser")))
        self.stream_timeout = (
            float(proxy_config.get("stream_connect_timeout_seconds", 30)),
            float(proxy_config.get("stream_read_timeout_seconds", 900)),
        )
        self.normal_timeout = (
            float(proxy_config.get("normal_connect_timeout_seconds", 5)),
            float(proxy_config.get("normal_read_timeout_seconds", 900)),
        )
        self.stream_total_timeout = float(proxy_config.get("stream_total_timeout_seconds", 900))
        self.client_disconnect_check_interval = float(proxy_config.get("client_disconnect_check_interval_seconds", 0.5))
        self.opencode_failure_delay = float(proxy_config.get("opencode_failure_delay_seconds", 30))
        self.circuit_breaker = CircuitBreakerService()
        self.vip_service = VIPChannelService()
        self.vip_port = int(APP_CONFIG.get("server", {}).get("vip_port", 8008))
        self._router_system_prompt = None

    def _get_auto_route_model(self, body: bytes, record: Any, context: ServerSelectionContext) -> tuple[Any, str | None]:
        active_models = ModelRepository.list_active_models()
        if not active_models:
            return None, self._routing_unavailable_result(
                "missing_target_model",
                "no active target model for auto request",
            )

        model_names = [m.model_name for m in active_models]
        selection_body = self._auto_selection_body(body)

        cached_model = self._check_cache_hit(selection_body or body, active_models, model_names)
        if cached_model:
            return cached_model, "cache_hit"

        if selection_body is None:
            return self._get_default_model(), self._routing_unavailable_result(
                "missing_user_prompt",
                "no user prompt for auto request",
            )

        return self._query_routing_llm(selection_body, record, context, active_models, model_names)

    def _check_cache_hit(self, body: bytes, active_models: list[Any], model_names: list[str]) -> Any | None:
        chooser = self.chooser
        if hasattr(chooser, "get_all_model_prefix_ratios"):
            ratios = chooser.get_all_model_prefix_ratios(body, model_names)
            if ratios:
                best_name = max(ratios, key=ratios.get)
                if ratios[best_name] > 0.9:
                    return next((m for m in active_models if m.model_name == best_name), None)
        return None

    def _query_routing_llm(self, body: bytes, record: Any, context: ServerSelectionContext, active_models: list[Any], model_names: list[str]) -> tuple[Any, str | None]:
        routing_models = ModelRepository.get_routing_models()
        if not routing_models:
            return self._get_default_model(), self._routing_unavailable_result(
                "missing_routing_model",
                "no routing model configured",
            )

        routing_servers = []
        model_id_to_name = {rm.id: rm.model_name for rm in routing_models}
        for rm in routing_models:
            routing_servers.extend(ServerRepository.list_by_model_id(rm.id, vip=False, estimate_tokens=0))

        if not routing_servers:
            return self._get_default_model(), self._routing_unavailable_result()

        server = self.chooser.choose(routing_servers, context, set()) or random.choice(routing_servers)

        self._ensure_system_prompt(model_names)
        routing_model_name = model_id_to_name.get(server.model_id, "router")

        payload = self._build_routing_payload(routing_model_name, body)

        url = self._build_url(server.base_url, "chat/completions", "")
        headers = {"Content-Type": "application/json"}
        if server.csb_token:
            headers["csb-token"] = server.csb_token

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
        except Exception as e:
            router_result = self._routing_exception_result(e)
            self._safe_append_request_log(record.id, f"Routing LLM error: {str(e)}")
            return self._get_default_model(), router_result

        if resp.status_code != 200:
            return self._get_default_model(), self._routing_response_error_result(resp)

        try:
            result = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            router_result = self._routing_exception_result(e, status_code=resp.status_code)
            self._safe_append_request_log(record.id, f"Routing LLM error: {str(e)}")
            return self._get_default_model(), router_result

        for name in model_names:
            if name in result:
                matched = next((m for m in active_models if m.model_name == name), None)
                if matched:
                    return matched, result

        return self._get_default_model(), self._invalid_routing_result(result)

    def _routing_response_error_result(self, response) -> str:
        status_code = getattr(response, "status_code", None)
        content = self._response_content_bytes(response)
        reason = self._response_reason(response)
        message = self._extract_fail_reason(content, reason or "routing request failed")
        return self._format_router_result("routing_failed", status_code, message)

    def _routing_exception_result(self, exc: Exception, status_code: int | None = None) -> str:
        return self._format_router_result("routing_error", status_code, str(exc))

    def _routing_unavailable_result(
        self,
        code: str = "missing_routing_server",
        message: str = "no available routing server",
    ) -> str:
        return self._format_router_result("routing_failed", code, message)

    def _invalid_routing_result(self, result: str) -> str:
        detail = self._compact_router_message(result) or "empty routing result"
        return self._format_router_result(
            "routing_failed",
            "invalid_routing_result",
            f"router returned no active model: {detail}",
        )

    @staticmethod
    def _format_router_result(prefix: str, status_code: int | str | None, message: str) -> str:
        code = str(status_code) if status_code is not None else "exception"
        detail = ProxyService._compact_router_message(message)
        return f"{prefix}:{code}:{detail}"[:ROUTER_RESULT_MAX_LENGTH]

    @staticmethod
    def _compact_router_message(message: Any) -> str:
        return " ".join(str(message or "").split())

    @staticmethod
    def _response_content_bytes(response) -> bytes:
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            return content.encode("utf-8", errors="replace")
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        text = getattr(response, "text", "")
        if isinstance(text, str):
            return text.encode("utf-8", errors="replace")
        return b""

    @staticmethod
    def _response_reason(response) -> str:
        reason = getattr(response, "reason", "")
        if isinstance(reason, str):
            return reason
        text = getattr(response, "text", "")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _safe_append_request_log(request_id: int, message: str) -> None:
        try:
            append_request_log(request_id, message)
        except Exception:
            pass

    def _ensure_system_prompt(self, model_names: list[str]) -> None:
        if self._router_system_prompt is None:
            prompt_path = APP_CONFIG.get("router", {}).get("system_prompt_path", "router/assets/router_system_prompt.md")
            try:
                with open(prompt_path, "r") as f:
                    self._router_system_prompt = f.read()
            except Exception:
                self._router_system_prompt = "You are a router. Return one of: " + ", ".join(model_names)

    def _build_routing_payload(self, model_name: str, body: bytes) -> dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": self._routing_messages_from_body(body),
            "stream": False,
        }
        self._disable_thinking(payload)
        return payload

    def _routing_messages_from_body(self, body: bytes) -> list[dict[str, Any]]:
        messages = [{"role": "system", "content": self._router_system_prompt}]
        messages.extend(self._user_messages_from_body(body))
        return messages

    @staticmethod
    def _user_messages_from_body(body: bytes) -> list[dict[str, Any]]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []

        source_messages = data.get("messages")
        if not isinstance(source_messages, list):
            return []

        user_messages: list[dict[str, Any]] = []
        for message in source_messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if "content" not in message:
                continue
            user_messages.append({"role": "user", "content": message["content"]})
        return user_messages

    @classmethod
    def _auto_selection_body(cls, body: bytes) -> bytes | None:
        user_messages = cls._user_messages_from_body(body)
        if user_messages:
            return json.dumps({"messages": user_messages}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return None

    def _get_default_model(self) -> Any:
        name = APP_CONFIG.get("router", {}).get("fallback_model", "DeepSeek-V4-Flash")
        return ModelRepository.get_by_name(name)

    def forward(self, django_request, path: str, parsed, ip_id: int | None, model, user_agent: str | None, is_vip_channel: bool = False):
        headers = filter_request_headers(dict(django_request.headers), django_request.method)
        record = self._create_processing_record(ip_id, model, parsed, user_agent)
        append_verbose_request_log(record.id, django_request.body)
        context = self._selection_context(record, ip_id, model, parsed, path, django_request.method)
        original_model_name = parsed.model_name
        should_record_model_choice = original_model_name == "auto" or self._should_route_small_request(parsed)
        model_choice_started = time.monotonic() if should_record_model_choice else None
        router_result = None
        try:
            model, router_result = self._resolve_small_request_routing_model(parsed, record, context, model)
            if router_result is None:
                model, router_result = self._resolve_auto_model(parsed, record, context, model)
        finally:
            if model_choice_started is not None:
                RequestRepository.record_model_choosing_latency(
                    record,
                    int((time.monotonic() - model_choice_started) * 1000),
                )

        candidates, served_as_vip = self._select_candidates(path, model, is_vip_channel, estimate_tokens=parsed.estimated_input_tokens)
        if served_as_vip:
            record.user_ip_id = 2
            record.save(update_fields=["user_ip_id"])

        context.router_result = router_result
        if not candidates:
            return self._handle_no_candidates(record, user_agent, context, model)

        return self._route_with_retry(
            django_request, path, headers, parsed.body, record, user_agent,
            candidates, context, served_as_vip, model, parsed.stream
        )

    @staticmethod
    def _create_processing_record(ip_id: int | None, model, parsed, user_agent: str | None):
        user_ip_id = 1
        return RequestRepository.create_processing(
            ip_id,
            model.id if model else 0,
            parsed.stream,
            user_agent,
            user_ip_id=user_ip_id,
            estimate_tokens=parsed.estimated_input_tokens,
        )

    @staticmethod
    def _selection_context(record, ip_id: int | None, model, parsed, path: str, method: str) -> ServerSelectionContext:
        return ServerSelectionContext(
            request_id=record.id,
            ip_id=ip_id,
            model_id=model.id if model else 0,
            model_name=model.model_name if model else None,
            path=path,
            method=method,
            is_stream=parsed.stream,
            body=parsed.body,
            origin_model_name=parsed.model_name,
        )

    def _resolve_auto_model(self, parsed, record, context: ServerSelectionContext, model):
        if parsed.model_name != "auto":
            return model, None

        model, router_result = self._get_auto_route_model(parsed.body, record, context)
        if model:
            self._apply_resolved_model(parsed, record, context, model)
        return model, router_result

    def _resolve_small_request_routing_model(self, parsed, record, context: ServerSelectionContext, model):
        if not self._should_route_small_request(parsed):
            return model, None

        routing_model = self._get_small_request_routing_model(parsed.estimated_input_tokens)
        if routing_model is None:
            return model, None

        self._apply_resolved_model(parsed, record, context, routing_model, disable_thinking=True)
        return routing_model, "small_request_routing"

    def _should_route_small_request(self, parsed) -> bool:
        return int(parsed.estimated_input_tokens or 0) < self.SMALL_REQUEST_ROUTING_TOKEN_LIMIT

    @staticmethod
    def _get_small_request_routing_model(estimate_tokens: int = 0):
        for routing_model in ModelRepository.get_routing_models():
            candidates = ServerRepository.list_by_model_id(routing_model.id, vip=False, estimate_tokens=estimate_tokens)
            if candidates:
                return routing_model
        return None

    def _apply_resolved_model(self, parsed, record, context: ServerSelectionContext, model, disable_thinking: bool = False) -> None:
        record.model_id = model.id
        record.save(update_fields=["model_id"])
        parsed.model_name = model.model_name
        parsed.body = self._update_body_model(parsed.body, model.model_name, disable_thinking=disable_thinking)
        context.model_id = model.id
        context.model_name = model.model_name
        context.body = parsed.body

    def _build_url(self, base_url: str, path: str, query_string: str) -> str:
        url = base_url.rstrip("/") + "/" + path
        if query_string:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query_string}"
        return url

    def _candidates_for_request(self, path: str, model_id: int | None, vip: bool | None = None, estimate_tokens: int = 0):
        if path.rstrip("/") == "models" and model_id is None:
            candidates = ServerRepository.list_all_online()
            filtered = [s for s in candidates if s.context_window is None or s.context_window >= estimate_tokens]
            return [random.choice(filtered)] if filtered else []
        return ServerRepository.list_by_model_id(model_id, vip=vip, estimate_tokens=estimate_tokens)

    def _select_candidates(self, path: str, model, is_vip_channel: bool, estimate_tokens: int = 0):
        model_id = model.id if model else None
        if path.rstrip("/") == "models" and model_id is None:
            return self._candidates_for_request(path, None, estimate_tokens=estimate_tokens), False

        if self.vip_service.is_vip_eligible(model):
            ServerRepository.demote_expired_cooldowns(self.vip_service.cooldown_seconds, model.id)

        if is_vip_channel and self.vip_service.is_vip_eligible(model):
            return self.vip_service.select_candidates(model, estimate_tokens=estimate_tokens)
        return self._candidates_for_request(path, model_id, vip=False, estimate_tokens=estimate_tokens), False

    def _after_finish(self, served_as_vip: bool, model, estimate_tokens: int = 0) -> None:
        if served_as_vip and model is not None:
            self.vip_service.maybe_scale_down(model, estimate_tokens=estimate_tokens)

    def _handle_no_candidates(self, record, user_agent, context: ServerSelectionContext, model):
        reason = "no available server"
        if model is not None:
            reason = f"no available server for model {model.model_name}"
        RequestRepository.finish(
            record,
            502,
            reason,
            model_id=model.id if model else None,
            attempt_count=0,
            router_result=getattr(context, "router_result", None),
        )
        self._maybe_delay_opencode_failure(user_agent, 502)
        return HttpResponse(
            json.dumps(error_payload("502 Bad Gateway", "server_error")),
            status=502,
            content_type="application/json",
        )

    def _route_with_retry(self, django_request, path, headers, body, record, user_agent, candidates, context, served_as_vip, model, is_stream):
        disconnect_scope = self._open_disconnect_scope(django_request, is_stream)
        state = _RetryState()
        try:
            while state.attempts < self.max_attempts_per_request:
                server = self.chooser.choose(candidates, context, state.attempted_server_ids)
                if server is None:
                    break

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
                    continue
                break

            return self._retry_failure_response(record, state, served_as_vip, model, user_agent, context)
        finally:
            self._close_disconnect_scope(disconnect_scope)

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
        finally:
            if not workload_handed_off:
                self._decrement_workload(server)

    def _start_attempt(self, django_request, path, record, context, state: _RetryState, server):
        state.last_server = server
        state.attempted_server_ids.add(server.id)
        state.attempts += 1
        upstream_url = self._build_url(server.base_url, path, django_request.META.get("QUERY_STRING", ""))
        target_pod_ip = self._target_identifier(server)
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
        self._record_upstream_status(record, state, server, user_agent, context, status_code)

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

    def _record_upstream_status(self, record, state: _RetryState, server, user_agent, context, status_code: int) -> None:
        self._log_attempt(record.id, state.attempts, server, "status", False, status=status_code)
        if status_code >= 500:
            self._mark_unhealthy(server)
        self._maybe_log_multi_server_route(record.id, state.attempted_server_ids, server.id)
        self._maybe_delay_opencode_failure(user_agent, status_code)
        self._notify_chooser_response(server, context, status_code)

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
    ):
        if is_stream:
            content = upstream.content
            upstream.close()

        fail_reason = self._extract_fail_reason(content, reason)
        switched_model, switched_candidates, switched_body = self._context_overflow_switch(
            record,
            context,
            context.path,
            served_as_vip,
            body,
            model,
            status_code,
            fail_reason,
        )
        if switched_model is not None:
            model = switched_model
            body = switched_body
            if switched_candidates:
                return _RouteAttemptResult(
                    should_retry=True,
                    candidates=switched_candidates,
                    model=model,
                    body=body,
                )

        RequestRepository.finish(
            record,
            status_code,
            fail_reason,
            0,
            0,
            target_pod_ip,
            model.id if model else None,
            attempt_count=attempts,
            router_result=getattr(context, "router_result", None),
        )
        self._after_finish(served_as_vip, model, estimate_tokens=record.estimate_tokens)
        self._log_error_detail(record.id, django_request.method, upstream_url, headers, body, status_code, content)
        return _RouteAttemptResult(response=self._response_from_upstream(upstream, content, status_code))

    @staticmethod
    def _response_from_upstream(upstream, content: bytes, status_code: int):
        response = HttpResponse(content, status=status_code)
        for key, value in filter_response_headers(dict(upstream.headers)).items():
            response[key] = value
        return response

    def _context_overflow_switch(self, record, context, path, served_as_vip, body, model, status_code, fail_reason):
        if context.origin_model_name != "auto":
            return None, None, body
        if not model or model.model_name == "DeepSeek-V4-Flash":
            return None, None, body
        if not self._check_context_overflow(status_code, model, fail_reason):
            return None, None, body

        flash_model = ModelRepository.get_by_name("DeepSeek-V4-Flash")
        if not flash_model:
            return None, None, body

        append_request_log(record.id, f"Context overflow detected ({fail_reason}), switching to DeepSeek-V4-Flash")
        candidates, _ = self._select_candidates(path, flash_model, served_as_vip)
        body = self._update_body_model(body, flash_model.model_name)
        context.model_id = flash_model.id
        context.model_name = flash_model.model_name
        context.body = body
        return flash_model, candidates, body

    def _normal_success_response(self, upstream, content, record, model, context, status_code, reason, target_pod_ip, attempts, served_as_vip):
        input_tokens, output_tokens, cached_tokens = self._parse_json_usage(content)
        RequestRepository.finish(
            record,
            status_code,
            reason,
            input_tokens,
            output_tokens,
            target_pod_ip,
            model.id if model else None,
            attempt_count=attempts,
            final_prefix_cache=cached_tokens,
            router_result=getattr(context, "router_result", None),
        )
        self._after_finish(served_as_vip, model, estimate_tokens=record.estimate_tokens)
        return _RouteAttemptResult(response=self._response_from_upstream(upstream, content, status_code))

    def _handle_read_timeout(self, record, server, disconnect_scope, state: _RetryState, served_as_vip, model):
        if disconnect_scope.disconnect_event.is_set():
            return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))
        state.last_status = 504
        state.last_reason = "Gateway Timeout"
        self._log_attempt(record.id, state.attempts, server, "read_timeout", False, reason="ReadTimeout")
        return _RouteAttemptResult()

    def _handle_request_exception(self, record, server, exc, disconnect_scope, state: _RetryState, served_as_vip, model):
        if disconnect_scope.disconnect_event.is_set():
            return _RouteAttemptResult(response=self._client_closed_response(record, served_as_vip, model))
        state.last_status = 502
        state.last_reason = "Bad Gateway"
        retry = state.attempts < self.max_attempts_per_request
        self._mark_unhealthy(server)
        self._log_attempt(record.id, state.attempts, server, exc.__class__.__name__, retry, reason=str(exc))
        return _RouteAttemptResult(should_retry=retry)

    def _retry_failure_response(self, record, state: _RetryState, served_as_vip, model, user_agent, context):
        final_server_id = state.last_server.id if state.last_server else None
        self._maybe_log_multi_server_route(record.id, state.attempted_server_ids, final_server_id)
        status = 504 if state.last_status == 504 else 502
        message = "request timeout" if status == 504 else "502 Bad Gateway"
        RequestRepository.finish(
            record,
            status,
            message,
            target_pod_ip=self._target_identifier(state.last_server),
            attempt_count=state.attempts,
            router_result=getattr(context, "router_result", None),
        )
        self._after_finish(served_as_vip, model, estimate_tokens=record.estimate_tokens)
        error_type = "gateway_timeout_error" if status == 504 else "server_error"
        self._maybe_delay_opencode_failure(user_agent, status)
        return HttpResponse(json.dumps(error_payload(message, error_type)), status=status, content_type="application/json")

    def _handle_normal(self, django_request, server, upstream_url, headers, body, upstream_client):
        req_headers = {**headers}
        if server.csb_token:
            req_headers["csb-token"] = server.csb_token
        return upstream_client.request(
            django_request.method,
            upstream_url,
            headers=req_headers,
            data=body if django_request.method.upper() not in {"GET", "HEAD"} else None,
            timeout=self.normal_timeout,
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
        def generate():
            chunks: list[bytes] = []
            try:
                deadline = time.monotonic() + self.stream_total_timeout
                for chunk in upstream.iter_content(chunk_size=8192):
                    if time.monotonic() > deadline:
                        yield timeout_sse_event()
                        RequestRepository.finish(record, 504, "request timeout, please try again later", target_pod_ip=target_pod_ip, attempt_count=attempts)
                        return
                    tracker = getattr(django_request, "client_disconnect_tracker", None)
                    if tracker and tracker.client_disconnected():
                        RequestRepository.finish(record, 499, "Client Closed Request", target_pod_ip=target_pod_ip, task_status="agent_disconnected", attempt_count=attempts)
                        return
                    if chunk:
                        chunks.append(chunk)
                        yield chunk
                self._notify_chooser_response(server, context, status_code)
                input_tokens, output_tokens, cached_tokens = parse_sse_usage(chunks)
                final_model_id = self._ensure_model_after_success(model_name, status_code)
                RequestRepository.finish(record, status_code, reason, input_tokens, output_tokens, target_pod_ip, final_model_id, attempt_count=attempts, final_prefix_cache=cached_tokens, router_result=getattr(context, "router_result", None))
            except requests.exceptions.ReadTimeout:
                yield timeout_sse_event()
                RequestRepository.finish(record, 504, "request timeout, please try again later", target_pod_ip=target_pod_ip, attempt_count=attempts, model_id=model.id if model else None, router_result=getattr(context, "router_result", None))
            except requests.RequestException:
                message = "502 Bad Gateway"
                payload = error_payload(message, "server_error")
                yield f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                self._mark_unhealthy(server)
                RequestRepository.finish(record, 502, message, target_pod_ip=target_pod_ip, attempt_count=attempts, model_id=model.id if model else None, router_result=getattr(context, "router_result", None))
            finally:
                upstream.close()
                self._decrement_workload(server)
                self._after_finish(served_as_vip, model, estimate_tokens=record.estimate_tokens)

        response = StreamingHttpResponse(generate(), status=200, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def _client_closed_response(self, record, served_as_vip: bool = False, model=None):
        RequestRepository.finish(record, 499, "Client Closed Request", task_status="agent_disconnected")
        self._after_finish(served_as_vip, model, estimate_tokens=record.estimate_tokens)
        return HttpResponse(status=499)

    def _maybe_delay_opencode_failure(self, user_agent: str | None, status_code: int) -> None:
        if self.opencode_failure_delay > 0 and OpencodeVersionService.should_delay_failure(user_agent, status_code):
            time.sleep(self.opencode_failure_delay)

    @staticmethod
    def _parse_json_usage(content: bytes) -> tuple[int, int, int]:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 0, 0, 0
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return 0, 0, 0
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        cached_tokens = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        return prompt_tokens, completion_tokens, cached_tokens

    @staticmethod
    def _extract_fail_reason(content: bytes, http_reason: str) -> str:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return http_reason
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                msg = error.get("message", "")
                err_type = error.get("type", "")
                if msg:
                    return f"{err_type}: {msg}" if err_type else msg
        return http_reason

    @staticmethod
    def _ensure_model_after_success(model_name: str | None, status_code: int) -> int | None:
        if model_name and 200 <= status_code < 300:
            model, _ = ModelRepository.get_or_create(model_name)
            return model.id
        return None

    @staticmethod
    def _load_chooser(path: str):
        try:
            module_name, class_name = path.rsplit(".", 1)
            chooser_class = getattr(importlib.import_module(module_name), class_name)
            return chooser_class()
        except (ImportError, AttributeError, ValueError, TypeError):
            return LeastConnectionServerChooser()

    def _notify_chooser_response(self, server, context, status_code: int) -> None:
        if 200 <= status_code < 300:
            self.circuit_breaker.record_success(server)
        hook = getattr(self.chooser, "on_response", None)
        if not hook:
            return
        try:
            hook(server, context, status_code)
        except Exception as exc:
            append_request_log(context.request_id, json.dumps({
                "event": "chooser_response_hook_error",
                "server_id": getattr(server, "id", None),
                "status_code": status_code,
                "reason": str(exc)[:500],
            }, ensure_ascii=False))

    def _mark_unhealthy(self, server) -> None:
        if server.id != 0:
            self.circuit_breaker.record_failure(server)

    def _increment_workload(self, server) -> None:
        if server and getattr(server, "id", 0) != 0:
            ServerRepository.increment_workload(server)

    def _decrement_workload(self, server) -> None:
        if server and getattr(server, "id", 0) != 0:
            ServerRepository.decrement_workload(server)

    @staticmethod
    def _target_identifier(server) -> str | None:
        if not server:
            return None
        return server.base_url[:500]

    @staticmethod
    def _log_attempt(request_id: int, attempt: int, server, result: str, retry: bool, status: int | None = None, reason: str | None = None) -> None:
        payload = {
            "event": "server_attempt",
            "request_id": request_id,
            "attempt": attempt,
            "server_id": server.id,
            "base_url": server.base_url,
            "model_id": server.model_id,
            "result": result,
            "retry": retry,
        }
        if status is not None:
            payload["status"] = status
        if reason:
            payload["reason"] = reason[:500]
        append_request_log(request_id, json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _maybe_log_multi_server_route(request_id: int, attempted_server_ids: set[int], final_server_id: int | None) -> None:
        if len(attempted_server_ids) <= 1:
            return
        payload = {
            "event": "multi_server_route",
            "request_id": request_id,
            "server_ids": sorted(attempted_server_ids),
            "final_server_id": final_server_id,
            "reason": "retried_after_failure",
        }
        append_request_log(request_id, json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _log_error_detail(request_id: int, method: str, url: str, headers: dict, body: bytes, status_code: int, response_body: bytes) -> None:
        try:
            req_body_str = body.decode("utf-8") if body else ""
        except (UnicodeDecodeError, AttributeError):
            req_body_str = repr(body)[:2000]
        try:
            resp_body_str = response_body.decode("utf-8") if response_body else ""
        except (UnicodeDecodeError, AttributeError):
            resp_body_str = repr(response_body)[:2000]
        safe_headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "csb-token")}
        log_entry = json.dumps({
            "event": "upstream_error",
            "request_id": request_id,
            "method": method,
            "url": url,
            "request_headers": safe_headers,
            "request_body": req_body_str[:5000],
            "response_status": status_code,
            "response_body": resp_body_str[:5000],
        }, ensure_ascii=False)
        append_error_log(request_id, log_entry)

    def _perform_request(self, django_request, server, upstream_url, headers, body, is_stream, upstream_client):
        if is_stream:
            return self._handle_stream(django_request, server, upstream_url, headers, body)
        return self._handle_normal(django_request, server, upstream_url, headers, body, upstream_client)

    def _check_context_overflow(self, status_code: int, model: Any, fail_reason: str) -> bool:
        if status_code == 400 and model and model.max_context_window:
            return str(model.max_context_window) in fail_reason
        return False

    def _update_body_model(self, body: bytes, model_name: str, disable_thinking: bool = False) -> bytes:
        try:
            body_data = json.loads(body.decode("utf-8"))
            body_data["model"] = model_name
            if disable_thinking:
                self._disable_thinking(body_data)
            return json.dumps(body_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            return body

    @staticmethod
    def _disable_thinking(body_data: dict[str, Any]) -> None:
        chat_template_kwargs = body_data.get("chat_template_kwargs")
        if not isinstance(chat_template_kwargs, dict):
            chat_template_kwargs = {}
        chat_template_kwargs["enable_thinking"] = False
        body_data["chat_template_kwargs"] = chat_template_kwargs
