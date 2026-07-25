from __future__ import annotations

import json
import threading

from django.core.exceptions import RequestDataTooBig
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from router.config import APP_CONFIG
from router.repositories.ips import IPRepository
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.repositories.whitelist import WhitelistRepository
from router.services.admission import AdmissionService
from router.services.cmdb import CMDBService
from router.services.opencode import OpencodeVersionService
from router.services.parser import RequestParser
from router.services.proxy import ProxyService
from router.utils.errors import error_response


@require_http_methods(["GET"])
def healthy(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "unhealthy"}, status=503)
    return JsonResponse({"status": "healthy"})


_ALLOWED_V1_PATHS = {"chat/completions", "models", "embeddings"}


@csrf_exempt
def proxy(request, path: str):
    normalized_path = path.rstrip("/")
    if normalized_path not in _ALLOWED_V1_PATHS:
        message = f"The endpoint /v1/{normalized_path} is not implemented. Supported endpoints are /v1/chat/completions, /v1/models and /v1/embeddings."
        return error_response(501, message, "not_implemented")
    user_agent = request.headers.get("User-Agent", "")
    client_ip = _client_ip(request)
    is_vip_channel = _is_vip_channel(request)
    ip = None
    model = None
    parsed = None
    try:
        body = request.body
    except RequestDataTooBig:
        message = "Request body exceeds the maximum allowed size"
        RequestRepository.create_blocked(None, 0, None, user_agent, 413, message)
        return error_response(413, message, "request_too_large")
    except Exception:
        return HttpResponse(status=499)

    try:
        ip, created = IPRepository.get_or_create(client_ip)
        if created:
            threading.Thread(target=CMDBService().fetch_and_save_user, args=(client_ip,), daemon=True).start()

        if is_vip_channel and not ip.vip:
            message = _vip_port_closed_message(request)
            RequestRepository.create_blocked(ip.id, 0, None, user_agent, 503, message)
            return error_response(503, message, "service_unavailable")

        admission = AdmissionService()
        permission = admission.check_permission(ip)
        if not permission.allowed:
            message = permission.message or "Access denied, you do not have permission"
            RequestRepository.create_blocked(ip.id, 0, None, user_agent, 403, message)
            return error_response(permission.status_code, message, permission.error_type or "permission_denied")

        blocked, version = OpencodeVersionService.should_block(user_agent)
        if blocked:
            message = f"Your opencode version ({version}) is no longer supported. Please upgrade opencode to latest version."
            RequestRepository.create_blocked(ip.id, 0, None, user_agent, 403, message)
            return error_response(403, message, "version_too_old")

        parser = RequestParser(int(APP_CONFIG.get("proxy", {}).get("default_max_tokens", 18528)))
        parsed = parser.parse(body, path, is_vip=is_vip_channel)
        input_model_name = parsed.model_name
        input_is_auto = ModelRepository.is_auto_model_name(input_model_name)
        model = None if input_is_auto else ModelRepository.get_by_name(input_model_name)

        if input_model_name and not input_is_auto and model is None:
            message = f"Model {input_model_name} is not supported."
            RequestRepository.create_blocked(ip.id, 0, parsed.stream, user_agent, 400, message, estimate_tokens=parsed.estimated_full_body_tokens)
            return error_response(400, message, "invalid_request_error")

        if model and model.deprecation and not is_vip_channel:
            message = model.deprecation
            RequestRepository.create_blocked(ip.id, model.id, parsed.stream, user_agent, 400, message, estimate_tokens=parsed.estimated_full_body_tokens)
            return error_response(400, message, "invalid_request_error")

        max_token_check = admission.check_max_tokens(parsed.max_tokens, model)
        if not max_token_check.allowed:
            message = max_token_check.message or "invalid request"
            RequestRepository.create_blocked(ip.id, model.id if model else 0, parsed.stream, user_agent, 400, message, estimate_tokens=parsed.estimated_full_body_tokens)
            return error_response(max_token_check.status_code, message, max_token_check.error_type or "invalid_request_error")

        if not is_vip_channel:
            concurrency = admission.check_concurrency(
                ip,
                model,
                is_auto=input_is_auto or ModelRepository.should_auto_select(model),
            )
            if not concurrency.allowed:
                message = concurrency.message or "concurrent limit exceeded"
                RequestRepository.create_blocked(ip.id, model.id if model else 0, parsed.stream, user_agent, 429, message, estimate_tokens=parsed.estimated_full_body_tokens)
                return error_response(concurrency.status_code, message, concurrency.error_type or "concurrent_limit_exceeded")

        return ProxyService().forward(request, path, parsed, ip.id, model, user_agent, is_vip_channel=is_vip_channel)
    except Exception:
        message = "502 Bad Gateway"
        model_id = model.id if model else 0
        ip_id = ip.id if ip else None
        is_stream = parsed.stream if parsed else None
        estimate_tokens = parsed.estimated_full_body_tokens if 'parsed' in locals() and parsed else 0
        RequestRepository.create_blocked(ip_id, model_id, is_stream, user_agent, 502, message, estimate_tokens=estimate_tokens)
        return error_response(502, message, "server_error")


