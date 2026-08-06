from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus

from django.db import models
from django.utils import timezone

from router.models import RequestRecord


LLM_CHOOSING_IP_ID = 0
LLM_CHOOSING_USER_AGENT = "llm-choosing"


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
        user_ip_id: int = 1,
        estimate_tokens: int = 0,
    ) -> RequestRecord:
        return RequestRecord.objects.create(
            user_ip_id=user_ip_id,
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
        )

    @staticmethod
    def create_llm_choosing(
        model_id: int,
        target_pod_ip: str | None,
    ) -> RequestRecord:
        return RequestRecord.objects.create(
            user_ip_id=1,
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
        user_ip_id: int = 1,
        estimate_tokens: int = 0,
    ) -> RequestRecord:
        now = timezone.now()
        return RequestRecord.objects.create(
            user_ip_id=user_ip_id,
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
    ) -> None:
        end_time = timezone.now()
        record.end_time = end_time
        record.latency = int((end_time - record.send_time).total_seconds() * 1000)
        record.status = _status_text(http_status)
        record.task_status = task_status or ("success" if 200 <= http_status < 300 else "failed")
        record.fail_reason = None if record.task_status == "success" else reason[:200]
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
        from router.repositories.servers import ServerRepository

        cutoff = timezone.now() - timedelta(minutes=threshold_minutes)
        qs = RequestRecord.objects.filter(task_status="processing", send_time__lt=cutoff)
        if model_id:
            qs = qs.filter(model_id=model_id)
        if ip_id:
            qs = qs.filter(ip_id=ip_id)

        # Batch process up to 100 stale records in a single transaction.
        # This ensures that workload decrements perfectly match the records 
        # being marked as incomplete, even with concurrent cleanup attempts.
        # skip_locked=True prevents multiple requests from blocking on the same stale records.
        with transaction.atomic():
            stale_records = list(qs.select_for_update(skip_locked=True)[:100])
            if not stale_records:
                return 0

            target_counts = {}
            record_ids = []
            for record in stale_records:
                record_ids.append(record.id)
                if record.target_pod_ip:
                    target_counts[record.target_pod_ip] = target_counts.get(record.target_pod_ip, 0) + 1

            # Atomic status update
            RequestRecord.objects.filter(id__in=record_ids).update(
                task_status="incomplete",
                end_time=timezone.now(),
                fail_reason="stale processing",
            )
            # Atomic workload decrement
            if target_counts:
                ServerRepository.decrement_workload_by_targets(target_counts)
            return len(record_ids)

    @staticmethod
    def list_processing_for_concurrency(ip_id: int) -> list[dict]:
        """In-flight rows for an IP, excluding VIP-sentinel traffic (user_ip_id == 2).

        VIP-channel requests are accounted separately by VIP scaling, so they
        must not be counted against a user's normal concurrency buckets.
        """
        return list(
            RequestRecord.objects.filter(ip_id=ip_id, task_status="processing")
            .exclude(user_ip_id=2)
            .values("model_id", "router_result")
        )

    @staticmethod
    def count_processing_by_targets(targets: list[str]) -> dict[str, int]:
        if not targets:
            return {}
        return {
            row["target_pod_ip"]: row["count"]
            for row in RequestRecord.objects.filter(task_status="processing", target_pod_ip__in=targets)
            .values("target_pod_ip")
            .annotate(count=models.Count("id"))
        }

    @staticmethod
    def count_vip_processing(model_id: int) -> int:
        return RequestRecord.objects.filter(
            task_status="processing", user_ip_id=2, model_id=model_id
        ).count()

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
            Dict mapping bucket to total output tokens
        """
        qs = RequestRepository.external_requests().filter(
            send_time__gte=start,
            send_time__lte=end,
            task_status="success"
        )
        if model_id is not None:
            qs = qs.filter(model_id=model_id)
        return {
            row["bucket"]: row["total_output"]
            for row in qs.annotate(bucket=bucket_expr)
            .values("bucket")
            .annotate(total_output=models.Sum("output_token_cnt"))
            .order_by("bucket")
        }

    @staticmethod
    def count_success_by_ip_with_user_info(
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
        聚合查询每个IP的成功请求数，关联用户和部门信息。

        Args:
            start: 开始时间
            end: 结束时间
            dept1: 一级部门，"all"表示所有
            dept2: 二级部门，"all"表示所有
            dept3: 三级部门，"all"表示所有
            dept4: 四级部门，"all"表示所有
            employee_no: 工号过滤列表（可选）
            user_name: 用户名过滤列表（可选）
            ip: IP地址过滤列表（可选）

        Returns:
            包含ip、access_count、input_token、output_token、user_name、user_charge、employee_no、dept1-4的字典列表
            input_token = input_token_cnt 的总和
            output_token = output_token_cnt 的总和
        """
        from router.models import Ips, UserIP, Department

        # 构建基础查询
        qs = (
            RequestRepository.external_requests()
            .filter(
                send_time__gte=start,
                send_time__lte=end,
                task_status="success",
                ip_id__isnull=False,
            )
            .values("ip_id")
            .annotate(
                access_count=models.Count("id"),
                input_token=models.Sum("input_token_cnt"),
                output_token=models.Sum("output_token_cnt"),
            )
        )

        # 获取聚合结果
        ip_counts = {
            row["ip_id"]: {
                "access_count": row["access_count"],
                "input_token": row["input_token"] or 0,
                "output_token": row["output_token"] or 0,
            }
            for row in qs
        }

        if not ip_counts:
            return []

        # 构建关联查询：ips -> user_ips -> departments
        ip_ids = list(ip_counts.keys())

        # 查询ips表获取ip地址
        ips_query = Ips.objects.filter(id__in=ip_ids, deleted_at__isnull=True)
        ips_map = {ip.id: ip.ip for ip in ips_query}

        # 查询user_ips表
        user_ips_query = UserIP.objects.filter(
            ip_id__in=ip_ids,
            is_valid=True,
            deleted_at__isnull=True
        ).select_related()

        user_ips_map = {
            user_ip.ip_id: {
                "user_name": user_ip.user_name,
                "user_charge": user_ip.user_charge,
                "employee_no": user_ip.employee_no,
                "department_id": user_ip.department_id,
            }
            for user_ip in user_ips_query
        }

        # 获取所有涉及的部门ID
        dept_ids = [info["department_id"] for info in user_ips_map.values() if info["department_id"]]

        # 查询departments表
        departments_query = Department.objects.filter(id__in=dept_ids, deleted_at__isnull=True)
        departments_map = {
            dept.id: {
                "dept1": dept.dept1,
                "dept2": dept.dept2,
                "dept3": dept.dept3,
                "dept4": dept.dept4,
            }
            for dept in departments_query
        }

        # 组装结果并应用部门过滤
        results = []
        for ip_id, stats in ip_counts.items():
            user_info = user_ips_map.get(ip_id, {})
            department_id = user_info.get("department_id")
            dept_info = departments_map.get(department_id, {}) if department_id else {}

            # 应用部门过滤条件
            if dept1 and dept1 != "all" and dept_info.get("dept1") != dept1:
                continue
            if dept2 and dept2 != "all" and dept_info.get("dept2") != dept2:
                continue
            if dept3 and dept3 != "all" and dept_info.get("dept3") != dept3:
                continue
            if dept4 and dept4 != "all" and dept_info.get("dept4") != dept4:
                continue

            # 应用新增的过滤条件
            if employee_no and user_info.get("employee_no") not in employee_no:
                continue
            if user_name and user_info.get("user_name") not in user_name:
                continue
            ip_addr = ips_map.get(ip_id, "")
            if ip and ip_addr not in ip:
                continue

            results.append({
                "ip": ips_map.get(ip_id, ""),
                "access_count": stats["access_count"],
                "input_token": stats["input_token"],
                "output_token": stats["output_token"],
                "user_name": user_info.get("user_name", ""),
                "user_charge": user_info.get("user_charge", ""),
                "employee_no": user_info.get("employee_no", ""),
                "dept1": dept_info.get("dept1", ""),
                "dept2": dept_info.get("dept2", ""),
                "dept3": dept_info.get("dept3", ""),
                "dept4": dept_info.get("dept4", ""),
            })

        # 按访问次数从高到低排序
        results.sort(key=lambda x: x["access_count"], reverse=True)

        return results
