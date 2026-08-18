# API Endpoints

## Health

```http
GET /healthy
```

Returns `200` when the app and database are healthy. Returns `503` when the database check fails.

## Proxy

```http
ANY /v1/<path>
```

All `/v1/*` requests are proxied to an online upstream server. The router preserves the incoming path and query string and appends them to the selected server `base_url`.

Request handling includes:

- client IP creation from `X-Forwarded-For` or `REMOTE_ADDR`
- permission checks through `user_ips`, `departments`, and `whitelist`
- opencode version blocking
- JSON body parsing, `max_tokens` defaulting, and streaming `include_usage` injection
- exact `model: auto` and concrete-model auto routing
- normal-port small-request routing
- VIP-channel eligibility and pool scaling
- retry, circuit breaker, workload accounting, and request lifecycle logging

Example:

```bash
curl -i http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"test-model","messages":[{"role":"user","content":"hi"}]}'
```

Auto-routing example:

```bash
curl -i http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Summarize this design risk"}]}'
```

`GET /v1/models` is special-cased: when no concrete model is present, the router chooses a random online routable server and forwards the request there.

## Load-Balancer Metadata

The default chooser is `PrefixCachePrebleServerChooser`. Before each backend attempt, the router records:

- `requests.target_pod_ip`: selected server `base_url`
- `requests.attempt_count`: attempt number for the client request
- `requests.prefix_cache`: best prefix-cache match ratio found for that attempt
- `requests.last_match`: historical request id that produced the best match, or `NULL`

If `match_ratio > prefix_cache.primary_match_threshold` (`0.9` by default), the chooser picks the least-loaded cached server. If no primary match exists but some server has `match_ratio > prefix_cache.secondary_match_threshold` (`0.5` by default), it picks the least-loaded server from that secondary set. Otherwise it picks the least-loaded candidate overall.

Prefix cache blocks are measured in Unicode characters with `prefix_cache.prefix_block_chars` (`128` by default). Prefix cache metadata is written only after a successful upstream response.

Candidate servers are filtered by model, VIP pool, online state, soft delete, and circuit-breaker state (never by estimated request size; `servers.context_window` only gates the context-overflow retry).

## Statistics APIs

All time-range statistics endpoints require Beijing-local query parameters:

```text
start_time=YYYY-MM-DD HH:mm:ss
end_time=YYYY-MM-DD HH:mm:ss
```

The range is inclusive. Bucketed endpoints choose granularity automatically: hour for ranges up to 2 days, day for ranges up to 31 days, and month for longer ranges. Internal routing LLM rows (`ip_id = 0`) are excluded from external request statistics.

| Endpoint | Method | Required params | Optional params | Description |
|----------|--------|-----------------|-----------------|-------------|
| `/api/request_stats` | GET | `start_time`, `end_time` | | Distinct successful requesting IP count. |
| `/api/total_request_count` | GET | `start_time`, `end_time` | | Successful external request count. |
| `/api/input_token` | GET | `start_time`, `end_time` | `model_name`; use `total` or omit for all models | Sum of input tokens with cache hit/miss breakdown. |
| `/api/output_token` | GET | `start_time`, `end_time` | `model_name`; use `total` or omit for all models | Sum of output tokens. |
| `/api/model_request_stats` | GET | `start_time`, `end_time`, `model_name` | | Successful request count for one model. |
| `/api/all_model_request_stats` | GET | `start_time`, `end_time` | `model_name` | Successful request counts grouped by model, or one model when provided. |
| `/api/request_time_stats` | GET | `start_time`, `end_time` | | Average latency series for all models. |
| `/api/model_request_time_stats` | GET | `start_time`, `end_time`, `model_name` | | Average latency series for one model. |
| `/api/model_request_count_by_period` | GET | `start_time`, `end_time` | `model_name`; omit for all models | Bucketed successful request count, optionally filtered by model. |
| `/api/model_ip_count_by_period` | GET | `start_time`, `end_time` | `model_name`; omit for all models | Bucketed distinct IP count, optionally filtered by model. |
| `/api/model_latency_boxplot` | GET | `start_time`, `end_time` | `model_names` comma list | Per-model latency boxplot data. Drops latencies above 890 seconds from quartiles and reports their ratio. |
| `/api/access_stats_by_department` | GET | `start_time`, `end_time` | `dept1`, `dept2`, `dept3`, `dept4`, `employee_no`, `user_name`, `ip`; use `all` or omit department params for any department. `employee_no`, `user_name`, `ip` support multiple values via comma-separated (`employee_no=EMP001,EMP002`) or repeated params (`employee_no=EMP001&employee_no=EMP002`) | Aggregates successful requests by IP with user and department info. Filters by department levels and optional user/IP filters when provided. |

### Input Token Cache Statistics

`/api/input_token` returns prefix cache statistics alongside total input tokens:

**Response fields:**
- `total_input_tokens`: Sum of `input_token_cnt`
- `cache_hit`: Sum of `final_prefix_cache` (prefix cache hits)
- `cache_miss`: `total_input_tokens - cache_hit` (prefix cache misses)

**Example response (all models):**
```json
{
  "code": 200,
  "total_input_tokens": 1250000,
  "cache_hit": 280000,
  "cache_miss": 970000
}
```

**Example response (specific model):**
```json
{
  "code": 200,
  "model_name": "test-model",
  "total_input_tokens": 500000,
  "cache_hit": 120000,
  "cache_miss": 380000
}
```

Example:

```bash
curl 'http://localhost:8001/api/model_request_count_by_period?model_name=test-model&start_time=2026-06-01%2000:00:00&end_time=2026-06-02%2023:59:59'
```

## Model Catalog APIs

```http
GET /api/models
GET /api/model_online_list
GET /api/model_info?model_name=<name>
```

`/api/models` returns all model rows with `id`, `model_name`, and `concurrent_limit`.

`/api/model_online_list` returns model names whose `deprecation` is `NULL`.

`/api/model_info` returns `model_name` and `concurrent_limit` for a single model, or `404` when it is not found.

## AI Assistant Download

```http
GET /api/download/ai_assistant
```

Downloads `/home/AI_Assistant/AI_Assistant.exe` as `application/octet-stream`. Returns `404` when the file is missing.

## Whitelist Update

```http
POST /api/whitelist/update
```

Upserts a whitelist entry by `employee_no`.

```bash
curl -i -X POST http://localhost:8001/api/whitelist/update \
  -H 'Content-Type: application/json' \
  -d '{"employee_no":"E001","is_allowed":1}'
```

`is_allowed` must be `0` or `1`.

## API-Key Registration

```http
POST /api/apikey
```

Requests registration of an employee API key. This endpoint is unauthenticated and must not be exposed to untrusted callers. Keys are never returned in the response.

```bash
curl -i -X POST http://localhost:8001/api/apikey \
  -H 'Content-Type: application/json' \
  -d '{"apikey":"employee-key","employee_no":"E001"}'
```

