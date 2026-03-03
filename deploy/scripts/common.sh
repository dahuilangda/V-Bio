#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: required command not found: ${cmd}" >&2
    exit 1
  fi
}

ensure_env_file() {
  local target_env="$1"
  local example_env="$2"
  if [[ -f "${target_env}" ]]; then
    return 0
  fi
  if [[ ! -f "${example_env}" ]]; then
    echo "ERROR: example env not found: ${example_env}" >&2
    exit 1
  fi
  cp "${example_env}" "${target_env}"
  echo "Created ${target_env} from ${example_env}. Please edit it before production use."
}

compose_up() {
  local compose_file="$1"
  local env_file="$2"
  docker compose -f "${compose_file}" --env-file "${env_file}" up -d --build
}

compose_down() {
  local compose_file="$1"
  local env_file="$2"
  docker compose -f "${compose_file}" --env-file "${env_file}" down
}

install_systemd_unit() {
  local unit_src="$1"
  local unit_name
  unit_name="$(basename "${unit_src}")"
  sudo cp "${unit_src}" "/etc/systemd/system/${unit_name}"
  sudo systemctl daemon-reload
}

enable_start_unit() {
  local unit_name="$1"
  sudo systemctl enable "${unit_name}"
  sudo systemctl restart "${unit_name}"
  sudo systemctl status "${unit_name}" --no-pager -l || true
}

print_next_steps() {
  local message="$1"
  echo
  echo "Done: ${message}"
  echo "Project root: ${PROJECT_ROOT}"
}
