from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

import requests as http_requests
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from router.api.stats import (
    APIValidationError,
    BEIJING_TZ,
    TIME_FORMAT,
    bucket_expression,
    bucket_labels,
    bucket_start,
    choose_granularity,
    fill_series,
    format_bucket,
    latency_boxplot,
    parse_time_range,
)
from router.models import Model, Server, ServerOperation
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.repositories.user_ips import UserIPRepository
from router.repositories.whitelist import WhitelistRepository
from router.services.admission import AdmissionService
from router.services.cmdb import CMDBService

DOWNLOAD_FILE_PATH = Path("/home/AI_Assistant/AI_Assistant.exe")
logger = logging.getLogger(__name__)


@require_http_methods(["POST"])
def register_apikey(request):
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("invalid JSON body")

    if not isinstance(data, dict):
        return _bad_request("JSON body must be an object")

    apikey = data.get("apikey")
    employee_no = data.get("employee_no")
    if not isinstance(apikey, str) or not apikey.strip():
        return _bad_request("apikey is required")
    if not isinstance(employee_no, str) or not employee_no.strip():
        return _bad_request("employee_no is required")

    apikey = apikey.strip()
    employee_no = employee_no.strip()
    if len(apikey) > 255:
        return _bad_request("apikey must be at most 255 characters")
    if len(employee_no) > 50:
        return _bad_request("employee_no must be at most 50 characters")

    if UserIPRepository.get_active_apikey_by_employee_no(employee_no):
        return JsonResponse(
            {"code": 409, "error": "employee already has an active apikey"}, status=409
        )

    # Whitelisted employees bypass CMDB: CMDB sometimes fails to resolve a
    # department, and a whitelist entry (while unexpired) is enough to trust
    # the employee. Insert the row directly with department_id = 0 before the
    # CMDB sync runs; the sync overwrites the department once CMDB can
    # resolve one (rows without CMDB data are skipped there).
    whitelist_row = WhitelistRepository.get_active_by_employee_no(employee_no)
    if whitelist_row is not None:
        UserIPRepository.create_or_update_apikey(
            apikey,
            employee_no,
            user_name=whitelist_row.user_name,
            department_id=0,
        )
        return JsonResponse(
            {
                "code": 200,
                "message": "success",
                "data": {
                    "employee_no": employee_no,
                },
            }
        )

    try:
        CMDBService().fetch_and_save_apikey(apikey, employee_no)
    except NotImplementedError:
        return JsonResponse({"code": 404, "error": "API key registration is not implemented"}, status=404)
    except LookupError:
        return JsonResponse({"code": 404, "error": "employee_no not found"}, status=404)
    except Exception:
        logger.exception("CMDB API key registration failed for employee %s", employee_no)
        return JsonResponse({"code": 502, "error": "CMDB API key registration failed"}, status=502)

    # CMDB only reports the department; whether that department may use the
    # router is decided here. A denied department with no whitelist entry
    # leaves the key stored but invalid (refused by the proxy).
    user_ip = UserIPRepository.get_active_apikey_by_employee_no(employee_no)
    if user_ip is None:
        logger.error("CMDB registered no apikey row for employee %s", employee_no)
        return JsonResponse({"code": 502, "error": "CMDB API key registration failed"}, status=502)

    if not AdmissionService.verify_apikey_registration(user_ip):
        UserIPRepository.deactivate_apikey_by_employee_no(employee_no)
        return JsonResponse(
            {"code": 403, "error": "department is not allowed and employee is not whitelisted"},
            status=403,
        )

    return JsonResponse(
        {
            "code": 200,
            "message": "success",
            "data": {
                "employee_no": employee_no,
            },
        }
    )


@require_http_methods(["GET", "POST"])
def apikey(request):
    if request.method == "POST":
        return register_apikey(request)
    return get_apikey(request)


def _mask_apikey(apikey: str) -> str:
    if len(apikey) <= 8:
        return ""
    return f"{apikey[:4]}…{apikey[-4:]}"


def _apikey_info(user_ip) -> dict:
    return {
        "employee_no": user_ip.employee_no,
        "apikey_preview": _mask_apikey(user_ip.apikey),
        "is_valid": user_ip.is_valid,
        "created_at": user_ip.created_at.isoformat() if user_ip.created_at else None,
        "updated_at": user_ip.updated_at.isoformat() if user_ip.updated_at else None,
    }


@require_http_methods(["GET"])
def get_apikey(request):
    employee_no = request.GET.get("employee_no")
    if not employee_no or not employee_no.strip():
        return _bad_request("employee_no is required")
    employee_no = employee_no.strip()

    user_ip = UserIPRepository.get_active_apikey_by_employee_no(employee_no)
    if user_ip is None:
        return JsonResponse({"code": 404, "error": "apikey not found for employee"}, status=404)

    return JsonResponse({"code": 200, "message": "success", "data": _apikey_info(user_ip)})


@require_http_methods(["POST"])
def invalidate_apikey(request):
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("invalid JSON body")

    if not isinstance(data, dict):
        return _bad_request("JSON body must be an object")

    employee_no = data.get("employee_no")
    if not isinstance(employee_no, str) or not employee_no.strip():
        return _bad_request("employee_no is required")
    employee_no = employee_no.strip()
    if len(employee_no) > 50:
        return _bad_request("employee_no must be at most 50 characters")

    if not UserIPRepository.invalidate_apikey_by_employee_no(employee_no):
        return JsonResponse({"code": 404, "error": "apikey not found for employee"}, status=404)

    return JsonResponse(
        {
            "code": 200,
            "message": "success",
            "data": {
                "employee_no": employee_no,
            },
        }
    )