After validating the JSON fields, the endpoint rejects an `employee_no` that already has an active apikey with HTTP 409 before performing any write. Otherwise it delegates lookup and all database writes to `CMDBService.fetch_and_save_apikey(apikey, employee_no)`. The internal CMDB adapter owns employee lookup, department data, VIP inheritance, idempotency, conflict handling, and key rotation. The public CMDB adapter is unimplemented, so the endpoint returns HTTP 404 until an internal adapter provides this method.

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "employee_no": "E001"
  }
}
```

When the employee already has an active apikey:

```json
{
  "code": 409,
  "error": "employee already has an active apikey"
}
```

This first-stage endpoint only stores credentials. Proxy requests do not resolve or apply registered API keys yet.

## API-Key Lookup

```http
GET /api/apikey?employee_no=E001
```

Looks up a single employee's active apikey. `employee_no` is required; omitting it returns HTTP 400. The full key value is never returned — only a masked preview of its first and last four characters.

```bash
curl -i 'http://localhost:8001/api/apikey?employee_no=E001'
```

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "employee_no": "E001",
    "apikey_preview": "abcd…wxyz",
    "is_valid": true,
    "created_at": "2026-08-05T06:42:47+00:00",
    "updated_at": "2026-08-05T06:42:47+00:00"
  }
}
```

Returns HTTP 404 when the employee has no active apikey.

## API-Key Invalidation

```http
POST /api/apikey/invalidate
```

Deletes an employee's active apikey row. The key immediately stops authenticating proxy requests, and the per-employee slot is released so a new key can be registered (key rotation).

```bash
curl -i -X POST http://localhost:8001/api/apikey/invalidate \
  -H 'Content-Type: application/json' \
  -d '{"employee_no":"E001"}'
```

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "employee_no": "E001"
  }
}
```

Returns HTTP 404 when the employee has no active apikey to invalidate.

## Whitelist List

```http
GET /api/whitelist/list
```

Retrieves whitelist entries with optional pagination. Results are ordered by `update_time` descending.

Query parameters (both optional):

- `page`: Page number starting from 1
- `page_size`: Number of records per page (max 100)

If both parameters are provided, returns paginated results. Otherwise returns all records.

Response format with pagination:

```json
{
  "code": 200,
  "data": [
    {
      "id": 1,
      "employee_no": "E001",
      "user_name": "张三",
      "is_allowed": 1,
      "update_time": "2026-06-25 10:00:00"
    },
    {
      "id": 2,
      "employee_no": "E002",
      "user_name": "李四",
      "is_allowed": 0,
      "update_time": "2026-06-24 15:30:00"
    }
  ],
  "total": 50,
  "page": 1,
  "page_size": 10
}
```

Response format without pagination:

```json
{
  "code": 200,
  "data": [
    {
      "id": 1,
      "employee_no": "E001",
      "user_name": "张三",
      "is_allowed": 1,
      "update_time": "2026-06-25 10:00:00"
    }
  ],
  "total": 50
}
```

Example - get all records:

```bash
curl 'http://localhost:8001/api/whitelist/list'
```

Example - get paginated records:

```bash
curl 'http://localhost:8001/api/whitelist/list?page=1&page_size=10'
```

## IP List with User Info

```http
GET /api/ip/list
```

Retrieves IP addresses with concurrent multiplier and associated user/department information. Supports optional pagination and filtering. Results are sorted by concurrent_multiplier in descending order (highest concurrent multiplier first).

Query parameters (all optional):

- `page`: Page number starting from 1
- `page_size`: Number of records per page (max 100)
- `employee_no`: Filter by employee number (partial match)
- `ip`: Filter by IP address (partial match)

If both `page` and `page_size` are provided, returns paginated results. Otherwise returns all records matching the filters.

Response format with pagination:

```json
{
  "code": 200,
  "data": [
    {
      "id": 1,
      "ip": "192.168.1.100",
      "concurrent_multiplier": 2.0,
      "vip": false,
      "employee_no": "EMP001",
      "user_name": "张三",
      "user_charge": "产品经理",
      "dept1": "技术部",
      "dept2": "研发中心",
      "dept3": "后端组",
      "dept4": "平台研发"
    },
    {
      "id": 2,
      "ip": "192.168.1.101",
      "concurrent_multiplier": 1.5,
      "vip": true,
      "employee_no": "EMP002",
      "user_name": "李四",
      "user_charge": "开发工程师",
      "dept1": "技术部",
      "dept2": "研发中心",
      "dept3": "前端组",
      "dept4": ""
    }
  ],
  "total": 50,
  "page": 1,
  "page_size": 10
}
```

Response format without pagination:

```json
{
  "code": 200,
  "data": [
    {
      "id": 1,
      "ip": "192.168.1.100",
      "concurrent_multiplier": 2.0,
      "vip": false,
      "employee_no": "EMP001",
      "user_name": "张三",
      "user_charge": "产品经理",
      "dept1": "技术部",
      "dept2": "研发中心",
      "dept3": "后端组",
      "dept4": "平台研发"
    }
  ],
  "total": 50
}
```

Example - get all IPs with user info:

```bash
curl 'http://localhost:8001/api/ip/list'
```

Example - get paginated IPs:

```bash
curl 'http://localhost:8001/api/ip/list?page=1&page_size=10'
```

Example - filter by employee number:

```bash
curl 'http://localhost:8001/api/ip/list?employee_no=EMP001'
```

Example - filter by IP address:

```bash
curl 'http://localhost:8001/api/ip/list?ip=192.168.1'
```

Example - combined filters with pagination:

```bash
curl 'http://localhost:8001/api/ip/list?page=1&page_size=10&employee_no=EMP&ip=192.168'
```

Notes:

- IPs without associated user records will have empty user and department fields
- Filtering supports partial matching (case-insensitive contains)
- Results are ordered by IP ID ascending


## Refresh User Info

```http
POST /api/refresh_user_info
```

Starts the CMDB user refresh flow in a background thread. Requires `cmdb.enabled: true`; otherwise returns `403`.

```bash
curl -i -X POST http://localhost:8001/api/refresh_user_info
```

## Add Server

```http
POST /api/add_server
```

Registers one or more upstream servers. The endpoint verifies that `<base_url>/models` advertises the requested `model_name` before persisting the row. `base_url` must end with `/v1`. All operations are logged to `server_operations`.

Single request body:

```json
{
  "base_url": "http://10.1.2.3:8000/v1",
  "model_name": "gpt-3.5-turbo"
}
```

Multiple request body:

```json
[
  {
    "base_url": "http://10.1.2.3:8000/v1",
    "model_name": "gpt-3.5-turbo"
  },
  {
    "base_url": "http://10.1.2.4:8000/v1",
    "model_name": "gpt-3.5-turbo"
  }
]
```

Duplicate `base_url` values within a single request are rejected before any operation row is created. For list payloads, individual items can succeed or fail independently.

## MR Live Review APIs

```http
POST /api/mr_live_review
GET /api/mr_live_review/stats
GET /api/mr_live_review/stats_by_confidence
GET /api/mr_live_review/stats_by_date
GET /api/mr_live_review/list
GET /api/mr_live_review/list_by_confidence
```

`POST /api/mr_live_review` upserts by unique `discussion_id`. If the row exists and `state` is unchanged, the request is skipped. If `state` changes, all provided fields are updated. Payload keys must match `MrLiveReview` model fields except `id`.

Stats endpoints:

- `/api/mr_live_review/stats?project_name=<project>` groups valid, invalid, no-reply counts and accept rate by `target_branch`.
- `/api/mr_live_review/stats_by_confidence?project_name=<project>` groups by `confidence_score`.
- `/api/mr_live_review/stats_by_date` requires `project_name`, `target_branch`, `stats`, `start_date`, and `end_date`. `stats` must be one of `valid`, `invalid`, `no_reply`, `total`, or `accept_rate`. Dates use `YYYY-MM-DD`.

List endpoints:

- `/api/mr_live_review/list` requires `project_name`, `target_branch`, and `type`.
- `/api/mr_live_review/list_by_confidence` requires `project_name` and `type`; `confidence_score` is optional.
- `type` must be `valid`, `invalid`, or `no_reply`.
- `page` defaults to `1`; `page_size` defaults to `10` and must be at most `100`.

## CodeHub Review API

```http
POST /api/codehub_review
```

Creates a CodeHub review row. Payload keys must match `CodehubReview` model fields.

Fields include:
- `project_id`: Project identifier
- `project_name`: Project name
- `branch_name`: Branch name
- `scan_commit_id`: Commit ID that was scanned
- `scan_date`: Scan date (format: `YYYY-MM-DD HH:mm:ss`)
- `completion_date`: Completion date (optional, format: `YYYY-MM-DD HH:mm:ss`)
- `relative_path`: File path relative to project root
- `line`: Line number
- `issue_description`: Description of the issue
- `severity`: Issue severity level
- `issue_category`: Issue category
- `module`: Module name
- `first_level_confirmer`: First level confirmer (optional)
- `second_level_confirmer`: Second level confirmer (optional)
- `is_modified`: Whether the issue has been modified (default: `false`)
- `is_valid_issue`: Whether this is a valid issue (default: `false`)
- `is_modified_completed`: Whether the modification has been completed (default: `false`, auto-set if not provided)
- `need_analysis`: Whether the issue requires further analysis (optional)
- `conclusion`: Analysis conclusion or resolution notes (optional)
- `notes`: Additional notes (optional)

Example:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": 123,
    "project_name": "my-project",
    "branch_name": "main",
    "scan_commit_id": "abc123",
    "scan_date": "2026-06-25 10:00:00",
    "relative_path": "src/main.py",
    "line": 42,
    "issue_description": "Potential null pointer",
    "severity": "high",
    "issue_category": "bug",
    "module": "core",
    "is_modified": false,
    "is_valid_issue": true
  }'
```

