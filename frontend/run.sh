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
VBIO_MGMT_HOST="${VBIO_MGMT_HOST:-0.0.0.0}"
VBIO_MGMT_PORT="${VBIO_MGMT_PORT:-5055}"
VBIO_MGMT_WORKERS="${VBIO_MGMT_WORKERS:-2}"
VBIO_MGMT_TIMEOUT="${VBIO_MGMT_TIMEOUT:-180}"

is_running() {
  local pid_file="$1"
  if [[ ! -f "${pid_file}" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
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
}

start_supabase() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[supabase-lite] docker not found, skip startup." >&2
    return 0
  fi
  echo "[supabase-lite] starting db/rest..."
  (
    cd "${VBIO_DIR}/supabase-lite"
    docker compose up -d db rest >/dev/null
  )
}

run_db_migrate() {
  echo "[supabase-lite] running schema migration..."
  (
    cd "${VBIO_DIR}"
    npm run db:migrate >/dev/null
  )
}

start_management_api() {
  if is_running "${MGMT_PID_FILE}"; then
    echo "[management-api] already running (pid: $(cat "${MGMT_PID_FILE}"))."
    return 0
  fi

  load_frontend_env
  activate_python_env

  export VBIO_POSTGREST_URL="${VBIO_POSTGREST_URL:-http://127.0.0.1:54321}"
  export VBIO_RUNTIME_API_BASE_URL="${VBIO_RUNTIME_API_BASE_URL:-http://127.0.0.1:5000}"
  export VBIO_RUNTIME_API_TOKEN="${VBIO_RUNTIME_API_TOKEN:-${BOLTZ_API_TOKEN:-${VITE_API_TOKEN:-}}}"

  echo "[management-api] starting on ${VBIO_MGMT_HOST}:${VBIO_MGMT_PORT}..."
  if command -v gunicorn >/dev/null 2>&1; then
    (
      cd "${VBIO_DIR}/server"
      nohup gunicorn \
        --workers "${VBIO_MGMT_WORKERS}" \
        --bind "${VBIO_MGMT_HOST}:${VBIO_MGMT_PORT}" \
        --timeout "${VBIO_MGMT_TIMEOUT}" \
        "vbio_management_api:app" >"${MGMT_LOG}" 2>&1 &
      echo $! >"${MGMT_PID_FILE}"
    )
  else
    (
      cd "${VBIO_DIR}/server"
      nohup python ./vbio_management_api.py >"${MGMT_LOG}" 2>&1 &
      echo $! >"${MGMT_PID_FILE}"
    )
  fi
  echo "[management-api] started (pid: $(cat "${MGMT_PID_FILE}"))."
}

start_frontend_prod() {
  if is_running "${FRONTEND_PID_FILE}"; then
    echo "[V-Bio frontend] tracked instance found, restarting..."
    stop_by_pid_file "frontend" "${FRONTEND_PID_FILE}"
  fi
  force_clear_listeners_on_port "${VBIO_FRONTEND_PORT}"
  echo "[V-Bio frontend] building..."
  (
    cd "${VBIO_DIR}"
    npm run build >/dev/null
  )
  echo "[V-Bio frontend] starting preview on ${VBIO_FRONTEND_HOST}:${VBIO_FRONTEND_PORT}..."
  (
    cd "${VBIO_DIR}"
    nohup npm run preview -- --host "${VBIO_FRONTEND_HOST}" --port "${VBIO_FRONTEND_PORT}" --strictPort >"${FRONTEND_LOG}" 2>&1 &
    echo $! >"${FRONTEND_PID_FILE}"
  )
  echo "[V-Bio frontend] started (pid: $(cat "${FRONTEND_PID_FILE}"))."
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
    nohup npm run dev -- --host "${VBIO_FRONTEND_HOST}" --port "${VBIO_FRONTEND_PORT}" --strictPort >"${FRONTEND_LOG}" 2>&1 &
    echo $! >"${FRONTEND_PID_FILE}"
  )
  echo "[V-Bio frontend] started (pid: $(cat "${FRONTEND_PID_FILE}"))."
}

status_all() {
  if is_running "${MGMT_PID_FILE}"; then
    echo "[management-api] running (pid: $(cat "${MGMT_PID_FILE}"))."
  else
    echo "[management-api] stopped."
  fi

  if is_running "${FRONTEND_PID_FILE}"; then
    echo "[V-Bio frontend] running (pid: $(cat "${FRONTEND_PID_FILE}"))."
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
  run_db_migrate
  start_management_api
  start_frontend_prod
  echo "frontend: http://127.0.0.1:${VBIO_FRONTEND_PORT}"
  echo "V-Bio management API: http://127.0.0.1:${VBIO_MGMT_PORT}/vbio-api"
}

start_dev() {
  start_supabase
  run_db_migrate
  start_management_api
  start_frontend_dev
  echo "frontend (dev): http://127.0.0.1:${VBIO_FRONTEND_PORT}"
  echo "V-Bio management API: http://127.0.0.1:${VBIO_MGMT_PORT}/vbio-api"
}

stop_all() {
  stop_by_pid_file "frontend" "${FRONTEND_PID_FILE}"
  stop_by_pid_file "management-api" "${MGMT_PID_FILE}"
}

usage() {
  cat <<'EOF'
Usage: bash frontend/run.sh <command>

Commands:
  start      Start production stack (supabase-lite + db:migrate + management-api + frontend preview)
  dev        Start development stack (supabase-lite + db:migrate + management-api + frontend dev)
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
