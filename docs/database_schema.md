# Database Schema

The database schema is intentionally not managed by this project. Django models are unmanaged (`managed = False`) and no schema-changing migrations should be generated or applied.

## Required Tables

The router expects these existing tables:

- `ips`
- `departments`
- `user_ips`
- `models`
- `requests`
- `whitelist`
- `servers`

## Timezone

All datetime columns should use `TIMESTAMPTZ`. The router runs with `TIME_ZONE = Asia/Shanghai` and sets the database connection time zone to `Asia/Shanghai`, so request lifecycle times such as `send_time` and `end_time` are saved and read in Beijing time.

## `ips` Table

```sql
ALTER TABLE ips ADD COLUMN vip BOOLEAN NOT NULL DEFAULT FALSE;
```

`vip` is admin-managed. Set it to `TRUE` for client IPs that may send traffic to `server.vip_port`; non-VIP IPs that use the VIP port receive HTTP 503 with the configured normal port in the error message.

## `servers` Table

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
    circuit_state VARCHAR(20) NOT NULL DEFAULT 'closed',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_state_change_at TIMESTAMPTZ NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 30,
    workload INTEGER NOT NULL DEFAULT 0,
    vip BOOLEAN NOT NULL DEFAULT FALSE,
    vip_cooldown TIMESTAMPTZ NULL,
    context_window INTEGER NULL,
    created_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);

CREATE INDEX servers_online_model_idx
    ON servers (is_online, model_id)
    WHERE deleted_at IS NULL;
```

`vip` and `vip_cooldown` are router-managed; do not edit them by hand. The router promotes and demotes servers automatically based on VIP request load. `circuit_state`, `consecutive_failures`, `last_state_change_at`, and `cooldown_seconds` are router-managed circuit-breaker state; `workload` is the in-flight request counter used by the least-connection chooser and the auto-routing server selector. `context_window`, when non-null, is the per-server context limit used to filter out servers too small for an estimated request size.

## `models` Table

```sql
ALTER TABLE models ADD COLUMN vip INTEGER NULL;
ALTER TABLE models ADD COLUMN deprecation VARCHAR(500) NULL;
ALTER TABLE models ADD COLUMN is_routing_model BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE models ADD COLUMN auto BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE models ADD COLUMN max_context_window INTEGER NOT NULL DEFAULT 204800;
ALTER TABLE models ADD COLUMN complexity_min INTEGER NULL;
ALTER TABLE models ADD COLUMN complexity_max INTEGER NULL;
ALTER TABLE models ADD COLUMN multimodal BOOLEAN NOT NULL DEFAULT FALSE;
```

`vip` is admin-managed. Set it to a positive integer to enable VIP routing for that model: it is the per-active-VIP-server workload threshold above which the router promotes another normal server into the VIP pool. `NULL` or `0` disables VIP routing for the model — VIP-port traffic from VIP-authorized IPs for it is served from the normal pool.

`deprecation` is admin-managed. If it is not `NULL`, the router will return a 400 error with the value of this column as the error message, effectively disabling the model. Models with a non-null `deprecation` are excluded from the active catalog (`model_online_list`, auto-routing targets).

`is_routing_model` is admin-managed. When `TRUE`, the model can be used as the auto-routing classification backend (see [Auto Routing](auto_routing.md)) and as the direct target for small auto requests. It is independent of `auto` (a routing model does not auto-route its own concrete requests unless `auto` is also `TRUE`).

`auto` is admin-managed. It controls auto-routing entrance, not target eligibility. Requests whose model name is `auto` (case-insensitive) enter auto routing. On the normal port, requests for a concrete model with `auto = TRUE` also enter auto routing; on the VIP port, concrete model requests keep the requested model.

`max_context_window` is admin-managed. It is the fallback trigger for auto-routed requests: when the upstream returns a 400 whose reason text contains this value, the router switches the request to `router.fallback_model` and retries.

`complexity_min` and `complexity_max` are admin-managed text auto-routing target bounds. For an auto-routed text request, the routing model returns a complexity score from 1 to 10, and the router selects the active model whose inclusive range contains that score. Both columns must be non-null and within 1–10 (with `complexity_min <= complexity_max`) for the model to be an auto-selection target. A score must match exactly one model; zero or multiple matching models fall back to the configured default model and record a routing failure reason in `router_result`. Stored routing results are prefixed with the originally requested model name, for example `auto:complexity:7` or `glm-5:routing_failed:missing_routing_server:no available routing server`.

`multimodal` is admin-managed multimodal auto-routing target eligibility. For an auto-routed image request, the router bypasses text complexity classification and selects an active `multimodal = TRUE` model. Concrete requests for models with `auto = FALSE` keep the requested model, even when the request contains image content.

## Request-Table Indexes

The required request-table indexes are declared in `RequestRecord._meta.indexes`. The processing partial indexes are required for the LLM request hot path because the `requests` table can be very large while active `processing` rows are small. `check_db_schema` reports missing model-declared indexes so the dry run can catch drift before production traffic depends on them; `--fix` creates missing model indexes with `CREATE INDEX CONCURRENTLY IF NOT EXISTS` on PostgreSQL.

## Load-Balancer Columns

The load balancer also records the selected backend and number of backend attempts per request:

```sql
ALTER TABLE servers ADD COLUMN cache_time INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE requests ALTER COLUMN target_pod_ip TYPE VARCHAR(500);
ALTER TABLE requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN prefix_cache DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN final_prefix_cache INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN last_match BIGINT NULL;
ALTER TABLE requests ADD COLUMN model_choosing_latency BIGINT NULL;
ALTER TABLE requests ADD COLUMN router_result VARCHAR(300) NULL;
ALTER TABLE requests ADD COLUMN estimate_tokens INTEGER NOT NULL DEFAULT 0;
```

`prefix_cache` is the best prefix match ratio recorded per attempt; `final_prefix_cache` is the cached-token count reported by the successful upstream response (also used in token accounting as `final_prefix_cache + input_token_cnt`). `estimate_tokens` is the lightweight estimate of the request size used for small-request routing and context-window filtering. `router_result` holds the auto-routing decision string (e.g. `auto:complexity:7`, `auto:cache_hit`, `auto:small_request_routing`) prefixed with the originally requested model name; `model_choosing_latency` records the choosing-stage duration in milliseconds. See [Auto Routing](auto_routing.md).

Internal routing-model calls used to choose an auto-routed target are also recorded in
`requests`. These rows use `ip_id = 0`, `user_agent = "llm-choosing"`, `is_stream = FALSE`, and the routing model's
`model_id`; the original client request row remains separate and its `latency` still
measures the full end-to-end request.

## Schema Validation

Use the management commands to validate schema state without altering tables:

```bash
python manage.py test init_db
python manage.py test check_db_schema --dry-run
```