Note: The `is_modified_completed` field defaults to `false` if not explicitly provided in the request.

## CodeHub Review Statistics API

```http
GET /api/codehub_review/stats
```

Retrieves statistical information about CodeHub review issues. All query parameters are optional.

Query parameters (all optional):

- `project_name`: Filter by project name (exact match)
- `branch_name`: Filter by branch name (exact match)
- `start_time`: Start time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)

If no parameters are provided, returns statistics for all records.

Response format:

```json
{
  "code": 200,
  "data": {
    "total_count": 150,
    "valid_issue_count": 80,
    "invalid_issue_count": 70,
    "modified_completed_count": 45,
    "severity": {
      "high": 25,
      "medium": 60,
      "low": 50,
      "critical": 15
    },
    "latest_scan_commit_id": "abc123def456"
  }
}
```

Response fields:

- `total_count`: Total number of records matching the filter
- `valid_issue_count`: Number of records where `is_valid_issue` is `true`
- `invalid_issue_count`: Number of records where `is_valid_issue` is `false`
- `modified_completed_count`: Number of records where `is_modified_completed` is `true`
- `severity`: Object with severity types as keys and their counts as values
- `latest_scan_commit_id`: Most recent `scan_commit_id` based on `scan_date` ordering, or `null` if no records exist

Example - get all statistics:

```bash
curl 'http://localhost:8001/api/codehub_review/stats'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/stats?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/stats?project_name=my-project&branch_name=main'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/stats?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/stats?project_name=my-project&branch_name=main&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

## CodeHub Review Category Statistics API

```http
GET /api/codehub_review/category_stats
```

Retrieves detailed issue category statistics from CodeHub review records. For each issue category type, returns total count, valid issue count, invalid issue count, and modified completed count. All query parameters are optional.

Query parameters (all optional):

- `project_name`: Filter by project name (exact match)
- `branch_name`: Filter by branch name (exact match)
- `start_time`: Start time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)

If no parameters are provided, returns statistics for all records.

Response format:

```json
{
  "code": 200,
  "data": {
    "Code Smell": {
      "count": 45,
      "valid_issue_count": 30,
      "invalid_issue_count": 15,
      "modified_completed_count": 20
    },
    "Bug": {
      "count": 32,
      "valid_issue_count": 25,
      "invalid_issue_count": 7,
      "modified_completed_count": 18
    },
    "Vulnerability": {
      "count": 18,
      "valid_issue_count": 10,
      "invalid_issue_count": 8,
      "modified_completed_count": 5
    },
    "Security Hotspot": {
      "count": 12,
      "valid_issue_count": 5,
      "invalid_issue_count": 7,
      "modified_completed_count": 3
    },
    "Maintainability": {
      "count": 25,
      "valid_issue_count": 15,
      "invalid_issue_count": 10,
      "modified_completed_count": 8
    },
    "Reliability": {
      "count": 18,
      "valid_issue_count": 12,
      "invalid_issue_count": 6,
      "modified_completed_count": 7
    }
  }
}
```

Response fields per category type:

- `count`: Total number of records for this category type
- `valid_issue_count`: Number of records where `is_valid_issue` is `true` for this category type
- `invalid_issue_count`: Number of records where `is_valid_issue` is `false` for this category type
- `modified_completed_count`: Number of records where `is_modified_completed` is `true` for this category type

Example - get all category statistics:

```bash
curl 'http://localhost:8001/api/codehub_review/category_stats'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/category_stats?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/category_stats?project_name=my-project&branch_name=main'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/category_stats?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/category_stats?project_name=my-project&branch_name=main&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

## CodeHub Review Severity Statistics API

```http
GET /api/codehub_review/severity_stats
```

Retrieves detailed severity-level statistics from CodeHub review records. For each severity type, returns total count, valid issue count, invalid issue count, and modified completed count. All query parameters are optional.

Query parameters (all optional):

- `project_name`: Filter by project name (exact match)
- `branch_name`: Filter by branch name (exact match)
- `start_time`: Start time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time based on scan_date (format: `YYYY-MM-DD HH:mm:ss`)

