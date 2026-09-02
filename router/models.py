from django.db import models
from django.db.models import Q


class TimestampedSoftDeleteModel(models.Model):
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        abstract = True


class Ips(TimestampedSoftDeleteModel):
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
    manager = models.CharField(max_length=100, blank=True, default="")
    is_allowed = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "departments"
        unique_together = (("dept1", "dept2", "dept3", "dept4"),)


class UserIP(TimestampedSoftDeleteModel):
    ip_id = models.IntegerField(default=0)
    apikey = models.CharField(max_length=255, blank=True, default="")
    vip = models.BooleanField(default=False)
    user_name = models.CharField(max_length=100, blank=True, default="")
    user_charge = models.CharField(max_length=100, blank=True, default="")
    department_id = models.IntegerField(blank=True, null=True)
    employee_no = models.CharField(max_length=50, blank=True, default="")
    is_valid = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "user_ips"
        constraints = [
            models.UniqueConstraint(
                fields=["ip_id"],
                condition=Q(ip_id__gt=0),
                name="uniq_user_ips_nonzero_ip",
            ),
            models.UniqueConstraint(
                fields=["apikey"],
                condition=~Q(apikey=""),
                name="uniq_user_ips_nonempty_apikey",
            ),
            models.UniqueConstraint(
                fields=["employee_no"],
                condition=~Q(apikey="") & Q(is_valid=True, deleted_at__isnull=True),
                name="uniq_user_ips_active_emp_key",
            ),
            models.CheckConstraint(
                check=Q(ip_id__gt=0, apikey="") | (Q(ip_id=0) & ~Q(apikey="")),
                name="user_ips_credential_xor",
            ),
        ]
        indexes = [
            models.Index(name="idx_user_ips_employee_no", fields=["employee_no"]),
        ]


class Model(models.Model):
    model_name = models.CharField(max_length=100, unique=True)
    concurrent_limit = models.IntegerField(blank=True, null=True, default=3)
    max_tokens = models.IntegerField(default=20480)
    vip = models.IntegerField(blank=True, null=True)
    deprecation = models.CharField(max_length=500, blank=True, null=True)
    is_routing_model = models.BooleanField(default=False)
    auto = models.BooleanField(default=False)
    complexity_min = models.IntegerField(blank=True, null=True)
    complexity_max = models.IntegerField(blank=True, null=True)
    multimodal = models.BooleanField(default=False)
    model_path = models.CharField(max_length=500, blank=True, null=True)

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
    api_key = models.CharField(max_length=500, blank=True, null=True)
    circuit_state = models.CharField(max_length=20, default="closed")
    consecutive_failures = models.IntegerField(default=0)
    last_state_change_at = models.DateTimeField(blank=True, null=True)
    cooldown_seconds = models.IntegerField(default=30)
    workload = models.IntegerField(default=0)
    vip = models.BooleanField(default=False)
    vip_cooldown = models.DateTimeField(blank=True, null=True)
    context_window = models.IntegerField(blank=True, null=True)
    role = models.CharField(max_length=32, blank=True, default="mixed")
    group_id = models.CharField(max_length=64, blank=True, null=True)
    active_tokens = models.FloatField(default=0.0)

    class Meta:
        managed = False
        db_table = "servers"


class ExternalRoute(TimestampedSoftDeleteModel):
    """One employee's passthrough route to an external provider (issue #287).

    Provider fields (``name``, ``base_url``) are denormalized per row — one
    row per (provider, employee), because every employee uses their own
    ``api_key`` — and the circuit-breaker fields are always updated for the
    whole ``base_url`` group so every employee of a provider sees the same
    circuit state.
    """

    name = models.CharField(max_length=100)
    base_url = models.CharField(max_length=500)
    employee_no = models.CharField(max_length=50)
    api_key = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    model_mapping_policy = models.IntegerField()
    circuit_state = models.CharField(max_length=20, default="closed")
    consecutive_failures = models.IntegerField(default=0)
    last_state_change_at = models.DateTimeField(blank=True, null=True)
    cooldown_seconds = models.IntegerField(default=30)

    class Meta:
        managed = False
        db_table = "external_routes"
        constraints = [
            models.UniqueConstraint(
                fields=["employee_no"],
                condition=Q(is_active=True, deleted_at__isnull=True),
                name="uniq_external_routes_active",
            ),
        ]


