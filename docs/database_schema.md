# Database Schema

The database schema is intentionally not owned by Django migrations. Router models use `managed = False`; do not run `makemigrations` for normal schema drift. The live database should be validated with `check_db_schema`.

## Required Tables

`check_db_schema` compares the live database against every model in `router/models.py`. The required tables are:

- `ips`
- `departments`
- `user_ips`
- `models`
- `servers`
- `external_routes`
- `external_model_mappings`
- `requests`
- `whitelist`
- `server_operations`
- `mr_live_review`
- `codehub_review`
- `daily_mr_review`
- `live_review_requests`
- `ai_assistant_user_feedback`
- `review_slices`
- `review_summary`

## Production Table Scale

Row counts this router serves in production (as of 2026-08; update when the magnitude changes):

| Table | Rows (order of magnitude) | Notes |
| --- | --- | --- |
| `models` | < 10 | hot-path lookups per request |
| `servers`, `departments`, `whitelist` | < 100 each | `servers` rows are updated per request |
| `external_routes`, `external_model_mappings` | < 100 each | one indexed equality lookup per chat request; circuit updates write the whole `base_url` group |
| `ips`, `user_ips` | ~1,000 | |
| `requests` | ~7,000,000, expected to reach ~100,000,000 | ~12k requests/hour, no retention job; append-heavy with several UPDATEs per row |

Any schema or query change must be designed against these magnitudes, in particular:

- Every hot-path query against `requests` must be index-backed (verify with `EXPLAIN` before shipping). One sequential scan of `requests` costs seconds of CPU, and hot-path queries run per request — even a handful of seq scans per second can saturate the database host.
- Assume `requests` holds ~100M rows when adding columns or indexes; `CREATE INDEX CONCURRENTLY` (what `check_db_schema --fix` emits) is mandatory for live creation.
- Small tables are fully cached, so per-request reads are cheap — but per-request `UPDATE`s on the same `servers` rows still cause row-lock contention and dead-tuple churn.

## Timezone

Datetime columns should use `TIMESTAMPTZ` on PostgreSQL. The router runs with `TIME_ZONE = Asia/Shanghai` and sets the database connection time zone to `Asia/Shanghai`, so request lifecycle times such as `send_time` and `end_time` are saved and read in Beijing time.

## Core Access Tables

`ips.vip` is admin-managed. Set it to `TRUE` for client IPs allowed to use `server.vip_port`; non-VIP IPs that use the VIP port receive HTTP 503.

```sql
ALTER TABLE ips ADD COLUMN vip BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE ips ADD COLUMN concurrent_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0;
```

`departments.is_allowed`, `user_ips.department_id`, and `whitelist.is_allowed` form the permission chain:

```text
user_ips -> departments.is_allowed -> whitelist (expire_time, user_name)
```

`whitelist` entries are due-time limited: an entry grants access only while `is_allowed = 1` and before `expire_time` (`NULL` = never expires). Admission matches a whitelist entry by `employee_no`, or by `user_name` against `user_ips.user_charge` (the person in charge CMDB recorded for the IP — some IPs resolve a `user_charge` but no department, leaving `department_id` NULL).

Complete identities (a `user_ips` row with `employee_no` and a resolvable department) follow the department chain; an explicitly disallowed department is denied unless whitelisted. Incomplete identities — no `user_ips` row for the IP, no `employee_no`, or a department CMDB could not resolve (NULL, unknown id, or the whitelist apikey-bypass `0`) — are rescued only by the whitelist, then by `admission.allow_when_user_info_missing`. Registered API keys skip the permission check entirely (a valid key authorizes on its own); their gate is registration-time: after CMDB writes the key row, an allowed department passes, otherwise only a whitelist entry rescues (`employee_no` against `whitelist.employee_no`, `user_charge` against `whitelist.user_name`), and a key that fails both is stored with `is_valid = false`. The proxy refuses requests presenting such a key with HTTP 403 (`invalid_apikey`) instead of falling back to IP-based admission.

Whitelisted employees may also register an apikey without CMDB (`POST /api/apikey`): the `user_ips` apikey row is inserted directly with `department_id = 0`, and the CMDB sync overwrites the department once CMDB can resolve one.

```sql
ALTER TABLE whitelist ADD COLUMN expire_time TIMESTAMPTZ NULL;
```

### API-Key Identity Foundation

`user_ips` supports separate IP-backed and API-key-backed rows. An IP-backed row has `ip_id > 0` and an empty `apikey`; an API-key-backed row has `ip_id = 0` and a nonempty `apikey`. Nonzero IP IDs and nonempty API keys are individually unique, and an employee can have only one active API-key row.