If no parameters are provided, returns statistics for all records.

Response format:

```json
{
  "code": 200,
  "data": {
    "critical": {
      "count": 15,
      "valid_issue_count": 10,
      "invalid_issue_count": 5,
      "modified_completed_count": 8
    },
    "high": {
      "count": 25,
      "valid_issue_count": 18,
      "invalid_issue_count": 7,
      "modified_completed_count": 12
    },
    "medium": {
      "count": 60,
      "valid_issue_count": 35,
      "invalid_issue_count": 25,
      "modified_completed_count": 20
    },
    "low": {
      "count": 50,
      "valid_issue_count": 17,
      "invalid_issue_count": 33,
      "modified_completed_count": 5
    }
  }
}
```

Response fields per severity type:

- `count`: Total number of records for this severity type
- `valid_issue_count`: Number of records where `is_valid_issue` is `true` for this severity type
- `invalid_issue_count`: Number of records where `is_valid_issue` is `false` for this severity type
- `modified_completed_count`: Number of records where `is_modified_completed` is `true` for this severity type

Example - get all severity statistics:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_stats'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_stats?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_stats?project_name=my-project&branch_name=main'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_stats?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_stats?project_name=my-project&branch_name=main&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

### CodehubReview List API

```http
GET /api/codehub_review/list
```

查询 CodehubReview 表数据列表，支持多条件过滤和分页。所有参数均为可选。

**分页行为说明**：
- 若 **不传** `page` 和 `page_size` 参数，返回 **全量数据**（不分页）
- 若 **传入** `page` 或 `page_size` 参数，则进行分页（默认 page=1, page_size=10）

