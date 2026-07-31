# Management Commands

All commands are invoked via `python manage.py <env> <command>`, where `<env>` is `prod` or `test`. The token is required (except for `help`/`--help`) and selects DB port `5431` / `5432` before delegating to Django.

## `init_db`

Verifies DB connectivity and that all required tables exist; exits non-zero if any are missing.

```bash
python manage.py test init_db
```

## `check_db_schema`

Diffs the live PostgreSQL schema against every model in `router/models.py`: missing tables/columns, extra columns, extra defaults, missing auto-increment identity on auto primary keys, NULL/NOT NULL mismatches, type mismatches, single-column unique-constraint mismatches, and missing model-declared indexes.

```bash
python manage.py test check_db_schema --dry-run
python manage.py test check_db_schema --fix
```

Use `--dry-run` first in production. `--fix` emits and executes corrective DDL, including concurrent index creation on PostgreSQL.

## `check_server_health`

Issues HTTP health probes against active servers, updating circuit-breaker state. `--recover-offline` brings passing offline servers back online.

```bash
python manage.py prod check_server_health --recover-offline
python manage.py prod check_server_health --server-id 12
```

## `cleanup_stale_processing`

Flips `processing` rows older than the threshold (default 20 minutes) to `incomplete` with `fail_reason="stale processing"` and decrements upstream workload counters.

```bash
python manage.py prod cleanup_stale_processing --threshold 20
python manage.py prod cleanup_stale_processing --threshold 20 --dry-run
```

## `release_vip_cooldowns`

Demotes VIP servers whose `vip_cooldown` timestamp is older than the configured cooldown period (default 300 seconds). Useful for cron when VIP traffic is idle for long periods.

```bash
python manage.py prod release_vip_cooldowns
python manage.py prod release_vip_cooldowns --cooldown 600 --dry-run
```

## `refresh_user_info`

Refresh `user_ips` table from the CMDB source. Requires `cmdb.enabled` to be true in `config.yaml` and a CMDB adapter that implements the lookup methods in `CMDBService`. The full refresh walks both row kinds:

- **IP-backed rows** (`ip`/`ip_id` fixed, `apikey` empty): iterates active IPs, calls `CMDBService.fetch_user_data(ip) -> dict`.
- **API-key-backed rows** (`apikey`/`employee_no` fixed, `ip_id = 0`): iterates active API-key rows, calls `CMDBService.fetch_user_data_by_employee_no(employee_no) -> dict`.

Both lookups return `user_name`, `user_charge`, `employee_no`, `department_id`, and `vip`; `None` when there is no CMDB record. The public adapter is a dummy, so each pass exits with an explanatory error until an internal adapter provides the corresponding method. Supports dry-run mode to generate SQL without applying changes and `--ip` to refresh a single IP-backed address (the API-key pass only runs in a full refresh).

```bash
python manage.py prod refresh_user_info --dry-run
python manage.py prod refresh_user_info --ip 127.0.0.1
```