`employee_no` resolution is apikey-first (issue #287): a valid Bearer key makes the API-key-backed row the identity and its `employee_no` wins over the IP-backed row's. A key row stored without an `employee_no` (e.g. inserted directly rather than via `/api/apikey`) borrows the IP-backed row's `employee_no`, so admission, whitelist matching and external-provider routing still resolve one. A Bearer key matching no `user_ips` row falls back to the IP-backed identity — unchanged behavior, but now with a warning logged, since a client presenting an unknown credential is almost always a misconfiguration.

`user_ips.vip` now drives identity-based VIP routing: a request carrying a VIP `user_ips` row (apikey- or IP-backed) is routed through the VIP server pool on the normal port. `ips.vip` still gates VIP-port admission. VIP load accounting uses `requests.vip` (set for VIP-channel and VIP-identity requests); the old `requests.user_ip_id = 2` sentinel is retired. `requests.user_ip_id` now holds the real backing `user_ips.id`, or `0` when no identity resolves.

Before applying the schema change, these preflight queries must return no rows:

```sql
SELECT id, ip_id FROM user_ips WHERE ip_id IS NULL OR ip_id <= 0;
SELECT ip_id, COUNT(*) FROM user_ips WHERE ip_id > 0 GROUP BY ip_id HAVING COUNT(*) > 1;
```

Apply the table changes while writes are stopped. If the existing unconditional IP constraint has a nonstandard name, find it in `pg_constraint` and drop that name instead of `user_ips_ip_id_key`.

```sql
BEGIN;

LOCK TABLE user_ips IN ACCESS EXCLUSIVE MODE;

ALTER TABLE user_ips ADD COLUMN apikey VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE user_ips ADD COLUMN vip BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE user_ips AS user_row
SET vip = ip_row.vip
FROM ips AS ip_row
WHERE user_row.ip_id = ip_row.id
  AND user_row.ip_id > 0;

ALTER TABLE user_ips DROP CONSTRAINT IF EXISTS user_ips_ip_id_key;
ALTER TABLE user_ips ALTER COLUMN ip_id SET DEFAULT 0;
ALTER TABLE user_ips ALTER COLUMN ip_id SET NOT NULL;
ALTER TABLE user_ips ADD CONSTRAINT user_ips_credential_xor CHECK (
    (ip_id > 0 AND apikey = '') OR (ip_id = 0 AND apikey <> '')
);

ALTER TABLE requests ADD COLUMN vip BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;

CREATE UNIQUE INDEX CONCURRENTLY uniq_user_ips_nonzero_ip
    ON user_ips (ip_id) WHERE ip_id > 0;
CREATE UNIQUE INDEX CONCURRENTLY uniq_user_ips_nonempty_apikey
    ON user_ips (apikey) WHERE apikey <> '';
CREATE UNIQUE INDEX CONCURRENTLY uniq_user_ips_active_emp_key
    ON user_ips (employee_no)
    WHERE apikey <> '' AND is_valid = TRUE AND deleted_at IS NULL;
CREATE INDEX CONCURRENTLY idx_user_ips_employee_no ON user_ips (employee_no);
CREATE INDEX CONCURRENTLY idx_req_vip_proc_model
    ON requests (model_id) WHERE task_status IN ('processing', 'prefilling', 'decoding') AND vip = TRUE;
```

Do not convert historical `requests.user_ip_id` values or derive `requests.vip` in this phase. Rollback is safe only before API-key rows are registered; afterward, dropping `apikey` would destroy registered credentials.

> Note: after the request-identity rollout, `requests.vip` is the active VIP-accounting column. If you need historical VIP traffic reflected, run a one-time `UPDATE requests SET vip = TRUE WHERE user_ip_id = 2;` backfill before retiring the sentinel.

## `models` Table

Important model columns:

```sql
ALTER TABLE models ADD COLUMN concurrent_limit INTEGER NULL DEFAULT 3;
ALTER TABLE models ADD COLUMN max_tokens INTEGER NOT NULL DEFAULT 20480;
ALTER TABLE models ADD COLUMN vip INTEGER NULL;
ALTER TABLE models ADD COLUMN deprecation VARCHAR(500) NULL;
ALTER TABLE models ADD COLUMN is_routing_model BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE models ADD COLUMN auto BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE models ADD COLUMN complexity_min INTEGER NULL;
ALTER TABLE models ADD COLUMN complexity_max INTEGER NULL;
ALTER TABLE models ADD COLUMN multimodal BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE models ADD COLUMN model_path VARCHAR(500) NULL;
```

`vip` is admin-managed. Set it to a positive integer to enable VIP routing for that model. The value is the per-active-VIP-server workload threshold above which the router promotes another normal server into the VIP pool. `NULL` or `0` disables VIP routing for the model.

`deprecation` is admin-managed. If it is not `NULL`, the router returns HTTP 400 with this value as the error message. This block applies to the normal port only; VIP-port requests for a concrete model are still served from that model's own servers. Deprecation does not affect auto-routing target eligibility: a deprecated model with `complexity_min`/`complexity_max` set can still serve `auto` requests.

`is_routing_model` marks models that can receive internal complexity-classification requests and normal-port small-request routing (which applies to auto requests only).

`auto` controls auto-routing entry, not target eligibility. Exact `model: auto` requests enter auto routing case-insensitively. On the normal port, requests for a concrete model with `auto = TRUE` also enter auto routing. On the VIP port, concrete model requests keep the requested model.

`complexity_min` and `complexity_max` are text auto-routing target bounds. Both must be non-NULL, between 1 and 10, and `complexity_min <= complexity_max`. A returned complexity score must match exactly one target model; otherwise the router uses `router.fallback_model` where applicable and records the reason in `requests.router_result`.

`multimodal` marks the model as eligible for auto-routed requests that contain `image_url` chat parts.

`model_path` is admin-managed and locates the underlying model artifact for tokenizer counting: a local directory or a Hugging Face repo id passed to `AutoTokenizer.from_pretrained`. It is used only when `tokenizer.enabled` is on; counting runs after a model is selected. `NULL` (or the toggle off) leaves `estimate_tokens` at the fast heuristic value. It is set via `/api/add_server`.

## `servers` Table

Current server columns:

```sql
CREATE TABLE servers (
    id BIGSERIAL PRIMARY KEY,
    model_id INTEGER NULL,
    base_url VARCHAR(500) NOT NULL UNIQUE,
    is_online BOOLEAN NOT NULL DEFAULT TRUE,
    weight INTEGER NOT NULL DEFAULT 1,
    health_path VARCHAR(200) NOT NULL DEFAULT '/healthy',
    last_checked_at TIMESTAMPTZ NULL,
    last_failure_at TIMESTAMPTZ NULL,
    cache_time INTEGER NOT NULL DEFAULT 3600,
    csb_token VARCHAR(500) NULL,
    api_key VARCHAR(500) NULL,
    circuit_state VARCHAR(20) NOT NULL DEFAULT 'closed',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_state_change_at TIMESTAMPTZ NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 30,
    workload INTEGER NOT NULL DEFAULT 0,
    vip BOOLEAN NOT NULL DEFAULT FALSE,
    vip_cooldown TIMESTAMPTZ NULL,
    context_window INTEGER NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'mixed',
    group_id VARCHAR(64) NULL,
    active_tokens DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);

CREATE INDEX servers_online_model_idx
    ON servers (is_online, model_id)
    WHERE deleted_at IS NULL;
```

`base_url` should include the upstream API prefix expected by the router, normally `/v1`. The proxy appends the incoming path such as `chat/completions`.

`cache_time` controls how long successful prefix-cache entries for this server stay valid in Redis.

`csb_token`, when present, is injected into upstream requests as the `csb-token` header.

`api_key` (issue #279) holds the credential for servers managed by an external system that only accept one specific key. When present, every upstream send to that server — normal, stream, retries, PD prefill and decode, health probes, and add-server verification — strips the client's `Authorization`/`x-api-key`/`api-key` headers and sends `Authorization: Bearer <api_key>` instead (see `build_upstream_headers` in `router/utils/headers.py`). `NULL` keeps today's behavior: the client's own `Authorization` is forwarded. It is set via the optional `api_key` field of `/api/add_server` (masked in the response); like other admin-maintained columns it can also be set directly on existing rows. The column is added by `check_db_schema --fix` (it surfaces `ALTER TABLE servers ADD COLUMN api_key VARCHAR(500) NULL`).

`circuit_state`, `consecutive_failures`, `last_state_change_at`, and `cooldown_seconds` are router-managed circuit-breaker fields. Closed servers are routable. Open servers become half-open after cooldown. Half-open servers are routable for probe traffic.

`workload` is router-managed. It is incremented before an upstream send and decremented when the request finishes or stale processing cleanup runs. Auto-routing classifier servers are selected by this value.

`vip` and `vip_cooldown` are router-managed. The router promotes and demotes servers automatically based on VIP request load.

`context_window` is an optional per-server context-window ceiling that must be maintained manually (set it to the server's real limit, e.g. vLLM's `--max-model-len`). It is not used to pre-filter candidate servers. When an upstream rejects a request with an overflow error whose message contains this value, the router retries on a larger-window server of the same model — for single-node servers on the main proxy path, and for PD clusters on the prefill phase (the prefill probe is the first upstream call, so overflow always surfaces there). The router never switches to a different model on overflow. `NULL` means unlimited (and disables the overflow retry for that server). Keep decoder windows `NULL` or strictly larger than their prefiller's: the larger-window candidate query drops any server — including a cluster's decoders — whose `context_window` is not larger than the failed prefiller's.

`weight` is the server's capacity multiplier and suggested workload (default 1). Server selection compares normalized load `workload / weight`, so a server with weight 2 is chosen over a weight-1 server as long as its own workload is below twice the other's. It is also the per-server overload threshold for prefix-cache affinity: the chooser escapes the cached server (falling back to least-loaded selection among all candidates) only when its in-flight workload has reached its `weight` **and** exceeds twice the lightest candidate's workload, so affinity is kept unless a materially lighter server exists (mixed servers and prefillers; decoders keep their own `active_tokens` path). Set it to the server's suggested concurrent request count (e.g. 4 for a 910C pool, 2 for a 910B4 pool). VIP channel candidates are restricted to weight-1 servers.

`role` is the server's PD-disaggregation style:

- `mixed` — single-node server, handles both prefill and decode. Never a cluster member: **keep `group_id` NULL for mixed servers** (the router ignores a mixed server's `group_id`, but a set value is a configuration error).
- `prefiller` (n-prefiller) — PD prefiller taking **new** requests (<90% prefix match). Also serves prefix-cached requests when it has no new prefill in flight.
- `prefix-prefiller` (p-prefiller) — PD prefiller for **prefix-cached** requests (>90% prefix match, KV pulled from the cluster pool via RDMA when the local holder is busy). Requires `group_id`. Give p-prefillers a larger `weight` than n-prefillers: it raises their #247 overload threshold and makes them preferred under normalized load.
- `decoder` — PD decoder, chosen after prefill by least `active_tokens` within its cluster. Never directly routable.

A cluster = prefiller-role servers sharing a non-empty `group_id` (plus its decoders); the cluster's prefillers share one kv-cache pool (issue #276). Prefillers and decoders require `group_id`; blank-`group_id` prefillers keep server-based (non-cluster) routing. `role` was widened from `varchar(12)` to `varchar(32)` for `prefix-prefiller` (16 chars) — deploy the router first, then apply the ALTER (surfaced by `check_db_schema`), then assign `prefix-prefiller` roles:

```sql
ALTER TABLE servers ALTER COLUMN role TYPE varchar(32);
```

`active_tokens` is a decoder-only load estimate (router-managed): reserved when a decoder is chosen after prefill and released when its decode phase ends.

## External Routing Tables (issue #287)

Employees routed to external third-party OpenAI-compatible providers are configured in two tables, deliberately separate from `servers` (providers never join server selection, health probing, workload, or VIP logic). `requests` is unchanged: external requests reuse its columns with the conventions at the end of this section.

`external_routes` holds one row per (provider, employee) — the provider fields are denormalized because every employee uses their own `api_key`. The circuit-breaker fields are per row in storage but always updated for the whole `base_url` group, so every employee of a provider sees the same circuit state. `check_db_schema --fix` creates both tables and their partial unique indexes (expect multiple passes on first run: tables first, then identity/constraints/indexes).

```sql
CREATE TABLE external_routes (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,                 -- provider label; appears in router_result, keep short
    base_url VARCHAR(500) NOT NULL,             -- ends with /v1, same convention as servers
    employee_no VARCHAR(50) NOT NULL,
    api_key VARCHAR(500) NOT NULL DEFAULT '',   -- per-employee provider credential
    is_active BOOLEAN NOT NULL DEFAULT TRUE,    -- admin kill-switch; FALSE => fallback to internal
    model_mapping_policy INTEGER NOT NULL,      -- groups external_model_mappings rows
    circuit_state VARCHAR(20) NOT NULL DEFAULT 'closed',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_state_change_at TIMESTAMPTZ NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX CONCURRENTLY uniq_external_routes_active
    ON external_routes (employee_no) WHERE is_active AND deleted_at IS NULL;

CREATE TABLE external_model_mappings (
    id BIGSERIAL PRIMARY KEY,
    policy_id INTEGER NOT NULL,
    internal_model_name VARCHAR(100) NOT NULL,  -- name clients request; == models.model_name when served internally
    external_model_name VARCHAR(200) NOT NULL,  -- exact name this provider expects (case-sensitive)
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX CONCURRENTLY uniq_external_model_mappings_active
    ON external_model_mappings (policy_id, internal_model_name) WHERE deleted_at IS NULL;
```

Model-name resolution is exact-match only — the GLM-5.2 vs glm-5.2 casing difference is an explicit mapping row, not fuzzy logic. `internal_model_name` must equal `models.model_name` exactly whenever the model is also served internally; provider-only models (e.g. `deepseek-v4-pro`) have no `models` row and their `internal_model_name` is simply the exposed alias (there is no FK, so admin tooling should validate the exact-match rule on insert). Write mapping rows even for identical names so each provider's catalog is explicit.

Routing rules (VIP-port requests never go external; identity VIP does not matter on the normal port):

- Concrete model name with a mapping → forwarded to the provider: body `model` rewritten to `external_model_name`, client `Authorization`/`x-api-key`/`api-key` stripped, `Authorization: Bearer <api_key>` sent. Deprecation / max_tokens / concurrency checks are skipped (they govern internal capacity).
- Concrete name without a mapping → internal pipeline as usual (unknown names keep the standard 400).
- Concrete name whose model has `auto = TRUE` → the mapping wins; without one, the request enters auto routing as usual.
- `model: auto` → auto routing runs first; if the resolved model is mapped, the request is diverted to the provider, otherwise it serves internally.
- Circuit open or `is_active = FALSE` → `ExternalRouteService.resolve` returns None and the request falls back to the internal pipeline (provider-only names then get the standard 400).

The provider circuit mirrors `servers` (`load_balancer.circuit_breaker.*` thresholds): transport errors and HTTP >= 500 count as failures; 4xx passes through without tripping (a bad per-employee key must not open the circuit for colleagues). Open → half-open probe after `cooldown_seconds` (no probe limit — there is no workload counter; a failed probe doubles the cooldown), success closes. On a failure the whole `base_url` group is updated in one statement.

Recording conventions: `router_result = "external:{name}:{internal_model_name}"` — the `external:` prefix must stay first because `AdmissionService` buckets in-flight rows by the prefix before the first colon (any other format would count external requests toward an internal model's or the auto concurrency bucket); it is persisted at dispatch, never NULL in flight. `target_pod_ip` stores the provider `base_url` (matches no `servers` row, so stale cleanup's workload decrement is a no-op). `model_id` is the internal model's id when one exists, else 0 — provider-only names never get a `models` row created. `GET /v1/models` merges the mapped names (owned_by `external:{name}`, null capability limits) over the internal list for mapped employees on the normal port; mapped names shadow the internal entries.

## `requests` Table

Current request-tracking columns include:

```sql
ALTER TABLE requests ALTER COLUMN target_pod_ip TYPE VARCHAR(500);
ALTER TABLE requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN prefix_cache DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN final_prefix_cache INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN last_match BIGINT NULL;
ALTER TABLE requests ADD COLUMN router_result VARCHAR(300) NULL;
ALTER TABLE requests ADD COLUMN estimate_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN model_choosing_latency BIGINT NULL;
ALTER TABLE requests ADD COLUMN ttft BIGINT NULL;
ALTER TABLE requests ADD COLUMN prefill_latency BIGINT NULL;
ALTER TABLE requests ADD COLUMN decode_latency BIGINT NULL;
ALTER TABLE requests ADD COLUMN vip BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE requests ADD COLUMN session VARCHAR(255) NULL;
```

`task_status` is one of the request lifecycle states used by the router, including `processing`, `success`, `failed`, `agent_disconnected`, and `incomplete`.

`attempt_count`, `target_pod_ip`, `prefix_cache`, and `last_match` are updated before each upstream attempt.

`final_prefix_cache` stores cached-token usage parsed from successful upstream responses when available.

`router_result` stores auto-routing and small-request-routing decisions, prefixed by the originally requested model name. Examples: `auto:complexity:7`, `AUTO:cache_hit`, `source-model:small_request_routing`, `auto:routing_failed:missing_routing_server:no available routing server`.

`estimate_tokens` stores a token estimate of the original request body. At parse time it holds the fast heuristic estimate (`fast_estimate_tokens`, a char/byte heuristic that needs no model). When `tokenizer.enabled` is on and the resolved model has a `model_path`, it is overwritten with a real tokenizer count after model selection; otherwise it stays at the heuristic value. It is used for small-request routing (auto requests only, gated by this estimate) and VIP scale-down; it is not used to pre-filter candidate servers (server context-window handling is reactionary).

`model_choosing_latency` stores elapsed milliseconds from request receipt (`send_time`) to the first send of the request to an LLM server (the prefill probe for pd-disaggregation requests). It is recorded once per request, on the first dispatch, for every request that reaches a server — not only auto-routed ones; requests that fail before dispatch (e.g. no candidates, blocked) leave it NULL. `ttft - model_choosing_latency` is therefore the LLM-side time to first token as observed by the router.

`ttft` stores time-to-first-token in milliseconds, measured from request receipt (`send_time`). For streaming requests it ends at the first non-empty chunk received from the upstream server — for pd-disaggregation streaming requests this includes the prefill phase. For non-streaming pd-disaggregation requests it ends when the prefill probe completes (the probe generates exactly one token, so that completion is the first-token moment; see `_normal_decode` in `proxy_pd_forward.py` and `_stream_success` in `proxy.py`). Single-node non-streaming requests leave it NULL.

`prefill_latency` and `decode_latency` split pd-disaggregation upstream processing time into its two phases, in milliseconds; single-node requests leave both NULL. `prefill_latency` is the wall time of the prefill probe (probe dispatch to probe response) and is persisted the moment prefill completes — together with `input_token_cnt`/`final_prefix_cache`, whose values are equally final at that point — so it survives a later decode failure. It stays NULL when prefill itself never completed. `decode_latency` is the wall time of the decode phase, from the first decoder dispatch to the terminal end of the phase (success, upstream error, timeout, or client disconnect), including KV-transfer wait, decoder re-selection, and all recompute rounds; it is persisted when the phase ends, whatever the outcome. Both record the last PD attempt — a retry overwrites them with the new attempt's phases. For profiling, `ttft - model_choosing_latency - prefill_latency` on a streaming PD row is the KV-transfer + decoder-queue + recompute overhead before the first delivered token.

`session` stores the client session id extracted from the request headers (`x-session-id` and friends; see `router/utils/session.py`), capped at 255 chars, `NULL` when absent. Auto-routing uses it for session-sticky model selection (the most recent committed `router_result` for the session wins within a one-hour window). The `idx_requests_session_send` index on `(session, send_time)` backs that lookup and is declared in `RequestRecord._meta.indexes`; `check_db_schema` creates it when missing.

Internal routing-model calls used to classify auto-routed targets are also recorded in `requests`. These rows use `ip_id = 0`, `user_agent = "llm-choosing"`, `is_stream = FALSE`, and the routing model's `model_id`. Statistics APIs exclude `ip_id = 0` rows.

The required request-table indexes are declared in `RequestRecord._meta.indexes`. Processing partial indexes are important on large `requests` tables because the hot path counts only active `processing` rows.

> **Condition drift warning:** `check_db_schema` detects index *definition* drift — an index that exists under the declared name but with a different column list or partial-index `WHERE` condition — and `--fix` recreates it (`DROP INDEX CONCURRENTLY` + `CREATE INDEX CONCURRENTLY`). It does this by building the expected index on a temporary empty copy of the table and comparing the `pg_get_expr`-canonicalized definitions of both sides.
>
> This matters because Postgres silently stops using a partial index when the query predicate stops implying the index predicate (a query filtering `task_status IN ('processing','prefilling','decoding')` cannot use a partial index whose predicate is `task_status = 'processing'`). This exact drift disabled all four processing partial indexes from 2026-07-25 (PD-disaggregation statuses added to queries but not to the live indexes) until 2026-08-21, forcing every hot-path query into full sequential scans of the multi-million-row table.

## Admin And Review Tables

`server_operations` records `/api/add_server` operations:

```sql
CREATE TABLE server_operations (
    id BIGSERIAL PRIMARY KEY,
    server_id INTEGER NULL,
    operation_type VARCHAR(50) NOT NULL,
    request_data JSONB NULL,
    response_data JSONB NULL,
    status VARCHAR(20) NOT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
```

`add_server` requests that carry an `api_key` store it in plaintext in `request_data` (an internal audit table) and masked in `response_data`/the API response.

`mr_live_review` stores MR review ingestion and reporting data. `discussion_id` must be unique.

`codehub_review` stores CodeHub review issues. The `is_modified_completed` field (default `FALSE`) tracks whether the modification for the issue has been completed.

```sql
ALTER TABLE codehub_review ADD COLUMN is_modified_completed BOOLEAN DEFAULT FALSE;
ALTER TABLE codehub_review ADD COLUMN need_analysis BOOLEAN NULL;
ALTER TABLE codehub_review ADD COLUMN conclusion TEXT NULL;
```

`need_analysis` (nullable `BOOLEAN`) indicates whether the issue requires further analysis. `conclusion` (nullable `TEXT`) stores the analysis conclusion or resolution notes for the issue.

## `daily_mr_review` Table

`daily_mr_review` stores daily MR review issues. `issue_hash` must be unique and prevents duplicate issue creation.

```sql
CREATE TABLE daily_mr_review (
    id BIGSERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL,
    branch VARCHAR(200) NOT NULL,
    issue_hash VARCHAR(50) NOT NULL UNIQUE,
    mr_hash VARCHAR(50) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    line INTEGER NOT NULL,
    body TEXT NOT NULL,
    review_comment TEXT NOT NULL,
    severity VARCHAR(50) NOT NULL,
    categories VARCHAR(200) NOT NULL,
    fix_suggestion TEXT NOT NULL,
    created_at VARCHAR(100) NOT NULL,
    confidence_score VARCHAR(50) NOT NULL,
    issue_url TEXT NOT NULL
);
```

`issue_hash` is the unique identifier computed from the issue content and location. `mr_hash` links the issue to the merge request. `confidence_score` indicates the review confidence level.

## `live_review_requests` Table

`live_review_requests` stores live review request metadata including project name, merge request details, start/end times, duration, model IDs used for expert and reflect phases, and review statistics.

```sql
CREATE TABLE live_review_requests (
    id BIGSERIAL PRIMARY KEY,
    project_name VARCHAR(200) NOT NULL,
    merge_requests_id INTEGER NOT NULL,
    merge_url TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NULL,
    duration_seconds INTEGER NULL,
    expert_model_id INTEGER NULL,
    reflect_model_id INTEGER NULL,
    review_file_num INTEGER NOT NULL DEFAULT 0,
    diff_part_num INTEGER NOT NULL DEFAULT 0,
    review_num INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
```

`expert_model_id` and `reflect_model_id` reference the models table and track which models were used in the two-phase review process. `duration_seconds` is automatically calculated from `start_time` and `end_time`. `review_file_num`, `diff_part_num`, and `review_num` track review coverage statistics.

## `ai_assistant_user_feedback` Table

`ai_assistant_user_feedback` stores user feedback for AI Assistant tool features. The table tracks issue lifecycle from reporting through resolution.

```sql
CREATE TABLE ai_assistant_user_feedback (
    id BIGSERIAL PRIMARY KEY,
    domain VARCHAR(50) NOT NULL,
    tool_version VARCHAR(100) NULL,
    issue_description TEXT NOT NULL,
    reporter VARCHAR(200) NOT NULL,
    reported_at TIMESTAMPTZ NOT NULL,
    priority VARCHAR(20) NULL,
    assignee VARCHAR(200) NULL,
    status VARCHAR(20) NOT NULL,
    estimated_resolution_at TIMESTAMPTZ NULL,
    actual_resolution_at TIMESTAMPTZ NULL,
    bugfix_version VARCHAR(100) NULL,
    progress_tracking TEXT NULL,
    remarks TEXT NULL,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
```

`domain` must be one of: `知识管理`, `辅助设计`, `代码分析`, `问题定位`, `Agent`, or `公共`.

`status` must be one of: `open` (新建), `close` (已关闭), or `cancel` (已取消).

`priority` is optional and must be one of: `高`, `中`, or `低`.

`progress_tracking` is a free-text field for tracking resolution progress and intermediate updates.

The field definitions for these reporting tables are in `router/models.py`; `check_db_schema --dry-run` is the safest way to confirm that a live database matches the current model definitions.

## `review_slices` Table

`review_slices` stores review slice records for MR live review processing. Each slice represents a unit of review work with expert and reflector model details.

```sql
CREATE TABLE review_slices (
    id BIGSERIAL PRIMARY KEY,
    project_id VARCHAR(100) NOT NULL,
    mr_iid VARCHAR(100) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    review_id VARCHAR(100) NOT NULL,
    expert_model_name VARCHAR(200) NOT NULL,
    reflector_model_name VARCHAR(200) NOT NULL,
    expert_duration DOUBLE PRECISION NULL,
    reflector_duration DOUBLE PRECISION NULL,
    expert_comments INTEGER NULL,
    reflector_passed INTEGER NULL,
    expert_retries INTEGER NULL,
    reflector_retries INTEGER NULL,
    result VARCHAR(500) NULL,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
```

| Column | Type | Description |
|--------|------|-------------|
| `project_id` | VARCHAR(100) | Project identifier (string) |
| `mr_iid` | VARCHAR(100) | Merge request IID (string) |
| `start_time` | TIMESTAMPTZ | Review slice start timestamp |
| `review_id` | VARCHAR(100) | Review identifier (string) |
| `expert_model_name` | VARCHAR(200) | Expert model name used for review |
| `reflector_model_name` | VARCHAR(200) | Reflector model name used for review |
| `expert_duration` | DOUBLE PRECISION | Expert model processing duration in seconds |
| `reflector_duration` | DOUBLE PRECISION | Reflector model processing duration in seconds |
| `expert_comments` | INTEGER | Number of comments generated by expert model |
| `reflector_passed` | INTEGER | Number of reviews passed by reflector |
| `expert_retries` | INTEGER | Number of retry attempts by expert model |
| `reflector_retries` | INTEGER | Number of retry attempts by reflector model |
| `result` | VARCHAR(500) | Review result status or outcome |

`project_id`, `mr_iid`, `review_id`, `expert_model_name`, `reflector_model_name`, and `result` are string fields storing identifiers and model names.

`expert_duration` and `reflector_duration` track processing time in seconds (floating-point values).

`expert_comments`, `reflector_passed`, and `expert_retries` are integer counters for review metrics.

## `review_summary` Table

`review_summary` stores aggregated review summary statistics for MR live review processing. Each record represents a summary of multiple review slices with aggregated metrics for expert and reflector models.

```sql
CREATE TABLE review_summary (
    id BIGSERIAL PRIMARY KEY,
    project_id VARCHAR(100) NOT NULL,
    mr_iid VARCHAR(100) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    review_id VARCHAR(100) NOT NULL,
    expert_model_name VARCHAR(200) NOT NULL,
    reflector_model_name VARCHAR(200) NOT NULL,
    file_modified_count INTEGER NULL,
    total_duration DOUBLE PRECISION NULL,
    slice_count INTEGER NULL,
    expert_avg_duration DOUBLE PRECISION NULL,
    expert_trigger_count INTEGER NULL,
    expert_total_comments INTEGER NULL,
    expert_avg_comments DOUBLE PRECISION NULL,
    expert_total_retries INTEGER NULL,
    reflector_avg_duration DOUBLE PRECISION NULL,
    reflector_trigger_count INTEGER NULL,
    reflector_total_comments INTEGER NULL,
    reflector_avg_comments DOUBLE PRECISION NULL,
    reflector_total_retries INTEGER NULL,
    reflector_total_passed INTEGER NULL,
    timeout BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
```

| Column | Type | Description |
|--------|------|-------------|
| `project_id` | VARCHAR(100) | Project identifier (string) |
| `mr_iid` | VARCHAR(100) | Merge request IID (string) |
| `start_time` | TIMESTAMPTZ | Review start timestamp |
| `review_id` | VARCHAR(100) | Review identifier (string) |
| `expert_model_name` | VARCHAR(200) | Expert model name used for review |
| `reflector_model_name` | VARCHAR(200) | Reflector model name used for review |
| `file_modified_count` | INTEGER | Number of modified files reviewed |
| `total_duration` | DOUBLE PRECISION | Total review duration in seconds |
| `slice_count` | INTEGER | Number of review slices in this summary |
| `expert_avg_duration` | DOUBLE PRECISION | Average expert model processing duration per slice |
| `expert_trigger_count` | INTEGER | Number of times expert model was triggered |
| `expert_total_comments` | INTEGER | Total comments generated by expert model |
| `expert_avg_comments` | DOUBLE PRECISION | Average comments per expert trigger |
| `expert_total_retries` | INTEGER | Total retry attempts by expert model |
| `reflector_avg_duration` | DOUBLE PRECISION | Average reflector model processing duration per slice |
| `reflector_trigger_count` | INTEGER | Number of times reflector model was triggered |
| `reflector_total_comments` | INTEGER | Total comments generated by reflector model |
| `reflector_avg_comments` | DOUBLE PRECISION | Average comments per reflector trigger |
| `reflector_total_retries` | INTEGER | Total retry attempts by reflector model |
| `reflector_total_passed` | INTEGER | Total reviews passed by reflector |
| `timeout` | BOOLEAN | Whether the review timed out (default FALSE) |

**String fields**: `project_id`, `mr_iid`, `review_id`, `expert_model_name`, `reflector_model_name`

**Duration fields (floating-point)**: `total_duration`, `expert_avg_duration`, `reflector_avg_duration`

**Counter fields (integer)**: `file_modified_count`, `slice_count`, `expert_trigger_count`, `expert_total_comments`, `expert_total_retries`, `reflector_trigger_count`, `reflector_total_comments`, `reflector_total_retries`, `reflector_total_passed`

**Average fields (floating-point)**: `expert_avg_comments`, `reflector_avg_comments`

This table aggregates data from `review_slices` to provide a high-level summary of review performance metrics.

## Schema Validation

Use the management commands to validate schema state:

```bash
python manage.py test init_db
python manage.py test check_db_schema --dry-run
```

`check_db_schema --fix` can create missing tables, add missing columns, drop extra columns/defaults, fix nullable/type/unique mismatches, add missing auto-increment identity, and create missing model-declared indexes. Review the dry-run output before applying fixes to production.