Query parameters (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_name` | string | 项目名称筛选 |
| `branch_name` | string | 分支名称筛选 |
| `relative_path` | string or string[] | 相对路径筛选（支持模糊匹配，可传入多个值，用逗号分隔或多次传参） |
| `severity` | string or string[] | 严重级别筛选（可传入多个值，用逗号分隔或多次传参） |
| `issue_category` | string or string[] | 问题类别筛选（可传入多个值，用逗号分隔或多次传参） |
| `page` | integer | 页码（默认 1，仅分页模式有效） |
| `page_size` | integer | 每页大小（默认 10，最大 100，仅分页模式有效） |
| `start_time` | string | 开始时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |
| `end_time` | string | 结束时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |

**多值参数说明**：

`relative_path`、`severity`、`issue_category` 三个参数支持传入多个值，有两种方式：

1. **逗号分隔**：在单个参数值中用逗号分隔多个值
   ```bash
   # 筛选 severity 为 critical 或 high
   curl 'http://localhost:8001/api/codehub_review/list?severity=critical,high'
   
   # 筛选 relative_path 包含 src/main 或 src/utils
   curl 'http://localhost:8001/api/codehub_review/list?relative_path=src/main,src/utils'
   ```

2. **多次传参**：同一个参数名多次传递（标准 HTTP 多值参数方式）
   ```bash
   # 筛选 severity 为 critical 或 high
   curl 'http://localhost:8001/api/codehub_review/list?severity=critical&severity=high'
   
   # 筛选 issue_category 为 security 或 performance
   curl 'http://localhost:8001/api/codehub_review/list?issue_category=security&issue_category=performance'
   ```

两种方式可以混合使用，最终结果为所有值的合集筛选。

Response JSON:

**分页模式（传入 page 或 page_size）**：

```json
{
  "code": 200,
  "data": {
    "total_count": 150,
    "total_pages": 15,
    "current_page": 1,
    "page_size": 10,
    "has_next": true,
    "has_previous": false,
    "items": [
      {
        "id": 123,
        "project_id": 1,
        "project_name": "my-project",
        "branch_name": "main",
        "scan_commit_id": "abc123",
        "scan_date": "2026-06-15T10:30:00+08:00",
        "completion_date": "2026-06-16T15:00:00+08:00",
        "relative_path": "src/main.py",
        "line": 42,
        "issue_description": "Potential null pointer dereference",
        "severity": "critical",
        "issue_category": "security",
        "module": "core",
        "first_level_confirmer": "user1",
        "second_level_confirmer": "user2",
        "is_modified": false,
        "is_valid_issue": true,
        "is_modified_completed": false,
        "need_analysis": true,
        "conclusion": "Analysis completed, requires refactoring",
        "notes": null,
        "created_at": "2026-06-15T10:30:00+08:00",
        "updated_at": "2026-06-15T10:30:00+08:00"
      }
    ]
  }
}
```

**全量模式（不传 page 和 page_size）**：

```json
{
  "code": 200,
  "data": {
    "total_count": 150,
    "items": [
      {
        "id": 123,
        "project_id": 1,
        ...
      },
      {
        "id": 124,
        ...
      }
    ]
  }
}
```

Example - get all reviews without pagination (returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list'
```

Example - filter by project (returns full data, no pagination params):

```bash
curl 'http://localhost:8001/api/codehub_review/list?project_name=my-project'
```

Example - filter by project and branch (returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?project_name=my-project&branch_name=main'
```

Example - filter by severity (single value, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?severity=critical'
```

Example - filter by severity (multiple values, comma-separated, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?severity=critical,high,medium'
```

Example - filter by severity (multiple values, repeated parameter, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?severity=critical&severity=high'
```

Example - filter by issue category (single value, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?issue_category=security'
```

Example - filter by issue category (multiple values, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?issue_category=security,performance'
```

Example - filter by relative path (single value, fuzzy match, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?relative_path=src/main'
```

Example - filter by relative path (multiple values, fuzzy match, returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?relative_path=src/main,src/utils,tests/'
```

Example - filter by time range (returns full data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - custom pagination (returns paginated data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?page=2&page_size=20'
```

Example - combined filters with pagination (returns paginated data):

```bash
curl 'http://localhost:8001/api/codehub_review/list?project_name=my-project&branch_name=main&severity=critical,high&page=1&page_size=20&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters with pagination (multiple issue categories with multiple relative paths):

```bash
curl 'http://localhost:8001/api/codehub_review/list?project_name=my-project&issue_category=security,performance&relative_path=src/api,src/auth&page=1&page_size=50'
```

### CodehubReview Update API

```http
POST /api/codehub_review/update
```

更新 CodehubReview 表中的记录。必传参数为 `id`，可选修改参数至少需提供一个。

**自动更新字段**：调用此接口时，`updated_at` 字段会自动更新为当前时间。

Request body (JSON):

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | integer | **Yes** | 记录ID |
| `module` | string | No | 模块名称 |
| `first_level_confirmer` | string | No | 一级确认人 |
| `second_level_confirmer` | string | No | 二级确认人 |
| `is_valid_issue` | boolean | No | 是否为有效问题 |
| `is_modified` | boolean | No | 是否已修改 |
| `is_modified_completed` | boolean | No | 是否修改完成 |
| `need_analysis` | boolean | No | 是否需要进一步分析 |
| `conclusion` | string | No | 分析结论或解决方案 |
| `notes` | string | No | 备注 |

注意：`id` 为必传参数，可选修改参数中至少需要提供一个字段。

Response JSON (success):

```json
{
  "code": 200,
  "message": "updated",
  "data": {
    "id": 123,
    "module": "auth",
    "first_level_confirmer": "user1",
    "second_level_confirmer": "user2",
    "is_valid_issue": true,
    "is_modified": true,
    "is_modified_completed": true,
    "need_analysis": false,
    "conclusion": null,
    "notes": "Issue confirmed and fixed",
    "updated_at": "2026-06-30T15:30:00+08:00"
  }
}
```

Response JSON (not found):

```json
{
  "code": 404,
  "error": "CodehubReview with id 123 not found"
}
```

Response JSON (missing fields):

```json
{
  "code": 400,
  "error": "at least one field to update is required"
}
```

Example - update module:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{"id": 123, "module": "authentication"}'
```

Example - update confirmation fields:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{"id": 123, "first_level_confirmer": "zhang_san", "second_level_confirmer": "li_si"}'
```

Example - update validity status:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{"id": 123, "is_valid_issue": true}'
```

Example - update modification status:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{"id": 123, "is_modified": true, "is_modified_completed": true}'
```

Example - update notes:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{"id": 123, "notes": "Issue verified and fixed in commit abc123"}'
```

Example - update multiple fields:

```bash
curl -i -X POST http://localhost:8001/api/codehub_review/update \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 123,
    "module": "security",
    "first_level_confirmer": "security_team",
    "is_valid_issue": true,
    "is_modified_completed": true,
    "notes": "Critical security issue fixed"
  }'
```

### CodehubReview Relative Path List API

```http
GET /api/codehub_review/relative_path_list
```

查询 CodehubReview 表中 relative_path 的去重列表，支持多条件过滤。所有参数均为可选，若无参数则返回全量 relative_path 种类列表（去重并按字母顺序排序）。

Query parameters (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_name` | string | 项目名称筛选 |
| `branch_name` | string | 分支名称筛选 |
| `severity` | string | 严重级别筛选 |
| `issue_category` | string | 问题类别筛选 |
| `start_time` | string | 开始时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |
| `end_time` | string | 结束时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |

Response JSON:

```json
{
  "code": 200,
  "data": {
    "total_count": 45,
    "relative_paths": [
      "src/auth/login.py",
      "src/auth/session.py",
      "src/core/config.py",
      "src/models/user.py",
      "tests/test_auth.py"
    ]
  }
}
```

Example - get all relative paths:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?project_name=my-project&branch_name=main'
```

Example - filter by severity:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?severity=critical'
```

Example - filter by issue category:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?issue_category=security'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/relative_path_list?project_name=my-project&branch_name=main&severity=critical&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

### CodehubReview Severity List API

```http
GET /api/codehub_review/severity_list
```

查询 CodehubReview 表中 severity 的去重列表，支持多条件过滤。所有参数均为可选，若无参数则返回全量 severity 种类列表（去重并按字母顺序排序）。

Query parameters (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_name` | string | 项目名称筛选 |
| `branch_name` | string | 分支名称筛选 |
| `relative_path` | string | 相对路径筛选（支持模糊匹配） |
| `issue_category` | string | 问题类别筛选 |
| `start_time` | string | 开始时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |
| `end_time` | string | 结束时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |

Response JSON:

```json
{
  "code": 200,
  "data": {
    "total_count": 4,
    "severities": [
      "critical",
      "high",
      "low",
      "medium"
    ]
  }
}
```

Example - get all severities:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?project_name=my-project&branch_name=main'
```

Example - filter by relative path (fuzzy match):

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?relative_path=src/auth'
```

Example - filter by issue category:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?issue_category=security'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/severity_list?project_name=my-project&branch_name=main&relative_path=src&issue_category=security&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

### CodehubReview Issue Category List API

```http
GET /api/codehub_review/issue_category_list
```

查询 CodehubReview 表中 issue_category 的去重列表，支持多条件过滤。所有参数均为可选，若无参数则返回全量 issue_category 种类列表（去重并按字母顺序排序）。

Query parameters (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_name` | string | 项目名称筛选 |
| `branch_name` | string | 分支名称筛选 |
| `relative_path` | string | 相对路径筛选（支持模糊匹配） |
| `severity` | string | 严重级别筛选 |
| `start_time` | string | 开始时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |
| `end_time` | string | 结束时间（基于 scan_date，格式：YYYY-MM-DD HH:MM:SS） |

Response JSON:

```json
{
  "code": 200,
  "data": {
    "total_count": 5,
    "issue_categories": [
      "Bug",
      "Code Smell",
      "Maintainability",
      "Reliability",
      "Vulnerability"
    ]
  }
}
```

Example - get all issue categories:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list'
```

Example - filter by project:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?project_name=my-project'
```

Example - filter by project and branch:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?project_name=my-project&branch_name=main'
```

Example - filter by relative path (fuzzy match):

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?relative_path=src/auth'
```

Example - filter by severity:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?severity=critical'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/codehub_review/issue_category_list?project_name=my-project&branch_name=main&relative_path=src&severity=critical&start_time=2026-06-01%2000:00:00&end_time=2026-06-30%2023:59:59'
```

## Daily MR Review API

```http
POST /api/daily_mr_review
```

Creates a daily MR review row when `issue_hash` is new. If `issue_hash` already exists, the request is skipped and returns success without modification.

Required fields:

- `project_id`: Project identifier
- `branch`: Target branch name
- `issue_hash`: Unique issue identifier computed from content and location
- `mr_hash`: Merge request identifier
- `file_path`: File path where the issue was found
- `line`: Line number
- `body`: Issue body or code snippet
- `review_comment`: AI-generated review comment
- `severity`: Issue severity level
- `categories`: Issue category labels
- `fix_suggestion`: Suggested fix
- `created_at`: Timestamp string when the issue was created
- `confidence_score`: Review confidence level
- `issue_url`: URL to the issue

Example:

```bash
curl -i -X POST http://localhost:8001/api/daily_mr_review \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": 123,
    "branch": "main",
    "issue_hash": "abc123def456",
    "mr_hash": "mr789",
    "file_path": "src/utils.py",
    "line": 42,
    "body": "def calculate(): return x / y",
    "review_comment": "Potential division by zero",
    "severity": "high",
    "categories": "bug,safety",
    "fix_suggestion": "Add zero check before division",
    "created_at": "2026-06-25 10:00:00",
    "confidence_score": "0.95",
    "issue_url": "https://gitlab.example.com/issues/123"
  }'
```

## Live Review Request API

```http
POST /api/live_review_requests
```

Creates a live review request record to track MR review sessions. Model ID fields accept either integer model IDs or model name strings, which are automatically resolved to IDs. `duration_seconds` is automatically calculated from `start_time` and `end_time` when both are provided.

Required fields:

- `project_name`: Project name
- `merge_requests_id`: Merge request ID
- `merge_url`: URL to the merge request
- `start_time`: Review start time (format: `YYYY-MM-DD HH:mm:ss`)

Optional fields:

- `end_time`: Review end time (format: `YYYY-MM-DD HH:mm:ss`)
- `expert_model_id`: Model ID or name used in expert review phase
- `reflect_model_id`: Model ID or name used in reflection phase
- `review_file_num`: Number of files reviewed (default: `0`)
- `diff_part_num`: Number of diff parts analyzed (default: `0`)
- `review_num`: Number of review comments generated (default: `0`)

Example with model names:

```bash
curl -i -X POST http://localhost:8001/api/live_review_requests \
  -H 'Content-Type: application/json' \
  -d '{
    "project_name": "llm-router",
    "merge_requests_id": 456,
    "merge_url": "https://gitlab.example.com/project/llm-router/-/merge_requests/456",
    "start_time": "2026-06-25 09:00:00",
    "end_time": "2026-06-25 09:15:00",
    "expert_model_id": "gpt-4",
    "reflect_model_id": "claude-3-opus",
    "review_file_num": 5,
    "diff_part_num": 12,
    "review_num": 8
  }'
```

Example with model IDs:

```bash
curl -i -X POST http://localhost:8001/api/live_review_requests \
  -H 'Content-Type: application/json' \
  -d '{
    "project_name": "llm-router",
    "merge_requests_id": 457,
    "merge_url": "https://gitlab.example.com/project/llm-router/-/merge_requests/457",
    "start_time": "2026-06-25 10:00:00",
    "expert_model_id": 1,
    "reflect_model_id": 2
  }'
```

## Concurrent Multiplier Update API

```http
POST /api/concurrent_multiplier/update
```

Updates the `concurrent_multiplier` field for an IP address. Requires either `employee_no` or `ip` (not both), and `concurrent_multiplier` (must be >= 1.0).

```bash
curl -i -X POST http://localhost:8001/api/concurrent_multiplier/update \
  -H 'Content-Type: application/json' \
  -d '{"employee_no":"E001","concurrent_multiplier":2.0}'
```

Or by IP:

```bash
curl -i -X POST http://localhost:8001/api/concurrent_multiplier/update \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.100","concurrent_multiplier":1.5}'
```

## AI Assistant User Feedback API

```http
POST /api/ai_assistant_user_feedback
```

Creates an AI Assistant user feedback record. Required fields: `domain` (one of: 知识管理, 辅助设计, 代码分析, 问题定位, Agent, 公共), `issue_description`, `reporter`, `reported_at`, `status` (one of: open, close, cancel). Optional fields include `tool_version`, `priority` (高/中/低), `assignee`, `estimated_resolution_at`, `actual_resolution_at`, `bugfix_version`, `progress_tracking`, and `remarks`.

```bash
curl -i -X POST http://localhost:8001/api/ai_assistant_user_feedback \
  -H 'Content-Type: application/json' \
  -d '{
    "domain": "代码分析",
    "issue_description": "代码分析功能响应缓慢",
    "reporter": "张三",
    "reported_at": "2026-06-25 10:00:00",
    "status": "open",
"priority": "高"
  }'
  ```

```http
POST /api/ai_assistant_user_feedback/update
```

Updates an existing AI Assistant user feedback record by ID. At least one field must be provided for update.

Required field:

- `id`: Record ID (integer)

Optional update fields (at least one required):

- `domain`: Domain (one of: 知识管理, 辅助设计, 代码分析, 问题定位, Agent, 公共)
- `tool_version`: Tool version
- `issue_description`: Issue description
- `reporter`: Reporter name
- `reported_at`: Reported time (format: YYYY-MM-DD HH:MM:SS)
- `priority`: Priority (one of: 高, 中, 低)
- `assignee`: Assignee
- `status`: Status (one of: open, close, cancel)
- `estimated_resolution_at`: Estimated resolution time (format: YYYY-MM-DD HH:MM:SS)
- `actual_resolution_at`: Actual resolution time (format: YYYY-MM-DD HH:MM:SS)
- `bugfix_version`: Bugfix version
- `progress_tracking`: Progress tracking notes
- `remarks`: Remarks

Response format:

```json
{
  "code": 200,
  "message": "updated",
  "data": {
    "id": 1,
    "domain": "代码分析",
    "tool_version": "v1.2.0",
    "issue_description": "代码分析功能响应缓慢",
    "reporter": "张三",
    "reported_at": "2026-06-25 10:00:00",
    "priority": "高",
    "assignee": "李四",
    "status": "close",
    "estimated_resolution_at": "2026-06-30 18:00:00",
    "actual_resolution_at": "2026-06-28 16:30:00",
    "bugfix_version": "v1.2.1",
    "progress_tracking": "问题已定位并修复",
    "remarks": "优化了查询性能",
    "updated_at": "2026-06-28 16:30:00"
  }
}
```

Error responses:

- `400`: Invalid request (missing id, invalid field values, or no fields provided for update)
- `404`: Record not found
- `500`: Server error

Example - update status and assignee:

```bash
curl -i -X POST http://localhost:8001/api/ai_assistant_user_feedback/update \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "status": "close",
    "assignee": "李四",
    "actual_resolution_at": "2026-06-28 16:30:00",
    "bugfix_version": "v1.2.1"
  }'
```

Example - update priority:

```bash
curl -i -X POST http://localhost:8001/api/ai_assistant_user_feedback/update \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "priority": "中"
  }'
```

## Access Stats by Department API

```http
GET /api/access_stats_by_department
```

Aggregates successful request counts by IP address with associated user and department information. Filters results by department levels when provided. Results are sorted by access_count in descending order (highest access count first).

Query parameters:

- `start_time`: Start time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)
- `dept1`: Level 1 department filter (optional; use `all` or omit to include all)
- `dept2`: Level 2 department filter (optional; use `all` or omit to include all)
- `dept3`: Level 3 department filter (optional; use `all` or omit to include all)
- `dept4`: Level 4 department filter (optional; use `all` or omit to include all)
- `employee_no`: Employee number filter (optional; exact match)
- `user_name`: User name filter (optional; exact match)
- `ip`: IP address filter (optional; exact match)

The endpoint performs the following:

1. Queries successful requests (`task_status="success"`) within the time range
2. Aggregates by `ip_id` to count requests per IP
3. Joins `ips` table to retrieve IP addresses
4. Joins `user_ips` table to retrieve user information (`user_name`, `user_charge`, `employee_no`)
5. Joins `departments` table to retrieve department hierarchy (`dept1`-`dept4`)
6. Filters results by department parameters when provided

Response format:

```json
{
  "code": 200,
  "data": [
    {
      "ip": "192.168.1.100",
      "access_count": 1520,
      "input_token": 1250000,
      "output_token": 380000,
      "user_name": "张三",
      "user_charge": "产品经理",
      "employee_no": "EMP001",
      "dept1": "技术部",
      "dept2": "研发中心",
      "dept3": "后端组",
      "dept4": "平台研发"
    },
    {
      "ip": "192.168.1.101",
      "access_count": 890,
      "input_token": 780000,
      "output_token": 210000,
      "user_name": "李四",
      "user_charge": "开发工程师",
      "employee_no": "EMP002",
      "dept1": "技术部",
      "dept2": "研发中心",
      "dept3": "前端组",
      "dept4": ""
    }
  ],
  "total": 2,
  "start_time": "2026-06-24 00:00:00",
  "end_time": "2026-06-25 23:59:59"
}
```

Field descriptions:

- `ip`: IP address
- `access_count`: Number of successful requests from this IP
- `input_token`: Total input tokens (input_token_cnt) for this IP
- `output_token`: Total output tokens (output_token_cnt) for this IP
- `user_name`: User name associated with this IP
- `user_charge`: User role/position
- `employee_no`: Employee number
- `dept1`-`dept4`: Department hierarchy levels

Example - query all departments:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59'
```

Example - filter by level 1 department:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&dept1=技术部'
```

Example - filter by multiple department levels:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&dept1=技术部&dept2=研发中心&dept3=后端组'
```

Example - filter by employee number:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&employee_no=EMP001'
```

Example - filter by user name:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&user_name=张三'
```

Example - filter by IP address:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&ip=192.168.1.100'
```

Example - multiple employee numbers (comma-separated):

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&employee_no=EMP001,EMP002,EMP003'
```

Example - multiple user names (repeated parameter):

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&user_name=张三&user_name=李四'
```

Example - multiple IPs (mixed format):

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&ip=192.168.1.100,192.168.1.101'
```

Example - combine multiple filters:

```bash
curl 'http://localhost:8001/api/access_stats_by_department?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&dept1=技术部&employee_no=EMP001'
```

Notes:

- Department filters use exact matching, not pattern matching
- Multiple department filters are combined with AND logic
- `employee_no`, `user_name`, and `ip` filters support multiple values:
  - Comma-separated: `employee_no=EMP001,EMP002,EMP003`
  - Repeated parameter: `user_name=张三&user_name=李四`
  - Both formats can be mixed
- Multiple values are combined with OR logic (any match includes the result)
- All filters can be combined together with AND logic
- Only valid, non-deleted user and department records are included
- Internal routing requests (`ip_id=0`) are excluded from results

## Export Access Stats by Department API

```http
GET /api/access_stats_by_department/export
```

Exports access statistics by department as a CSV file. Uses the same query logic as the `access_stats_by_department` endpoint but returns a downloadable CSV instead of JSON.

Query parameters:

- `start_time`: Start time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)
- `dept1`: Level 1 department filter (optional; use `all` or omit to include all)
- `dept2`: Level 2 department filter (optional; use `all` or omit to include all)
- `dept3`: Level 3 department filter (optional; use `all` or omit to include all)
- `dept4`: Level 4 department filter (optional; use `all` or omit to include all)
- `employee_no`: Employee number filter (optional; supports multiple values via comma-separated or repeated params)
- `user_name`: User name filter (optional; supports multiple values via comma-separated or repeated params)
- `ip`: IP address filter (optional; supports multiple values via comma-separated or repeated params)

