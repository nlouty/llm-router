from django.db import models
from django.db.models import Q


ROUTER_RESULT_MAX_LENGTH = 500


class TimestampedSoftDeleteModel(models.Model):
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        abstract = True


class IP(TimestampedSoftDeleteModel):
    ip = models.CharField(max_length=50, unique=True)
    concurrent_multiplier = models.FloatField(default=1.0)
    vip = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "ips"


class Department(TimestampedSoftDeleteModel):
    dept1 = models.CharField(max_length=100, blank=True, default="")
    dept2 = models.CharField(max_length=100, blank=True, default="")
    dept3 = models.CharField(max_length=100, blank=True, default="")
    dept4 = models.CharField(max_length=100, blank=True, default="")
    is_allowed = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "departments"
        unique_together = (("dept1", "dept2", "dept3", "dept4"),)


class UserIP(TimestampedSoftDeleteModel):
    ip_id = models.IntegerField(unique=True, blank=True, null=True)
    user_name = models.CharField(max_length=100, blank=True, default="")
    user_charge = models.CharField(max_length=100, blank=True, default="")
    department_id = models.IntegerField(blank=True, null=True)
    employee_no = models.CharField(max_length=50, blank=True, default="")
    is_valid = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "user_ips"


class Model(models.Model):
    model_name = models.CharField(max_length=100, unique=True)
    concurrent_limit = models.IntegerField(blank=True, null=True, default=3)
    max_tokens = models.IntegerField(default=20480)
    vip = models.IntegerField(blank=True, null=True)
    deprecation = models.CharField(max_length=500, blank=True, null=True)
    is_routing_model = models.BooleanField(default=False)
    max_context_window = models.IntegerField(default=204800)

    class Meta:
        managed = False
        db_table = "models"


class Server(TimestampedSoftDeleteModel):
    model_id = models.IntegerField(blank=True, null=True)
    base_url = models.CharField(max_length=500, unique=True)
    is_online = models.BooleanField(default=True)
    weight = models.IntegerField(default=1)
    health_path = models.CharField(max_length=200, blank=True, default="/healthy")
    last_checked_at = models.DateTimeField(blank=True, null=True)
    last_failure_at = models.DateTimeField(blank=True, null=True)
    cache_time = models.IntegerField(default=3600)
    csb_token = models.CharField(max_length=500, blank=True, null=True)
    circuit_state = models.CharField(max_length=20, default="closed")
    consecutive_failures = models.IntegerField(default=0)
    last_state_change_at = models.DateTimeField(blank=True, null=True)
    cooldown_seconds = models.IntegerField(default=30)
    workload = models.IntegerField(default=0)
    vip = models.BooleanField(default=False)
    vip_cooldown = models.DateTimeField(blank=True, null=True)
    context_window = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "servers"


class RequestRecord(TimestampedSoftDeleteModel):
    user_ip_id = models.IntegerField()
    ip_id = models.IntegerField(blank=True, null=True)
    send_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    latency = models.BigIntegerField(blank=True, null=True)
    model_id = models.IntegerField()
    input_token_cnt = models.IntegerField(default=0)
    output_token_cnt = models.IntegerField(default=0)
    task_status = models.CharField(max_length=20)
    status = models.CharField(max_length=50, blank=True, null=True)
    fail_reason = models.CharField(max_length=200, blank=True, null=True)
    is_stream = models.BooleanField(blank=True, null=True)
    user_agent = models.CharField(max_length=500, blank=True, null=True)
    target_pod_ip = models.CharField(max_length=500, blank=True, null=True)
    attempt_count = models.IntegerField(default=0)
    prefix_cache = models.FloatField(default=0.0)
    final_prefix_cache = models.IntegerField(default=0)
    last_match = models.BigIntegerField(blank=True, null=True)
    router_result = models.CharField(max_length=ROUTER_RESULT_MAX_LENGTH, blank=True, null=True)
    estimate_tokens = models.IntegerField(default=0)
    model_choosing_latency = models.BigIntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "requests"
        indexes = [
            models.Index(
                name="idx_requests_concurrent_count",
                fields=["ip_id", "model_id"],
                condition=Q(task_status="processing"),
            ),
            models.Index(
                name="idx_req_proc_model_send",
                fields=["model_id", "send_time"],
                condition=Q(task_status="processing"),
            ),
            models.Index(
                name="idx_requests_processing_target",
                fields=["target_pod_ip"],
                condition=Q(task_status="processing"),
            ),
            models.Index(
                name="idx_requests_success_send",
                fields=["send_time"],
                condition=Q(task_status="success"),
            ),
            models.Index(
                name="idx_req_succ_model_send",
                fields=["model_id", "send_time"],
                condition=Q(task_status="success"),
            ),
            models.Index(
                name="idx_requests_model_send_ip",
                fields=["model_id", "send_time", "ip_id"],
                condition=Q(ip_id__isnull=False),
            ),
        ]


class Whitelist(models.Model):
    employee_no = models.CharField(max_length=50, blank=True, default="")
    user_name = models.CharField(max_length=100, blank=True, default="")
    is_allowed = models.IntegerField(blank=True, null=True)
    update_time = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "whitelist"


class ServerOperation(TimestampedSoftDeleteModel):
    server_id = models.IntegerField(blank=True, null=True)
    operation_type = models.CharField(max_length=50)
    request_data = models.JSONField(blank=True, null=True)
    response_data = models.JSONField(blank=True, null=True)
    status = models.CharField(max_length=20)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "server_operations"


class MrLiveReview(models.Model):
    project_name = models.CharField(max_length=200)
    source = models.CharField(max_length=50)
    discussion_id = models.CharField(max_length=100, unique=True)
    is_ai_comment = models.BooleanField()
    is_valid_ai_comment = models.BooleanField()
    rejected = models.BooleanField()
    target_branch = models.CharField(max_length=200)
    state = models.CharField(max_length=50)
    merge_request_iid = models.IntegerField()
    merge_url = models.TextField()
    assignee = models.CharField(max_length=200)
    resolved_by_committer = models.CharField(max_length=200)
    diff_file = models.CharField(max_length=500)
    severity = models.CharField(max_length=50)
    severity_cn = models.CharField(max_length=50)
    body = models.TextField()
    code = models.TextField()
    comment = models.TextField()
    categories = models.CharField(max_length=200)
    fix_suggestion = models.TextField()
    confidence_score = models.CharField(max_length=50)
    line = models.IntegerField()
    old_path = models.CharField(max_length=500)
    new_path = models.CharField(max_length=500)
    patchset_iid = models.IntegerField()
    author_name = models.CharField(max_length=200)
    created_at = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = "mr_live_review"


class CodehubReview(models.Model):
    id = models.AutoField(primary_key=True)
    project_id = models.IntegerField()
    branch = models.CharField(max_length=200)
    issue_hash = models.CharField(max_length=50, unique=True)
    mr_hash = models.CharField(max_length=50)
    file_path = models.CharField(max_length=500)
    line = models.IntegerField()
    body = models.TextField()
    review_comment = models.TextField()
    severity = models.CharField(max_length=50)
    categories = models.CharField(max_length=200)
    fix_suggestion = models.TextField()
    created_at = models.CharField(max_length=100)
    confidence_score = models.CharField(max_length=50)
    issue_url = models.TextField()

    class Meta:
        managed = False
        db_table = "codehub_review"
