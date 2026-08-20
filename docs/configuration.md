# Configuration

Default configuration is loaded from `config.yaml` and deep-merged onto built-in defaults in `router/config.py`. Optional keys that are not present in the repository sample can still be supplied in your own config file.

## Example `config.yaml`

```yaml
log_path: ./logs/requests

server:
  bind: 0.0.0.0:8001
  vip_port: 8008
  data_upload_max_memory_size_mb: 50

vip:
  cooldown_seconds: 300
  min_normal_servers: 2

proxy:
  default_max_tokens: 28528
  unknown_model_max_tokens: 20480
  auto_max_tokens: 40000
  stream_connect_timeout_seconds: 30
  stream_read_timeout_seconds: 900
  stream_total_timeout_seconds: 900
  normal_connect_timeout_seconds: 5
  normal_read_timeout_seconds: 900
  llm_choosing_timeout_seconds: 10
  client_disconnect_check_interval_seconds: 0.5
  stale_processing_minutes: 20
  opencode_failure_delay_seconds: 180

load_balancer:
  max_attempts_per_request: 3
  retry_status_codes: [502, 503, 504]
  mark_unhealthy_status_codes: [502, 503, 504]
  health_check_timeout_seconds: 2
  chooser_class: router.route_algorithm.prefix_cache_preble.PrefixCachePrebleServerChooser
  circuit_breaker:
    failure_threshold: 3
    base_cooldown_seconds: 30
    max_cooldown_seconds: 3000
    success_threshold: 1

prefix_cache:
  primary_match_threshold: 0.9
  secondary_match_threshold: 0.5
  max_prefix_chars: 2000000
  prefix_block_chars: 128
  redis:
    host: localhost
    port: 6379
    db: 0
    password: null

opencode:
  enabled: true
  block_max_version: "1.2.26"

admission:
  allow_when_user_info_missing: true

tokenizer:
  enabled: false

cmdb:
  enabled: false
  dummy: true
  refresh_interval_between_ips_seconds: 1

router:
  fallback_model: DeepSeek-V4-Flash
  system_prompt_path: router/assets/router_system_prompt.md
  auto_concurrent_limit: 6

database:
  host: localhost
  port: 5432
  user: postgres
  password: postgres
  name: postgres
  sslmode: disable
```

Request logs are written below `log_path` as `YYYY/MM/DD/HH/MM/<request_id>.log`. `start_prod.sh` defaults `log_path` to `/data/router_log` and disables verbose request logging. `start_test.sh` defaults `log_path` to `.logs/requests` and enables a `user_request` event containing the full request body as pretty JSON.

Point the router to another config file with:

```bash
export LLM_ROUTER_CONFIG=/path/to/config.yaml
```

## Environment Variables

Database, listener, and log values can be overridden with environment variables:

- `LLM_ROUTER_LOG_PATH` overrides `log_path`.

```bash
export DB_HOST=localhost
export DB_PORT=5432
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_NAME=postgres
export DB_SSLMODE=disable
export HTTP_PORT=8001
export VIP_PORT=8008
export LLM_ROUTER_LOG_PATH=/data/router_log
```

Other useful environment variables:

```bash
export DJANGO_SECRET_KEY='change-me'
export DJANGO_DEBUG=0
export PREFIX_CACHE_PRIMARY_MATCH_THRESHOLD=0.9
export PREFIX_CACHE_SECONDARY_MATCH_THRESHOLD=0.5
export PREFIX_CACHE_MAX_PREFIX_CHARS=2000000
export PREFIX_CACHE_BLOCK_CHARS=8
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export REDIS_DB=0
export REDIS_PASSWORD=
export USE_SQLITE_FOR_TESTS=1
```

`start_prod.sh` defaults to Redis port `6379`; `start_test.sh` defaults to Redis port `6380`. Both scripts start local Redis automatically only for local Redis hosts. Install Redis first, for example with `sudo dnf install redis`.

Prefix cache blocks are measured in Python Unicode characters, not LLM tokenizer tokens. This keeps matching language-neutral for Chinese and other no-whitespace prompts. The final partial block is also stored so short prompts and exact full-prefix matches are cacheable.

## Important Sections

`server.bind` controls the normal HTTP listener. `server.vip_port` is used to detect VIP-channel traffic; non-VIP IPs that hit the VIP port receive HTTP 503. `server.data_upload_max_memory_size_mb` feeds Django's upload/body size limit.

`vip.cooldown_seconds` controls how long a VIP server stays in cooldown before it can be demoted to the normal pool. `vip.min_normal_servers` keeps at least that many normal servers available when VIP scale-up promotes servers.

`proxy.default_max_tokens` is injected into JSON bodies that omit `max_tokens`. `proxy.unknown_model_max_tokens` is used only when admission checks a request without a model name. `proxy.auto_max_tokens` is the `max_tokens` limit for auto-routed requests — exact `model: auto` or any model with `auto = TRUE` — because the serving target is not known until routing resolves it. Stream and normal requests have separate connect/read timeout settings; streaming also has `stream_total_timeout_seconds`. `proxy.llm_choosing_timeout_seconds` is the absolute budget for the internal llm-choosing request: every attempt's socket timeouts are clamped to the remaining budget, so a hung routing server is disconnected at the deadline and the choosing call fails fast with a 504 (the client request then falls back to the default model).

`load_balancer.chooser_class` must point to a class implementing `choose(candidates, context, attempted_server_ids)`. The default prefix-cache chooser stores successful request prefixes in Redis and falls back to least-connection selection when no useful cache match exists. Retries are attempted **only** on connection failures (the request body never reached the upstream, so retrying another server is safe for non-idempotent POST). Read timeouts and any HTTP response status — including the values listed in `retry_status_codes` — are **not** retried; the only deliberate exception is context-overflow, which retries once on a larger-context server/model. `mark_unhealthy_status_codes` controls passive circuit-breaker failures.

`prefix_cache.primary_match_threshold` chooses among cached servers when the best per-server match is above the threshold. `secondary_match_threshold` chooses among partially matching servers before falling back to all candidates. `max_prefix_chars` caps the prefix text stored and checked per request. `prefix_block_chars` controls the character block size used to build Redis prefix hashes.

`router.fallback_model` is used by auto routing when the classifier cannot produce a unique target. `router.system_prompt_path` points to the complexity-classifier prompt. `router.auto_concurrent_limit` is the base concurrency limit for exact `model: auto` requests before multiplying by `ips.concurrent_multiplier`.

`tokenizer.enabled` toggles real tokenizer counting. It is **off by default**: the router always computes a fast heuristic estimate (`fast_estimate_tokens`, a char/byte heuristic in `router/utils/token_count.py` that needs no model) at parse time, which feeds `requests.estimate_tokens` and small-request routing. When enabled, after a model is selected the router counts the request again with that model's tokenizer (loaded from `models.model_path` via `AutoTokenizer.from_pretrained`, fast with slow fallback, cached per worker) and overwrites `estimate_tokens` with the real count, logging a `tokenizer_count` event (tokens, latency_ms) to the per-request log. Small-request routing always uses the fast heuristic, because no model is resolved yet.