Note: Pagination parameters (`page`, `page_size`) are NOT supported for this endpoint. All matching records are exported.

CSV file format:

| Column | Description |
|--------|-------------|
| IP地址 | IP address |
| 访问次数 | Number of successful requests |
| 输入Token | Total input tokens (input_token_cnt) |
| 输出Token | Total output tokens (output_token_cnt) |
| 用户姓名 | User name |
| 用户职务 | User role/position |
| 员工工号 | Employee number |
| 一级部门 | Level 1 department |
| 二级部门 | Level 2 department |
| 三级部门 | Level 3 department |
| 四级部门 | Level 4 department |

File naming: `access_stats_{start_time}_{end_time}.csv` (timestamps formatted as `YYYYMMDD_HHMMSS`)

Example - export all departments:

```bash
curl 'http://localhost:8001/api/access_stats_by_department/export?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59' -o access_stats.csv
```

Example - export filtered by department:

```bash
curl 'http://localhost:8001/api/access_stats_by_department/export?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&dept1=技术部' -o tech_dept_stats.csv
```

Example - export filtered by employee number:

```bash
curl 'http://localhost:8001/api/access_stats_by_department/export?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&employee_no=EMP001' -o emp001_stats.csv
```

Example - export filtered by user name:

```bash
curl 'http://localhost:8001/api/access_stats_by_department/export?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&user_name=张三' -o user_stats.csv
```

