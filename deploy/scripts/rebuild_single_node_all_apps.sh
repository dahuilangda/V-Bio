#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/common.sh
source "${SCRIPT_DIR}/common.sh"

DOCKER_DIR="${PROJECT_ROOT}/deploy/docker"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
SUPABASE_DIR="${FRONTEND_DIR}/supabase-lite"

FRONTEND_MODE="dev"
REBUILD_RUNTIME_IMAGE=1
RESTART_FRONTEND=1

usage() {
  cat <<'EOF'
Usage: bash deploy/scripts/rebuild_single_node_all_apps.sh [options]

Options:
  --prod-frontend       Start frontend with `frontend/run.sh start`
  --skip-frontend       Skip supabase-lite + frontend restart
  --skip-runtime-image  Skip rebuilding `vbio-boltz2-runtime`
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod-frontend)
      FRONTEND_MODE="start"
      shift
      ;;
    --skip-frontend)
      RESTART_FRONTEND=0
      shift
      ;;
    --skip-runtime-image)
      REBUILD_RUNTIME_IMAGE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_cmd docker
require_cmd bash

compose_force_recreate() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  docker compose -f "${compose_file}" --env-file "${env_file}" up -d --build --force-recreate "$@"
}

remove_container_if_exists() {
  local name="$1"
  if docker ps -a --format '{{.Names}}' | grep -Fx "${name}" >/dev/null 2>&1; then
    docker rm -f "${name}" >/dev/null 2>&1 || true
  fi
}

remove_container_matches() {
  local suffix="$1"
  mapfile -t _matched_names < <(docker ps -a --format '{{.Names}}' | grep -E "${suffix}" || true)
  if [[ ${#_matched_names[@]} -eq 0 ]]; then
    return 0
  fi
  for container_name in "${_matched_names[@]}"; do
    docker rm -f "${container_name}" >/dev/null 2>&1 || true
  done
}

recover_mmp_postgres_conflict() {
  remove_container_if_exists "leadopt_mmp_db"
  remove_container_matches '(^|_)leadopt_mmp_db$'
}

resolve_compose_pair() {
  local preferred_compose="$1"
  local preferred_env="$2"
  local fallback_compose="$3"
  local fallback_env="$4"
  if [[ -f "${preferred_env}" ]]; then
    printf '%s\n%s\n' "${preferred_compose}" "${preferred_env}"
    return 0
  fi
  if [[ -f "${fallback_env}" ]]; then
    printf '%s\n%s\n' "${fallback_compose}" "${fallback_env}"
    return 0
  fi
  echo "ERROR: neither env file exists: ${preferred_env} / ${fallback_env}" >&2
  exit 1
}

require_env_file() {
  local env_file="$1"
  if [[ ! -f "${env_file}" ]]; then
    echo "ERROR: required env file not found: ${env_file}" >&2
    exit 1
  fi
}

echo "==> Rebuilding single-node V-Bio services"
echo "Project root: ${PROJECT_ROOT}"

REDIS_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_REDIS.compose.yml"
REDIS_ENV="${DOCKER_DIR}/DOCKER_STACK_REDIS.env"
COLABFOLD_COMPOSE="${DOCKER_DIR}/DOCKER_CAP_COLABFOLD_SERVER.compose.yml"
COLABFOLD_ENV="${DOCKER_DIR}/DOCKER_CAP_COLABFOLD_SERVER.env"
MMP_COMPOSE="${DOCKER_DIR}/DOCKER_CAP_MMP_POSTGRES.compose.yml"
MMP_ENV="${DOCKER_DIR}/DOCKER_CAP_MMP_POSTGRES.env"
GPU_CAPS_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml"
GPU_CAPS_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU_CAPS.env"
GPU_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU.compose.yml"
GPU_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU.env"
CPU_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_CPU.compose.yml"
CPU_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_CPU.env"

require_env_file "${REDIS_ENV}"
require_env_file "${COLABFOLD_ENV}"
require_env_file "${MMP_ENV}"
require_env_file "${CPU_ENV}"

mapfile -t CENTRAL_PAIR < <(
  resolve_compose_pair \
    "${DOCKER_DIR}/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml" \
    "${DOCKER_DIR}/DOCKER_STACK_CENTRAL_DECOUPLED.env" \
    "${DOCKER_DIR}/DOCKER_STACK_CENTRAL.compose.yml" \
    "${DOCKER_DIR}/DOCKER_STACK_CENTRAL.env"
)
CENTRAL_COMPOSE="${CENTRAL_PAIR[0]}"
CENTRAL_ENV="${CENTRAL_PAIR[1]}"

GPU_MODE="caps"
if [[ -f "${GPU_CAPS_ENV}" ]]; then
  require_env_file "${GPU_CAPS_ENV}"
else
  GPU_MODE="single"
  require_env_file "${GPU_ENV}"
fi

echo "==> Redis"
compose_force_recreate "${REDIS_COMPOSE}" "${REDIS_ENV}"

echo "==> ColabFold MSA"
compose_force_recreate "${COLABFOLD_COMPOSE}" "${COLABFOLD_ENV}"

echo "==> MMP PostgreSQL"
recover_mmp_postgres_conflict
compose_force_recreate "${MMP_COMPOSE}" "${MMP_ENV}"

echo "==> Central API / Monitor"
compose_force_recreate "${CENTRAL_COMPOSE}" "${CENTRAL_ENV}"

if [[ "${REBUILD_RUNTIME_IMAGE}" == "1" ]]; then
  echo "==> Boltz runtime image"
  docker build -f "${DOCKER_DIR}/DOCKER_BOLTZ2_RUNTIME.Dockerfile" -t vbio-boltz2-runtime "${PROJECT_ROOT}"
fi

if [[ "${GPU_MODE}" == "caps" ]]; then
  echo "==> GPU capability workers"
  docker compose \
    -f "${GPU_CAPS_COMPOSE}" \
    --env-file "${GPU_CAPS_ENV}" \
    --profile boltz2 \
    --profile boltz2score \
    --profile affinity \
    --profile alphafold3 \
    --profile protenix \
    --profile pocketxmol \
    up -d --build --force-recreate
else
  echo "==> Unified GPU worker"
  compose_force_recreate "${GPU_COMPOSE}" "${GPU_ENV}"
fi

echo "==> CPU worker"
compose_force_recreate "${CPU_COMPOSE}" "${CPU_ENV}"

if [[ "${RESTART_FRONTEND}" == "1" ]]; then
  echo "==> Frontend"
  (
    cd "${SUPABASE_DIR}"
    docker compose up -d --build
  )
  (
    cd "${PROJECT_ROOT}"
    bash frontend/run.sh stop || true
    bash frontend/run.sh "${FRONTEND_MODE}"
  )
fi

echo
echo "Rebuild complete."
echo "Central compose: ${CENTRAL_COMPOSE}"
echo "GPU mode: ${GPU_MODE}"
if [[ "${RESTART_FRONTEND}" == "1" ]]; then
  if [[ "${FRONTEND_MODE}" == "dev" ]]; then
    echo "Frontend URL: http://127.0.0.1:5173"
  else
    echo "Frontend preview URL: http://127.0.0.1:5173"
  fi
fi
