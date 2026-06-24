#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_DIR="${ROOT_DIR}/deploy/docker"

CENTRAL_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml"
CENTRAL_ENV="${DOCKER_DIR}/DOCKER_STACK_CENTRAL_DECOUPLED.env"
if [[ ! -f "${CENTRAL_ENV}" ]]; then
  CENTRAL_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_CENTRAL.compose.yml"
  CENTRAL_ENV="${DOCKER_DIR}/DOCKER_STACK_CENTRAL.env"
fi
REDIS_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_REDIS.compose.yml"
REDIS_ENV="${DOCKER_DIR}/DOCKER_STACK_REDIS.env"
CPU_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_CPU.compose.yml"
CPU_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_CPU.env"
GPU_CAPS_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml"
GPU_CAPS_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU_CAPS.env"
GPU_COMPOSE="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU.compose.yml"
GPU_ENV="${DOCKER_DIR}/DOCKER_STACK_WORKER_GPU.env"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  ACTION="help"
  shift || true
else
  ACTION="${1:-status}"
  shift || true
fi
BUILD=0
FOLLOW=0
TAIL="120"
TARGETS=()
EXPLICIT_TARGETS=()

usage() {
  cat <<'USAGE'
Usage: bash backend/run.sh <action> [targets...] [options]

Actions:
  start       Start backend services
  stop        Stop backend services
  restart     Restart backend services
  status      Show compose/container status
  logs        Show logs for targets

Targets:
  all         redis + central + cpu + all GPU capability workers
  central     central api + monitor
  redis       redis stack
  cpu         cpu worker
  gpu         all GPU capability workers
  boltz2      boltz2 GPU worker
  boltz2score boltz2score GPU worker
  affinity    affinity GPU worker
  alphafold3  alphafold3 GPU worker
  protenix    protenix GPU worker
  pocketxmol  pocketxmol GPU worker

Options:
  --build     Build/rebuild images on start/restart
  --follow    Follow logs
  --tail N    Log lines to show, default 120
  -h,--help   Show help

Examples:
  bash backend/run.sh restart central boltz2
  bash backend/run.sh start all --build
  bash backend/run.sh logs boltz2 --follow
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --follow|-f)
      FOLLOW=1
      shift
      ;;
    --tail)
      TAIL="${2:-120}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      TARGETS+=("$1")
      EXPLICIT_TARGETS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=(all)
fi

is_explicit_target() {
  local query="$1"
  local item
  for item in "${EXPLICIT_TARGETS[@]:-}"; do
    [[ "${item}" == "${query}" || "${item}" == "all" || "${item}" == "gpu" ]] && return 0
  done
  return 1
}

has_stack_files() {
  local compose_file="$1"
  local env_file="$2"
  [[ -f "${compose_file}" && -f "${env_file}" ]]
}