class ExternalModelMapping(TimestampedSoftDeleteModel):
    """Model-name mapping for one external provider policy (issue #287).

    ``internal_model_name`` is the router-facing name clients request: exactly
    ``models.model_name`` when the model is also served internally, or the
    exposed alias for a provider-only model (which has no ``models`` row).
    Rows are grouped by ``policy_id``; ``external_routes.model_mapping_policy``
    references the same value.
    """

    policy_id = models.IntegerField()
    internal_model_name = models.CharField(max_length=100)
    external_model_name = models.CharField(max_length=200)
    is_enabled = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "external_model_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["policy_id", "internal_model_name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_external_model_mappings_active",
            ),
        ]


PREFILLER_ROLES = ("prefiller", "prefix-prefiller")


def is_prefiller_role(role: str | None) -> bool:
    """True for both prefiller styles: ``prefiller`` (n-prefiller, takes new
    requests) and ``prefix-prefiller`` (p-prefiller, takes prefix-cached
    requests; issue #276). Mixed servers and decoders are never prefillers."""
    return (role or "mixed") in PREFILLER_ROLES


class RequestRecord(TimestampedSoftDeleteModel):
    user_ip_id = models.IntegerField()
    vip = models.BooleanField(default=False)
    ip_id = models.IntegerField(blank=True, null=True)
    send_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    latency = models.BigIntegerField(blank=True, null=True)
    ttft = models.BigIntegerField(blank=True, null=True)
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
    router_result = models.CharField(max_length=300, blank=True, null=True)
    estimate_tokens = models.IntegerField(default=0)
    model_choosing_latency = models.BigIntegerField(blank=True, null=True)
    prefill_latency = models.BigIntegerField(blank=True, null=True)
    decode_latency = models.BigIntegerField(blank=True, null=True)
    session = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "requests"
        indexes = [
            models.Index(
                name="idx_requests_concurrent_count",
                fields=["ip_id", "model_id"],
                condition=Q(task_status__in=["processing", "prefilling", "decoding"]),
            ),
            models.Index(
                name="idx_req_proc_model_send",
                fields=["model_id", "send_time"],
                condition=Q(task_status__in=["processing", "prefilling", "decoding"]),
            ),
            models.Index(
                name="idx_requests_processing_target",
                fields=["target_pod_ip"],
                condition=Q(task_status__in=["processing", "prefilling", "decoding"]),
            ),
            models.Index(
                name="idx_requests_proc_sendtime",
                fields=["send_time"],
                condition=Q(task_status__in=["processing", "prefilling", "decoding"]),
            ),
            models.Index(
                name="idx_req_vip_proc_model",
                fields=["model_id"],
                condition=Q(task_status__in=["processing", "prefilling", "decoding"], vip=True),
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
            models.Index(
                name="idx_requests_session_send",
                fields=["session", "send_time"],
            ),
        ]


class Whitelist(models.Model):
    employee_no = models.CharField(max_length=50, blank=True, default="")
    user_name = models.CharField(max_length=100, blank=True, default="")
    is_allowed = models.IntegerField(blank=True, null=True)
    expire_time = models.DateTimeField(blank=True, null=True)
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


class DailyMrReview(models.Model):
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
        db_table = "daily_mr_review"


class LiveReviewRequest(TimestampedSoftDeleteModel):
    id = models.AutoField(primary_key=True)
    project_name = models.CharField(max_length=200)
    merge_requests_id = models.IntegerField()
    merge_url = models.TextField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    duration_seconds = models.IntegerField(blank=True, null=True)
    expert_model_id = models.IntegerField(blank=True, null=True)
    reflect_model_id = models.IntegerField(blank=True, null=True)
    review_file_num = models.IntegerField(default=0)
    diff_part_num = models.IntegerField(default=0)
    review_num = models.IntegerField(default=0)

    class Meta:
        managed = False
        db_table = "live_review_requests"


class CodehubReview(TimestampedSoftDeleteModel):
    id = models.AutoField(primary_key=True)
    project_id = models.IntegerField()
    project_name = models.CharField(max_length=200)
    branch_name = models.CharField(max_length=200)
    scan_commit_id = models.CharField(max_length=100)
    scan_date = models.DateTimeField()
    completion_date = models.DateTimeField(blank=True, null=True)
    relative_path = models.CharField(max_length=500)
    line = models.IntegerField()
    issue_description = models.TextField()
    severity = models.CharField(max_length=50)
    issue_category = models.CharField(max_length=200)
    module = models.CharField(max_length=200)
    first_level_confirmer = models.CharField(max_length=200, blank=True, null=True)
    second_level_confirmer = models.CharField(max_length=200, blank=True, null=True)
    is_modified = models.BooleanField(default=False, blank=True, null=True)
    is_valid_issue = models.BooleanField(default=False, blank=True, null=True)
    is_modified_completed = models.BooleanField(default=False, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    need_analysis = models.BooleanField(null=True, blank=True)
    conclusion = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "codehub_review"


class AiAssistantUserFeedback(TimestampedSoftDeleteModel):
    DOMAIN_CHOICES = [
        ("知识管理", "知识管理"),
        ("辅助设计", "辅助设计"),
        ("代码分析", "代码分析"),
        ("问题定位", "问题定位"),
        ("Agent", "Agent"),
        ("公共", "公共"),
    ]

    PRIORITY_CHOICES = [
        ("高", "高"),
        ("中", "中"),
        ("低", "低"),
    ]

    STATUS_CHOICES = [
        ("open", "open"),
        ("close", "close"),
        ("cancel", "cancel"),
    ]

    id = models.AutoField(primary_key=True)
    domain = models.CharField(max_length=50, choices=DOMAIN_CHOICES)
    tool_version = models.CharField(max_length=100, blank=True, null=True)
    issue_description = models.TextField()
    reporter = models.CharField(max_length=200)
    reported_at = models.DateTimeField()
    priority = models.CharField(
        max_length=20, choices=PRIORITY_CHOICES, blank=True, null=True
    )
    assignee = models.CharField(max_length=200, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    estimated_resolution_at = models.DateTimeField(blank=True, null=True)
    actual_resolution_at = models.DateTimeField(blank=True, null=True)
    bugfix_version = models.CharField(max_length=100, blank=True, null=True)
    progress_tracking = models.TextField(blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "ai_assistant_user_feedback"


class ReviewSlices(TimestampedSoftDeleteModel):
    id = models.AutoField(primary_key=True)
    project_id = models.CharField(max_length=100)
    mr_iid = models.CharField(max_length=100)
    start_time = models.DateTimeField()
    review_id = models.CharField(max_length=100)
    expert_model_name = models.CharField(max_length=200)
    reflector_model_name = models.CharField(max_length=200)
    expert_duration = models.FloatField(blank=True, null=True)
    reflector_duration = models.FloatField(blank=True, null=True)
    expert_comments = models.IntegerField(blank=True, null=True)
    reflector_passed = models.IntegerField(blank=True, null=True)
    expert_retries = models.IntegerField(blank=True, null=True)
    reflector_retries = models.IntegerField(blank=True, null=True)
    result = models.CharField(max_length=500, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "review_slices"


class ReviewSummary(TimestampedSoftDeleteModel):
    id = models.AutoField(primary_key=True)
    project_id = models.CharField(max_length=100)
    mr_iid = models.CharField(max_length=100)
    start_time = models.DateTimeField()
    review_id = models.CharField(max_length=100)
    expert_model_name = models.CharField(max_length=200)
    reflector_model_name = models.CharField(max_length=200)
    file_modified_count = models.IntegerField(blank=True, null=True)
    total_duration = models.FloatField(blank=True, null=True)
    slice_count = models.IntegerField(blank=True, null=True)
    expert_avg_duration = models.FloatField(blank=True, null=True)
    expert_trigger_count = models.IntegerField(blank=True, null=True)
    expert_total_comments = models.IntegerField(blank=True, null=True)
    expert_avg_comments = models.FloatField(blank=True, null=True)
    expert_total_retries = models.IntegerField(blank=True, null=True)
    reflector_avg_duration = models.FloatField(blank=True, null=True)
    reflector_trigger_count = models.IntegerField(blank=True, null=True)
    reflector_total_comments = models.IntegerField(blank=True, null=True)
    reflector_avg_comments = models.FloatField(blank=True, null=True)
    reflector_total_retries = models.IntegerField(blank=True, null=True)
    reflector_total_passed = models.IntegerField(blank=True, null=True)
    timeout = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "review_summary"
