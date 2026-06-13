# llm-router

A Django + Gunicorn based reverse-proxy / API gateway that sits in front of one or more OpenAI-compatible LLM inference servers (e.g. vLLM / SGLang clusters exposed via `/v1/...`). It performs admission control, prefix-cache-aware load balancing across upstream pods, retry / circuit-breaker / cancellable-upstream handling for both streaming (SSE) and non-streaming responses, and records every request's full lifecycle into PostgreSQL for monitoring and analytics. PostgreSQL is the source of truth for `ips`, `user_ips`, `departments`, `models`, `servers`, `requests`, and `whitelist`; the router declares those models as unmanaged and validates the live schema with dedicated commands.

## Documentation

- [Database Schema](docs/database_schema.md)
- [Configuration](docs/configuration.md)
- [Setup](docs/setup.md)
- [API Endpoints](docs/api_endpoints.md)
- [Management Commands](docs/management_commands.md)
- [Auto Routing](docs/auto_routing.md)
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
  - `PrefixCachePrebleServerChooser` (default): Redis-backed character-prefix cache, primary/secondary match thresholds, least-loaded-among-matches selection, per-server `cache_time` eviction
  - `LeastConnectionServerChooser`: picks server with fewest in-flight `processing` requests
  - Configurable retry on `retry_status_codes` (default 502/503/504), bounded by `max_attempts_per_request`
  - Per-attempt logging of `server_attempt` and `multi_server_route` events
  - `servers.workload` counter incremented before send and decremented after (or by stale cleanup)

- **VIP Channel**
  - Second listening port (`server.vip_port`, default 8008 prod / 9001 test) routes traffic to a dedicated VIP server pool for VIP-eligible models (`models.vip > 0` is the workload threshold)
  - Client eligibility is controlled by `ips.vip`; non-VIP IPs on the VIP port receive HTTP 503 with `Port <vip_port> is closed, please use port <http_port>`
  - Router-managed `servers.vip` and `servers.vip_cooldown` track pool membership; non-VIP traffic never lands on VIP servers
  - Scale-up: on each VIP request, if `(current_load + 1) / active_vip_servers > threshold`, cancels a cooling cooldown if any, otherwise promotes the least-loaded normal server (subject to `vip.min_normal_servers` floor, default 2)
  - Scale-down: on each VIP request finish, if projected average drops below threshold, cools the least-loaded VIP server; if VIP load reaches zero, cools all active VIP servers; cooldowns demote after `vip.cooldown_seconds` (default 300)
  - VIP load counted via `requests.user_ip_id = 2` so leftover normal traffic on freshly-promoted servers does not skew scaling decisions
  - `release_vip_cooldowns` management command demotes expired cooldowns when the VIP channel is fully idle

- **Circuit Breaker & Health Probing**
  - Three states on `servers.circuit_state`: `closed` / `open` / `half_open`
  - Failure counter with `failure_threshold`; exponential cooldown capped at `max_cooldown_seconds`
  - Cooldown-expired servers auto-transition to `half_open` on next listing
  - Active `ServerHealthService` probes `GET <base_url>/<health_path>`; passive failures from `mark_unhealthy_status_codes` also trip the breaker

- **Admission Control & Permissions**
  - IP auto-creation on first request; background CMDB lookup for new IPs
  - Permission chain: `user_ips` → `departments.is_allowed` → `whitelist.is_allowed`, with a configurable fallback when user info is missing
  - `check_max_tokens`: rejects when request exceeds model's `max_tokens` (or `unknown_model_max_tokens`)
  - `check_concurrency`: per-(IP, model) limit using `ceil(model.concurrent_limit × ip.concurrent_multiplier)`; cleans stale rows before counting
  - Auto routing: `model: auto` is case-insensitive; concrete models with `models.auto = TRUE` also enter auto routing on the normal port. The chooser runs a two-stage decision — small requests (estimated `< 3000` tokens) are routed directly to a `is_routing_model` backend, otherwise it picks an auto-selectable target via multimodal bypass, prefix-cache hit, or a complexity-classification call to a routing model. Targets are active models with valid 1–10 complexity bounds; multimodal targets are active models with `multimodal = TRUE`. Full decision sequence is documented in [Auto Routing](docs/auto_routing.md).

- **Opencode Client Compatibility**
  - Parses `opencode/<X.Y.Z>` from `User-Agent`
  - Hard-blocks clients ≤ `opencode.block_max_version` (default 1.2.26)
  - Delays failed opencode responses by `proxy.opencode_failure_delay_seconds` (default 180) to slow buggy retry storms

