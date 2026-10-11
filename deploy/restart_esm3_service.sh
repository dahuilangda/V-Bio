#!/usr/bin/env bash
# Restart the ESM3 HTTP proposal service with the CURRENT code.
#
# Why this exists: the ESM3 service is a long-lived host process; celery and
# docker restarts do NOT reload it. Deploying changes to
# capabilities/peptide_lm/peplm/integrate/esm3_proposer.py (or anything it
# imports) without running this script leaves designs running on the old
# proposal policy — silently, with no failure signature.
#
# GPU lifecycle (pool citizen): the service acquires a GPU from the shared
# Redis pool on the first request and releases it after
# ESM3_IDLE_RELEASE_SECONDS (default 600s) idle — weights are dropped so the
# pool's memory-reclaim gate passes. Do NOT pin CUDA_VISIBLE_DEVICES here:
# the pool decides the card. ESM3_GPU_ID is the UNPOOLED emergency fallback
# for when Redis/the pool is unreachable.
#
# Usage: bash deploy/restart_esm3_service.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ESM3_PYTHON="${ESM3_VENV_PYTHON:-/data/Boltz2Score/.venv/bin/python}"
ESM3_GPU_ID="${ESM3_GPU_ID:-0}"
ESM3_PORT="${ESM3_PORT:-9333}"
LOG_DIR="${ROOT_DIR}/frontend/.run"
LOG_FILE="${LOG_DIR}/esm3_http_service.log"

mkdir -p "${LOG_DIR}"

old_pids="$(pgrep -f "_esm3_http_service.py ${ESM3_PORT}" || true)"
if [[ -n "${old_pids}" ]]; then
  echo "[esm3] stopping: ${old_pids}"
  kill ${old_pids} || true
  sleep 5
fi

cd "${ROOT_DIR}"
ESM3_GPU_ID="${ESM3_GPU_ID}" \
ESM3_DAEMON=1 \
ESM3_VENV_PYTHON="${ESM3_PYTHON}" \
env -u CUDA_VISIBLE_DEVICES \
nohup "${ESM3_PYTHON}" \
  capabilities/peptide_lm/peplm/integrate/_esm3_http_service.py "${ESM3_PORT}" \
  >"${LOG_FILE}" 2>&1 &

echo "[esm3] starting on port ${ESM3_PORT} (pool-managed GPU, log: ${LOG_FILE})"
for _ in $(seq 1 30); do
  if grep -q "listening on" "${LOG_FILE}" 2>/dev/null; then
    echo "[esm3] ready"
    exit 0
  fi
  sleep 2
done
echo "[esm3] WARNING: not ready after 60s — check ${LOG_FILE}" >&2
exit 1