@csrf_exempt
@require_http_methods(["POST"])
def whitelist_update(request):
    data = _request_data(request)
    employee_no = str(data.get("employee_no", "")).strip()
    user_name = data.get("user_name")
    if user_name is not None:
        user_name = str(user_name).strip()
    try:
        is_allowed = int(data.get("is_allowed"))
    except (TypeError, ValueError):
        return JsonResponse({"code": 400, "error": "is_allowed must be 0 or 1"}, status=400)
    if not employee_no or is_allowed not in (0, 1):
        return JsonResponse({"code": 400, "error": "employee_no and is_allowed are required"}, status=400)
    row, created, changed = WhitelistRepository.upsert(employee_no, is_allowed, user_name)
    message = "创建成功" if created else ("更新成功" if changed else "本次修改未生效")
    return JsonResponse(
        {
            "code": 200,
            "message": message,
            "data": {
                "employee_no": row.employee_no,
                "user_name": row.user_name,
                "is_allowed": row.is_allowed,
                "update_time": timezone.localtime(row.update_time).strftime("%Y-%m-%d %H:%M:%S") if row.update_time else None,
            },
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def refresh_user_info(request):
    if not APP_CONFIG.get("cmdb", {}).get("enabled", False):
        return JsonResponse({"code": 403, "error": "CMDB is not enabled"}, status=403)
    threading.Thread(target=CMDBService().fetch_all_users, daemon=True).start()
    return JsonResponse({"code": 200, "message": "用户信息刷新任务已启动"})


def _request_data(request) -> dict:
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST.dict()


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _is_vip_channel(request) -> bool:
    vip_port = _configured_vip_port()
    if vip_port is None:
        return False
    server_port = _request_port(request)
    if server_port is None:
        return False
    return server_port == vip_port


def _request_port(request) -> int | None:
    server_port = request.META.get("SERVER_PORT")
    if server_port is None:
        return None
    try:
        return int(server_port)
    except (TypeError, ValueError):
        return None


def _configured_vip_port() -> int | None:
    try:
        return int(APP_CONFIG.get("server", {}).get("vip_port", 8008))
    except (TypeError, ValueError):
        return None


def _configured_normal_port() -> int | None:
    server_config = APP_CONFIG.get("server", {})
    try:
        return int(server_config["normal_port"])
    except (KeyError, TypeError, ValueError):
        return _parse_bind_port(server_config.get("bind"))


def _parse_bind_port(bind: object) -> int | None:
    if not bind:
        return None
    bind_text = str(bind).strip()
    if bind_text.isdigit():
        return int(bind_text)
    if ":" not in bind_text:
        return None
    port_text = bind_text.rsplit(":", 1)[1].strip()
    try:
        return int(port_text)
    except (TypeError, ValueError):
        return None


def _vip_port_closed_message(request) -> str:
    vip_port = _request_port(request) or _configured_vip_port()
    normal_port = _configured_normal_port()
    return f"Port {vip_port} is closed, please use port {normal_port}"
