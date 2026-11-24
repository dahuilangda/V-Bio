#!/usr/bin/env bash
set -euo pipefail

VBIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${VBIO_DIR}/.." && pwd)"
RUNTIME_DIR="${VBIO_DIR}/.run"

mkdir -p "${RUNTIME_DIR}"

FRONTEND_PID_FILE="${RUNTIME_DIR}/frontend.pid"
MGMT_PID_FILE="${RUNTIME_DIR}/management_api.pid"
FRONTEND_LOG="${RUNTIME_DIR}/frontend.log"
MGMT_LOG="${RUNTIME_DIR}/management_api.log"

VBIO_FRONTEND_HOST="${VBIO_FRONTEND_HOST:-0.0.0.0}"
VBIO_FRONTEND_PORT="${VBIO_FRONTEND_PORT:-5173}"
# Loopback by default: the SPA reaches the API through the vite proxy on this host; binding
# 0.0.0.0 would expose the (unauthenticated) copilot inference endpoints to the network.
VBIO_MGMT_HOST="${VBIO_MGMT_HOST:-127.0.0.1}"
VBIO_MGMT_PORT="${VBIO_MGMT_PORT:-5055}"
VBIO_MGMT_WORKERS="${VBIO_MGMT_WORKERS:-2}"
VBIO_MGMT_THREADS="${VBIO_MGMT_THREADS:-8}"
VBIO_MGMT_TIMEOUT="${VBIO_MGMT_TIMEOUT:-180}"

FRONTEND_UNIT="vbio-frontend"
MGMT_UNIT="vbio-management-api"