Example - export filtered by IP address:

```bash
curl 'http://localhost:8001/api/access_stats_by_department/export?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59&ip=192.168.1.100' -o ip_stats.csv
```

Notes:

- CSV uses UTF-8 encoding with BOM for Excel compatibility
- Data is sorted by access_count descending (highest first)
- Same filtering logic as the JSON endpoint

## Department Cascade API

```http
GET /api/department/cascade
```

Returns department hierarchy in cascade format for frontend cascading selectors.

Query parameters (optional):

- `start_time`: Start time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)
- `end_time`: End time in Beijing timezone (format: `YYYY-MM-DD HH:mm:ss`)

If time parameters are not provided, returns all valid departments. If time parameters are provided, only returns departments that have access records within that time range.

Response format:

```json
{
  "code": 200,
  "data": {
    "options": [
      {
        "value": "技术部",
        "label": "技术部",
        "children": [
          {
            "value": "研发中心",
            "label": "研发中心",
            "children": [
              {
                "value": "后端组",
                "label": "后端组",
                "children": [
                  {
                    "value": "平台研发",
                    "label": "平台研发"
                  },
                  {
                    "value": "业务研发",
                    "label": "业务研发"
                  }
                ]
              },
              {
                "value": "前端组",
                "label": "前端组",
                "children": []
              }
            ]
          },
          {
            "value": "运维中心",
            "label": "运维中心",
            "children": []
          }
        ]
      },
      {
        "value": "产品部",
        "label": "产品部",
        "children": []
      }
    ]
  }
}
```

Field descriptions:

- `value`: Department name (used as filter value)
- `label`: Department name (display text)
- `children`: Array of sub-department objects (empty array if no sub-departments)

Example - get all departments:

```bash
curl 'http://localhost:8001/api/department/cascade'
```

Example - get departments with access in time range:

```bash
curl 'http://localhost:8001/api/department/cascade?start_time=2026-06-24%2000:00:00&end_time=2026-06-25%2023:59:59'
```

Notes:

- Departments are sorted alphabetically at each level
- Empty department fields are excluded from the cascade
- Only valid, non-deleted department records are included
- When time range is specified, only departments with active IPs that have successful requests are returned

## Review Slice Create API

```http
POST /api/review_slice
```

