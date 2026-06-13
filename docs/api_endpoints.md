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

All `/v1/*` requests are proxied to an online row from `servers` for the request `model_id` with the same path and query string. `/v1/models` requests do not need a `model_id` and are routed to a random online server.

Example server rows:

```sql
INSERT INTO servers (model_id, base_url, is_online)
VALUES
  (7, 'http://10.0.0.11:8000', true),
  (7, 'http://10.0.0.12:8000', true),
  (8, 'http://10.0.0.20:8000', true);
```

The default chooser is prefix-cache-preble: before each backend attempt, the router records the server `base_url` in `target_pod_ip`, records `attempt_count`, records the best prefix-cache `match_ratio` in `prefix_cache`, and records the historical request id that produced that best match in `last_match` (`NULL` when there is no match). If `match_ratio > prefix_cache.primary_match_threshold` (`0.9` by default), it chooses the least-loaded cached server; otherwise it chooses the least-loaded online server. The secondary threshold is configured with `prefix_cache.secondary_match_threshold` (`0.5` by default). Both can be overridden with `PREFIX_CACHE_PRIMARY_MATCH_THRESHOLD` and `PREFIX_CACHE_SECONDARY_MATCH_THRESHOLD`. Prefix cache blocks are measured in Unicode characters with `prefix_cache.prefix_block_chars` (`8` by default), and the final partial block is cached. Prefix cache metadata is marked only after a successful response completes.

Use `python manage.py prod check_server_health --recover-offline` from cron or a scheduler to actively probe server health. Passive request failures also mark servers offline and the router retries another online candidate when it is still safe to do so.

Example:

```bash
curl -i http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"test-model","messages":[{"role":"user","content":"hi"}]}'
```

## Statistics APIs

```http
GET /api/request_stats
GET /api/total_request_count
GET /api/model_request_stats
GET /api/all_model_request_stats
GET /api/models
GET /api/model_online_list
GET /api/model_info
GET /api/request_time_stats
GET /api/model_request_time_stats
GET /api/model_request_count_by_period
GET /api/model_ip_count_by_period
GET /api/model_latency_boxplot
```

Statistics endpoints use query-string parameters. Time ranges use Beijing-local `YYYY-MM-DD HH:mm:ss` values. Bucket granularity (hour/day/month) is chosen automatically from the range.

## AI Assistant Download

```http
GET /api/download/ai_assistant
```

Downloads `/home/AI_Assistant/AI_Assistant.exe` as `application/octet-stream`.

## Whitelist Update

```http
POST /api/whitelist/update
```

JSON example:

```bash
curl -i -X POST http://localhost:8001/api/whitelist/update \
  -H 'Content-Type: application/json' \
  -d '{"employee_no":"E001","is_allowed":1}'
```

## Refresh User Info

```http
POST /api/refresh_user_info
```

Starts the CMDB refresh flow in a background thread (requires `cmdb.enabled`):

```bash
curl -i -X POST http://localhost:8001/api/refresh_user_info
```

## Add Server

```http
POST /api/add_server
```

Registers one or more new upstream servers. The endpoint verifies that the upstream `/models` advertises the requested `model_name` before persisting the row.

Accepts either a single dictionary or a list of dictionaries.

### Request Body (Single)

```json
{
  "base_url": "http://10.1.2.3:8000/v1",
  "model_name": "gpt-3.5-turbo"
}
```

### Request Body (Multiple)

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

Note: Duplicate `base_url` within a single request is not allowed. All operations are logged to the `server_operations` table.