- **Request Lifecycle Tracking**
  - `processing` row inserted at proxy start; admission denials inserted directly as `failed`
  - Per-attempt update of `attempt_count`, `target_pod_ip`, `prefix_cache` (best match ratio), `last_match` (matched request id)
  - Final state: `end_time`, `latency`, `status`, `task_status` (`success` / `failed` / `agent_disconnected` / `incomplete`), token counts; auto-creates `models` row on successful unknown-model response
  - Per-request log file under `log_path/YYYY/MM/DD/HH/MM/<id>.log`; `start_prod.sh` uses `/data/router_log` with verbose request logging off, while `start_test.sh` uses `.logs/requests` and records the full request body as pretty JSON
  - Stale `processing` cleanup flips rows to `incomplete` and decrements workload counters

- **Statistics & Monitoring API**
  - `request_stats`, `total_request_count`, `model_request_stats`, `all_model_request_stats`
  - `input_token`, `output_token`: summed input (`final_prefix_cache + input_token_cnt`) / output tokens, filterable by `model_name` (or `total`)
  - `request_time_stats`, `model_request_time_stats` (bucketed average latency)
  - `model_request_count_by_period`, `model_ip_count_by_period` (bucketed counts)
  - `model_latency_boxplot`: min/Q1/median/Q3/max + over-limit ratio, drops > 890s, trims top 1%
  - `models`, `model_info`, `model_online_list` (active model catalog, excludes deprecated); automatic hour/day/month granularity selection in Asia/Shanghai

- **Auto Routing** (see [Auto Routing](docs/auto_routing.md))
  - Entry: `model: auto` (case-insensitive), or `models.auto = TRUE` concrete models on the normal port; VIP port never auto-routes by model flag
  - Two-stage decision in `AutoRouteAlgorithm`: small requests (estimated `< 3000` tokens) go straight to a `is_routing_model` backend; otherwise multimodal bypass → prefix-cache hit (`> 0.7` ratio, unambiguous) → routing-LLM complexity classification (1–10) matched against `complexity_min`/`complexity_max`
  - Routing call uses structured outputs (`json_schema` `{"complexity": N}`), least-workload routing-server selection, 10s timeout, recorded as a separate `ip_id = 0` `llm-choosing` request row
  - Context-overflow fallback: a 400 mentioning the target's `max_context_window` switches an auto-routed request to `router.fallback_model` and retries
  - Decision trail persisted in `requests.router_result` (prefixed with origin model name) and `requests.model_choosing_latency`

- **Management & Admin APIs**
  - `POST /api/whitelist/update` — upsert whitelist entry by `employee_no`
  - `POST /api/refresh_user_info` — kick off CMDB user refresh thread (requires `cmdb.enabled`)
  - `POST /api/add_server` — register a new upstream server after verifying its `/models`
  - `GET /api/download/ai_assistant` — download `AI_Assistant.exe`

- **Management Commands**
  - `init_db` — validate DB connectivity and required tables
  - `check_db_schema` — diff live schema against Django models; `--fix` emits/executes corrective DDL
  - `check_server_health` — probe servers, update circuit-breaker state, optionally recover offline servers
  - `cleanup_stale_processing` — drain abandoned `processing` rows and decrement workload counters
  - `release_vip_cooldowns` — demote VIP servers whose `vip_cooldown` has expired
  - `refresh_user_info` — refresh `user_ips` from CMDB (requires `cmdb.enabled`), supports `--dry-run`

- **Configuration**
  - `config.yaml` (overridable via `LLM_ROUTER_CONFIG`) deep-merged onto built-in defaults
  - Env-var overrides for DB, Redis, `VIP_PORT`, prefix-cache thresholds, Django secret/debug, test SQLite mode
  - `start_prod.sh` (ports 8001+8008, Redis 6379, 8×64) and `start_test.sh` (ports 9000+9001, Redis 6380, 1×8) gunicorn launchers
  - WSGI entrypoint validates DB connectivity on boot; `ClientDisconnectMiddleware` registered globally

- **Tests**
  - 33 pytest files covering proxy, parser, headers, SSE, errors, server choosers, circuit breaker, cancellable upstream, disconnect tracking, request logger, requests repository, workload accounting, schema check, management API, downloads, statistics API, opencode policy, manage.py wrapper, config env overrides, VIP channel, context-overflow fallback, token filtering/estimation, the online model list endpoint, the `refresh_user_info` command, MR live review (upsert/list/stats/by-date/by-confidence), and codehub review creation

## Notes

- Do not run `makemigrations` for schema changes unless the database ownership model changes.
- Do not commit real database passwords, upstream API keys, or corporate CMDB credentials.
