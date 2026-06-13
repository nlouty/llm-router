# Tests

Run the full test suite:

```bash
python -m pytest tests
```

Tests use SQLite when `USE_SQLITE_FOR_TESTS=1` (see `tests/conftest.py`).

## Test Files

| File | Coverage |
|------|----------|
| `test_admission_view.py` | Admission denials: deprecated model 400, max-tokens reason, unknown-model 400, small unknown model 400 |
| `test_api_download.py` | `/api/download/ai_assistant` success and 404 paths |
| `test_api_stats.py` | All `/api/*_stats` endpoints, hour/day/month bucketing, boxplot edge cases |
| `test_cancellable_upstream.py` | `CancellableUpstreamRequest.cancel()` shuts down in-flight HTTP via socket close |
| `test_check_db_schema.py` | `check_db_schema` drift detection and `--fix` on PostgreSQL |
| `test_circuit_breaker.py` | Failure counting, threshold, open/half_open transitions, exponential cooldown |
| `test_codehub_review.py` | `POST /api/codehub_review` create, missing `issue_hash`, invalid fields, duplicate skip |
| `test_config.py` | `PREFIX_CACHE_*` env overrides applied by `load_config` |
| `test_context_overflow.py` | Auto-routed context-overflow switches to fallback; explicit-model requests do not switch |
| `test_disconnect.py` | `DisconnectWatcher` event/callback semantics |
| `test_errors.py` | Error payload shape and SSE timeout event format |
| `test_headers.py` | Request-header filtering (hop-by-hop + bodyless `Content-Type`) |
| `test_management_api.py` | Whitelist upsert messages and `refresh_user_info` thread launch |
| `test_manage.py` | `manage.py prod`/`test` argument parsing and DB port selection |
| `test_model_online_list.py` | `/api/model_online_list` returns only non-deprecated models |
| `test_mr_live_review.py` | `POST /api/mr_live_review` upsert, missing `discussion_id`, invalid fields |
| `test_mr_live_review_list.py` | MR live review list by type (valid/invalid/no_reply), missing params, invalid type |
| `test_mr_live_review_stats.py` | MR live review stats and by-confidence aggregation |
| `test_mr_live_review_stats_by_date.py` | MR live review stats-by-date (valid/invalid/no_reply/total) and accept_rate |
| `test_opencode.py` | Opencode UA blocking and 400-delay version comparisons |
| `test_parser.py` | JSON body rewriting (stream_options, default max_tokens, non-JSON passthrough) |
| `test_proxy.py` | `GET /v1/models` random-online routing and other proxy paths |
| `test_proxy_usage.py` | `parse_json_usage` with/without cached tokens, invalid/missing usage |
| `test_redis_prefix_cache.py` | Redis-backed prefix cache trie flow |
| `test_refresh_user_info_command.py` | `refresh_user_info` dry-run SQL and actual update paths |
| `test_request_logger.py` | Per-request log file append and relative-path resolution |
| `test_requests_repository.py` | `record_attempt` persists `prefix_cache` / `last_match` |
| `test_server_chooser.py` | Least-connection and prefix-cache-Preble chooser selection logic |
| `test_server_operations.py` | `POST /api/add_server` single/multiple/duplicate/partial-failure, `server_operations` logging |
| `test_sse.py` | `parse_sse_usage` extracts the last `usage` block from SSE |
| `test_token_filtering.py` | Server filtering by `context_window`, unlimited window, parser token estimation |
| `test_vip.py` | VIP port gating, VIP/non-VIP candidate selection, scaling up/down |
| `test_workload.py` | `servers.workload` increment/decrement and stale cleanup decrements |