@require_http_methods(["GET"])
def request_stats(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed
    return JsonResponse({"code": 200, "total_count": RequestRepository.count_distinct_ips(start, end)})


@require_http_methods(["GET"])
def total_request_count(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed
    return JsonResponse({"code": 200, "total_count": RequestRepository.count_success_requests(start, end)})


@require_http_methods(["GET"])
def input_token(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # Handle model_name parameter
    model_name = request.GET.get("model_name")
    if model_name is not None and not model_name.strip():
        return _bad_request("model_name cannot be blank")

    # Determine model_id filter
    model_id = None
    model_obj = None
    if model_name and model_name.strip().lower() != "total":
        model_obj = _model_or_error(model_name.strip())
        if isinstance(model_obj, JsonResponse):
            return model_obj
        model_id = model_obj.id

    # Determine granularity based on time range
    granularity = choose_granularity(start, end)

    # Return bucketed data based on granularity
    labels = bucket_labels(start, end, granularity)
    rows = RequestRepository.sum_input_tokens_by_bucket(
        start, end, bucket_expression("send_time", granularity), model_id
    )
    stats = []
    for label in labels:
        bucket_data = rows.get(label)
        if bucket_data:
            stats.append({
                "time": label,
                "total_input_tokens": bucket_data["total_input"],
                "cache_hit": bucket_data["cache_hit"],
                "cache_miss": bucket_data["cache_miss"]
            })
        else:
            stats.append({
                "time": label,
                "total_input_tokens": 0,
                "cache_hit": 0,
                "cache_miss": 0
            })
    response = {"code": 200, "stats": stats}
    if model_obj:
        response["model_name"] = model_obj.model_name
    return JsonResponse(response)


@require_http_methods(["GET"])
def output_token(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # Handle model_name parameter
    model_name = request.GET.get("model_name")
    if model_name is not None and not model_name.strip():
        return _bad_request("model_name cannot be blank")

    # Determine granularity
    granularity = choose_granularity(start, end)

    # Determine model_id filter
    model_id = None
    model_obj = None
    if model_name and model_name.strip().lower() != "total":
        model_obj = _model_or_error(model_name.strip())
        if isinstance(model_obj, JsonResponse):
            return model_obj
        model_id = model_obj.id

    # Return bucketed data based on granularity
    labels = bucket_labels(start, end, granularity)
    values = RequestRepository.sum_output_tokens_by_bucket(
        start, end, bucket_expression("send_time", granularity), model_id
    )
    response = {"code": 200, "stats": fill_series(labels, values, "total_output_tokens", 0)}
    
    if model_obj:
        response["model_name"] = model_obj.model_name
    
    return JsonResponse(response)


@require_http_methods(["GET"])
def token_summary(request):
    """
    Token总览接口 - 返回指定时间范围和模型的token统计摘要。

    查询参数：
    - start_time: 开始时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - model_name: 模型名称（可选，不传或传"total"表示所有模型）

    返回：
    时间跨度<=2天时返回单个聚合值：
    - input_token: 输入token总数
    - output_token: 输出token总数
    - cache_hit: 缓存命中数
    - cache_miss: 缓存未命中数
    - hit_rate: 命中率（百分比）

    时间跨度>2天时返回分组数据（每天或每月）：
    - stats: 数组，每项包含 date, input_token, output_token, cache_hit, cache_miss, hit_rate
    """
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # Handle model_name parameter
    model_name = request.GET.get("model_name")
    if model_name is not None and not model_name.strip():
        return _bad_request("model_name cannot be blank")

    # Determine model_id filter
    model_id = None
    model_obj = None
    if model_name and model_name.strip().lower() != "total":
        model_obj = _model_or_error(model_name.strip())
        if isinstance(model_obj, JsonResponse):
            return model_obj
        model_id = model_obj.id

    # Determine granularity based on time range
    granularity = choose_granularity(start, end)

    # If granularity is "hour" (date range <= 2 days), return single aggregate
    if granularity == "hour":
        # Get token statistics
        input_stats = RequestRepository.sum_input_tokens(start, end, model_id)
        output_tokens = RequestRepository.sum_output_tokens(start, end, model_id)

        # Calculate hit rate
        total_input = input_stats["total_input"]
        cache_hit = input_stats["cache_hit"]
        hit_rate = round((cache_hit / total_input * 100), 2) if total_input > 0 else 0.0

        response = {
            "code": 200,
            "input_token": total_input,
            "output_token": output_tokens,
            "cache_hit": cache_hit,
            "cache_miss": input_stats["cache_miss"],
            "hit_rate": hit_rate
        }

        if model_obj:
            response["model_name"] = model_obj.model_name

        return JsonResponse(response)

    # Otherwise (time span > 2 days), return bucketed data
    labels = bucket_labels(start, end, granularity)

    # Get input token stats by bucket
    input_rows = RequestRepository.sum_input_tokens_by_bucket(
        start, end, bucket_expression("send_time", granularity), model_id
    )

    # Get output token stats by bucket
    output_rows = RequestRepository.sum_output_tokens_by_bucket(
        start, end, bucket_expression("send_time", granularity), model_id
    )

    # Merge data and calculate hit rates
    stats = []
    for label in labels:
        input_data = input_rows.get(label, {"total_input": 0, "cache_hit": 0, "cache_miss": 0})
        output_count = output_rows.get(label, 0)

        total_input = input_data["total_input"]
        cache_hit = input_data["cache_hit"]
        cache_miss = input_data["cache_miss"]
        hit_rate = round((cache_hit / total_input * 100), 2) if total_input > 0 else 0.0

        stats.append({
            "date": label,
            "input_token": total_input,
            "output_token": output_count,
            "cache_hit": cache_hit,
            "cache_miss": cache_miss,
            "hit_rate": hit_rate
        })

    response = {"code": 200, "stats": stats}
    if model_obj:
        response["model_name"] = model_obj.model_name

    return JsonResponse(response)


@require_http_methods(["GET"])
def model_request_stats(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    model = _model_or_error(request.GET.get("model_name"))
    if isinstance(model, JsonResponse):
        return model
    start, end = parsed
    return JsonResponse(
        {
            "code": 200,
            "model_id": model.id,
            "total_count": RequestRepository.count_success_requests_by_model(start, end, model.id),
        }
    )


@require_http_methods(["GET"])
def all_model_request_stats(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed
    model_name = request.GET.get("model_name")
    if model_name is not None and not model_name.strip():
        return _bad_request("model_name cannot be blank")
    if model_name:
        model = _model_or_error(model_name)
        if isinstance(model, JsonResponse):
            return model
        models = [model]
    else:
        models = ModelRepository.list_all()
    counts = RequestRepository.count_success_requests_grouped_by_model(start, end, [model.id for model in models])
    return JsonResponse(
        {
            "code": 200,
            "data": [{"model_name": model.model_name, "total_count": counts.get(model.id, 0)} for model in models],
        }
    )


@require_http_methods(["GET"])
def models(request):
    return JsonResponse(
        {
            "code": 200,
            "data": [
                {"id": model.id, "model_name": model.model_name, "concurrent_limit": model.concurrent_limit}
                for model in ModelRepository.list_all()
            ],
        }
    )


@require_http_methods(["GET"])
def model_online_list(request):
    return JsonResponse(
        {
            "code": 200,
            "data": [model.model_name for model in ModelRepository.list_online()],
        }
    )


@require_http_methods(["GET"])
def model_info(request):
    model = _model_or_error(request.GET.get("model_name"))
    if isinstance(model, JsonResponse):
        return model
    return JsonResponse(
        {
            "code": 200,
            "data": {"model_name": model.model_name, "concurrent_limit": model.concurrent_limit},
        }
    )


@require_http_methods(["GET"])
def request_time_stats(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed
    return JsonResponse({"code": 200, "stats": _average_latency_stats(start, end)})


@require_http_methods(["GET"])
def model_request_time_stats(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    model = _model_or_error(request.GET.get("model_name"))
    if isinstance(model, JsonResponse):
        return model
    start, end = parsed
    return JsonResponse({"code": 200, "stats": _average_latency_stats(start, end, model.id)})


@require_http_methods(["GET"])
def model_request_count_by_period(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # Handle model_name parameter (optional)
    model_name = request.GET.get("model_name")
    model_id = None
    model_obj = None
    
    if model_name and model_name.strip().lower() != "total":
        model_obj = _model_or_error(model_name.strip())
        if isinstance(model_obj, JsonResponse):
            return model_obj
        model_id = model_obj.id

    granularity = choose_granularity(start, end)
    labels = bucket_labels(start, end, granularity)
    rows = RequestRepository.count_success_by_bucket(start, end, model_id, bucket_expression("send_time", granularity))
    values = {format_bucket(bucket, granularity): count for bucket, count in rows.items()}
    response = {"code": 200, "stats": fill_series(labels, values, "count", 0)}
    
    if model_obj:
        response["model_name"] = model_obj.model_name
    
    return JsonResponse(response)


@require_http_methods(["GET"])
def model_ip_count_by_period(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # Handle model_name parameter (optional)
    model_name = request.GET.get("model_name")
    model_id = None
    model_obj = None
    
    if model_name and model_name.strip().lower() != "total":
        model_obj = _model_or_error(model_name.strip())
        if isinstance(model_obj, JsonResponse):
            return model_obj
        model_id = model_obj.id

    granularity = choose_granularity(start, end)
    labels = bucket_labels(start, end, granularity)
    rows = RequestRepository.count_distinct_ips_by_bucket(start, end, model_id, bucket_expression("send_time", granularity))
    values = {format_bucket(bucket, granularity): count for bucket, count in rows.items()}
    response = {"code": 200, "stats": fill_series(labels, values, "count", 0)}
    
    if model_obj:
        response["model_name"] = model_obj.model_name
    
    return JsonResponse(response)


@require_http_methods(["GET"])
def model_latency_boxplot(request):
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed
    models_or_response = _models_for_boxplot(request.GET.get("model_names"))
    if isinstance(models_or_response, JsonResponse):
        return models_or_response
    selected_models = models_or_response
    granularity = choose_granularity(start, end)
    labels = bucket_labels(start, end, granularity)
    model_by_id = {model.id: model for model in selected_models}
    grouped = defaultdict(list)

    for row in RequestRepository.latency_rows_for_boxplot(start, end, list(model_by_id.keys())):
        model = model_by_id.get(row["model_id"])
        if model is None:
            continue
        label = format_bucket(bucket_start(row["send_time"], granularity), granularity)
        grouped[(model.model_name, label)].append(row["latency"])

    model_data = {}
    valid_labels = set()
    for model in selected_models:
        time_labels = []
        boxplot_data = []
        over_threshold_count = []
        over_threshold_ratio = []
        for label in labels:
            summary = latency_boxplot(grouped.get((model.model_name, label), []))
            if summary["sample_count"] == 0:
                continue
            valid_labels.add(label)
            time_labels.append(_short_label(label, granularity))
            boxplot_data.append([summary["min"], summary["q1"], summary["median"], summary["q3"], summary["max"]])
            over_threshold_count.append(summary["over_threshold_count"])
            over_threshold_ratio.append(summary["over_threshold_ratio"])
        model_data[model.model_name] = {
            "time_labels": time_labels,
            "boxplot_data": boxplot_data,
            "over_threshold_count": over_threshold_count,
            "over_threshold_ratio": over_threshold_ratio,
        }

    root_time_labels = [_short_label(label, granularity) for label in labels if label in valid_labels]

    return JsonResponse(
        {
            "code": 200,
            "start_time": start.astimezone(BEIJING_TZ).strftime(TIME_FORMAT),
            "end_time": end.astimezone(BEIJING_TZ).strftime(TIME_FORMAT),
            "time_labels": root_time_labels,
            "model_data": model_data,
        }
    )


@require_http_methods(["GET"])
def download_ai_assistant(request):
    if not DOWNLOAD_FILE_PATH.exists() or not DOWNLOAD_FILE_PATH.is_file():
        return JsonResponse({"code": 404, "error": "file not found"}, status=404)
    return FileResponse(
        DOWNLOAD_FILE_PATH.open("rb"),
        content_type="application/octet-stream",
        as_attachment=True,
        filename="AI_Assistant.exe",
    )


def _time_range_or_error(request):
    try:
        return parse_time_range(request.GET)
    except APIValidationError as exc:
        return _bad_request(str(exc))


def _model_or_error(model_name: str | None):
    if not model_name or not model_name.strip():
        return _bad_request("model_name is required")
    model = ModelRepository.get_by_name(model_name.strip())
    if model is None:
        return JsonResponse({"code": 404, "error": "model_name not found"}, status=404)
    return model


def _models_for_boxplot(model_names: str | None):
    if model_names is None:
        return ModelRepository.list_all()
    names = [name.strip() for name in model_names.split(",") if name.strip()]
    if not names:
        return _bad_request("model_names cannot be blank")
    models = ModelRepository.get_by_names(names)
    missing = [name for name in names if name not in models]
    if missing:
        return JsonResponse({"code": 404, "error": "model_names not found", "missing": missing}, status=404)
    return [models[name] for name in names]


def _short_label(label: str, granularity: str) -> str:
    if granularity == "hour":
        return label[11:16]
    return label


def _average_latency_stats(start, end, model_id: int | None = None):
    granularity = choose_granularity(start, end)
    labels = bucket_labels(start, end, granularity)
    rows = RequestRepository.average_latency_by_bucket(start, end, bucket_expression("send_time", granularity), model_id)
    values = {format_bucket(bucket, granularity): round(value, 2) if value is not None else None for bucket, value in rows.items()}
    return fill_series(labels, values, "avg_duration_ms", None)


@require_http_methods(["POST"])
def add_server(request):
    parsed_payload = _add_server_payload_or_error(request)
    if isinstance(parsed_payload, JsonResponse):
        return parsed_payload
    payload, servers_data = parsed_payload

    if _has_duplicate_base_urls(servers_data):
        return _bad_request("duplicate base_url in request")

    now = timezone.now()
    results = [_process_add_server_item(data, now) for data in servers_data]
    return _add_server_result_response(payload, results)


def _add_server_payload_or_error(request):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    if isinstance(payload, dict):
        return payload, [payload]
    if isinstance(payload, list):
        return payload, payload
    return _bad_request("payload must be a dictionary or a list")


def _has_duplicate_base_urls(servers_data) -> bool:
    base_urls = [server.get("base_url", "").strip() for server in servers_data if isinstance(server, dict)]
    return len(base_urls) != len(set(base_urls))


def _process_add_server_item(data, now):
    if not isinstance(data, dict):
        return {"error": "invalid item in list, must be a dictionary"}

    base_url = data.get("base_url", "").strip()
    model_name = data.get("model_name", "").strip()
    role = (data.get("role") or "mixed").strip()
    group_id = (data.get("group_id") or "").strip() or None
    model_path = (data.get("model_path") or "").strip() or None
    api_key = (data.get("api_key") or "").strip() or None
    operation = _create_add_server_operation(data, now)

    validation_failure = _add_server_validation_failure(base_url, model_name, role, group_id)
    if validation_failure:
        message, result_base_url = validation_failure
        return _fail_add_server_operation(operation, message, result_base_url)

    verify_error = _verify_server_model(base_url, model_name, api_key)
    if verify_error:
        return _fail_add_server_operation(operation, verify_error, base_url)

    return _create_add_server_success(operation, base_url, model_name, role, group_id, model_path, api_key)


def _create_add_server_operation(data, now):
    return ServerOperation.objects.create(
        operation_type="add_server",
        request_data=data,
        status="pending",
        created_at=now,
        updated_at=now,
    )


def _add_server_validation_failure(base_url: str, model_name: str, role: str, group_id: str | None) -> tuple[str, str | None] | None:
    if not base_url:
        return "base_url is required", None
    if not base_url.rstrip("/").endswith("/v1"):
        return "base_url must end with /v1", base_url
    if not model_name:
        return "model_name is required", base_url
    if role not in ("mixed", "prefiller", "prefix-prefiller", "decoder"):
        return f"role must be one of mixed, prefiller, prefix-prefiller, decoder (got {role!r})", base_url
    if role in ("prefiller", "prefix-prefiller", "decoder") and not group_id:
        return "group_id is required for prefiller/prefix-prefiller/decoder servers", base_url
    if Server.objects.filter(base_url=base_url).exists():
        return "base_url already exists", base_url
    return None


def _verify_server_model(base_url: str, model_name: str, api_key: str | None = None) -> str | None:
    verify_url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        resp = http_requests.get(verify_url, timeout=10, headers=headers)
        resp.raise_for_status()
        models_data = resp.json()
        model_ids = [model.get("id", "") for model in models_data.get("data", [])]
        if model_name not in model_ids:
            raise ValueError(f"model '{model_name}' not found in server response, available: {model_ids}")
    except Exception as e:
        return f"failed to reach server at {verify_url}: {e}"
    return None


def _fail_add_server_operation(operation, message: str, base_url: str | None = None):
    operation.status = "failed"
    operation.error_message = message
    operation.updated_at = timezone.now()
    operation.save()

    result = {"error": message}
    if base_url is not None:
        result["base_url"] = base_url
    return result


def _create_add_server_success(operation, base_url: str, model_name: str, role: str = "mixed", group_id: str | None = None, model_path: str | None = None, api_key: str | None = None):
    model_obj, _ = Model.objects.get_or_create(
        model_name=model_name,
        defaults={"model_path": model_path},
    )
    if model_path and model_obj.model_path != model_path:
        model_obj.model_path = model_path
        model_obj.save(update_fields=["model_path"])
    server = Server.objects.create(
        model_id=model_obj.id,
        base_url=base_url,
        role=role,
        group_id=group_id,
        api_key=api_key,
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )

    operation.server_id = server.id
    operation.status = "success"
    operation.response_data = {
        "id": server.id,
        "base_url": server.base_url,
        "model_name": model_name,
        "role": role,
        "group_id": group_id,
        "model_path": model_path,
        "api_key": _mask_apikey(api_key) if api_key else None,
    }
    operation.updated_at = timezone.now()
    operation.save()
    return operation.response_data


def _add_server_result_response(payload, results):
    if isinstance(payload, dict):
        if "error" in results[0]:
            return _bad_request(results[0]["error"])
        return JsonResponse({"code": 200, "data": results[0]})
    return JsonResponse({"code": 200, "data": results})


@require_http_methods(["POST"])
def upsert_mr_live_review(request):
    import json
    from router.models import MrLiveReview

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    discussion_id = data.get("discussion_id")
    if not discussion_id:
        return _bad_request("discussion_id is required")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in MrLiveReview._meta.fields if f.name != "id"}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    state = data.get("state")

    try:
        review = MrLiveReview.objects.get(discussion_id=discussion_id)
        if review.state != state:
            # Update the record if state is different
            for field, value in data.items():
                setattr(review, field, value)
            review.save()
            return JsonResponse({"code": 200, "message": "updated", "data": {"id": review.id}})
        else:
            # Skip if state is the same
            return JsonResponse({"code": 200, "message": "skipped", "data": {"id": review.id}})
    except MrLiveReview.DoesNotExist:
        # Create a new record if discussion_id does not exist
        review = MrLiveReview.objects.create(**data)
        return JsonResponse({"code": 200, "message": "created", "data": {"id": review.id}})


@require_http_methods(["GET"])
def mr_live_review_stats(request):
    from router.repositories.mr_live_review import MrLiveReviewRepository

    project_name = request.GET.get("project_name")
    if not project_name or not project_name.strip():
        return _bad_request("project_name is required")

    rows = MrLiveReviewRepository.count_by_target_branch(project_name.strip())

    branches = []
    total_valid = total_invalid = total_no_reply = 0
    for row in rows:
        valid = row["valid"]
        invalid = row["invalid"]
        no_reply = row["no_reply"]
        total_valid += valid
        total_invalid += invalid
        total_no_reply += no_reply
        branches.append(
            {
                "target_branch": row["target_branch"],
                "valid": valid,
                "invalid": invalid,
                "no_reply": no_reply,
                "accept_rate": _accept_rate(valid, invalid),
            }
        )

    total = {
        "target_branch": "总计",
        "valid": total_valid,
        "invalid": total_invalid,
        "no_reply": total_no_reply,
        "accept_rate": _accept_rate(total_valid, total_invalid),
    }

    return JsonResponse({"code": 200, "data": branches, "total": total})


def _accept_rate(valid: int, invalid: int) -> float:
    denominator = valid + invalid
    if denominator == 0:
        return 0.0
    return round(valid / denominator, 4)


@require_http_methods(["GET"])
def mr_live_review_stats_by_confidence(request):
    from router.repositories.mr_live_review import MrLiveReviewRepository

    project_name = request.GET.get("project_name")
    if not project_name or not project_name.strip():
        return _bad_request("project_name is required")

    rows = MrLiveReviewRepository.count_by_confidence_score(project_name.strip())

    scores = []
    total_valid = total_invalid = total_no_reply = 0
    for row in rows:
        valid = row["valid"]
        invalid = row["invalid"]
        no_reply = row["no_reply"]
        total_valid += valid
        total_invalid += invalid
        total_no_reply += no_reply
        total_count = valid + invalid + no_reply
        scores.append(
            {
                "confidence_score": row["confidence_score"],
                "valid": valid,
                "invalid": invalid,
                "no_reply": no_reply,
                "total": total_count,
                "accept_rate": _accept_rate(valid, invalid),
            }
        )

    total_count = total_valid + total_invalid + total_no_reply
    total = {
        "confidence_score": "总计",
        "valid": total_valid,
        "invalid": total_invalid,
        "no_reply": total_no_reply,
        "total": total_count,
        "accept_rate": _accept_rate(total_valid, total_invalid),
    }

    return JsonResponse({"code": 200, "data": scores, "total": total})


@require_http_methods(["GET"])
def mr_live_review_list(request):
    from router.repositories.mr_live_review import TYPE_FILTERS, MrLiveReviewRepository

    project_name = request.GET.get("project_name")
    if not project_name or not project_name.strip():
        return _bad_request("project_name is required")

    target_branch = request.GET.get("target_branch")
    if not target_branch or not target_branch.strip():
        return _bad_request("target_branch is required")

    review_type = request.GET.get("type")
    if review_type not in TYPE_FILTERS:
        return _bad_request("type must be one of: valid, invalid, no_reply")

    page, page_size, error = _parse_pagination(request)
    if error:
        return _bad_request(error)

    rows, total = MrLiveReviewRepository.list_by_type(
        project_name.strip(), target_branch.strip(), review_type, page, page_size
    )
    return JsonResponse(
        {
            "code": 200,
            "data": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@require_http_methods(["GET"])
def mr_live_review_list_by_confidence(request):
    from router.repositories.mr_live_review import TYPE_FILTERS, MrLiveReviewRepository

    project_name = request.GET.get("project_name")
    if not project_name or not project_name.strip():
        return _bad_request("project_name is required")

    # confidence_score can be empty, None, or a specific value
    confidence_score = request.GET.get("confidence_score")
    if confidence_score is not None:
        confidence_score = confidence_score.strip() if confidence_score.strip() else None

    review_type = request.GET.get("type")
    if review_type not in TYPE_FILTERS:
        return _bad_request("type must be one of: valid, invalid, no_reply")

    page, page_size, error = _parse_pagination(request)
    if error:
        return _bad_request(error)

    rows, total = MrLiveReviewRepository.list_by_type_and_confidence(
        project_name.strip(), confidence_score, review_type, page, page_size
    )
    return JsonResponse(
        {
            "code": 200,
            "data": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@require_http_methods(["GET"])
def mr_live_review_stats_by_date(request):
    from router.api.stats import parse_date_range
    from router.repositories.mr_live_review import MrLiveReviewRepository

    # Validate stats parameter
    stats = request.GET.get("stats")
    valid_stats = ["valid", "invalid", "no_reply", "total", "accept_rate"]
    if not stats or stats not in valid_stats:
        return _bad_request(f"stats must be one of: {', '.join(valid_stats)}")

    # Validate target_branch parameter
    target_branch = request.GET.get("target_branch")
    if not target_branch or not target_branch.strip():
        return _bad_request("target_branch is required")
    target_branch = target_branch.strip()

    # Validate project_name parameter
    project_name = request.GET.get("project_name")
    if not project_name or not project_name.strip():
        return _bad_request("project_name is required")
    project_name = project_name.strip()

    # Parse date range (YYYY-MM-DD format)
    try:
        start_date, end_date = parse_date_range(request.GET)
    except APIValidationError as exc:
        return _bad_request(str(exc))

    # Handle accept_rate separately
    if stats == "accept_rate":
        from datetime import datetime

        valid_data = MrLiveReviewRepository.count_by_date(
            project_name, target_branch, "valid", start_date, end_date
        )
        invalid_data = MrLiveReviewRepository.count_by_date(
            project_name, target_branch, "invalid", start_date, end_date
        )

        # Merge data and calculate accept_rate
        valid_dict = {item["date"]: item["count"] for item in valid_data}
        invalid_dict = {item["date"]: item["count"] for item in invalid_data}

        all_dates = sorted(set(valid_dict.keys()) | set(invalid_dict.keys()))
        result = []
        for date in all_dates:
            valid_count = valid_dict.get(date, 0)
            invalid_count = invalid_dict.get(date, 0)

            # Calculate total counts until this date (inclusive)
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            from zoneinfo import ZoneInfo
            from django.utils import timezone
            date_obj = timezone.make_aware(date_obj.replace(hour=23, minute=59, second=59), ZoneInfo("Asia/Shanghai"))

            total_valid = MrLiveReviewRepository.count_until_date(
                project_name, target_branch, "valid", date_obj
            )
            total_invalid = MrLiveReviewRepository.count_until_date(
                project_name, target_branch, "invalid", date_obj
            )

            accept_rate = _accept_rate(valid_count, invalid_count)
            total_accept_rate = _accept_rate(total_valid, total_invalid)
            result.append({
                "date": date,
                "accept_rate": accept_rate,
                "total_accept_rate": total_accept_rate
            })

        return JsonResponse({"code": 200, "data": result})

    # For other stats types
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from django.utils import timezone

    data = MrLiveReviewRepository.count_by_date(
        project_name, target_branch, stats, start_date, end_date
    )

    # Calculate total count until each date (inclusive)
    for item in data:
        date_str = item["date"]
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_obj = timezone.make_aware(date_obj.replace(hour=23, minute=59, second=59), ZoneInfo("Asia/Shanghai"))

        total_count = MrLiveReviewRepository.count_until_date(
            project_name, target_branch, stats, date_obj
        )
        item["total_count"] = total_count

    return JsonResponse({"code": 200, "data": data})


@require_http_methods(["POST"])
def create_daily_mr_review(request):
    import json
    from router.models import DailyMrReview
    from router.repositories.codehub_review import DailyMrReviewRepository

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in DailyMrReview._meta.fields}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    issue_hash = data.get("issue_hash")
    if not issue_hash:
        return _bad_request("issue_hash is required")

    if DailyMrReviewRepository.exists_by_hash(issue_hash):
        return JsonResponse({"code": 200, "message": "skipped", "data": {"issue_hash": issue_hash}})

    try:
        review = DailyMrReviewRepository.create(data)
        return JsonResponse({"code": 200, "message": "created", "data": {"id": review.id}})
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def create_live_review_request(request):
    import json
    from datetime import datetime
    from router.models import LiveReviewRequest, Model

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Required fields validation
    required_fields = ["project_name", "merge_requests_id", "merge_url", "start_time"]
    for field in required_fields:
        if field not in data or not data[field]:
            return _bad_request(f"{field} is required")

    # Validate that all keys in data match model fields (excluding auto fields)
    valid_fields = {f.name for f in LiveReviewRequest._meta.fields if f.name not in ["id", "created_at", "updated_at", "deleted_at", "duration_seconds"]}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    # Process model_id fields (convert from model_name string to model_id)
    warnings = []
    processed_data = {}

    for key, value in data.items():
        if key in ["expert_model_id", "reflect_model_id"]:
            if value:
                # Value is a model_name string, need to convert to model_id
                try:
                    model = Model.objects.get(model_name=value)
                    processed_data[key] = model.id
                except Model.DoesNotExist:
                    processed_data[key] = None
                    warnings.append(f"{key}: model_name '{value}' 在现有数据库中不存在")
            else:
                processed_data[key] = None
        elif key in ["start_time", "end_time"]:
            # Convert time string to datetime object
            if value:
                try:
                    # Support multiple datetime formats
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(value, fmt)
                            processed_data[key] = timezone.make_aware(dt, timezone.get_current_timezone())
                            break
                        except ValueError:
                            continue
                    else:
                        return _bad_request(f"{key} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                except Exception as e:
                    return _bad_request(f"{key} conversion failed: {str(e)}")
            else:
                processed_data[key] = None
        else:
            processed_data[key] = value

    # Calculate duration_seconds from start_time and end_time
    if processed_data.get("start_time") and processed_data.get("end_time"):
        time_delta = processed_data["end_time"] - processed_data["start_time"]
        processed_data["duration_seconds"] = int(time_delta.total_seconds())
    else:
        processed_data["duration_seconds"] = None

    # Set timestamps
    now = timezone.now()
    processed_data["created_at"] = now
    processed_data["updated_at"] = now

    try:
        review_pipeline = LiveReviewRequest.objects.create(**processed_data)
        response_data = {
            "code": 200,
            "message": "created",
            "data": {"id": review_pipeline.id}
        }
        if warnings:
            response_data["warnings"] = warnings
        return JsonResponse(response_data)
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def upsert_codehub_review(request):
    import json
    from datetime import datetime
    from router.models import CodehubReview

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in CodehubReview._meta.fields if f.name != "id"}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    # Process datetime fields
    processed_data = {}
    for key, value in data.items():
        if key in ["scan_date", "completion_date", "created_at", "updated_at", "deleted_at"]:
            if value:
                try:
                    # Support multiple datetime formats
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(value, fmt)
                            processed_data[key] = timezone.make_aware(dt, timezone.get_current_timezone())
                            break
                        except ValueError:
                            continue
                    else:
                        return _bad_request(f"{key} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                except Exception as e:
                    return _bad_request(f"{key} conversion failed: {str(e)}")
            else:
                processed_data[key] = None
        else:
            processed_data[key] = value

    # Create a new record
    now = timezone.now()
    if "created_at" not in processed_data:
        processed_data["created_at"] = now
    if "updated_at" not in processed_data:
        processed_data["updated_at"] = now

    # Set default value for is_modified_completed if not provided
    if "is_modified_completed" not in processed_data:
        processed_data["is_modified_completed"] = False

    try:
        review = CodehubReview.objects.create(**processed_data)
        return JsonResponse({"code": 200, "message": "created", "data": {"id": review.id}})
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


def _bad_request(message: str):
    return JsonResponse({"code": 400, "error": message}, status=400)


def _parse_pagination(request, default_page_size: int = 10, max_page_size: int = 100):
    """Parse ``page`` / ``page_size`` query params.

    Returns ``(page, page_size, error)``. ``page`` defaults to 1 and
    ``page_size`` to ``default_page_size`` (capped at ``max_page_size``).
    On invalid input ``error`` is a message string and the page values are
    undefined.
    """
    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", default_page_size))
    except (TypeError, ValueError):
        return None, None, "page and page_size must be integers"

    if page < 1:
        return None, None, "page must be >= 1"
    if page_size < 1:
        return None, None, "page_size must be >= 1"
    if page_size > max_page_size:
        return None, None, f"page_size must be <= {max_page_size}"

    return page, page_size, None


@require_http_methods(["POST"])
def update_concurrent_multiplier(request):
    from router.repositories.ips import IPRepository
    from router.repositories.user_ips import UserIPRepository

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # 获取参数
    employee_no = data.get("employee_no")
    ip = data.get("ip")
    concurrent_multiplier = data.get("concurrent_multiplier")

    # 验证必须提供employee_no或ip其中之一
    if not employee_no and not ip:
        return _bad_request("employee_no or ip is required")

    if employee_no and ip:
        return _bad_request("only one of employee_no or ip should be provided")

    # 验证concurrent_multiplier
    if concurrent_multiplier is None:
        return _bad_request("concurrent_multiplier is required")

    try:
        multiplier_value = float(concurrent_multiplier)
    except (TypeError, ValueError):
        return _bad_request("concurrent_multiplier must be a number")

    if multiplier_value < 1:
        return _bad_request("concurrent_multiplier must be >= 1")

    # 处理employee_no
    if employee_no:
        employee_no = str(employee_no).strip()
        if not employee_no:
            return _bad_request("employee_no cannot be empty")

        user_ip = UserIPRepository.get_ip_backed_by_employee_no(employee_no)
        if not user_ip:
            return JsonResponse({"code": 404, "error": "employee_no not found"}, status=404)

        try:
            ip_obj = IPRepository.update_concurrent_multiplier(user_ip.ip_id, multiplier_value)
            return JsonResponse({
                "code": 200,
                "message": "更新成功",
                "data": {
                    "employee_no": user_ip.employee_no,
                    "ip": ip_obj.ip,
                    "concurrent_multiplier": ip_obj.concurrent_multiplier,
                }
            })
        except Exception as e:
            return JsonResponse({"code": 500, "error": f"update failed: {str(e)}"}, status=500)

    # 处理ip
    if ip:
        ip = str(ip).strip()
        if not ip:
            return _bad_request("ip cannot be empty")

        ip_obj = IPRepository.get_by_ip(ip)
        if not ip_obj:
            return JsonResponse({"code": 404, "error": "ip not found"}, status=404)

        try:
            ip_obj = IPRepository.update_concurrent_multiplier(ip_obj.id, multiplier_value)
            return JsonResponse({
                "code": 200,
                "message": "更新成功",
                "data": {
                    "ip": ip_obj.ip,
                    "concurrent_multiplier": ip_obj.concurrent_multiplier,
                }
            })
        except Exception as e:
            return JsonResponse({"code": 500, "error": f"update failed: {str(e)}"}, status=500)


@require_http_methods(["POST"])
def create_ai_assistant_user_feedback(request):
    import json
    from datetime import datetime
    from router.models import AiAssistantUserFeedback

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Required fields validation
    required_fields = ["domain", "issue_description", "reporter", "reported_at", "status"]
    for field in required_fields:
        if field not in data or not data[field]:
            return _bad_request(f"{field} is required")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in AiAssistantUserFeedback._meta.fields if f.name not in ["id"]}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    # Validate domain
    valid_domains = ["知识管理", "辅助设计", "代码分析", "问题定位", "Agent", "公共"]
    if data["domain"] not in valid_domains:
        return _bad_request(f"domain must be one of: {', '.join(valid_domains)}")

    # Validate priority (optional)
    if "priority" in data and data["priority"] is not None:
        valid_priorities = ["高", "中", "低"]
        if data["priority"] not in valid_priorities:
            return _bad_request(f"priority must be one of: {', '.join(valid_priorities)}")

    # Validate status
    valid_statuses = ["open", "close", "cancel"]
    if data["status"] not in valid_statuses:
        return _bad_request(f"status must be one of: {', '.join(valid_statuses)}")

    # Process datetime fields
    processed_data = {}
    datetime_fields = ["reported_at", "estimated_resolution_at", "actual_resolution_at", "created_at", "updated_at", "deleted_at"]
    for key, value in data.items():
        if key in datetime_fields:
            if value:
                try:
                    # Support multiple datetime formats
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(value, fmt)
                            processed_data[key] = timezone.make_aware(dt, timezone.get_current_timezone())
                            break
                        except ValueError:
                            continue
                    else:
                        return _bad_request(f"{key} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                except Exception as e:
                    return _bad_request(f"{key} conversion failed: {str(e)}")
            else:
                processed_data[key] = None
        else:
            processed_data[key] = value

    # Set timestamps if not provided
    now = timezone.now()
    if "created_at" not in processed_data or not processed_data["created_at"]:
        processed_data["created_at"] = now
    if "updated_at" not in processed_data or not processed_data["updated_at"]:
        processed_data["updated_at"] = now

    try:
        feedback = AiAssistantUserFeedback.objects.create(**processed_data)
        return JsonResponse({
            "code": 200,
            "message": "created",
            "data": {"id": feedback.id}
        })
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def update_ai_assistant_user_feedback(request):
    """
    根据 ID 更新 AI Assistant 用户反馈记录。

    必传参数：
    - id: 记录 ID

    可选修改参数（至少提供一个）：
    - domain: 领域（可选值：知识管理、辅助设计、代码分析、问题定位、Agent、公共）
    - tool_version: 工具版本
    - issue_description: 问题描述
    - reporter: 报告人
    - reported_at: 报告时间（格式：YYYY-MM-DD HH:MM:SS）
    - priority: 优先级（可选值：高、中、低）
    - assignee: 指派人
    - status: 状态（可选值：open、close、cancel）
    - estimated_resolution_at: 预计解决时间（格式：YYYY-MM-DD HH:MM:SS）
    - actual_resolution_at: 实际解决时间（格式：YYYY-MM-DD HH:MM:SS）
    - bugfix_version: 修复版本
    - progress_tracking: 进度跟踪
    - remarks: 备注

    返回：
    - 更新成功返回更新后的记录信息
    - 记录不存在返回 404
    - 参数错误返回 400
    """
    from datetime import datetime
    from router.models import AiAssistantUserFeedback

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # 验证必传参数 id
    feedback_id = data.get("id")
    if feedback_id is None:
        return _bad_request("id is required")

    try:
        feedback_id = int(feedback_id)
    except (TypeError, ValueError):
        return _bad_request("id must be an integer")

    # 定义允许修改的字段（不包括 id、created_at、updated_at、deleted_at）
    allowed_fields = {
        "domain": "choice",
        "tool_version": "str",
        "issue_description": "str",
        "reporter": "str",
        "reported_at": "datetime",
        "priority": "choice_optional",
        "assignee": "str_optional",
        "status": "choice",
        "estimated_resolution_at": "datetime_optional",
        "actual_resolution_at": "datetime_optional",
        "bugfix_version": "str_optional",
        "progress_tracking": "str_optional",
        "remarks": "str_optional",
    }

    # 验证值范围
    valid_domains = ["知识管理", "辅助设计", "代码分析", "问题定位", "Agent", "公共"]
    valid_priorities = ["高", "中", "低"]
    valid_statuses = ["open", "close", "cancel"]

    # 提取并验证可选字段
    update_data = {}
    for field, field_type in allowed_fields.items():
        if field in data:
            value = data[field]

            # 处理 datetime 类型字段
            if field_type in ["datetime", "datetime_optional"]:
                if value:
                    try:
                        # 支持多种 datetime 格式
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                            try:
                                dt = datetime.strptime(value, fmt)
                                update_data[field] = timezone.make_aware(dt, timezone.get_current_timezone())
                                break
                            except ValueError:
                                continue
                        else:
                            return _bad_request(f"{field} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                    except Exception as e:
                        return _bad_request(f"{field} conversion failed: {str(e)}")
                else:
                    update_data[field] = None

            # 处理 choice 类型字段
            elif field_type == "choice":
                if not value:
                    return _bad_request(f"{field} cannot be empty")
                if field == "domain" and value not in valid_domains:
                    return _bad_request(f"domain must be one of: {', '.join(valid_domains)}")
                if field == "status" and value not in valid_statuses:
                    return _bad_request(f"status must be one of: {', '.join(valid_statuses)}")
                update_data[field] = value

            # 处理可选 choice 类型字段
            elif field_type == "choice_optional":
                if value is not None:
                    if field == "priority" and value not in valid_priorities:
                        return _bad_request(f"priority must be one of: {', '.join(valid_priorities)}")
                    update_data[field] = value
                else:
                    update_data[field] = None

            # 处理字符串类型字段
            elif field_type == "str":
                if not value:
                    return _bad_request(f"{field} cannot be empty")
                update_data[field] = value

            # 处理可选字符串类型字段
            elif field_type in ["str_optional"]:
                update_data[field] = value if value else None

    # 检查是否至少提供了一个修改字段
    if not update_data:
        return _bad_request("at least one field to update is required")

    # 查询记录是否存在
    try:
        feedback = AiAssistantUserFeedback.objects.get(id=feedback_id)
    except AiAssistantUserFeedback.DoesNotExist:
        return JsonResponse({
            "code": 404,
            "error": f"AiAssistantUserFeedback with id {feedback_id} not found"
        }, status=404)

    # 更新字段
    for field, value in update_data.items():
        setattr(feedback, field, value)

    # 更新 updated_at 时间戳
    feedback.updated_at = timezone.now()

    # 保存更新
    try:
        feedback.save()
    except Exception as e:
        return JsonResponse({"code": 500, "error": f"update failed: {str(e)}"}, status=500)

    # 返回更新后的记录信息
    return JsonResponse({
        "code": 200,
        "message": "updated",
        "data": {
            "id": feedback.id,
            "domain": feedback.domain,
            "tool_version": feedback.tool_version,
            "issue_description": feedback.issue_description,
            "reporter": feedback.reporter,
            "reported_at": feedback.reported_at.astimezone(BEIJING_TZ).strftime(TIME_FORMAT) if feedback.reported_at else None,
            "priority": feedback.priority,
            "assignee": feedback.assignee,
            "status": feedback.status,
            "estimated_resolution_at": feedback.estimated_resolution_at.astimezone(BEIJING_TZ).strftime(TIME_FORMAT) if feedback.estimated_resolution_at else None,
            "actual_resolution_at": feedback.actual_resolution_at.astimezone(BEIJING_TZ).strftime(TIME_FORMAT) if feedback.actual_resolution_at else None,
            "bugfix_version": feedback.bugfix_version,
            "progress_tracking": feedback.progress_tracking,
            "remarks": feedback.remarks,
            "updated_at": feedback.updated_at.astimezone(BEIJING_TZ).strftime(TIME_FORMAT) if feedback.updated_at else None,
        }
    })


@require_http_methods(["GET"])
def access_stats_by_department(request):
    """
    按部门和<employee_no, ip>维度统计token用量（issue #295）。

    查询参数：
    - start_time: 开始时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - dept1: 一级部门（可选，"all"表示所有部门）
    - dept2: 二级部门（可选，"all"表示所有部门）
    - dept3: 三级部门（可选，"all"表示所有部门）
    - dept4: 四级部门（可选，"all"表示所有部门）
    - employee_no: 工号（可选，匹配解析后的工号，支持多个值，逗号分隔或多次传参）
    - user_name: 用户名（可选，支持多个值，逗号分隔或多次传参）
    - ip: IP地址（可选，支持多个值，逗号分隔或多次传参）

    返回：
    - 按 <employee_no, ip> 聚合的访问统计，包含用户信息、部门信息和token统计
    - employee_no 解析优先级：请求 apikey 的工号 > IP 的工号 > "stale"
    - ip_employee_no: IP 的工号，用于核对 apikey 与 IP 是否属于同一人
    - 部门信息取自解析后工号对应的用户
    - input_token: input_token_cnt 的总和
    - output_token: output_token_cnt 的总和
    - prefix_cache: final_prefix_cache 的总和（前缀缓存命中 token）
    """
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # 获取部门参数
    dept1 = request.GET.get("dept1")
    dept2 = request.GET.get("dept2")
    dept3 = request.GET.get("dept3")
    dept4 = request.GET.get("dept4")

    # 处理部门参数：空字符串或"all"视为查询所有
    dept1 = None if not dept1 or dept1.strip().lower() == "all" else dept1.strip()
    dept2 = None if not dept2 or dept2.strip().lower() == "all" else dept2.strip()
    dept3 = None if not dept3 or dept3.strip().lower() == "all" else dept3.strip()
    dept4 = None if not dept4 or dept4.strip().lower() == "all" else dept4.strip()

    # 获取新增的可选过滤参数（支持多值）
    employee_no_raw = request.GET.getlist("employee_no")
    user_name_raw = request.GET.getlist("user_name")
    ip_raw = request.GET.getlist("ip")

    # 处理多值参数：支持逗号分隔和多次传参两种方式
    def parse_multi_value(values: list) -> list | None:
        """解析多值参数，支持逗号分隔和多次传参"""
        if not values:
            return None
        # 展开逗号分隔的值
        expanded = []
        for v in values:
            if v:
                # 拆分逗号分隔的值
                parts = [p.strip() for p in v.split(",") if p.strip()]
                expanded.extend(parts)
        return expanded if expanded else None

    employee_no = parse_multi_value(employee_no_raw)
    user_name = parse_multi_value(user_name_raw)
    ip = parse_multi_value(ip_raw)

    # 查询数据
    results = RequestRepository.summarize_success_by_employee_and_ip(
        start, end, dept1, dept2, dept3, dept4, employee_no, user_name, ip
    )

    return JsonResponse({
        "code": 200,
        "data": results,
        "total": len(results),
        "start_time": start.astimezone(BEIJING_TZ).strftime(TIME_FORMAT),
        "end_time": end.astimezone(BEIJING_TZ).strftime(TIME_FORMAT),
    })


@require_http_methods(["GET"])
def export_access_stats_csv(request):
    """
    导出人员使用情况为CSV文件。

    查询参数：
    - start_time: 开始时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - dept1: 一级部门（可选，"all"表示所有部门）
    - dept2: 二级部门（可选，"all"表示所有部门）
    - dept3: 三级部门（可选，"all"表示所有部门）
    - dept4: 四级部门（可选，"all"表示所有部门）
    - employee_no: 工号（可选，匹配解析后的工号，支持多个值，逗号分隔或多次传参）
    - user_name: 用户名（可选，支持多个值，逗号分隔或多次传参）
    - ip: IP地址（可选，支持多个值，逗号分隔或多次传参）

    返回：
    - CSV文件下载，按 <employee_no, ip> 聚合的访问统计、用户信息、部门信息和token统计
    - 文件名格式：access_stats_{start_time}_{end_time}.csv
    """
    parsed = _time_range_or_error(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    start, end = parsed

    # 获取部门参数
    dept1 = request.GET.get("dept1")
    dept2 = request.GET.get("dept2")
    dept3 = request.GET.get("dept3")
    dept4 = request.GET.get("dept4")

    # 处理部门参数：空字符串或"all"视为查询所有
    dept1 = None if not dept1 or dept1.strip().lower() == "all" else dept1.strip()
    dept2 = None if not dept2 or dept2.strip().lower() == "all" else dept2.strip()
    dept3 = None if not dept3 or dept3.strip().lower() == "all" else dept3.strip()
    dept4 = None if not dept4 or dept4.strip().lower() == "all" else dept4.strip()

    # 获取新增的可选过滤参数（支持多值）
    employee_no_raw = request.GET.getlist("employee_no")
    user_name_raw = request.GET.getlist("user_name")
    ip_raw = request.GET.getlist("ip")

    # 处理多值参数：支持逗号分隔和多次传参两种方式
    def parse_multi_value(values: list) -> list | None:
        """解析多值参数，支持逗号分隔和多次传参"""
        if not values:
            return None
        # 展开逗号分隔的值
        expanded = []
        for v in values:
            if v:
                # 拆分逗号分隔的值
                parts = [p.strip() for p in v.split(",") if p.strip()]
                expanded.extend(parts)
        return expanded if expanded else None

    employee_no = parse_multi_value(employee_no_raw)
    user_name = parse_multi_value(user_name_raw)
    ip = parse_multi_value(ip_raw)

    # 查询数据
    results = RequestRepository.summarize_success_by_employee_and_ip(
        start, end, dept1, dept2, dept3, dept4, employee_no, user_name, ip
    )

    # 创建CSV响应
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    
    # 生成文件名
    start_str = start.astimezone(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    end_str = end.astimezone(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"access_stats_{start_str}_{end_str}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # 写入CSV数据
    writer = csv.writer(response)
    
    # 写入表头
    writer.writerow([
        "IP地址",
        "访问次数",
        "输入Token",
        "输出Token",
        "用户姓名",
        "资产挂账人",
        "员工工号",
        "一级部门",
        "二级部门",
        "三级部门",
        "四级部门",
        "IP工号",
        "前缀缓存Token",
    ])

    # 写入数据行
    for row in results:
        writer.writerow([
            row.get("ip", ""),
            row.get("access_count", 0),
            row.get("input_token", 0),
            row.get("output_token", 0),
            row.get("user_name", ""),
            row.get("user_charge", ""),
            row.get("employee_no", ""),
            row.get("dept1", ""),
            row.get("dept2", ""),
            row.get("dept3", ""),
            row.get("dept4", ""),
            row.get("ip_employee_no", ""),
            row.get("prefix_cache", 0),
        ])

    return response


@require_http_methods(["GET"])
def department_cascade(request):
    """
    获取部门级联数据，用于前端级联选择器。

    查询参数（可选）：
    - start_time: 开始时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（北京时间，格式：YYYY-MM-DD HH:MM:SS）

    如果不传时间参数，返回所有有效部门。
    如果传时间参数，只返回在该时间范围内有访问记录的部门。

    返回：
    - 级联部门数据，格式适合前端级联选择器使用
    """
    from router.repositories.departments import DepartmentRepository

    # 解析时间参数（可选）
    start_time = request.GET.get("start_time")
    end_time = request.GET.get("end_time")

    start = None
    end = None

    if start_time and end_time:
        parsed = _time_range_or_error(request)
        if isinstance(parsed, JsonResponse):
            return parsed
        start, end = parsed

    # 获取级联数据
    result = DepartmentRepository.get_cascade(start, end)

    return JsonResponse({"code": 200, "data": result})


@require_http_methods(["GET"])
def whitelist_list(request):
    """
    获取白名单列表，支持分页。

    查询参数：
    - page: 页码（从1开始，可选）
    - page_size: 每页条数（可选，默认不分页返回全量数据）

    返回：
    - 白名单数据列表
    """
    from router.repositories.whitelist import WhitelistRepository

    # 获取分页参数
    page_param = request.GET.get("page")
    page_size_param = request.GET.get("page_size")

    # 如果两个参数都提供了，则进行分页
    if page_param is not None and page_size_param is not None:
        page, page_size, error = _parse_pagination(request)
        if error:
            return _bad_request(error)

        data, total = WhitelistRepository.list_all(page, page_size)

        return JsonResponse({
            "code": 200,
            "data": data,
            "total": total,
            "page": page,
            "page_size": page_size,
        })

    # 否则返回全量数据
    data, total = WhitelistRepository.list_all()

    return JsonResponse({
        "code": 200,
        "data": data,
        "total": total,
    })


@require_http_methods(["GET"])
def ip_list_with_user_info(request):
    """
    获取IP列表及关联的用户和部门信息，支持分页和筛选。

    查询参数：
    - page: 页码（从1开始，可选）
    - page_size: 每页条数（可选）
    - employee_no: 员工工号筛选（模糊匹配，可选）
    - ip: IP地址筛选（模糊匹配，可选）

    返回：
    - IP数据列表，包含并发数和关联的用户、部门信息
    """
    from router.repositories.ips import IPRepository

    # 获取分页参数
    page_param = request.GET.get("page")
    page_size_param = request.GET.get("page_size")

    # 获取筛选参数
    employee_no = request.GET.get("employee_no")
    ip = request.GET.get("ip")

    # 处理筛选参数
    employee_no = employee_no.strip() if employee_no else None
    ip = ip.strip() if ip else None

    # 如果两个分页参数都提供了，则进行分页
    if page_param is not None and page_size_param is not None:
        page, page_size, error = _parse_pagination(request)
        if error:
            return _bad_request(error)

        data, total = IPRepository.list_with_user_info(
            page=page,
            page_size=page_size,
            employee_no=employee_no,
            ip=ip
        )

        return JsonResponse({
            "code": 200,
            "data": data,
            "total": total,
            "page": page,
            "page_size": page_size,
        })

    # 否则返回全量数据
    data, total = IPRepository.list_with_user_info(
        employee_no=employee_no,
        ip=ip
    )

    return JsonResponse({
        "code": 200,
        "data": data,
        "total": total,
    })


@require_http_methods(["GET"])
def codehub_review_stats(request):
    """
    获取CodehubReview统计信息。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回：
    - total_count: 总数据条数
    - valid_issue_count: is_valid_issue为true的条数
    - invalid_issue_count: is_valid_issue为false的条数
    - modified_completed_count: is_modified_completed为true的条数
    - severity: 各个severity类型的数量
    - latest_scan_commit_id: 最新的scan_commit_id（按scan_date排序）
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询统计数据
    stats = CodehubReviewRepository.get_statistics(
        project_name=project_name,
        branch_name=branch_name,
        start_time=start_time,
        end_time=end_time
    )

    return JsonResponse({
        "code": 200,
        "data": stats,
    })


@require_http_methods(["GET"])
def codehub_review_category_stats(request):
    """
    获取CodehubReview的issue_category统计信息。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - need_analysis: 是否需要分析（true/false）

    返回：
    - issue_category各个类型的数量
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")
    need_analysis_str = request.GET.get("need_analysis")

    # 处理筛选参数
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None

    # 解析 need_analysis 参数
    need_analysis = None
    if need_analysis_str:
        need_analysis_str = need_analysis_str.strip().lower()
        if need_analysis_str == "true":
            need_analysis = True
        elif need_analysis_str == "false":
            need_analysis = False

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询统计数据
    category_stats = CodehubReviewRepository.get_issue_category_statistics(
        project_name=project_name,
        branch_name=branch_name,
        start_time=start_time,
        end_time=end_time,
        need_analysis=need_analysis
    )

    return JsonResponse({
        "code": 200,
        "data": category_stats,
    })


@require_http_methods(["GET"])
def codehub_review_severity_stats(request):
    """
    获取CodehubReview的severity详细统计信息。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回每个severity类型的详细数据：
    - count: 各个severity类型的数量
    - valid_issue_count: 各个severity类型的is_valid_issue为true的条数
    - invalid_issue_count: 各个severity类型的is_valid_issue为false的条数
    - modified_completed_count: 各个severity类型的is_modified_completed为true的条数
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询统计数据
    severity_detail = CodehubReviewRepository.get_severity_detail_statistics(
        project_name=project_name,
        branch_name=branch_name,
        start_time=start_time,
        end_time=end_time
    )

    return JsonResponse({
        "code": 200,
        "data": severity_detail,
    })


@require_http_methods(["GET"])
def codehub_review_list(request):
    """
    获取CodehubReview列表查询接口（支持多条件过滤和分页）。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - relative_path: 相对路径（支持模糊匹配，可传入多个值，用逗号分隔或多次传参）
    - severity: 严重级别（可传入多个值，用逗号分隔或多次传参）
    - issue_category: 问题类别（可传入多个值，用逗号分隔或多次传参）
    - page: 页码（默认为1）
    - page_size: 每页大小（默认为10，最大100）
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回：
    - total_count: 总数据条数
    - total_pages: 总页数
    - current_page: 当前页码
    - page_size: 每页大小
    - has_next: 是否有下一页
    - has_previous: 是否有上一页
    - items: 数据列表，包含所有字段
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    # 支持多值参数：可以通过逗号分隔或多次传参（如 relative_path=a&relative_path=b）
    relative_path_raw = request.GET.getlist("relative_path")
    severity_raw = request.GET.getlist("severity")
    issue_category_raw = request.GET.getlist("issue_category")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数（去除空白）
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None

    # 处理多值参数：支持逗号分隔和多次传参两种方式
    def parse_multi_value(values: list) -> list | None:
        """解析多值参数，支持逗号分隔和多次传参"""
        if not values:
            return None
        # 展开逗号分隔的值
        expanded = []
        for v in values:
            if v:
                # 拆分逗号分隔的值
                parts = [p.strip() for p in v.split(",") if p.strip()]
                expanded.extend(parts)
        return expanded if expanded else None

    relative_path = parse_multi_value(relative_path_raw)
    severity = parse_multi_value(severity_raw)
    issue_category = parse_multi_value(issue_category_raw)

    # 解析分页参数：如果不传 page 和 page_size，返回全量数据
    page_param = request.GET.get("page")
    page_size_param = request.GET.get("page_size")

    if page_param is None and page_size_param is None:
        # 不传分页参数，返回全量数据
        page = None
        page_size = None
    else:
        # 正常分页处理
        page, page_size, error_msg = _parse_pagination(request, default_page_size=10, max_page_size=100)
        if error_msg:
            return _bad_request(error_msg)

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询数据
    result = CodehubReviewRepository.get_filtered_reviews(
        project_name=project_name,
        branch_name=branch_name,
        relative_path=relative_path,
        severity=severity,
        issue_category=issue_category,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )

    return JsonResponse({
        "code": 200,
        "data": result,
    })


@require_http_methods(["POST"])
def update_codehub_review(request):
    """
    更新CodehubReview记录接口。

    必传参数：
    - id: 记录ID

    可选修改参数（至少提供一个）：
    - module: 模块名称
    - first_level_confirmer: 一级确认人
    - second_level_confirmer: 二级确认人
    - is_valid_issue: 是否为有效问题（布尔值）
    - is_modified: 是否已修改（布尔值）
    - is_modified_completed: 是否修改完成（布尔值）
    - notes: 备注
    - need_analysis: 是否需要分析（布尔值）
    - conclusion: 结论

    返回：
    - 更新成功返回更新后的记录信息
    - 记录不存在返回404
    - 参数错误返回400
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from router.models import CodehubReview

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # 验证必传参数id
    review_id = data.get("id")
    if review_id is None:
        return _bad_request("id is required")

    try:
        review_id = int(review_id)
    except (TypeError, ValueError):
        return _bad_request("id must be an integer")

    # 定义允许修改的字段
    allowed_fields = {
        "module": str,
        "first_level_confirmer": str,
        "second_level_confirmer": str,
        "is_valid_issue": bool,
        "is_modified": bool,
        "is_modified_completed": bool,
        "notes": str,
        "need_analysis": bool,
        "conclusion": str,
    }

    # 提取并验证可选字段
    update_data = {}
    for field, field_type in allowed_fields.items():
        if field in data:
            value = data[field]

            # 处理布尔类型字段
            if field_type == bool:
                if isinstance(value, bool):
                    update_data[field] = value
                elif isinstance(value, str):
                    if value.lower() == "true":
                        update_data[field] = True
                    elif value.lower() == "false":
                        update_data[field] = False
                    else:
                        return _bad_request(f"{field} must be a boolean value")
                else:
                    return _bad_request(f"{field} must be a boolean value")
            else:
                # 字符串类型字段，允许空值
                update_data[field] = value if value else None

    # 检查是否至少提供了一个修改字段
    if not update_data:
        return _bad_request("at least one field to update is required")

    # 执行更新
    updated_review = CodehubReviewRepository.update_review(
        review_id=review_id,
        **update_data
    )

    if updated_review is None:
        return JsonResponse({
            "code": 404,
            "error": f"CodehubReview with id {review_id} not found"
        }, status=404)

    # 返回更新后的记录信息
    return JsonResponse({
        "code": 200,
        "message": "updated",
        "data": {
            "id": updated_review.id,
            "module": updated_review.module,
            "first_level_confirmer": updated_review.first_level_confirmer,
            "second_level_confirmer": updated_review.second_level_confirmer,
            "is_valid_issue": updated_review.is_valid_issue,
            "is_modified": updated_review.is_modified,
            "is_modified_completed": updated_review.is_modified_completed,
            "notes": updated_review.notes,
            "updated_at": updated_review.updated_at.isoformat() if updated_review.updated_at else None,
        }
    })


@require_http_methods(["GET"])
def codehub_review_relative_path_list(request):
    """
    获取CodehubReview中relative_path的去重列表。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - severity: 严重级别
    - issue_category: 问题类别
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回：
    - relative_path的去重列表（按字母顺序排序）
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    severity = request.GET.get("severity")
    issue_category = request.GET.get("issue_category")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数（去除空白）
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None
    severity = severity.strip() if severity else None
    issue_category = issue_category.strip() if issue_category else None

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询数据
    relative_paths = CodehubReviewRepository.get_relative_path_list(
        project_name=project_name,
        branch_name=branch_name,
        severity=severity,
        issue_category=issue_category,
        start_time=start_time,
        end_time=end_time,
    )

    return JsonResponse({
        "code": 200,
        "data": {
            "total_count": len(relative_paths),
            "relative_paths": relative_paths,
        }
    })


@require_http_methods(["GET"])
def codehub_review_severity_list(request):
    """
    获取CodehubReview中severity的去重列表。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - relative_path: 相对路径（支持模糊匹配）
    - issue_category: 问题类别
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回：
    - severity的去重列表（按字母顺序排序）
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    relative_path = request.GET.get("relative_path")
    issue_category = request.GET.get("issue_category")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数（去除空白）
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None
    relative_path = relative_path.strip() if relative_path else None
    issue_category = issue_category.strip() if issue_category else None

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询数据
    severities = CodehubReviewRepository.get_severity_list(
        project_name=project_name,
        branch_name=branch_name,
        relative_path=relative_path,
        issue_category=issue_category,
        start_time=start_time,
        end_time=end_time,
    )

    return JsonResponse({
        "code": 200,
        "data": {
            "total_count": len(severities),
            "severities": severities,
        }
    })


@require_http_methods(["GET"])
def codehub_review_issue_category_list(request):
    """
    获取CodehubReview中issue_category的去重列表。

    查询参数（全部可选）：
    - project_name: 项目名称
    - branch_name: 分支名称
    - relative_path: 相对路径（支持模糊匹配）
    - severity: 严重级别
    - start_time: 开始时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）
    - end_time: 结束时间（基于scan_date，格式：YYYY-MM-DD HH:MM:SS）

    返回：
    - issue_category的去重列表（按字母顺序排序）
    """
    from router.repositories.codehub_review import CodehubReviewRepository
    from datetime import datetime

    # 获取筛选参数
    project_name = request.GET.get("project_name")
    branch_name = request.GET.get("branch_name")
    relative_path = request.GET.get("relative_path")
    severity = request.GET.get("severity")
    start_time_str = request.GET.get("start_time")
    end_time_str = request.GET.get("end_time")

    # 处理筛选参数（去除空白）
    project_name = project_name.strip() if project_name else None
    branch_name = branch_name.strip() if branch_name else None
    relative_path = relative_path.strip() if relative_path else None
    severity = severity.strip() if severity else None

    # 解析时间参数
    start_time = None
    end_time = None

    if start_time_str:
        try:
            start_time = datetime.strptime(start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            start_time = timezone.make_aware(start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if end_time_str:
        try:
            end_time = datetime.strptime(end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            end_time = timezone.make_aware(end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 查询数据
    issue_categories = CodehubReviewRepository.get_issue_category_list(
        project_name=project_name,
        branch_name=branch_name,
        relative_path=relative_path,
        severity=severity,
        start_time=start_time,
        end_time=end_time,
    )

    return JsonResponse({
        "code": 200,
        "data": {
            "total_count": len(issue_categories),
            "issue_categories": issue_categories,
        }
    })


@require_http_methods(["POST"])
def create_review_slice(request):
    """
    创建 ReviewSlices 记录接口。

    必传参数：
    - project_id: 项目ID（字符串）
    - mr_iid: MR IID（字符串）
    - start_time: 开始时间（格式：YYYY-MM-DD HH:MM:SS）
    - review_id: Review ID（字符串）
    - expert_model_name: Expert 模型名称（字符串）
    - reflector_model_name: Reflector 模型名称（字符串）

    可选参数：
    - expert_duration: Expert 处理时长（浮点数）
    - reflector_duration: Reflector 处理时长（浮点数）
    - expert_comments: Expert 评论数（整数）
    - reflector_passed: Reflector 通过数（整数）
    - expert_retries: Expert 重试次数（整数）
    - reflector_retries: Reflector 重试次数（整数）
    - result: 结果（字符串）

    返回：
    - 创建成功返回记录 ID
    - 参数错误返回 400
    """
    import json
    from datetime import datetime
    from router.models import ReviewSlices

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in ReviewSlices._meta.fields if f.name not in ["id", "created_at", "updated_at", "deleted_at"]}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    # Required fields validation
    required_fields = ["project_id", "mr_iid", "start_time", "review_id", "expert_model_name", "reflector_model_name"]
    for field in required_fields:
        if field not in data or not data[field]:
            return _bad_request(f"{field} is required")

    # Process datetime fields
    processed_data = {}
    for key, value in data.items():
        if key == "start_time":
            if value:
                try:
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(value, fmt)
                            processed_data[key] = timezone.make_aware(dt, timezone.get_current_timezone())
                            break
                        except ValueError:
                            continue
                    else:
                        return _bad_request(f"{key} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                except Exception as e:
                    return _bad_request(f"{key} conversion failed: {str(e)}")
            else:
                processed_data[key] = None
        else:
            processed_data[key] = value

    # Set timestamps
    now = timezone.now()
    processed_data["created_at"] = now
    processed_data["updated_at"] = now

    try:
        slice_record = ReviewSlices.objects.create(**processed_data)
        return JsonResponse({"code": 200, "message": "created", "data": {"id": slice_record.id}})
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


@require_http_methods(["POST"])
def create_review_summary(request):
    """
    创建 ReviewSummary 记录接口。

    必传参数：
    - project_id: 项目ID（字符串）
    - mr_iid: MR IID（字符串）
    - start_time: 开始时间（格式：YYYY-MM-DD HH:MM:SS）
    - review_id: Review ID（字符串）
    - expert_model_name: Expert 模型名称（字符串）
    - reflector_model_name: Reflector 模型名称（字符串）

    可选参数：
    - file_modified_count: 修改文件数（整数）
    - total_duration: 总时长（浮点数）
    - slice_count: Slice 数量（整数）
    - expert_avg_duration: Expert 平均时长（浮点数）
    - expert_trigger_count: Expert 触发次数（整数）
    - expert_total_comments: Expert 总评论数（整数）
    - expert_avg_comments: Expert 平均评论数（浮点数）
    - expert_total_retries: Expert 总重试次数（整数）
    - reflector_avg_duration: Reflector 平均时长（浮点数）
    - reflector_trigger_count: Reflector 触发次数（整数）
    - reflector_total_comments: Reflector 总评论数（整数）
    - reflector_avg_comments: Reflector 平均评论数（浮点数）
    - reflector_total_retries: Reflector 总重试次数（整数）
    - reflector_total_passed: Reflector 总通过数（整数）
    - timeout: 是否超时（布尔值，默认 false）

    返回：
    - 创建成功返回记录 ID
    - 参数错误返回 400
    """
    import json
    from datetime import datetime
    from router.models import ReviewSummary

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # Validate that all keys in data match model fields
    valid_fields = {f.name for f in ReviewSummary._meta.fields if f.name not in ["id", "created_at", "updated_at", "deleted_at"]}
    extra_fields = set(data.keys()) - valid_fields
    if extra_fields:
        return _bad_request(f"invalid fields: {', '.join(sorted(extra_fields))}")

    # Required fields validation
    required_fields = ["project_id", "mr_iid", "start_time", "review_id", "expert_model_name", "reflector_model_name"]
    for field in required_fields:
        if field not in data or not data[field]:
            return _bad_request(f"{field} is required")

    # Process datetime fields
    processed_data = {}
    for key, value in data.items():
        if key == "start_time":
            if value:
                try:
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                        try:
                            dt = datetime.strptime(value, fmt)
                            processed_data[key] = timezone.make_aware(dt, timezone.get_current_timezone())
                            break
                        except ValueError:
                            continue
                    else:
                        return _bad_request(f"{key} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                except Exception as e:
                    return _bad_request(f"{key} conversion failed: {str(e)}")
            else:
                processed_data[key] = None
        else:
            processed_data[key] = value

    # Set timestamps
    now = timezone.now()
    processed_data["created_at"] = now
    processed_data["updated_at"] = now

    # Set default for timeout if not provided
    if "timeout" not in processed_data:
        processed_data["timeout"] = False

    try:
        summary_record = ReviewSummary.objects.create(**processed_data)
        return JsonResponse({"code": 200, "message": "created", "data": {"id": summary_record.id}})
    except Exception as e:
        return JsonResponse({"code": 500, "error": str(e)}, status=500)


@require_http_methods(["GET"])
def ai_assistant_user_feedback_list(request):
    """
    查询 AiAssistantUserFeedback 表数据列表（支持多条件过滤和分页）。

    查询参数（全部可选）：
    - create_start_time: 创建时间开始范围（基于 created_at，格式：YYYY-MM-DD HH:MM:SS）
    - create_end_time: 创建时间结束范围（基于 created_at，格式：YYYY-MM-DD HH:MM:SS）
    - domain: 领域筛选（可选值：知识管理、辅助设计、代码分析、问题定位、Agent）
    - status: 状态筛选（可选值：open、close、cancel）
    - reporter: 报告人筛选
    - assignee: 指派人筛选
    - priority: 优先级筛选（可选值：高、中、低）
    - page: 页码（默认为1）
    - page_size: 每页大小（默认为10，最大100）

    返回：
    - total_count: 总数据条数
    - total_pages: 总页数
    - current_page: 当前页码
    - page_size: 每页大小
    - has_next: 是否有下一页
    - has_previous: 是否有上一页
    - items: 数据列表，包含所有字段
    """
    import json
    from datetime import datetime
    from django.core.paginator import Paginator
    from router.models import AiAssistantUserFeedback

    # 获取筛选参数
    create_start_time_str = request.GET.get("create_start_time")
    create_end_time_str = request.GET.get("create_end_time")
    domain = request.GET.get("domain")
    status = request.GET.get("status")
    reporter = request.GET.get("reporter")
    assignee = request.GET.get("assignee")
    priority = request.GET.get("priority")

    # 处理筛选参数（去除空白）
    domain = domain.strip() if domain else None
    status = status.strip() if status else None
    reporter = reporter.strip() if reporter else None
    assignee = assignee.strip() if assignee else None
    priority = priority.strip() if priority else None

    # 验证 domain 参数
    if domain:
        valid_domains = ["知识管理", "辅助设计", "代码分析", "问题定位", "Agent", "公共"]
        if domain not in valid_domains:
            return _bad_request(f"domain must be one of: {', '.join(valid_domains)}")

    # 验证 status 参数
    if status:
        valid_statuses = ["open", "close", "cancel"]
        if status not in valid_statuses:
            return _bad_request(f"status must be one of: {', '.join(valid_statuses)}")

    # 验证 priority 参数
    if priority:
        valid_priorities = ["高", "中", "低"]
        if priority not in valid_priorities:
            return _bad_request(f"priority must be one of: {', '.join(valid_priorities)}")

    # 解析分页参数
    page, page_size, error_msg = _parse_pagination(request, default_page_size=10, max_page_size=100)
    if error_msg:
        return _bad_request(error_msg)

    # 解析时间参数
    create_start_time = None
    create_end_time = None

    if create_start_time_str:
        try:
            create_start_time = datetime.strptime(create_start_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            create_start_time = timezone.make_aware(create_start_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("create_start_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    if create_end_time_str:
        try:
            create_end_time = datetime.strptime(create_end_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            create_end_time = timezone.make_aware(create_end_time, BEIJING_TZ)
        except ValueError:
            return _bad_request("create_end_time format invalid, expected: YYYY-MM-DD HH:MM:SS")

    # 构建基础查询（排除已删除记录）
    queryset = AiAssistantUserFeedback.objects.filter(deleted_at__isnull=True)

    # 应用筛选条件
    if create_start_time:
        queryset = queryset.filter(created_at__gte=create_start_time)

    if create_end_time:
        queryset = queryset.filter(created_at__lte=create_end_time)

    if domain:
        queryset = queryset.filter(domain=domain)

    if status:
        queryset = queryset.filter(status=status)

    if reporter:
        queryset = queryset.filter(reporter__icontains=reporter)

    if assignee:
        queryset = queryset.filter(assignee__icontains=assignee)

    if priority:
        queryset = queryset.filter(priority=priority)

    # 按创建时间降序排序
    queryset = queryset.order_by('-created_at')

    # 分页
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.page(page)

    # 序列化数据
    items = []
    for feedback in page_obj.object_list:
        items.append({
            'id': feedback.id,
            'domain': feedback.domain,
            'tool_version': feedback.tool_version,
            'issue_description': feedback.issue_description,
            'reporter': feedback.reporter,
            'reported_at': feedback.reported_at.isoformat() if feedback.reported_at else None,
            'priority': feedback.priority,
            'assignee': feedback.assignee,
            'status': feedback.status,
            'estimated_resolution_at': feedback.estimated_resolution_at.isoformat() if feedback.estimated_resolution_at else None,
            'actual_resolution_at': feedback.actual_resolution_at.isoformat() if feedback.actual_resolution_at else None,
            'bugfix_version': feedback.bugfix_version,
            'progress_tracking': feedback.progress_tracking,
            'remarks': feedback.remarks,
            'created_at': feedback.created_at.isoformat() if feedback.created_at else None,
            'updated_at': feedback.updated_at.isoformat() if feedback.updated_at else None,
        })

    return JsonResponse({
        "code": 200,
        "data": {
            "total_count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page,
            "page_size": page_size,
            "has_next": page_obj.has_next(),
            "has_previous": page_obj.has_previous(),
            "items": items,
        }
    })


@require_http_methods(["POST"])
def update_review_slice(request):
    """
    更新 ReviewSlices 记录接口。

    必传参数：
    - id: 记录ID

    可选修改参数（至少提供一个）：
    - project_id: 项目ID（字符串）
    - mr_iid: MR IID（字符串）
    - start_time: 开始时间（格式：YYYY-MM-DD HH:MM:SS）
    - review_id: Review ID（字符串）
    - expert_model_name: Expert 模型名称（字符串）
    - reflector_model_name: Reflector 模型名称（字符串）
    - expert_duration: Expert 处理时长（浮点数）
    - reflector_duration: Reflector 处理时长（浮点数）
    - expert_comments: Expert 评论数（整数）
    - reflector_passed: Reflector 通过数（整数）
    - expert_retries: Expert 重试次数（整数）
    - reflector_retries: Reflector 重试次数（整数）
    - result: 结果（字符串）

    返回：
    - 更新成功返回更新后的记录信息
    - 记录不存在返回404
    - 参数错误返回400
    """
    from datetime import datetime
    from router.models import ReviewSlices

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _bad_request("invalid JSON body")

    # 验证必传参数id
    slice_id = data.get("id")
    if slice_id is None:
        return _bad_request("id is required")

    try:
        slice_id = int(slice_id)
    except (TypeError, ValueError):
        return _bad_request("id must be an integer")

    # 检查记录是否存在
    try:
        slice_record = ReviewSlices.objects.get(id=slice_id, deleted_at__isnull=True)
    except ReviewSlices.DoesNotExist:
        return JsonResponse({"code": 404, "error": "record not found"}, status=404)

    # 定义允许修改的字段及其类型
    allowed_fields = {
        "project_id": str,
        "mr_iid": str,
        "start_time": "datetime",
        "review_id": str,
        "expert_model_name": str,
        "reflector_model_name": str,
        "expert_duration": float,
        "reflector_duration": float,
        "expert_comments": int,
        "reflector_passed": int,
        "expert_retries": int,
        "reflector_retries": int,
        "result": str,
    }

    # 提取并验证可选字段
    update_data = {}
    for field, field_type in allowed_fields.items():
        if field in data:
            value = data[field]

            # 处理 datetime 类型字段
            if field_type == "datetime":
                if value:
                    try:
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                            try:
                                dt = datetime.strptime(value, fmt)
                                update_data[field] = timezone.make_aware(dt, timezone.get_current_timezone())
                                break
                            except ValueError:
                                continue
                        else:
                            return _bad_request(f"{field} format invalid, expected format: YYYY-MM-DD HH:MM:SS")
                    except Exception as e:
                        return _bad_request(f"{field} conversion failed: {str(e)}")
                else:
                    update_data[field] = None

            # 处理浮点数类型字段
            elif field_type == float:
                if value is not None:
                    try:
                        update_data[field] = float(value)
                    except (TypeError, ValueError):
                        return _bad_request(f"{field} must be a float value")
                else:
                    update_data[field] = None

            # 处理整数类型字段
            elif field_type == int:
                if value is not None:
                    try:
                        update_data[field] = int(value)
                    except (TypeError, ValueError):
                        return _bad_request(f"{field} must be an integer value")
                else:
                    update_data[field] = None

            # 处理字符串类型字段
            else:
                update_data[field] = value if value else None

    # 检查是否至少提供了一个修改字段
    if not update_data:
        return _bad_request("at least one field to update is required")

    # 更新时间戳
    update_data["updated_at"] = timezone.now()

    # 执行更新
    for field, value in update_data.items():
        setattr(slice_record, field, value)
    slice_record.save()

    # 返回更新后的记录信息
    return JsonResponse({
        "code": 200,
        "message": "updated",
        "data": {
            "id": slice_record.id,
            "project_id": slice_record.project_id,
            "mr_iid": slice_record.mr_iid,
            "start_time": slice_record.start_time.isoformat() if slice_record.start_time else None,
            "review_id": slice_record.review_id,
            "expert_model_name": slice_record.expert_model_name,
            "reflector_model_name": slice_record.reflector_model_name,
            "expert_duration": slice_record.expert_duration,
            "reflector_duration": slice_record.reflector_duration,
            "expert_comments": slice_record.expert_comments,
            "reflector_passed": slice_record.reflector_passed,
            "expert_retries": slice_record.expert_retries,
            "reflector_retries": slice_record.reflector_retries,
            "result": slice_record.result,
            "updated_at": slice_record.updated_at.isoformat() if slice_record.updated_at else None,
        }
    })