创建 ReviewSlices 表记录，用于存储 MR live review 的切片处理数据。

Request body (JSON):

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | **Yes** | 项目ID |
| `mr_iid` | string | **Yes** | Merge Request IID |
| `start_time` | string | **Yes** | 开始时间（格式：YYYY-MM-DD HH:MM:SS） |
| `review_id` | string | **Yes** | Review ID |
| `expert_model_name` | string | **Yes** | Expert 模型名称 |
| `reflector_model_name` | string | **Yes** | Reflector 模型名称 |
| `expert_duration` | float | No | Expert 处理时长（秒） |
| `reflector_duration` | float | No | Reflector 处理时长（秒） |
| `expert_comments` | integer | No | Expert 评论数 |
| `reflector_passed` | integer | No | Reflector 通过数 |
| `expert_retries` | integer | No | Expert 重试次数 |
| `reflector_retries` | integer | No | Reflector 重试次数 |
| `result` | string | No | 结果 |

Response JSON (success):

```json
{
  "code": 200,
  "message": "created",
  "data": {
    "id": 1
  }
}
```

Example:

```bash
curl -i -X POST http://localhost:8001/api/review_slice \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "my-project",
    "mr_iid": "123",
    "start_time": "2026-06-30 10:00:00",
    "review_id": "review-001",
    "expert_model_name": "gpt-4",
    "reflector_model_name": "gpt-3.5-turbo",
    "expert_duration": 15.5,
    "reflector_duration": 8.2,
    "expert_comments": 5,
    "reflector_passed": 3,
    "expert_retries": 1,
    "result": "passed"
  }'
```

## Review Summary Create API

```http
POST /api/review_summary
```

创建 ReviewSummary 表记录，用于存储 MR live review 的汇总统计数据。

Request body (JSON):

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | **Yes** | 项目ID |
| `mr_iid` | string | **Yes** | Merge Request IID |
| `start_time` | string | **Yes** | 开始时间（格式：YYYY-MM-DD HH:MM:SS） |
| `review_id` | string | **Yes** | Review ID |
| `expert_model_name` | string | **Yes** | Expert 模型名称 |
| `reflector_model_name` | string | **Yes** | Reflector 模型名称 |
| `file_modified_count` | integer | No | 修改文件数 |
| `total_duration` | float | No | 总时长（秒） |
| `slice_count` | integer | No | Slice 数量 |
| `expert_avg_duration` | float | No | Expert 平均时长（秒） |
| `expert_trigger_count` | integer | No | Expert 触发次数 |
| `expert_total_comments` | integer | No | Expert 总评论数 |
| `expert_avg_comments` | float | No | Expert 平均评论数 |
| `expert_total_retries` | integer | No | Expert 总重试次数 |
| `reflector_avg_duration` | float | No | Reflector 平均时长（秒） |
| `reflector_trigger_count` | integer | No | Reflector 触发次数 |
| `reflector_total_comments` | integer | No | Reflector 总评论数 |
| `reflector_avg_comments` | float | No | Reflector 平均评论数 |
| `reflector_total_retries` | integer | No | Reflector 总重试次数 |
| `reflector_total_passed` | integer | No | Reflector 总通过数 |
| `timeout` | boolean | No | 是否超时（默认 false） |

Response JSON (success):

```json
{
  "code": 200,
  "message": "created",
  "data": {
    "id": 1
  }
}
```

Example:

```bash
curl -i -X POST http://localhost:8001/api/review_summary \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "my-project",
    "mr_iid": "123",
    "start_time": "2026-06-30 10:00:00",
    "review_id": "review-001",
    "expert_model_name": "gpt-4",
    "reflector_model_name": "gpt-3.5-turbo",
    "file_modified_count": 10,
    "total_duration": 120.5,
    "slice_count": 5,
    "expert_avg_duration": 15.2,
    "expert_trigger_count": 5,
    "expert_total_comments": 25,
    "expert_avg_comments": 5.0,
    "expert_total_retries": 3,
    "reflector_avg_duration": 8.1,
    "reflector_trigger_count": 5,
    "reflector_total_comments": 15,
    "reflector_avg_comments": 3.0,
    "reflector_total_retries": 1,
    "reflector_total_passed": 12,
    "timeout": false
  }'
```

## AI Assistant User Feedback List API

```http
GET /api/ai_assistant_user_feedback/list
```

查询 `ai_assistant_user_feedback` 表数据列表，支持多条件过滤和分页。所有参数均为可选，若无参数则返回全量数据（分页）。

Query parameters (all optional):

| Parameter | Type | Description |
|-----------|------|-------------|
| `create_start_time` | string | 创建时间开始范围（基于 created_at，格式：YYYY-MM-DD HH:MM:SS） |
| `create_end_time` | string | 创建时间结束范围（基于 created_at，格式：YYYY-MM-DD HH:MM:SS） |
| `domain` | string | 领域筛选（可选值：知识管理、辅助设计、代码分析、问题定位、Agent、公共） |
| `status` | string | 状态筛选（可选值：open、close、cancel） |
| `reporter` | string | 报告人筛选（支持模糊匹配） |
| `assignee` | string | 指派人筛选（支持模糊匹配） |
| `priority` | string | 优先级筛选（可选值：高、中、低） |
| `page` | integer | 页码（默认 1） |
| `page_size` | integer | 每页大小（默认 10，最大 100） |

Response JSON:

```json
{
  "code": 200,
  "data": {
    "total_count": 50,
    "total_pages": 5,
    "current_page": 1,
    "page_size": 10,
    "has_next": true,
    "has_previous": false,
    "items": [
      {
        "id": 1,
        "domain": "知识管理",
        "tool_version": "v1.2.0",
        "issue_description": "搜索功能响应慢",
        "reporter": "张三",
        "reported_at": "2026-06-15T10:30:00+08:00",
        "priority": "高",
        "assignee": "李四",
        "status": "open",
        "estimated_resolution_at": "2026-06-20T18:00:00+08:00",
        "actual_resolution_at": null,
        "bugfix_version": null,
        "progress_tracking": "已定位问题，正在优化",
        "remarks": "优先处理",
        "created_at": "2026-06-15T10:30:00+08:00",
        "updated_at": "2026-06-16T09:00:00+08:00"
      }
    ]
  }
}
```

Example - get all feedback with default pagination:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list'
```

Example - filter by domain:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?domain=知识管理'
```

Example - filter by status:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?status=open'
```

Example - filter by time range:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?create_start_time=2026-06-01%2000:00:00&create_end_time=2026-06-30%2023:59:59'
```

Example - filter by reporter (fuzzy match):

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?reporter=张'
```

Example - filter by assignee (fuzzy match):

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?assignee=李'
```

Example - filter by priority:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?priority=高'
```

Example - custom pagination:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?page=2&page_size=20'
```

Example - combined filters:

```bash
curl 'http://localhost:8001/api/ai_assistant_user_feedback/list?domain=知识管理&status=open&priority=高&page=1&page_size=20'
```
