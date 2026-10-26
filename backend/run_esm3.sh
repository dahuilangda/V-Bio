#!/usr/bin/env bash
# ESM3 peptide-LM inference service lifecycle for backend/run.sh.
#
# The service is a host process (needs the local GPU + the ESM3 venv), so
# unlike the docker-compose capabilities it is managed with a pid file.
# It runs from a working directory that carries esm3_local/data (see
# INFRA_PROVIDER in _esm3_http_service.py) so startup never touches the
# network — the direct internet route this host depends on is gone.
set -euo pipefail

ESM3_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../capabilities/peptide_lm" && pwd)"
RUNTIME_DIR="$(cd "${ESM3_DIR}/../../frontend/.run" && pwd)"
mkdir -p "${RUNTIME_DIR}"

ESM3_PID_FILE="${RUNTIME_DIR}/esm3_http_service.pid"
ESM3_LOG="${RUNTIME_DIR}/esm3_http_service.log"
ESM3_PORT="${ESM3_PORT:-9333}"
ESM3_PYTHON="${ESM3_PYTHON:-/data/Boltz2Score/.venv/bin/python}"
ESM3_SERVICE="${ESM3_DIR}/peplm/integrate/_esm3_http_service.py"
ESM3_WORKDIR="${ESM3_WORKDIR:-${ESM3_DIR}/esm3_local}"

is_running() {
  local pid
  pid="$(cat "${ESM3_PID_FILE}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

port_in_use() {
  ss -tln 2>/dev/null | grep -q ":${ESM3_PORT} "
}

start() {
  if is_running; then
    echo "[esm3] already running (pid: $(cat "${ESM3_PID_FILE}"))."
    return 0
  fi
  # Stale port holder without a live pid-file entry: either a foreign
  # process or an orphan from before the pid-file existed. Surface it and
  # stop — killing an unknown listener is the caller's call.
  if port_in_use; then
    echo "[esm3] port ${ESM3_PORT} is held by an unknown process;" \
         "stop it first (ss -tlnp | grep ${ESM3_PORT})." >&2
    return 1
  fi
  if [[ ! -x "${ESM3_PYTHON}" || ! -f "${ESM3_SERVICE}" ]]; then
    echo "[esm3] python/service file missing: ${ESM3_PYTHON} ${ESM3_SERVICE}" >&2
    return 1
  fi
  # INFRA_PROVIDER pins the SDK data path to ./data (offline); the cwd must
  # be the dir holding that symlink. GPU lease happens inside the service.
  (
    cd "${ESM3_WORKDIR}"
    INFRA_PROVIDER=1 setsid nohup "${ESM3_PYTHON}" "${ESM3_SERVICE}" "${ESM3_PORT}" \
      >>"${ESM3_LOG}" 2>&1 < /dev/null &
    echo $! >"${ESM3_PID_FILE}"
  )
  sleep 2
  if is_running; then
    echo "[esm3] started (pid: $(cat "${ESM3_PID_FILE}"), port: ${ESM3_PORT})."
  else
    echo "[esm3] failed to start — see ${ESM3_LOG}" >&2
    return 1
  fi
}

stop() {
  if is_running; then
    local pid
    pid="$(cat "${ESM3_PID_FILE}")"
    kill "${pid}" >/dev/null 2>&1 || true
    # GPU-worker child dies with the parent's teardown; give it a moment
    sleep 2
    pkill -f "_esm3_gpu_worker.py" 2>/dev/null || true
    echo "[esm3] stopped (pid: ${pid})."
  else
    echo "[esm3] not running."
  fi
  rm -f "${ESM3_PID_FILE}"
}

status() {
  if is_running; then
    echo "[esm3] running (pid: $(cat "${ESM3_PID_FILE}"), port: ${ESM3_PORT})."
  elif port_in_use; then
    echo "[esm3] port ${ESM3_PORT} held by an unknown process (no pid file)."
  else
    echo "[esm3] stopped."
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *)
    echo "Usage: bash $(basename "${BASH_SOURCE[0]}") <start|stop|restart|status>"
    exit 1
    ;;
esac