is_running() {
  local pid_file="$1"
  if [[ ! -f "${pid_file}" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

require_user_systemd() {
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd user manager is required to run the frontend stack." >&2
    return 1
  fi
}

unit_active() {
  systemctl --user is-active --quiet "$1.service"
}

unit_pid() {
  systemctl --user show "$1.service" --property=MainPID --value
}

unit_env_args() {
  local args=() name
  local systemd_owned='^(INVOCATION_ID|JOURNAL_STREAM|SYSTEMD_EXEC_PID|LISTEN_FDS|LISTEN_PID|NOTIFY_SOCKET|WATCHDOG_PID|WATCHDOG_USEC)$'
  for name in $(compgen -e | /usr/bin/grep -Ev "${systemd_owned}"); do
    args+=(--setenv="${name}=${!name}")
  done
  printf '%s\0' "${args[@]}"
}

start_unit() {
  local unit="$1"
  shift
  local env_args=() arg
  require_user_systemd
  systemctl --user reset-failed "${unit}.service" >/dev/null 2>&1 || true
  while IFS= read -r -d '' arg; do env_args+=("${arg}"); done < <(unit_env_args)
  systemd-run --user --quiet --unit="${unit}" \
    --property=Restart=always \
    --property=RestartSec=3 \
    --property=KillMode=mixed \
    --property=TimeoutStopSec=20 \
    "${env_args[@]}" \
    "$@"
}

stop_unit() {
  local name="$1"
  local unit="$2"
  local pid_file="$3"
  if unit_active "${unit}"; then
    systemctl --user stop "${unit}.service"
    echo "[${name}] stopped."
  elif is_running "${pid_file}"; then
    stop_by_pid_file "${name}" "${pid_file}"
  else
    rm -f "${pid_file}"
    echo "[${name}] not running."
  fi
}

stop_by_pid_file() {
  local name="$1"
  local pid_file="$2"
  if ! is_running "${pid_file}"; then
    rm -f "${pid_file}"
    echo "[${name}] not running."
    return 0
  fi
  local pid
  pid="$(cat "${pid_file}")"
  kill "${pid}" >/dev/null 2>&1 || true
  sleep 1
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -9 "${pid}" >/dev/null 2>&1 || true
  fi
  rm -f "${pid_file}"
  echo "[${name}] stopped."
}

force_clear_listeners_on_port() {
  local port="$1"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti TCP:"${port}" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
  fi
  if [[ -z "${pids// }" ]] && command -v fuser >/dev/null 2>&1; then
    pids="$(fuser -n tcp "${port}" 2>/dev/null | tr '\n' ' ' || true)"
  fi
  if [[ -z "${pids// }" ]] && command -v ss >/dev/null 2>&1; then
    pids="$(
      ss -lntp 2>/dev/null \
        | awk -v port=":${port}" '$4 ~ port { if (match($0, /pid=[0-9]+/)) print substr($0, RSTART + 4, RLENGTH - 4) }' \
        | tr '\n' ' ' \
        || true
    )"
  fi

  local unique_pids=()
  local pid
  for pid in ${pids}; do
    if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
      continue
    fi
    if [[ "${pid}" -eq "$$" ]]; then
      continue
    fi
    local seen=0
    local existing
    for existing in "${unique_pids[@]:-}"; do
      if [[ "${existing}" -eq "${pid}" ]]; then
        seen=1
        break
      fi
    done
    if [[ "${seen}" -eq 0 ]]; then
      unique_pids+=("${pid}")
    fi
  done

  if [[ "${#unique_pids[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "[V-Bio frontend] clearing port ${port} listeners: ${unique_pids[*]}"
  kill "${unique_pids[@]}" >/dev/null 2>&1 || true
  sleep 1
  for pid in "${unique_pids[@]}"; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
  done
}

activate_python_env() {
  if [[ -d "${ROOT_DIR}/venv" ]]; then
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/venv/bin/activate"
    return 0
  fi
  if [[ -d "${VBIO_DIR}/venv" ]]; then
    # shellcheck disable=SC1091
    source "${VBIO_DIR}/venv/bin/activate"
    return 0
  fi
  echo "No python venv found at ${ROOT_DIR}/venv or ${VBIO_DIR}/venv." >&2
  return 1
}

load_frontend_env() {
  if [[ -f "${VBIO_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${VBIO_DIR}/.env"
    set +a
  fi
  # 标准配置：configs/vbio.toml 导出为进程环境（真实环境变量优先）
  if [[ -f "${ROOT_DIR}/configs/vbio.toml" ]] && command -v python3 >/dev/null 2>&1; then
    # shellcheck disable=SC2046
    eval "$(
      python3 "${ROOT_DIR}/configs/load_vbio_config.py" \
        --file "${ROOT_DIR}/configs/vbio.toml" --export
    )"
  fi
}

start_supabase() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[supabase-lite] docker is required." >&2
    return 1
  fi
  echo "[supabase-lite] starting db/rest..."
  (
    cd "${VBIO_DIR}/supabase-lite"
    docker compose up -d --wait --wait-timeout 120 db rest >/dev/null
  )
}

run_postgres_migrations() {
  load_frontend_env
  activate_python_env
  if [[ -z "${VBIO_MONITOR_DATABASE_URL:-}" ]]; then
    echo "VBIO_MONITOR_DATABASE_URL is required." >&2
    return 1
  fi
  if [[ -z "${VBIO_MONITOR_MIGRATION_DATABASE_URL:-}" ]]; then
    echo "VBIO_MONITOR_MIGRATION_DATABASE_URL is required." >&2
    return 1
  fi
  echo "[postgres] applying verified migrations..."
  (
    cd "${ROOT_DIR}"
    python -m backend.monitoring.migration_runner
  )
}

start_management_api() {
  if unit_active "${MGMT_UNIT}"; then
    echo "[management-api] already running (pid: $(unit_pid "${MGMT_UNIT}"))."
    return 0
  fi

  force_clear_listeners_on_port "${VBIO_MGMT_PORT}"
  load_frontend_env
  activate_python_env

  export VBIO_POSTGREST_URL="${VBIO_POSTGREST_URL:-http://127.0.0.1:54321}"
  export VBIO_RUNTIME_API_BASE_URL="${VBIO_RUNTIME_API_BASE_URL:-http://127.0.0.1:5000}"
  export VBIO_RUNTIME_API_TOKEN="${VBIO_RUNTIME_API_TOKEN:-${VBIO_API_TOKEN:-${VITE_API_TOKEN:-}}}"

  if [[ -z "${VBIO_MONITOR_DATABASE_URL:-}" ]]; then
    echo "VBIO_MONITOR_DATABASE_URL is required." >&2
    return 1
  fi

  echo "[management-api] starting on ${VBIO_MGMT_HOST}:${VBIO_MGMT_PORT}..."
  if ! command -v gunicorn >/dev/null 2>&1; then
    echo "gunicorn is required for the management API." >&2
    return 1
  fi
  start_unit "${MGMT_UNIT}" \
    --property=WorkingDirectory="${VBIO_DIR}/server" \
    --property=StandardOutput=append:"${MGMT_LOG}" \
    --property=StandardError=append:"${MGMT_LOG}" \
    "$(command -v gunicorn)" \
      --workers "${VBIO_MGMT_WORKERS}" \
      --worker-class gthread \
      --threads "${VBIO_MGMT_THREADS}" \
      --bind "${VBIO_MGMT_HOST}:${VBIO_MGMT_PORT}" \
      --timeout "${VBIO_MGMT_TIMEOUT}" \
      --keep-alive 75 \
      "vbio_management_api:app"
  wait_for_management_api
  rm -f "${MGMT_PID_FILE}"
  echo "[management-api] started (pid: $(unit_pid "${MGMT_UNIT}"))."
}

wait_for_management_api() {
  local host="${VBIO_MGMT_HOST}"
  if [[ "${host}" == "0.0.0.0" || "${host}" == "::" ]]; then
    host="127.0.0.1"
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "[management-api] curl is required to wait for the service to become ready." >&2
    return 1
  fi
  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if curl -fsS -m 2 -o /dev/null "http://${host}:${VBIO_MGMT_PORT}/vbio-api/healthz" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  systemctl --user stop "${MGMT_UNIT}.service" >/dev/null 2>&1 || true
  echo "[management-api] did not become ready within 30s; last log lines:" >&2
  tail -n 15 "${MGMT_LOG}" >&2
  return 1
}

start_frontend_prod() {
  if unit_active "${FRONTEND_UNIT}"; then
    echo "[V-Bio frontend] tracked instance found, restarting..."
    stop_unit "V-Bio frontend" "${FRONTEND_UNIT}" "${FRONTEND_PID_FILE}"
  fi
  force_clear_listeners_on_port "${VBIO_FRONTEND_PORT}"
  echo "[V-Bio frontend] building..."
  (
    cd "${VBIO_DIR}"
    npm run build -- --outDir dist-new --emptyOutDir >/dev/null
  )
  # Accumulate assets instead of replacing them: long-lived SPA tabs keep requesting
  # chunk files from the build they were loaded with. Wiping dist on every deploy
  # 404s those requests and freezes navigation (URL changes, view doesn't). Serving
  # the union of all builds keeps every already-open tab fully functional; the new
  # index.html steers fresh loads to the latest code.
  (
    cd "${VBIO_DIR}"
    mkdir -p "${VBIO_DIR}/dist-served"
    cp -R "${VBIO_DIR}/dist-new/." "${VBIO_DIR}/dist-served/"
    # Retention: hashed assets a tab could still reference are kept for 30 days.
    find "${VBIO_DIR}/dist-served/assets" -type f -mtime +30 -delete 2>/dev/null || true
  )
  echo "[V-Bio frontend] starting preview on ${VBIO_FRONTEND_HOST}:${VBIO_FRONTEND_PORT}..."
  start_unit "${FRONTEND_UNIT}" \
    --property=WorkingDirectory="${VBIO_DIR}" \
    --property=StandardOutput=append:"${FRONTEND_LOG}" \
    --property=StandardError=append:"${FRONTEND_LOG}" \
    "$(command -v npm)" run preview -- --outDir dist-served --host "${VBIO_FRONTEND_HOST}" --port "${VBIO_FRONTEND_PORT}" --strictPort
  rm -f "${FRONTEND_PID_FILE}"
  echo "[V-Bio frontend] started (pid: $(unit_pid "${FRONTEND_UNIT}"))."
}

start_frontend_dev() {
  if is_running "${FRONTEND_PID_FILE}"; then
    echo "[V-Bio frontend] tracked instance found, restarting..."
    stop_by_pid_file "frontend" "${FRONTEND_PID_FILE}"
  fi
  force_clear_listeners_on_port "${VBIO_FRONTEND_PORT}"
  echo "[V-Bio frontend] starting dev server on ${VBIO_FRONTEND_HOST}:${VBIO_FRONTEND_PORT}..."
  (
    cd "${VBIO_DIR}"
    setsid nohup npm run dev -- --host "${VBIO_FRONTEND_HOST}" --port "${VBIO_FRONTEND_PORT}" --strictPort >"${FRONTEND_LOG}" 2>&1 < /dev/null &
    echo $! >"${FRONTEND_PID_FILE}"
  )
  echo "[V-Bio frontend] started (pid: $(cat "${FRONTEND_PID_FILE}"))."
}

status_all() {
  if unit_active "${MGMT_UNIT}"; then
    echo "[management-api] running (pid: $(unit_pid "${MGMT_UNIT}"))."
  else
    echo "[management-api] stopped."
  fi

  if unit_active "${FRONTEND_UNIT}"; then
    echo "[V-Bio frontend] running (pid: $(unit_pid "${FRONTEND_UNIT}"))."
  else
    echo "[V-Bio frontend] stopped."
  fi

  if command -v docker >/dev/null 2>&1; then
    echo "[supabase-lite] containers:"
    (
      cd "${VBIO_DIR}/supabase-lite"
      docker compose ps || true
    )
  fi
}

start_prod() {
  start_supabase
  run_postgres_migrations
  start_management_api
  start_frontend_prod
  echo "frontend: http://127.0.0.1:${VBIO_FRONTEND_PORT}"
  echo "V-Bio management API: http://127.0.0.1:${VBIO_MGMT_PORT}/vbio-api"
}

start_dev() {
  start_supabase
  run_postgres_migrations
  start_management_api
  start_frontend_dev
  echo "frontend (dev): http://127.0.0.1:${VBIO_FRONTEND_PORT}"
  echo "V-Bio management API: http://127.0.0.1:${VBIO_MGMT_PORT}/vbio-api"
}

stop_all() {
  stop_unit "frontend" "${FRONTEND_UNIT}" "${FRONTEND_PID_FILE}"
  stop_unit "management-api" "${MGMT_UNIT}" "${MGMT_PID_FILE}"
}

usage() {
  cat <<'EOF'
Usage: bash frontend/run.sh <command>

Commands:
  start      Start production stack (supabase-lite + management-api + frontend preview)
  dev        Start development stack (supabase-lite + management-api + frontend dev)
  stop       Stop frontend and management-api launched by this script
  restart    Restart production stack
  status     Show runtime status
EOF
}

case "${1:-start}" in
  start)
    start_prod
    ;;
  dev)
    start_dev
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_prod
    ;;
  status)
    status_all
    ;;
  *)
    usage
    exit 1
    ;;
esac
