# llm-router

A Django + Gunicorn based reverse-proxy / API gateway that sits in front of one or more OpenAI-compatible LLM inference servers (e.g. vLLM / SGLang clusters exposed via `/v1/...`). It performs admission control, auto model selection, prefix-cache-aware load balancing across upstream pods, retry / circuit-breaker / cancellable-upstream handling for both streaming (SSE) and non-streaming responses, and records every request's full lifecycle into PostgreSQL for monitoring and analytics. PostgreSQL is the source of truth for the router tables; Django models are unmanaged and the live schema is validated with dedicated commands.

## Documentation

- [Database Schema](docs/database_schema.md)
- [Configuration](docs/configuration.md)
- [Auto Routing](docs/auto_routing.md)
- [Setup](docs/setup.md)
- [API Endpoints](docs/api_endpoints.md)
- [Management Commands](docs/management_commands.md)
- [Tests](docs/tests.md)

## All Functions

- **Reverse Proxy / Gateway**
  - `/v1/<path>` catch-all proxy for OpenAI-compatible APIs (all HTTP methods, CSRF-exempt)
  - Streaming (SSE) and non-streaming forwarding with independent connect / read / total timeouts
  - Request body parsing: extracts `model` / `stream` / `max_tokens`, injects `stream_options.include_usage=true`, defaults missing `max_tokens`
  - Hop-by-hop / `Host` / `Content-Length` / `Content-Encoding` header stripping; per-server `csb-token` injection
  - Client-disconnect tracking via `gunicorn.socket` + `MSG_PEEK`; cancels upstream request and records HTTP 499 / `agent_disconnected`
  - Cancellable upstream HTTP client (custom urllib3 `PoolManager`/`Connection` that force-closes sockets on cancel)
  - `413 Request Entity Too Large` handling for oversize bodies
  - Special routing: `GET /v1/models` is dispatched to a random online server

