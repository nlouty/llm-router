from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus

from django.db import models
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from router.models import RequestRecord


LLM_CHOOSING_IP_ID = 0
LLM_CHOOSING_USER_AGENT = "llm-choosing"


def is_processing_q() -> models.Q:
    """Return a Q object matching all in-flight (non-terminal) task_status values."""
    return models.Q(task_status__in=["processing", "prefilling", "decoding"])


_EXTRA_STATUS_PHRASES = {
    499: "Client Closed Request",
}


def _status_text(code: int) -> str:
    try:
        phrase = HTTPStatus(code).phrase
    except ValueError:
        phrase = _EXTRA_STATUS_PHRASES.get(code, "")
    return f"{code} {phrase}".rstrip()


class RequestRepository:
    @staticmethod
    def external_requests():
        return RequestRecord.objects.exclude(ip_id=LLM_CHOOSING_IP_ID)

    @staticmethod
    def create_processing(
        ip_id: int | None,
        model_id: int,
        is_stream: bool,
        user_agent: str | None,
        user_ip_id: int = 0,
        vip: bool = False,
        estimate_tokens: int = 0,
        session: str | None = None,
    ) -> RequestRecord:
        return RequestRecord.objects.create(
            user_ip_id=user_ip_id,
            vip=vip,
            ip_id=ip_id,
            send_time=timezone.now(),
            model_id=model_id,
            task_status="processing",
            is_stream=is_stream,
            user_agent=(user_agent or "")[:500],
            input_token_cnt=0,
            output_token_cnt=0,
            attempt_count=0,
            prefix_cache=0.0,
            final_prefix_cache=0,
            last_match=None,
            estimate_tokens=estimate_tokens,
            session=(session or "")[:255] or None,
        )

    @staticmethod
    def create_llm_choosing(
        model_id: int,
        target_pod_ip: str | None,
    ) -> RequestRecord:
        return RequestRecord.objects.create(
            user_ip_id=0,
            ip_id=LLM_CHOOSING_IP_ID,
            send_time=timezone.now(),
            model_id=model_id,
            task_status="processing",
            is_stream=False,
            user_agent=LLM_CHOOSING_USER_AGENT,
            input_token_cnt=0,
            output_token_cnt=0,
            target_pod_ip=target_pod_ip[:500] if target_pod_ip else None,
            attempt_count=1,
            prefix_cache=0.0,
            final_prefix_cache=0,
            last_match=None,
            estimate_tokens=0,
        )

    @staticmethod
    def create_blocked(
        ip_id: int | None,
        model_id: int,
        is_stream: bool | None,
        user_agent: str | None,
        status_code: int,
        fail_reason: str,
        user_ip_id: int = 0,
        vip: bool = False,
        estimate_tokens: int = 0,
    ) -> RequestRecord:
        now = timezone.now()
        return RequestRecord.objects.create(
            user_ip_id=user_ip_id,
            vip=vip,
            ip_id=ip_id,
            send_time=now,
            end_time=now,
            latency=0,
            model_id=model_id,
            input_token_cnt=0,
            output_token_cnt=0,
            task_status="failed",
            status=_status_text(status_code),
            fail_reason=fail_reason[:200],
            is_stream=is_stream,
            user_agent=(user_agent or "")[:500],
            attempt_count=0,
            prefix_cache=0.0,
            final_prefix_cache=0,
            last_match=None,
            estimate_tokens=estimate_tokens,
        )

    @staticmethod
    def finish(
        record: RequestRecord,
        http_status: int,
        reason: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        target_pod_ip: str | None = None,
        model_id: int | None = None,
        task_status: str | None = None,
        attempt_count: int | None = None,
        final_prefix_cache: int = 0,
        router_result: str | None = None,
        ttft: int | None = None,
        success_note: str | None = None,
    ) -> None:
        end_time = timezone.now()
        record.end_time = end_time
        record.latency = int((end_time - record.send_time).total_seconds() * 1000)
        record.status = _status_text(http_status)
        record.task_status = task_status or ("success" if 200 <= http_status < 300 else "failed")
        if record.task_status == "success":
            record.fail_reason = success_note[:200] if success_note else None
        else:
            record.fail_reason = reason[:200]
        record.input_token_cnt = input_tokens or 0
        record.output_token_cnt = output_tokens or 0
        record.final_prefix_cache = final_prefix_cache or 0
        if router_result:
            record.router_result = router_result[:300]
        update_fields = [
            "end_time",
            "latency",
            "status",
            "task_status",
            "fail_reason",
            "input_token_cnt",
            "output_token_cnt",
            "final_prefix_cache",
            "router_result",
        ]
        if target_pod_ip:
            record.target_pod_ip = target_pod_ip[:500]
            update_fields.append("target_pod_ip")
        if model_id is not None:
            record.model_id = model_id
            update_fields.append("model_id")
        if attempt_count is not None:
            record.attempt_count = attempt_count
            update_fields.append("attempt_count")
        if ttft is not None:
            record.ttft = ttft
            update_fields.append("ttft")
        record.save(update_fields=update_fields)

    @staticmethod
    def record_attempt(
        record: RequestRecord,
        target_pod_ip: str | None,
        attempt_count: int,
        prefix_cache: float | None = None,
        last_match: int | None = None,
    ) -> None:
        record.attempt_count = attempt_count
        update_fields = ["attempt_count"]
        if target_pod_ip:
            record.target_pod_ip = target_pod_ip[:500]
            update_fields.append("target_pod_ip")
        if prefix_cache is not None:
            record.prefix_cache = prefix_cache
            update_fields.append("prefix_cache")
        record.last_match = last_match
        update_fields.append("last_match")
        record.save(update_fields=update_fields)

    @staticmethod
    def record_model_choosing_latency(record: RequestRecord, latency_ms: int) -> None:
        record.model_choosing_latency = max(0, int(latency_ms))
        record.save(update_fields=["model_choosing_latency"])

    @staticmethod
    def cleanup_stale(model_id: int | None = None, threshold_minutes: int = 20, ip_id: int | None = None) -> int:
        from django.db import transaction

        from router.models import Server
        from router.repositories.servers import ServerRepository

        cutoff = timezone.now() - timedelta(minutes=threshold_minutes)
        qs = RequestRecord.objects.filter(send_time__lt=cutoff).filter(is_processing_q())
        if model_id:
            qs = qs.filter(model_id=model_id)
        if ip_id:
            qs = qs.filter(ip_id=ip_id)

        with transaction.atomic():
            stale_records = list(qs.select_for_update(skip_locked=True)[:100])
            if not stale_records:
                return 0

            # Separate records by status: each phase holds different resources.
            processing_targets: dict[str, int] = {}          # target_pod_ip -> count (non-PD)
            prefilling_targets: dict[str, int] = {}          # prefiller base_url -> count
            decoding_workload: dict[str, int] = {}           # decoder base_url -> count
            decoder_tokens: dict[str, float] = {}            # decoder base_url -> active_tokens sum
            record_ids = []

            for record in stale_records:
                record_ids.append(record.id)
                target = record.target_pod_ip or ""

                if record.task_status == "prefilling" and target.startswith("P: "):
                    prefiller_url = target[3:]
                    if prefiller_url:
                        prefilling_targets[prefiller_url] = prefilling_targets.get(prefiller_url, 0) + 1

                elif record.task_status == "decoding" and " -- D: " in target:
                    decoder_part = target.split(" -- D: ", 1)[1]
                    decoder_part = decoder_part.removesuffix(" -- KV_TRANS_FAIL")
                    if decoder_part:
                        decoding_workload[decoder_part] = decoding_workload.get(decoder_part, 0) + 1
                        decoder_tokens[decoder_part] = decoder_tokens.get(decoder_part, 0.0) + float(record.input_token_cnt or 0)

                elif record.task_status == "processing" and target:
                    processing_targets[target] = processing_targets.get(target, 0) + 1

            # Atomic status update
            RequestRecord.objects.filter(id__in=record_ids).update(
                task_status="incomplete",
                end_time=timezone.now(),
                fail_reason="stale processing",
            )

            # Atomic workload decrements
            if processing_targets:
                ServerRepository.decrement_workload_by_targets(processing_targets)

            if prefilling_targets:
                ServerRepository.decrement_workload_by_targets(prefilling_targets)

            # Decoder: decrement workload + release active_tokens
            if decoding_workload:
                decoder_urls = list(decoding_workload.keys())
                decoder_servers: dict[str, Server] = {
                    s.base_url: s for s in Server.objects.filter(base_url__in=decoder_urls)
                }
                for base_url, count in decoding_workload.items():
                    if count > 0:
                        Server.objects.filter(base_url=base_url, workload__gt=0).update(
                            workload=Greatest(F("workload") - count, Value(0))
                        )
                for base_url, tokens in decoder_tokens.items():
                    server = decoder_servers.get(base_url)
                    if server is not None and tokens > 0:
                        ServerRepository.release_active_tokens(server, tokens)

            return len(record_ids)

    @staticmethod
    def list_processing_for_concurrency(ip_id: int) -> list[dict]:
        """In-flight rows for an IP, excluding VIP traffic (``vip = TRUE``).

        VIP-channel requests are accounted separately by VIP scaling, so they
        must not be counted against a user's normal concurrency buckets.
        """
        return list(
            RequestRecord.objects.filter(ip_id=ip_id).filter(is_processing_q())
            .exclude(vip=True)
            .values("model_id", "router_result")
        )

    @staticmethod
    def list_recent_session_choices(session: str, since, limit: int = 10) -> list[dict]:
        """Recent committed model choices for one session, newest first.

        Only rows that already carry a concrete model and a router_result are
        returned; callers decide which of those results count as sticky anchors.
        """
        if not session:
            return []
        return list(
            RequestRecord.objects.filter(
                session=session,
                send_time__gte=since,
                model_id__gt=0,
                router_result__isnull=False,
            )
            .order_by("-send_time", "-id")
            .values("id", "model_id", "router_result")[:limit]
        )

    @staticmethod
    def count_processing_by_targets(targets: list[str]) -> dict[str, int]:
        if not targets:
            return {}
        return {
            row["target_pod_ip"]: row["count"]
            for row in RequestRecord.objects.filter(target_pod_ip__in=targets).filter(is_processing_q())
            .values("target_pod_ip")
            .annotate(count=models.Count("id"))
        }

    @staticmethod
    def count_vip_processing(model_id: int) -> int:
        return RequestRecord.objects.filter(
            vip=True, model_id=model_id
        ).filter(is_processing_q()).count()

    @staticmethod
    def count_distinct_ips(start: datetime, end: datetime) -> int:
        return (
            RequestRepository.external_requests()
            .filter(
                send_time__gte=start,
                send_time__lte=end,
                ip_id__isnull=False,
                task_status="success",
            )
            .values("ip_id")
            .distinct()
            .count()
        )

    @staticmethod
    def count_success_requests(start: datetime, end: datetime) -> int:
        return RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success",
        ).count()

    @staticmethod
    def count_success_requests_by_model(start: datetime, end: datetime, model_id: int) -> int:
        return RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success",
            model_id=model_id,
        ).count()

    @staticmethod
    def count_success_requests_grouped_by_model(start: datetime, end: datetime, model_ids: list[int]) -> dict[int, int]:
        if not model_ids:
            return {}
        return {
            row["model_id"]: row["count"]
            for row in RequestRepository.external_requests()
            .filter(
                send_time__gte=start,
                send_time__lte=end,
                task_status="success",
                model_id__in=model_ids,
            )
            .values("model_id")
            .annotate(count=models.Count("id"))
        }

    @staticmethod
    def average_latency_by_bucket(start: datetime, end: datetime, bucket_expr, model_id: int | None = None) -> dict:
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success",
            latency__isnull=False,
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        return {
            row["bucket"]: row["avg_latency"]
            for row in qs.annotate(bucket=bucket_expr).values("bucket").annotate(avg_latency=models.Avg("latency")).order_by("bucket")
        }

    @staticmethod
    def count_success_by_bucket(start: datetime, end: datetime, model_id: int | None, bucket_expr) -> dict:
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success",
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        
        return {
            row["bucket"]: row["count"]
            for row in qs.annotate(bucket=bucket_expr)
            .values("bucket")
            .annotate(count=models.Count("id"))
            .order_by("bucket")
        }

    @staticmethod
    def count_distinct_ips_by_bucket(start: datetime, end: datetime, model_id: int | None, bucket_expr) -> dict:
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success",
            ip_id__isnull=False,
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        
        return {
            row["bucket"]: row["count"]
            for row in qs.annotate(bucket=bucket_expr)
            .values("bucket")
            .annotate(count=models.Count("ip_id", distinct=True))
            .order_by("bucket")
        }

    @staticmethod
    def latency_rows_for_boxplot(start: datetime, end: datetime, model_ids: list[int]) -> list[dict]:
        if not model_ids:
            return []
        return list(
            RequestRepository.external_requests().filter(
                send_time__gte=start,
                send_time__lte=end,
                task_status="success",
                latency__isnull=False,
                model_id__in=model_ids,
            ).values("model_id", "send_time", "latency")
        )

    @staticmethod
    def sum_input_tokens(start: datetime, end: datetime, model_id: int | None = None) -> dict:
        """Calculate the sum of input_token_cnt and final_prefix_cache for the given time range.

        Args:
            start: Start datetime
            end: End datetime
            model_id: Optional model ID to filter by. If None, returns sum for all models.

        Returns:
            dict containing:
                - total_input: sum of input_token_cnt
                - cache_hit: sum of final_prefix_cache (命中)
                - cache_miss: total_input - cache_hit (未命中)
        """
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success"
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        result = qs.aggregate(
            total_input=models.Sum("input_token_cnt"),
            cache_hit=models.Sum("final_prefix_cache")
        )
        total_input = result["total_input"] or 0
        cache_hit = result["cache_hit"] or 0
        cache_miss = total_input - cache_hit
        return {
            "total_input": total_input,
            "cache_hit": cache_hit,
            "cache_miss": cache_miss
        }

    @staticmethod
    def sum_output_tokens(start: datetime, end: datetime, model_id: int | None = None) -> int:
        """Calculate the sum of output_token_cnt for the given time range.

        Args:
            start: Start datetime
            end: End datetime
            model_id: Optional model ID to filter by. If None, returns sum for all models.
        """
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success"
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        result = qs.aggregate(
            total_output=models.Sum("output_token_cnt")
        )
        return result["total_output"] or 0

    @staticmethod
    def sum_input_tokens_by_bucket(start: datetime, end: datetime, bucket_expr, model_id: int | None = None) -> dict:
        """Aggregate input_token_cnt and final_prefix_cache by time bucket.

        Args:
            start: Start datetime
            end: End datetime
            bucket_expr: Django Trunc function (TruncHour/TruncDay/TruncMonth)
            model_id: Optional model ID to filter by. If None, aggregates for all models.

        Returns:
            dict mapping bucket string -> {total_input, cache_hit, cache_miss}
        """
        from router.api.stats import format_bucket
        
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success"
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        
        # Determine granularity from bucket_expr type
        granularity = "hour"
        if "Day" in str(type(bucket_expr)):
            granularity = "day"
        elif "Month" in str(type(bucket_expr)):
            granularity = "month"
        
        rows = qs.annotate(bucket=bucket_expr).values("bucket").annotate(
            total_input=models.Sum("input_token_cnt"),
            cache_hit=models.Sum("final_prefix_cache")
        ).order_by("bucket")
        
        result = {}
        for row in rows:
            total_input = row["total_input"] or 0
            cache_hit = row["cache_hit"] or 0
            cache_miss = total_input - cache_hit
            # Convert bucket datetime to formatted string for lookup
            bucket_key = format_bucket(row["bucket"], granularity)
            result[bucket_key] = {
                "total_input": total_input,
                "cache_hit": cache_hit,
                "cache_miss": cache_miss
            }
        return result

    @staticmethod
    def sum_output_tokens_by_bucket(start: datetime, end: datetime, bucket_expr, model_id: int | None = None) -> dict:
        """Calculate the sum of output_token_cnt grouped by time bucket.

        Args:
            start: Start datetime
            end: End datetime
            bucket_expr: Django Trunc expression for bucketing
            model_id: Optional model ID to filter by. If None, returns sum for all models.

        Returns:
            Dict mapping formatted bucket string to total output tokens
        """
        from router.api.stats import format_bucket

        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success"
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)

        # Determine granularity from bucket_expr type
        granularity = "hour"
        if "Day" in str(type(bucket_expr)):
            granularity = "day"
        elif "Month" in str(type(bucket_expr)):
            granularity = "month"

        return {
            format_bucket(row["bucket"], granularity): row["total_output"]
            for row in qs.annotate(bucket=bucket_expr)
            .values("bucket")
            .annotate(total_output=models.Sum("output_token_cnt"))
            .order_by("bucket")
        }

    @staticmethod
    def summarize_success_by_employee_and_ip(
        start: datetime,
        end: datetime,
        dept1: str | None = None,
        dept2: str | None = None,
        dept3: str | None = None,
        dept4: str | None = None,
        employee_no: list[str] | None = None,
        user_name: list[str] | None = None,
        ip: list[str] | None = None,
    ) -> list[dict]:
        """
        聚合成功请求的token用量，按 <employee_no, ip> 作为分组键（issue #295）。

        employee_no 的解析优先级：
        1. 请求所用 apikey 对应 user_ips 行的 employee_no
           （请求的 user_ip_id 指向 apikey 行；行不存在或为空则降级）
        2. 请求 IP 对应 user_ips 行的 employee_no
        3. 仍缺失时用 "stale"

        每行同时返回 ip_employee_no（IP 的 employee_no），用于核对
        apikey 与 IP 是否属于同一人。

        Args:
            start: 开始时间
            end: 结束时间
            dept1: 一级部门，"all"表示所有
            dept2: 二级部门，"all"表示所有
            dept3: 三级部门，"all"表示所有
            dept4: 四级部门，"all"表示所有
            employee_no: 工号过滤列表（匹配解析后的 employee_no，可选）
            user_name: 用户名过滤列表（可选）
            ip: IP地址过滤列表（可选）

        Returns:
            按 (employee_no, ip) 聚合的字典列表，包含 access_count、
            prefix_cache、input_token、output_token、ip_employee_no，
            以及 employee_no 所属用户的 user_name、user_charge、dept1-4。
            access_count = 成功请求数
            prefix_cache = final_prefix_cache 的总和（前缀缓存命中 token）
            input_token = input_token_cnt 的总和
            output_token = output_token_cnt 的总和
        """
        from router.models import Ips, UserIP, Department

        # 按 (user_ip_id, ip_id) 聚合：user_ip_id 是请求时使用的凭证行
        # （带 apikey 请求指向 apikey 行），ip_id 是真实客户端 IP。
        qs = (
            RequestRepository.external_requests()
            .filter(
                send_time__gte=start,
                send_time__lte=end,
                task_status="success",
                ip_id__isnull=False,
            )
            .values("user_ip_id", "ip_id")
            .annotate(
                access_count=models.Count("id"),
                prefix_cache=models.Sum("final_prefix_cache"),
                input_token=models.Sum("input_token_cnt"),
                output_token=models.Sum("output_token_cnt"),
            )
        )

        groups = list(qs)
        if not groups:
            return []

        ip_ids = sorted({row["ip_id"] for row in groups})
        user_ip_ids = sorted({row["user_ip_id"] for row in groups if row["user_ip_id"]})

        # IP 地址
        ips_map = {
            obj.id: obj.ip
            for obj in Ips.objects.filter(id__in=ip_ids, deleted_at__isnull=True)
        }

        # 请求使用的凭证行（含 apikey 行）；软删除视为 apikey 不存在
        credential_rows = UserIP.objects.filter(id__in=user_ip_ids, deleted_at__isnull=True)
        credentials_map = {obj.id: obj for obj in credential_rows}

        # IP 归属的 user_ips 行：提供 ip_employee_no，以及 employee_no 降级来源
        ip_user_map = {
            obj.ip_id: obj
            for obj in UserIP.objects.filter(
                ip_id__in=ip_ids,
                is_valid=True,
                deleted_at__isnull=True,
            )
        }

        # 解析每个聚合组的 employee_no 及其归属行（用户/部门信息取自该行）
        def resolve_owner(credential, ip_user) -> UserIP | None:
            if credential is not None and credential.apikey and credential.employee_no:
                return credential
            if ip_user is not None and ip_user.employee_no:
                return ip_user
            return None

        STALE = "stale"

        # 同一 (employee_no, ip) 可能来自多个 user_ip_id（如多把空工号 key
        # 降级到同一 IP），在 Python 内二次归并
        merged: dict[tuple[str, str], dict] = {}
        for row in groups:
            ip_id = row["ip_id"]
            credential = credentials_map.get(row["user_ip_id"])
            ip_user = ip_user_map.get(ip_id)
            owner = resolve_owner(credential, ip_user)
            resolved_employee_no = owner.employee_no if owner is not None else STALE

            key = (resolved_employee_no, ips_map.get(ip_id, ""))
            entry = merged.setdefault(key, {
                "employee_no": resolved_employee_no,
                "ip": key[1],
                "ip_employee_no": ip_user.employee_no if ip_user is not None else "",
                "access_count": 0,
                "prefix_cache": 0,
                "input_token": 0,
                "output_token": 0,
                "owner": owner,
            })
            entry["access_count"] += row["access_count"]
            entry["prefix_cache"] += row["prefix_cache"] or 0
            entry["input_token"] += row["input_token"] or 0
            entry["output_token"] += row["output_token"] or 0

        # 部门信息取自 employee_no 归属行
        dept_ids = [entry["owner"].department_id for entry in merged.values() if entry["owner"] and entry["owner"].department_id]
        departments_map = {
            dept.id: dept
            for dept in Department.objects.filter(id__in=dept_ids, deleted_at__isnull=True)
        }

        # 应用过滤条件并组装结果
        results = []
        for entry in merged.values():
            owner = entry.pop("owner")
            dept = departments_map.get(owner.department_id) if owner and owner.department_id else None

            if dept1 and dept1 != "all" and (dept.dept1 if dept else "") != dept1:
                continue
            if dept2 and dept2 != "all" and (dept.dept2 if dept else "") != dept2:
                continue
            if dept3 and dept3 != "all" and (dept.dept3 if dept else "") != dept3:
                continue
            if dept4 and dept4 != "all" and (dept.dept4 if dept else "") != dept4:
                continue
            if employee_no and entry["employee_no"] not in employee_no:
                continue
            if user_name and ((owner.user_name if owner else "") or "") not in user_name:
                continue
            if ip and entry["ip"] not in ip:
                continue

            entry["user_name"] = owner.user_name if owner else ""
            entry["user_charge"] = owner.user_charge if owner else ""
            entry["dept1"] = dept.dept1 if dept else ""
            entry["dept2"] = dept.dept2 if dept else ""
            entry["dept3"] = dept.dept3 if dept else ""
            entry["dept4"] = dept.dept4 if dept else ""
            results.append(entry)

        # 按访问次数从高到低排序
        results.sort(key=lambda x: x["access_count"], reverse=True)

        return results
