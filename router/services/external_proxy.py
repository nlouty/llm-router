from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from types import SimpleNamespace

import requests
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone

from router.config import APP_CONFIG
from router.repositories.external import ExternalRouteRepository
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.route_algorithm.auto import AutoRouteAlgorithm
from router.services import proxy_response
from router.services.cancellable_upstream import CancellableUpstreamRequest
from router.services.disconnect import DisconnectWatcher
from router.services.request_context import clear_request_id, set_request_id
from router.services.request_logger import (
    append_request_log,
    append_verbose_request_log,
    flush_request_log,
)
from router.utils.errors import error_payload, error_response, timeout_sse_event
from router.utils.headers import build_upstream_headers, filter_request_headers
from router.utils.session import extract_session_id

logger = logging.getLogger(__name__)


@dataclass
class _DisconnectScope:
    disconnect_event: threading.Event
    stop_event: threading.Event
    upstream_client: CancellableUpstreamRequest | None = None
    watcher: DisconnectWatcher | None = None


class ExternalProxyService:
    """Forward a request to an external provider (issue #287).

    Records the request, rewrites the body's ``model`` to the provider's name,
    swaps the client credential for the employee's provider API key, and
    streams/passes through the response. Deliberately none of the internal
    machinery — no workload, retries, PD, VIP pool, or prefix cache: a single
    attempt against one provider. A provider failure (transport error or
    HTTP >= 500) counts toward the provider circuit; once it opens, routing
    falls back to the internal pipeline.
    """

    def __init__(self):
        proxy_config = APP_CONFIG.get("proxy", {})
        cb_config = APP_CONFIG.get("load_balancer", {}).get("circuit_breaker", {})
        self.stream_timeout = (
            float(proxy_config.get("stream_connect_timeout_seconds", 30)),
            float(proxy_config.get("stream_read_timeout_seconds", 900)),
        )
        self.normal_timeout = (
            float(proxy_config.get("normal_connect_timeout_seconds", 5)),
            float(proxy_config.get("normal_read_timeout_seconds", 900)),
        )
        self.stream_total_timeout = float(proxy_config.get("stream_total_timeout_seconds", 900))
        self.client_disconnect_check_interval = float(
            proxy_config.get("client_disconnect_check_interval_seconds", 0.5)
        )
        self.failure_threshold = int(cb_config.get("failure_threshold", 3))
        self.base_cooldown_seconds = int(cb_config.get("base_cooldown_seconds", 30))
        self.max_cooldown_seconds = int(cb_config.get("max_cooldown_seconds", 3000))

    def forward(self, django_request, path: str, parsed, ip_id: int | None, route, mapping, user_agent: str | None, user_ip_id: int = 0):
        """Hook-1 entry: a concrete mapped model name intercepted in views.proxy
        before internal model validation. Creates the processing record, then
        sends."""
        normalized = path.rstrip("/")
        session = extract_session_id(dict(django_request.headers)) if normalized == "chat/completions" else None
        model = ModelRepository.get_by_name(mapping.internal_model_name)
        record = RequestRepository.create_processing(
            ip_id,
            model.id if model else 0,
            parsed.stream,
            user_agent,
            user_ip_id=user_ip_id,
            estimate_tokens=parsed.estimated_full_body_tokens,
            session=session,
        )
        append_verbose_request_log(record.id, django_request.body)
        set_request_id(record.id)
        try:
            append_request_log(record.id, json.dumps({
                "event": "request_received",
                "request_id": record.id,
                "path": f"/v1/{normalized}",
                "model": parsed.model_name,
                "stream": bool(parsed.stream),
                "external": True,
                "user_ip_id": user_ip_id,
            }, ensure_ascii=False))
            headers = filter_request_headers(dict(django_request.headers), django_request.method)
            return self._send(django_request, path, headers, record, parsed, route, mapping)
        except Exception as exc:
            # Finish the record this method created (exactly one row per
            # request) instead of propagating to views.py's catch-all, which
            # would create a second row and orphan this one.
            return self._finish_unhandled(record, exc)
        finally:
            flush_request_log(record.id)
            clear_request_id()

    def forward_resolved(self, django_request, path: str, headers, record, parsed, route, mapping):
        """Hook-2 entry: auto routing already resolved the final model and the
        processing record exists. Rewrites the body to the provider name and
        sends; the caller (ProxyService.forward) owns the request-log scope."""
        try:
            return self._send(django_request, path, headers, record, parsed, route, mapping)
        except Exception as exc:
            return self._finish_unhandled(record, exc)

    def _send(self, django_request, path, headers, record, parsed, route, mapping):
        router_result = f"external:{route.name}:{mapping.internal_model_name}"[:300]
        # The "external:"-first prefix matters: AdmissionService buckets
        # in-flight rows by the router_result prefix (before the first ":"),
        # so any other format would count external requests toward an
        # internal model's (or the auto) concurrency bucket.
        parsed.body, new_data = AutoRouteAlgorithm._rewrite_body_data(
            getattr(parsed, "data", None), parsed.body, mapping.external_model_name, False
        )
        if new_data is not None:
            parsed.data = new_data
        context = SimpleNamespace(router_result=router_result, body=parsed.body)
        target_pod_ip = route.base_url[:500]
        # Persist the routing decision before the send: an in-flight external
        # row must never have a NULL router_result (NULL falls back to
        # model_id matching in the concurrency bucketing).
        record.router_result = router_result
        record.target_pod_ip = target_pod_ip
        record.attempt_count = 1
        record.save(update_fields=["router_result", "target_pod_ip", "attempt_count"])
        if record.model_choosing_latency is None:
            RequestRepository.record_model_choosing_latency(
                record,
                int((timezone.now() - record.send_time).total_seconds() * 1000),
            )
        append_request_log(record.id, json.dumps({
            "event": "external_route",
            "request_id": record.id,
            "provider": route.name,
            "base_url": route.base_url,
            "internal_model": mapping.internal_model_name,
            "external_model": mapping.external_model_name,
            "stream": bool(parsed.stream),
        }, ensure_ascii=False))

        upstream_url = self._build_url(route.base_url, path, django_request.META.get("QUERY_STRING", ""))
        disconnect_scope = self._open_disconnect_scope(django_request, bool(parsed.stream))
        try:
            upstream = self._perform_request(
                django_request, route, upstream_url, headers, parsed.body,
                bool(parsed.stream), disconnect_scope.upstream_client,
            )
            if disconnect_scope.disconnect_event.is_set():
                proxy_response.finish_client_closed(record)
                return HttpResponse(status=499)

            status_code = upstream.status_code
            reason = upstream.reason or ""
            if status_code >= 400:
                return self._upstream_error(upstream, record, route, context, status_code, reason, target_pod_ip, bool(parsed.stream))
            if not parsed.stream:
                content = upstream.content
                self._record_success(route)
                proxy_response.finish_normal_success(
                    record, content, None, context, status_code, reason, target_pod_ip, 1
                )
                return proxy_response.response_from_upstream(upstream, content, status_code)
            return self._stream_success(django_request, upstream, record, route, context, status_code, reason, target_pod_ip)
        except requests.exceptions.ReadTimeout:
            if disconnect_scope.disconnect_event.is_set():
                proxy_response.finish_client_closed(record)
                return HttpResponse(status=499)
            self._record_failure(route)
            append_request_log(record.id, json.dumps({
                "event": "external_read_timeout", "provider": route.name,
            }, ensure_ascii=False))
            proxy_response.finish_retry_failure(record, 504, "request timeout", target_pod_ip, 1, context)
            return error_response(504, "request timeout", "gateway_timeout_error")
        except requests.RequestException as exc:
            if disconnect_scope.disconnect_event.is_set():
                proxy_response.finish_client_closed(record)
                return HttpResponse(status=499)
            self._record_failure(route)
            append_request_log(record.id, json.dumps({
                "event": "external_upstream_exception",
                "provider": route.name,
                "error": type(exc).__name__,
                "reason": str(exc)[:200],
            }, ensure_ascii=False))
            fail_reason = f"external provider unreachable: {exc}"[:200]
            proxy_response.finish_retry_failure(record, 502, fail_reason, target_pod_ip, 1, context)
            return error_response(502, "502 Bad Gateway", "server_error")
        finally:
            self._close_disconnect_scope(disconnect_scope)

    def _upstream_error(self, upstream, record, route, context, status_code, reason, target_pod_ip, is_stream: bool):
        if is_stream:
            # Drain the error body and close defensively (same rationale as
            # ProxyService._handle_upstream_error): a broken connection must
            # not skip finish_upstream_error and orphan the record.
            try:
                content = upstream.content
            except Exception:
                content = b""
            try:
                upstream.close()
            except Exception:
                pass
        else:
            content = upstream.content
        # Only provider-side failures (5xx) count toward the circuit: a 4xx
        # (e.g. a bad per-employee key) is the client's problem and must not
        # open the circuit for colleagues sharing the provider.
        if status_code >= 500:
            self._record_failure(route)
        fail_reason = proxy_response.extract_fail_reason(content, reason)
        proxy_response.finish_upstream_error(record, status_code, fail_reason, target_pod_ip, None, 1, context)
        append_request_log(record.id, json.dumps({
            "event": "external_upstream_error",
            "provider": route.name,
            "status": status_code,
            "fail_reason": fail_reason[:200],
        }, ensure_ascii=False))
        return proxy_response.response_from_upstream(upstream, content, status_code)

    def _stream_success(self, django_request, upstream, record, route, context, status_code, reason, target_pod_ip):
        request_start = time.monotonic()

        def generate():
            chunks: list[bytes] = []
            first_chunk_at = None
            try:
                deadline = request_start + self.stream_total_timeout
                for chunk in upstream.iter_content(chunk_size=8192):
                    if time.monotonic() > deadline:
                        self._record_failure(route)
                        yield timeout_sse_event()
                        proxy_response.finish_stream_total_timeout(record, target_pod_ip, 1)
                        return
                    tracker = getattr(django_request, "client_disconnect_tracker", None)
                    if tracker and tracker.client_disconnected():
                        proxy_response.finish_stream_client_disconnected(record, target_pod_ip, 1)
                        return
                    if chunk:
                        if first_chunk_at is None:
                            first_chunk_at = timezone.now()
                        chunks.append(chunk)
                        yield chunk
                # Circuit success only on full completion, mirroring
                # ProxyService: a mid-stream timeout/disconnect is a failure.
                self._record_success(route)
                ttft = (
                    int((first_chunk_at - record.send_time).total_seconds() * 1000)
                    if first_chunk_at is not None
                    else None
                )
                # model_name=None so ensure_model_after_success cannot create a
                # models row named after the provider's model.
                proxy_response.finish_stream_success(
                    record, status_code, reason, chunks, target_pod_ip, None, 1, context, ttft
                )
            except requests.exceptions.ReadTimeout:
                self._record_failure(route)
                yield timeout_sse_event()
                proxy_response.finish_stream_read_timeout(record, target_pod_ip, 1, None, context)
            except requests.RequestException:
                payload = error_payload("502 Bad Gateway", "server_error")
                yield f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                self._record_failure(route)
                proxy_response.finish_stream_request_exception(record, "502 Bad Gateway", target_pod_ip, 1, None, context)
            except Exception as exc:
                fail_reason = f"unhandled external stream {type(exc).__name__}: {exc}"[:200]
                logger.error(
                    "external stream unhandled %s request_id=%s: %s",
                    type(exc).__name__, record.id, str(exc)[:200],
                )
                try:
                    RequestRepository.finish(record, 502, fail_reason)
                except Exception:
                    logger.exception("failed to finish external streaming record %s", record.id)
                try:
                    yield f"data: {json.dumps(error_payload('502 Bad Gateway', 'server_error'))}\n\ndata: [DONE]\n\n".encode("utf-8")
                except Exception:
                    pass
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
                flush_request_log(record.id)

        response = StreamingHttpResponse(generate(), status=200, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def _perform_request(self, django_request, route, upstream_url, headers, body, is_stream, upstream_client):
        # No csb_token on external routes; build_upstream_headers reads it via
        # getattr, so its absence simply means no injection.
        req_headers = build_upstream_headers(headers, SimpleNamespace(api_key=route.api_key))
        if is_stream:
            return requests.request(
                django_request.method,
                upstream_url,
                headers=req_headers,
                data=body,
                stream=True,
                timeout=self.stream_timeout,
            )
        return upstream_client.request(
            django_request.method,
            upstream_url,
            headers=req_headers,
            data=body if django_request.method.upper() not in {"GET", "HEAD"} else None,
            timeout=self.normal_timeout,
        )

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

    @staticmethod
    def _build_url(base_url: str, path: str, query_string: str) -> str:
        url = base_url.rstrip("/") + "/" + path
        if query_string:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query_string}"
        return url

    def _record_failure(self, route) -> None:
        ExternalRouteRepository.record_failure(
            route,
            failure_threshold=self.failure_threshold,
            base_cooldown_seconds=self.base_cooldown_seconds,
            max_cooldown_seconds=self.max_cooldown_seconds,
        )

    def _record_success(self, route) -> None:
        ExternalRouteRepository.record_success(route, base_cooldown_seconds=self.base_cooldown_seconds)

    def _finish_unhandled(self, record, exc: BaseException):
        fail_reason = f"unhandled {type(exc).__name__}: {exc}"[:200]
        logger.error("external proxy unhandled %s request_id=%s: %s", type(exc).__name__, record.id, str(exc)[:200])
        try:
            append_request_log(
                record.id,
                f"external proxy unhandled exception: {fail_reason}\n{traceback.format_exc()}",
            )
        except Exception:
            pass
        try:
            RequestRepository.finish(record, 502, fail_reason)
        except Exception:
            logger.exception("failed to finish external record %s after unhandled exception", record.id)
        return error_response(502, "502 Bad Gateway", "server_error")
