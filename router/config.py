from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict[str, Any] = {
    "log_path": "./logs/requests",
    "server": {"bind": "0.0.0.0:8001", "data_upload_max_memory_size_mb": 50, "vip_port": 8008},
    "vip": {
        "cooldown_seconds": 300,
        "min_normal_servers": 2,
    },
    "proxy": {
        "default_max_tokens": 28528,
        "unknown_model_max_tokens": 20480,
        "auto_max_tokens": 40000,
        "stream_connect_timeout_seconds": 30,
        "stream_read_timeout_seconds": 900,
        "stream_total_timeout_seconds": 900,
        "normal_connect_timeout_seconds": 5,
        "normal_read_timeout_seconds": 900,
        "client_disconnect_check_interval_seconds": 0.5,
        "stale_processing_minutes": 20,
        "opencode_failure_delay_seconds": 180,
    },
    "load_balancer": {
        "max_attempts_per_request": 3,
        "retry_status_codes": [502, 503, 504],
        "mark_unhealthy_status_codes": [502, 503, 504],
        "health_check_timeout_seconds": 2,
        "chooser_class": "router.route_algorithm.prefix_cache_preble.PrefixCachePrebleServerChooser",
        "circuit_breaker": {
            "failure_threshold": 3,
            "base_cooldown_seconds": 30,
            "max_cooldown_seconds": 3000,
            "success_threshold": 1,
            "half_open_probe_limit": 1,
        },
    },
    "prefix_cache": {
        "primary_match_threshold": 0.9,
        "secondary_match_threshold": 0.5,
        "overload_workload_gap": 4,
        "overload_workload_ratio": 2.0,
        "max_prefix_chars": 2000000,
        "prefix_block_chars": 128,
        "redis": {
            "host": "localhost",
            "port": 6379,
            "db": 0,
            "password": None,
        },
    },
    "pd": {
        "recompute_max": 3,
        "prefill_connect_timeout_seconds": 5,
        "prefill_read_timeout_seconds": 300,
    },
    "opencode": {
        "enabled": True,
        "block_max_version": "1.2.26",
    },
    "admission": {"allow_when_user_info_missing": True},
    "tokenizer": {"enabled": False},
    "cmdb": {"enabled": False, "dummy": True, "refresh_interval_between_ips_seconds": 1},
    "database": {
        "host": "localhost",
        "port": 5432,
        "user": "postgres",
        "password": "postgres",
        "name": "postgres",
        "sslmode": "disable",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("LLM_ROUTER_CONFIG", BASE_DIR / "config.yaml"))
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    config = _deep_merge(DEFAULT_CONFIG, data)
    if "LLM_ROUTER_LOG_PATH" in os.environ:
        config["log_path"] = os.environ["LLM_ROUTER_LOG_PATH"]
    prefix_cache = config.setdefault("prefix_cache", {})

    database = config.setdefault("database", {})
    for env_key, config_key in {
        "DB_HOST": "host",
        "DB_PORT": "port",
        "DB_USER": "user",
        "DB_PASSWORD": "password",
        "DB_NAME": "name",
        "DB_SSLMODE": "sslmode",
    }.items():
        if env_key in os.environ:
            database[config_key] = os.environ[env_key]
    if "VIP_PORT" in os.environ:
        try:
            config.setdefault("server", {})["vip_port"] = int(os.environ["VIP_PORT"])
        except (TypeError, ValueError):
            pass
    if "HTTP_PORT" in os.environ:
        try:
            config.setdefault("server", {})["bind"] = f"0.0.0.0:{int(os.environ['HTTP_PORT'])}"
        except (TypeError, ValueError):
            pass
    if "PREFIX_CACHE_PRIMARY_MATCH_THRESHOLD" in os.environ:
        prefix_cache["primary_match_threshold"] = os.environ["PREFIX_CACHE_PRIMARY_MATCH_THRESHOLD"]
    if "PREFIX_CACHE_SECONDARY_MATCH_THRESHOLD" in os.environ:
        prefix_cache["secondary_match_threshold"] = os.environ["PREFIX_CACHE_SECONDARY_MATCH_THRESHOLD"]
    if "PREFIX_CACHE_MAX_PREFIX_CHARS" in os.environ:
        try:
            prefix_cache["max_prefix_chars"] = int(os.environ["PREFIX_CACHE_MAX_PREFIX_CHARS"])
        except (TypeError, ValueError):
            pass
    for env_key in ("PREFIX_CACHE_BLOCK_CHARS", "PREFIX_CACHE_PREFIX_BLOCK_CHARS"):
        if env_key in os.environ:
            try:
                prefix_cache["prefix_block_chars"] = int(os.environ[env_key])
            except (TypeError, ValueError):
                pass
    
    redis_cfg = prefix_cache.setdefault("redis", {})
    if "REDIS_HOST" in os.environ:
        redis_cfg["host"] = os.environ["REDIS_HOST"]
    if "REDIS_PORT" in os.environ:
        try:
            redis_cfg["port"] = int(os.environ["REDIS_PORT"])
        except (TypeError, ValueError):
            pass
    if "REDIS_PASSWORD" in os.environ:
        redis_cfg["password"] = os.environ["REDIS_PASSWORD"]
    if "REDIS_DB" in os.environ:
        try:
            redis_cfg["db"] = int(os.environ["REDIS_DB"])
        except (TypeError, ValueError):
            pass
    return config


APP_CONFIG = load_config()
