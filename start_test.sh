#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export DB_HOST="${DB_HOST:-localhost}"
export DB_PORT="${DB_PORT:-5432}"
export LLM_ROUTER_CONFIG="${LLM_ROUTER_CONFIG:-$(pwd)/config.yaml}"
export LLM_ROUTER_LOG_PATH="${LLM_ROUTER_LOG_PATH:-.logs/requests}"
export LLM_ROUTER_VERBOSE_REQUEST_LOG=1
export HTTP_PORT="${HTTP_PORT:-9000}"
export VIP_PORT="${VIP_PORT:-9001}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export REDIS_DB="${REDIS_DB:-0}"

source ./scripts/local_redis.sh
ensure_local_redis test

exec gunicorn router_project.wsgi:application \
  --bind "0.0.0.0:${HTTP_PORT}" \
  --bind "0.0.0.0:${VIP_PORT}" \
  --workers "${GUNICORN_WORKERS:-1}" \
  --threads "${GUNICORN_THREADS:-8}" \
  --worker-class gthread \
  --timeout "${GUNICORN_TIMEOUT:-3720}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-3960}" \
  --access-logfile - \
  --error-logfile -