warn_missing_stack() {
  local target="$1"
  local compose_file="$2"
  local env_file="$3"
  cat >&2 <<EOF_WARN
WARN: skipped optional backend target '${target}'.
      Missing compose/env file:
        compose: ${compose_file}
        env:     ${env_file}
      To enable this service, create the env file from deploy/docker/*.env.example and follow docs/deployment/quick-start.md.
      Model/runtime details are in docs/deployment/model-services.md.
EOF_WARN
}

compose_cmd() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  docker compose -f "${compose_file}" --env-file "${env_file}" "$@"
}

up_args() {
  if [[ "${BUILD}" == "1" ]]; then
    printf '%s\0' up -d --build --force-recreate
  else
    printf '%s\0' up -d
  fi
}

run_up() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  local args=()
  while IFS= read -r -d '' item; do args+=("${item}"); done < <(up_args)
  compose_cmd "${compose_file}" "${env_file}" "${args[@]}" "$@"
}

run_stop() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  compose_cmd "${compose_file}" "${env_file}" stop "$@"
}

run_restart() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  if [[ "${BUILD}" == "1" ]]; then
    run_up "${compose_file}" "${env_file}" "$@"
  else
    compose_cmd "${compose_file}" "${env_file}" restart "$@"
  fi
}

run_status() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  compose_cmd "${compose_file}" "${env_file}" ps "$@"
}

run_logs() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  local args=(logs --tail "${TAIL}")
  if [[ "${FOLLOW}" == "1" ]]; then
    args+=(-f)
  fi
  compose_cmd "${compose_file}" "${env_file}" "${args[@]}" "$@"
}

GPU_PROFILES=(boltz2 boltz2score affinity alphafold3 protenix pocketxmol)

gpu_profile_args() {
  local profiles=("$@")
  local args=()
  local profile
  for profile in "${profiles[@]}"; do
    args+=(--profile "${profile}")
  done
  printf '%s\0' "${args[@]}"
}

gpu_service_name() {
  case "$1" in
    boltz2) echo "gpu-worker-boltz2" ;;
    boltz2score) echo "gpu-worker-boltz2score" ;;
    affinity) echo "gpu-worker-affinity" ;;
    alphafold3) echo "gpu-worker-alphafold3" ;;
    protenix) echo "gpu-worker-protenix" ;;
    pocketxmol) echo "gpu-worker-pocketxmol" ;;
    *) return 1 ;;
  esac
}

handle_compose() {
  local compose_file="$1"
  local env_file="$2"
  shift 2
  case "${ACTION}" in
    start) run_up "${compose_file}" "${env_file}" "$@" ;;
    stop) run_stop "${compose_file}" "${env_file}" "$@" ;;
    restart) run_restart "${compose_file}" "${env_file}" "$@" ;;
    status) run_status "${compose_file}" "${env_file}" "$@" ;;
    logs) run_logs "${compose_file}" "${env_file}" "$@" ;;
    *) echo "ERROR: unknown action: ${ACTION}" >&2; usage >&2; exit 1 ;;
  esac
}

handle_gpu_caps() {
  local profiles=("$@")
  local profile_args=()
  while IFS= read -r -d '' item; do profile_args+=("${item}"); done < <(gpu_profile_args "${profiles[@]}")
  local services=()
  local profile
  for profile in "${profiles[@]}"; do
    services+=("$(gpu_service_name "${profile}")")
  done

  case "${ACTION}" in
    start)
      run_up "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${profile_args[@]}" "${services[@]}"
      ;;
    stop)
      run_stop "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${profile_args[@]}" "${services[@]}"
      ;;
    restart)
      if [[ "${BUILD}" == "1" ]]; then
        run_up "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${profile_args[@]}" "${services[@]}"
      else
        compose_cmd "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${profile_args[@]}" restart "${services[@]}"
      fi
      ;;
    status)
      compose_cmd "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${profile_args[@]}" ps "${services[@]}"
      ;;
    logs)
      local args=("${profile_args[@]}" logs --tail "${TAIL}")
      if [[ "${FOLLOW}" == "1" ]]; then args+=(-f); fi
      args+=("${services[@]}")
      compose_cmd "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}" "${args[@]}"
      ;;
    *) echo "ERROR: unknown action: ${ACTION}" >&2; usage >&2; exit 1 ;;
  esac
}

expand_targets() {
  local expanded=()
  local target
  for target in "${TARGETS[@]}"; do
    case "${target}" in
      all) expanded+=(redis central cpu "${GPU_PROFILES[@]}") ;;
      gpu) expanded+=("${GPU_PROFILES[@]}") ;;
      *) expanded+=("${target}") ;;
    esac
  done
  printf '%s\n' "${expanded[@]}" | awk '!seen[$0]++'
}

if [[ "${ACTION}" == "help" ]]; then
  usage
  exit 0
fi

mapfile -t EXPANDED_TARGETS < <(expand_targets)

for target in "${EXPANDED_TARGETS[@]}"; do
  echo "==> ${ACTION} ${target}"
  case "${target}" in
    redis)
      if ! has_stack_files "${REDIS_COMPOSE}" "${REDIS_ENV}"; then warn_missing_stack "${target}" "${REDIS_COMPOSE}" "${REDIS_ENV}"; is_explicit_target "${target}" && exit 1 || continue; fi
      handle_compose "${REDIS_COMPOSE}" "${REDIS_ENV}"
      ;;
    central)
      if ! has_stack_files "${CENTRAL_COMPOSE}" "${CENTRAL_ENV}"; then warn_missing_stack "${target}" "${CENTRAL_COMPOSE}" "${CENTRAL_ENV}"; is_explicit_target "${target}" && exit 1 || continue; fi
      handle_compose "${CENTRAL_COMPOSE}" "${CENTRAL_ENV}"
      ;;
    cpu)
      if ! has_stack_files "${CPU_COMPOSE}" "${CPU_ENV}"; then warn_missing_stack "${target}" "${CPU_COMPOSE}" "${CPU_ENV}"; is_explicit_target "${target}" && exit 1 || continue; fi
      handle_compose "${CPU_COMPOSE}" "${CPU_ENV}" cpu-worker
      ;;
    boltz2|boltz2score|affinity|alphafold3|protenix|pocketxmol)
      if ! has_stack_files "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}"; then warn_missing_stack "${target}" "${GPU_CAPS_COMPOSE}" "${GPU_CAPS_ENV}"; is_explicit_target "${target}" && exit 1 || continue; fi
      handle_gpu_caps "${target}"
      ;;
    gpu-unified)
      if ! has_stack_files "${GPU_COMPOSE}" "${GPU_ENV}"; then warn_missing_stack "${target}" "${GPU_COMPOSE}" "${GPU_ENV}"; is_explicit_target "${target}" && exit 1 || continue; fi
      handle_compose "${GPU_COMPOSE}" "${GPU_ENV}" gpu-worker
      ;;
    *)
      echo "ERROR: unknown target: ${target}" >&2
      usage >&2
      exit 1
      ;;
  esac
done
