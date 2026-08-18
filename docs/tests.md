# Tests

Run the full test suite:

```bash
python -m pytest tests
```

Tests use SQLite when `USE_SQLITE_FOR_TESTS=1` (see `tests/conftest.py`).

## Test Files

| File | Coverage |
|------|----------|
| `test_admission_view.py` | Deprecated model, max-token, unknown-model, and unknown-small-request admission failures |
| `test_api_download.py` | `/api/download/ai_assistant` success and 404 paths |
| `test_api_stats.py` | Stats endpoints, hour/day/month bucketing, boxplot edge cases |
| `test_cancellable_upstream.py` | `CancellableUpstreamRequest.cancel()` shuts down in-flight HTTP via socket close |
| `test_check_db_schema.py` | `check_db_schema` drift detection and `--fix` on PostgreSQL |
| `test_circuit_breaker.py` | Failure counting, threshold, open/half_open transitions, exponential cooldown |
| `test_codehub_review.py` | CodeHub review create, duplicate skip, required hash, and invalid-field validation |
| `test_config.py` | `PREFIX_CACHE_*` env overrides applied by `load_config` |
| `test_context_overflow.py` | Auto-selected context-overflow retry to fallback model |
| `test_disconnect.py` | `DisconnectWatcher` event/callback semantics |
| `test_errors.py` | Error payload shape and SSE timeout event format |
| `test_headers.py` | Request-header filtering (hop-by-hop + bodyless `Content-Type`) |
| `test_manage.py` | `manage.py prod`/`test` argument parsing and DB port selection |
| `test_management_api.py` | Whitelist upsert messages and `refresh_user_info` thread launch |
| `test_model_online_list.py` | Online model catalog excludes deprecated models |
| `test_mr_live_review.py` | MR live review create, update, skip, and validation |
| `test_mr_live_review_list.py` | MR review list filters, pagination, and validation |
| `test_mr_live_review_stats.py` | MR review branch and confidence aggregations |
| `test_mr_live_review_stats_by_date.py` | MR review date-series counts and accept-rate stats |
| `test_opencode.py` | Opencode UA blocking and 400-delay version comparisons |
| `test_parser.py` | JSON body rewriting, stream options, default max tokens, non-JSON passthrough |
| `test_pd_context_overflow.py` | PD prefiller context-overflow switch to larger-window servers and circuit-breaker ticks |
| `test_proxy.py` | Proxy flow, auto routing, routing LLM calls, router results, retries, and `/v1/models` routing |
| `test_proxy_unhandled_exception.py` | View catch-all: 502 + self-describing `fail_reason`, one-line main log, full traceback in per-request log file |
| `test_proxy_usage.py` | JSON usage parsing, including cached token counts |
| `test_redis_prefix_cache.py` | Redis prefix-cache write/read flow and match ratios |
| `test_refresh_user_info_command.py` | CMDB refresh command dry-run and single-IP behavior |
| `test_request_logger.py` | Per-request log file append and relative-path resolution |
| `test_requests_repository.py` | Request attempt metadata, model choosing latency, cleanup, and repository counts |
| `test_server_chooser.py` | Least-connection and prefix-cache-Preble chooser selection logic |
| `test_server_operations.py` | `/api/add_server` success, duplicate, partial failure, and operation logging |
| `test_sse.py` | `parse_sse_usage` extracts the last `usage` block from SSE |
| `test_token_filtering.py` | Estimated-token storage and `servers.context_window` filtering |
| `test_vip.py` | VIP port eligibility, pool promotion/demotion, cooldowns, and workload accounting |
| `test_workload.py` | `servers.workload` increment/decrement and stale cleanup decrements |
