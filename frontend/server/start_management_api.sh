#!/bin/bash
# 管理网关启动脚本：/vbio-api 代理与鉴权层（127.0.0.1:5055）。
set -a
cd "$(dirname "$0")/.."
[ ! -f .env ] || source .env
set +a
export VBIO_POSTGREST_URL="${VBIO_POSTGREST_URL:-http://127.0.0.1:54321}"
export VBIO_RUNTIME_API_BASE_URL="${VBIO_RUNTIME_API_BASE_URL:-http://127.0.0.1:5000}"
export VBIO_RUNTIME_API_TOKEN="${VBIO_RUNTIME_API_TOKEN:-${BOLTZ_API_TOKEN:-${VITE_API_TOKEN:-}}}"
export VBIO_MGMT_HOST="${VBIO_MGMT_HOST:-0.0.0.0}"
export VBIO_MGMT_PORT="${VBIO_MGMT_PORT:-5055}"
export VBIO_MGMT_WORKERS="${VBIO_MGMT_WORKERS:-2}"
export VBIO_MGMT_TIMEOUT="${VBIO_MGMT_TIMEOUT:-180}"
RUN_DIR="$(dirname "$0")/.run"
mkdir -p "$RUN_DIR"
printf "%s\n" "$$" > "$RUN_DIR/management_api.pid"
cd "$(dirname "$0")/server"
exec /data/V-Bio/venv/bin/gunicorn --workers "${VBIO_MGMT_WORKERS}" \
  --bind "${VBIO_MGMT_HOST}:${VBIO_MGMT_PORT}" \
  --timeout "${VBIO_MGMT_TIMEOUT}" \
  vbio_management_api:app