- **Load Balancing & Server Selection**
  - Pluggable `ServerChooser` protocol with `ServerSelectionContext`
  - `PrefixCachePrebleServerChooser` (default): Redis-backed character-prefix cache, primary/secondary match thresholds, least-loaded-among-matches selection, overload escape when a cached server's `workload` reaches its `weight` and doubles the pool minimum, per-server `cache_time` eviction
  - `LeastConnectionServerChooser`: picks server with fewest in-flight `processing` requests
  - Candidate servers are filtered by online state, circuit-breaker state, soft delete, VIP pool, and model id (never by estimated request size)
  - Retry to another server **only** on a connection failure (request never reached the upstream) or a context overflow (HTTP 400 naming the server's `context_window`, retried on a same-model server with a strictly larger window — single-node path and PD prefill phase); read timeouts, other HTTP errors, and PD logic failures do not retry, to preserve POST idempotency. Bounded by `max_attempts_per_request`
  - Per-attempt logging of `server_attempt` and `multi_server_route` events
  - `servers.workload` counter incremented before send and decremented after (or by stale cleanup)

- **VIP Channel**
  - Second listening port (`server.vip_port`, default 8008 prod / 9001 test) routes traffic to a dedicated VIP server pool for VIP-eligible models (`models.vip > 0` is the workload threshold)
  - Client eligibility is controlled by `ips.vip`; non-VIP IPs on the VIP port receive HTTP 503 with `Port <vip_port> is closed, please use port <http_port>`
  - Router-managed `servers.vip` and `servers.vip_cooldown` track pool membership; non-VIP traffic never lands on VIP servers
  - Scale-up: on each VIP request, if `(current_load + 1) / active_vip_servers > threshold`, cancels a cooling cooldown if any, otherwise promotes the least-loaded normal server (subject to `vip.min_normal_servers` floor, default 2)
  - Scale-down: on each VIP request finish, if projected average drops below threshold, cools the least-loaded VIP server; if VIP load reaches zero, cools all active VIP servers; cooldowns demote after `vip.cooldown_seconds` (default 300)
  - VIP load counted via `requests.vip` (set for VIP-channel and VIP-identity requests) so leftover normal traffic on freshly-promoted servers does not skew scaling decisions
  - `release_vip_cooldowns` management command demotes expired cooldowns when the VIP channel is fully idle

- **Circuit Breaker & Health Probing**
  - Three states on `servers.circuit_state`: `closed` / `open` / `half_open`
  - Failure counter with `failure_threshold`; exponential cooldown capped at `max_cooldown_seconds`
  - Cooldown-expired servers auto-transition to `half_open` on next listing
  - Active `ServerHealthService` probes `GET <base_url>/<health_path>`; passive failures from `mark_unhealthy_status_codes` also trip the breaker

- **Admission Control & Permissions**
  - IP auto-creation on first request; background CMDB lookup for new IPs
  - Permission chain: `user_ips` → `departments.is_allowed` → `whitelist.is_allowed`, with a configurable fallback when user info is missing
  - `check_max_tokens`: rejects when request exceeds model's `max_tokens` (or `unknown_model_max_tokens`); auto-routed requests (`model: auto` or `models.auto = TRUE`) are capped by `proxy.auto_max_tokens`
  - `check_concurrency`: per-(IP, model) limit using `ceil(model.concurrent_limit × ip.concurrent_multiplier)`; exact `auto` requests use `router.auto_concurrent_limit`; limits are multiplied by 4 overnight (23:00–08:00 Beijing time), from 18:00 on Saturdays, and all day Sunday
  - Auto routing: `model: auto` is case-insensitive; concrete models with `models.auto = TRUE` also enter auto routing on the normal port. Text targets are active models with valid complexity bounds; multimodal targets are active models with `multimodal = TRUE`. See [Auto Routing](docs/auto_routing.md) for the full selection sequence.

- **Opencode Client Compatibility**
  - Parses `opencode/<X.Y.Z>` from `User-Agent`
  - Hard-blocks clients ≤ `opencode.block_max_version` (default 1.2.26)
  - Delays failed opencode responses by `proxy.opencode_failure_delay_seconds` (default 180) to slow buggy retry storms

- **Request Lifecycle Tracking**
  - `processing` row inserted at proxy start; admission denials inserted directly as `failed`
  - Per-attempt update of `attempt_count`, `target_pod_ip`, `prefix_cache` (best match ratio), `last_match` (matched request id)
  - Final state: `end_time`, `latency`, `status`, `task_status` (`success` / `failed` / `agent_disconnected` / `incomplete`), token counts, cached-token counts when the upstream reports them
  - Auto-routing metadata: `router_result` and `estimate_tokens`; timing: `model_choosing_latency` (request receipt to first upstream send) and `ttft` (request receipt to first token; streaming and pd-disaggregation requests). Internal routing LLM calls are recorded as separate `ip_id = 0` rows
  - Per-request log file under `log_path/YYYY/MM/DD/HH/MM/<id>.log`; every event line is prefixed with `[YYYY-MM-DD HH:MM:SS.mmm]` (the configured `TIME_ZONE`, same clock as the folder buckets). `start_prod.sh` uses `/data/router_log` with verbose request logging off, while `start_test.sh` uses `.logs/requests` and records the full request body as pretty JSON
  - Stale `processing` cleanup flips rows to `incomplete` and decrements workload counters

- **Statistics & Monitoring API**
  - `request_stats`, `total_request_count`, `input_token`, `output_token`, `model_request_stats`, `all_model_request_stats`
  - `request_time_stats`, `model_request_time_stats` (bucketed average latency)
  - `model_request_count_by_period`, `model_ip_count_by_period` (bucketed counts)
  - `model_latency_boxplot`: min/Q1/median/Q3/max + over-limit ratio, drops > 890s, trims top 1%
  - `models`, `model_online_list`, `model_info` model catalog endpoints; automatic hour/day/month granularity selection in Asia/Shanghai
  - MR live review and CodeHub review ingestion/list/statistics endpoints

- **Management & Admin APIs**
  - `POST /api/whitelist/update` — upsert whitelist entry by `employee_no`
  - `POST /api/apikey` — register or rotate an employee API key (storage only; proxy identity support is deferred)
  - `POST /api/refresh_user_info` — kick off CMDB user refresh thread (requires `cmdb.enabled`)
  - `POST /api/add_server` — register a new upstream server after verifying its `/models`
  - `POST /api/mr_live_review` and `POST /api/codehub_review` — ingest review records for reporting
  - `GET /api/download/ai_assistant` — download `AI_Assistant.exe`

- **Management Commands**
  - `init_db` — validate DB connectivity and required tables
  - `check_db_schema` — diff live schema against Django models; `--fix` emits/executes corrective DDL
  - `check_server_health` — probe servers, update circuit-breaker state, optionally recover offline servers
  - `cleanup_stale_processing` — drain abandoned `processing` rows and decrement workload counters
  - `release_vip_cooldowns` — demote VIP servers whose `vip_cooldown` has expired

- **Configuration**
  - `config.yaml` (overridable via `LLM_ROUTER_CONFIG`) deep-merged onto built-in defaults
  - Optional `router` config for auto-routing fallback model, classifier prompt path, and exact-auto concurrency
  - Env-var overrides for DB, Redis, `HTTP_PORT`, `VIP_PORT`, prefix-cache thresholds, Django secret/debug, test SQLite mode
  - `start_prod.sh` (ports 8001+8008, Redis 6379, 8×64) and `start_test.sh` (ports 9000+9001, Redis 6380, 1×8) gunicorn launchers
  - WSGI entrypoint validates DB connectivity on boot; `ClientDisconnectMiddleware` registered globally

- **Tests**
  - 33 pytest files covering proxy, parser, headers, SSE, errors, auto routing, context overflow, token/context-window filtering, Redis prefix cache, server choosers, circuit breaker, cancellable upstream, disconnect tracking, request logger, requests repository, workload accounting, schema check, management API, downloads, statistics API, MR/CodeHub APIs, opencode policy, manage.py wrapper, config env overrides, and VIP channel

## Notes

- Do not run `makemigrations` for schema changes unless the database ownership model changes.
- Do not commit real database passwords, upstream API keys, or corporate CMDB credentials.
