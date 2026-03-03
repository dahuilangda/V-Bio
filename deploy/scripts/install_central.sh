#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/common.sh
source "${SCRIPT_DIR}/common.sh"

DOCKER_DIR="${PROJECT_ROOT}/deploy/docker"
COMPOSE_FILE="${DOCKER_DIR}/DOCKER_STACK_CENTRAL.compose.yml"
ENV_FILE="${DOCKER_DIR}/DOCKER_STACK_CENTRAL.env"
ENV_EXAMPLE="${DOCKER_DIR}/DOCKER_STACK_CENTRAL.env.example"
UNIT_SRC="${PROJECT_ROOT}/deploy/systemd/boltz-central.service"
UNIT_NAME="boltz-central.service"

MODE="${1:-systemd}"

require_cmd docker
require_cmd sudo

ensure_env_file "${ENV_FILE}" "${ENV_EXAMPLE}"

if [[ "${MODE}" == "compose" ]]; then
  compose_up "${COMPOSE_FILE}" "${ENV_FILE}"
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" ps
  print_next_steps "central stack started via compose"
  exit 0
fi

if [[ "${MODE}" != "systemd" ]]; then
  echo "Usage: $0 [systemd|compose]" >&2
  exit 1
fi

install_systemd_unit "${UNIT_SRC}"
enable_start_unit "${UNIT_NAME}"
print_next_steps "central stack installed and started with systemd"
