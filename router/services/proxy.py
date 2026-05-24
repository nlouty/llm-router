from __future__ import annotations

import importlib
import json
import random
import threading
import time

from django.http import HttpResponse, StreamingHttpResponse
import requests

from router.config import APP_CONFIG
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.circuit_breaker import CircuitBreakerService
from router.services.disconnect import DisconnectWatcher
from router.services.opencode import OpencodeVersionService
from router.services.request_logger import append_request_log, append_error_log
from router.services.vip_channel import VIPChannelService
from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser
from router.utils.errors import error_payload, timeout_sse_event
from router.utils.headers import filter_request_headers, filter_response_headers
from router.utils.sse import parse_sse_usage


class ProxyService:
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

    def forward(self, django_request, path: str, parsed, ip_id: int | None, model, user_agent: str | None, is_vip_channel: bool = False):
        headers = filter_request_headers(dict(django_request.headers), django_request.method)
        model_id = model.id if model else None

        candidates, served_as_vip, vip_logs = self._select_candidates(path, model, is_vip_channel)
        user_ip_id = 2 if served_as_vip else 1
        record = RequestRepository.create_processing(
            ip_id, model.id if model else 0, parsed.stream, user_agent, user_ip_id=user_ip_id
        )
        for msg in vip_logs:
            append_request_log(record.id, msg)
        if is_vip_channel:
            append_request_log(record.id, f"Request received on VIP port. served_as_vip={served_as_vip}")

        context = ServerSelectionContext(
            request_id=record.id,
            ip_id=ip_id,
            model_id=model_id,
            model_name=parsed.model_name,
            path=path,
            method=django_request.method,
            is_stream=parsed.stream,
            body=parsed.body,
        )
        if not candidates:
            message = "no online upstream server available"
            RequestRepository.finish(record, 503, message)
            self._maybe_delay_opencode_failure(user_agent, 503)
            return HttpResponse(
                json.dumps(error_payload(message, "service_unavailable")),
                status=503,
                content_type="application/json",
            )

        return self._route_with_retry(
            django_request, path, headers, parsed.body, record, parsed.model_name,
            user_agent, candidates, context, served_as_vip, model, parsed.stream
        )

    def _build_url(self, base_url: str, path: str, query_string: str) -> str:
        url = base_url.rstrip("/") + "/" + path
        if query_string:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query_string}"
        return url

    def _candidates_for_request(self, path: str, model_id: int | None, vip: bool | None = None):
        if path.rstrip("/") == "models" and model_id is None:
            candidates = ServerRepository.list_all_online()
            return [random.choice(candidates)] if candidates else []
        return ServerRepository.list_by_model_id(model_id, vip=vip)

    def _select_candidates(self, path: str, model, is_vip_channel: bool) -> tuple[list[Server], bool, list[str]]:
        model_id = model.id if model else None
        if path.rstrip("/") == "models" and model_id is None:
            return self._candidates_for_request(path, None), False, []
        if is_vip_channel and self.vip_service.is_vip_eligible(model):
            return self.vip_service.select_candidates(model)
        return self._candidates_for_request(path, model_id, vip=False), False, []

    def _after_finish(self, served_as_vip: bool, model) -> None:
        if served_as_vip and model is not None:
            self.vip_service.maybe_scale_down(model)

    def _route_with_retry(self, django_request, path, headers, body, record, model_name, user_agent, candidates, context, served_as_vip, model, is_stream):
        disconnect_event = threading.Event()
        stop_event = threading.Event()
        upstream_client = None
        watcher = None

        if not is_stream:
            upstream_client = CancellableUpstreamRequest()
            tracker = getattr(django_request, "client_disconnect_tracker", None)
            if tracker:
                watcher = DisconnectWatcher(
                    tracker,
                    disconnect_event,
                    stop_event,
                    upstream_client.cancel,
                    self.client_disconnect_check_interval,
                )
                watcher.start()

        attempted_server_ids: set[int] = set()
        attempts = 0
        last_server = None
        last_status = 502
        last_reason = "Bad Gateway"
        try:
            while attempts < min(self.max_attempts_per_request, len(candidates)):
                server = self.chooser.choose(candidates, context, attempted_server_ids)
                if server is None:
                    break
                last_server = server
                attempted_server_ids.add(server.id)
                attempts += 1
                upstream_url = self._build_url(server.base_url, path, django_request.META.get("QUERY_STRING", ""))
                target_pod_ip = self._target_identifier(server)
                
                RequestRepository.record_attempt(
                    record,
                    target_pod_ip,
                    attempts,
                    getattr(context, "prefix_cache", None),
                    getattr(context, "last_match", None),
                )
                self._increment_workload(server)
                workload_handed_off = False
                try:
                    if is_stream:
                        upstream = self._handle_stream(django_request, server, upstream_url, headers, body)
                    else:
                        upstream = self._handle_normal(django_request, server, upstream_url, headers, body, upstream_client)

                    if not is_stream:
                        content = upstream.content
                    if disconnect_event.is_set():
                        return self._client_closed_response(record, served_as_vip, model)

                    status_code = upstream.status_code
                    reason = upstream.reason or ""
                    last_status = status_code
                    last_reason = reason
                    
                    # We no longer retry based on HTTP status codes.
                    retry = False
                    self._log_attempt(record.id, attempts, server, "status", retry, status=status_code)
                    
                    # Mark unhealthy if server returns >= 500 (but skip for 504 timeouts)
                    if status_code >= 500 and status_code != 504:
                        self._mark_unhealthy(server)

                    self._maybe_log_multi_server_route(record.id, attempted_server_ids, server.id)
                    self._maybe_delay_opencode_failure(user_agent, status_code)
                    self._notify_chooser_response(server, context, status_code)

                    if status_code >= 400:
                        if is_stream:
                            content = upstream.content
                            upstream.close()
                        
                        fail_reason = self._extract_fail_reason(content, reason)
                        RequestRepository.finish(record, status_code, fail_reason, 0, 0, target_pod_ip, model.id if model else None, attempt_count=attempts)
                        self._after_finish(served_as_vip, model)
                        self._log_error_detail(record.id, django_request.method, upstream_url, headers, body, status_code, content)
                        
                        response = HttpResponse(content, status=status_code)
                        for key, value in filter_response_headers(dict(upstream.headers)).items():
                            response[key] = value
                        return response

                    if not is_stream:
                        input_tokens, output_tokens, cached_tokens = self._parse_json_usage(content)
                        RequestRepository.finish(record, status_code, reason, input_tokens, output_tokens, target_pod_ip, model.id if model else None, attempt_count=attempts, final_prefix_cache=cached_tokens)
                        self._after_finish(served_as_vip, model)
                        
                        response = HttpResponse(content, status=status_code)
                        for key, value in filter_response_headers(dict(upstream.headers)).items():
                            response[key] = value
                        return response

                    workload_handed_off = True
                    return self._stream_success(django_request, upstream, record, server, model_name, status_code, reason, target_pod_ip, attempts, attempted_server_ids, context, served_as_vip, model)

                except requests.exceptions.ReadTimeout:
                    if disconnect_event.is_set():
                        return self._client_closed_response(record, served_as_vip, model)
                    last_status = 504
                    last_reason = "Gateway Timeout"
                    self._log_attempt(record.id, attempts, server, "read_timeout", False, reason="ReadTimeout")
                    break
                except requests.RequestException as exc:
                    if disconnect_event.is_set():
                        return self._client_closed_response(record, served_as_vip, model)
                    last_status = 502
                    last_reason = "Bad Gateway"
                    retry = attempts < min(self.max_attempts_per_request, len(candidates))
                    self._mark_unhealthy(server)
                    self._log_attempt(record.id, attempts, server, exc.__class__.__name__, retry, reason=str(exc))
                    if retry:
                        continue
                    break
                finally:
                    if not workload_handed_off:
                        self._decrement_workload(server)

            self._maybe_log_multi_server_route(record.id, attempted_server_ids, last_server.id if last_server else None)
            status = 504 if last_status == 504 else 502
            message = "request timeout" if status == 504 else "502 Bad Gateway"
            RequestRepository.finish(record, status, message, target_pod_ip=self._target_identifier(last_server) if last_server else None, attempt_count=attempts)
            self._after_finish(served_as_vip, model)
            error_type = "gateway_timeout_error" if status == 504 else "server_error"
            self._maybe_delay_opencode_failure(user_agent, status)
            return HttpResponse(json.dumps(error_payload(message, error_type)), status=status, content_type="application/json")
        finally:
            stop_event.set()
            if upstream_client:
                upstream_client.close()
            if watcher:
                watcher.join(timeout=0.1)

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

    def _stream_success(self, django_request, upstream, record, server, model_name, status_code, reason, target_pod_ip, attempts, attempted_server_ids, context, served_as_vip, model):
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
                RequestRepository.finish(record, status_code, reason, input_tokens, output_tokens, target_pod_ip, final_model_id, attempt_count=attempts, final_prefix_cache=cached_tokens)
            except requests.exceptions.ReadTimeout:
                yield timeout_sse_event()
                RequestRepository.finish(record, 504, "request timeout, please try again later", target_pod_ip=target_pod_ip, attempt_count=attempts)
            except requests.RequestException:
                message = "502 Bad Gateway"
                payload = error_payload(message, "server_error")
                yield f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                self._mark_unhealthy(server)
                RequestRepository.finish(record, 502, message, target_pod_ip=target_pod_ip, attempt_count=attempts)
            finally:
                upstream.close()
                self._decrement_workload(server)
                self._after_finish(served_as_vip, model)

        response = StreamingHttpResponse(generate(), status=200, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def _client_closed_response(self, record, served_as_vip: bool = False, model=None):
        RequestRepository.finish(record, 499, "Client Closed Request", task_status="agent_disconnected")
        self._after_finish(served_as_vip, model)
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
