#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

CONFIG_TASK="configs/sample/examples/dock_smallmol.yml"
CONFIG_MODEL="configs/sample/pxm.yml"
TASK_PRESET=""
OUTDIR="outputs_examples"
DEVICE="cuda:0"
GPU_CONSTRAINT="all"
VISIBLE_DEVICES=""
BATCH_SIZE="0"
DO_BUILD=0
EXTRA_ARGS=""
DO_RESCORE=0
RESCORE_DEVICE=""
RESCORE_BATCH_SIZE="0"
SCORE_CONFIG="configs/sample/confidence/tuned_cfd.yml"
RANK_MODE="tuned"
RANK_OUTPUT="confidence_ranking.csv"
EXP_NAME=""
RESCORE_ONLY=0

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/run_pocketxmol_docker.sh [options]

Options:
  --task NAME          Task preset: dock, dock_refdiff, sbdd, maskfill, linking, growing, pepdesign
  --list-tasks         Show available task presets
  --config-task PATH   Task config yaml (default: configs/sample/examples/dock_smallmol.yml)
  --config-model PATH  Model config yaml (default: configs/sample/pxm.yml)
  --outdir DIR         Output directory (default: outputs_examples)
  --gpus ARG           Docker GPU constraint, e.g. all or device=3 (default: all)
  --visible-devices V  Override NVIDIA_VISIBLE_DEVICES for compose run, e.g. 3 or 1,2
  --device DEV         Device, e.g. cuda:0 or cpu (default: cuda:0)
  --batch-size N       Override batch size, 0 means use config (default: 0)
  --extra-args STR     Extra args appended to sample_use.py, e.g. "--num_workers 0"
  --rescore            After inference, run tuned confidence rescoring + ranking
  --rescore-only       Only run rescoring/ranking on existing experiment (skip inference)
  --exp-name NAME      Experiment directory name (exact or prefix) for rescoring
  --score-config PATH  Confidence config yaml (default: configs/sample/confidence/tuned_cfd.yml)
  --rescore-device DEV Device for rescoring (default: same as --device)
  --rescore-batch-size N  Batch size for rescoring (default: 0 -> use config)
  --rank-mode MODE     Ranking mode: tuned | self | hybrid (default: tuned)
  --rank-output FILE   Ranking output file name under experiment dir (default: confidence_ranking.csv)
  --build              Build image before running
  -h, --help           Show this help message
USAGE
}

set_task_preset() {
  case "$1" in
    dock)
      CONFIG_TASK="configs/sample/examples/dock_smallmol.yml"
      ;;
    dock_refdiff)
      CONFIG_TASK="configs/sample/examples/dock_smallmol_refdiff.yml"
      ;;
    sbdd)
      CONFIG_TASK="configs/sample/examples/sbdd.yml"
      ;;
    maskfill)
      CONFIG_TASK="configs/sample/examples/linking_fixed_frags.yml"
      ;;
    linking)
      CONFIG_TASK="configs/sample/examples/linking_fixed_frags.yml"
      ;;
    growing)
      CONFIG_TASK="configs/sample/examples/growing_fixed_frag.yml"
      ;;
    pepdesign)
      CONFIG_TASK="configs/sample/examples/pepdesign_denovo.yml"
      ;;
    *)
      echo "Unknown task preset: $1" >&2
      echo "Use --list-tasks to show supported presets." >&2
      exit 1
      ;;
  esac
}

print_task_presets() {
  cat <<'TASKS'
Available --task presets:
  dock      -> configs/sample/examples/dock_smallmol.yml
  dock_refdiff -> configs/sample/examples/dock_smallmol_refdiff.yml
  sbdd      -> configs/sample/examples/sbdd.yml
  maskfill  -> configs/sample/examples/linking_fixed_frags.yml
  linking   -> configs/sample/examples/linking_fixed_frags.yml
  growing   -> configs/sample/examples/growing_fixed_frag.yml
  pepdesign -> configs/sample/examples/pepdesign_denovo.yml
TASKS
}

get_config_prefix() {
  local task_base model_base
  task_base="$(basename "${CONFIG_TASK}")"
  model_base="$(basename "${CONFIG_MODEL}")"
  task_base="${task_base%.yml}"
  model_base="${model_base%.yml}"
  echo "${task_base}_${model_base}"
}

find_latest_experiment() {
  local result_root="$1"
  local exp_prefix="$2"
  local latest=""
  if [[ ! -d "${result_root}" ]]; then
    echo ""
    return
  fi

  latest="$(
    find "${result_root}" -maxdepth 1 -mindepth 1 -type d -name "${exp_prefix}_202*" -printf '%f\n' \
      | sort \
      | tail -n 1
  )"
  echo "${latest}"
}

resolve_experiment_name() {
  local result_root="$1"
  local exp_name="$2"
  local config_prefix="$3"
  local resolved=""

  if [[ -n "${exp_name}" ]]; then
    if [[ -d "${result_root}/${exp_name}" ]]; then
      echo "${exp_name}"
      return
    fi
    resolved="$(find_latest_experiment "${result_root}" "${exp_name}")"
    if [[ -n "${resolved}" ]]; then
      echo "${resolved}"
      return
    fi
    echo ""
    return
  fi

  resolved="$(find_latest_experiment "${result_root}" "${config_prefix}")"
  echo "${resolved}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)
      TASK_PRESET="$2"
      set_task_preset "${TASK_PRESET}"
      shift 2
      ;;
    --list-tasks)
      print_task_presets
      exit 0
      ;;
    --config-task)
      CONFIG_TASK="$2"
      shift 2
      ;;
    --config-model)
      CONFIG_MODEL="$2"
      shift 2
      ;;
    --outdir)
      OUTDIR="$2"
      shift 2
      ;;
    --gpus)
      GPU_CONSTRAINT="$2"
      shift 2
      ;;
    --visible-devices)
      VISIBLE_DEVICES="$2"
      shift 2
      ;;
    --device)
      DEVICE="$2"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="$2"
      shift 2
      ;;
    --extra-args)
      EXTRA_ARGS="$2"
      shift 2
      ;;
    --rescore)
      DO_RESCORE=1
      shift
      ;;
    --rescore-only)
      RESCORE_ONLY=1
      DO_RESCORE=1
      shift
      ;;
    --exp-name)
      EXP_NAME="$2"
      shift 2
      ;;
    --score-config)
      SCORE_CONFIG="$2"
      shift 2
      ;;
    --rescore-device)
      RESCORE_DEVICE="$2"
      shift 2
      ;;
    --rescore-batch-size)
      RESCORE_BATCH_SIZE="$2"
      shift 2
      ;;
    --rank-mode)
      RANK_MODE="$2"
      shift 2
      ;;
    --rank-output)
      RANK_OUTPUT="$2"
      shift 2
      ;;
    --build)
      DO_BUILD=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "${RESCORE_ONLY}" != "1" ]]; then
  if [[ ! -f "${CONFIG_TASK}" ]]; then
    echo "Config task file not found: ${CONFIG_TASK}" >&2
    exit 1
  fi
  if [[ ! -f "${CONFIG_MODEL}" ]]; then
    echo "Config model file not found: ${CONFIG_MODEL}" >&2
    exit 1
  fi
fi
if [[ "${DO_RESCORE}" == "1" ]] && [[ ! -f "${SCORE_CONFIG}" ]]; then
  echo "Confidence config file not found: ${SCORE_CONFIG}" >&2
  exit 1
fi
if [[ "${RANK_MODE}" != "tuned" && "${RANK_MODE}" != "self" && "${RANK_MODE}" != "hybrid" ]]; then
  echo "Invalid --rank-mode: ${RANK_MODE}. Expected tuned, self, or hybrid." >&2
  exit 1
fi

mkdir -p "${OUTDIR}"

if [[ "${DO_BUILD}" == "1" ]]; then
  echo "[run_pocketxmol_docker] docker compose build pocketxmol"
  docker compose build pocketxmol
fi

if [[ -z "${VISIBLE_DEVICES}" && "${GPU_CONSTRAINT}" == device=* ]]; then
  VISIBLE_DEVICES="${GPU_CONSTRAINT#device=}"
fi

if [[ -n "${VISIBLE_DEVICES}" ]]; then
  export PXM_NVIDIA_VISIBLE_DEVICES="${VISIBLE_DEVICES}"
else
  unset PXM_NVIDIA_VISIBLE_DEVICES || true
fi

compose_run_cmd=(docker compose run --rm --no-deps)
if [[ -n "${PXM_NVIDIA_VISIBLE_DEVICES:-}" ]]; then
  compose_run_cmd+=(-e "NVIDIA_VISIBLE_DEVICES=${PXM_NVIDIA_VISIBLE_DEVICES}")
fi

if [[ "${RESCORE_ONLY}" != "1" ]]; then
  export PXM_CONFIG_TASK="${CONFIG_TASK}"
  export PXM_CONFIG_MODEL="${CONFIG_MODEL}"
  export PXM_OUTDIR="${OUTDIR}"
  export PXM_DEVICE="${DEVICE}"
  export PXM_BATCH_SIZE="${BATCH_SIZE}"
  if [[ -n "${EXTRA_ARGS}" ]]; then
    export PXM_EXTRA_ARGS="${EXTRA_ARGS}"
  else
    unset PXM_EXTRA_ARGS || true
  fi

  echo "[run_pocketxmol_docker] Running inference via docker compose"
  echo "  PXM_CONFIG_TASK=${PXM_CONFIG_TASK}"
  echo "  PXM_CONFIG_MODEL=${PXM_CONFIG_MODEL}"
  echo "  PXM_OUTDIR=${PXM_OUTDIR}"
  echo "  GPU_CONSTRAINT=${GPU_CONSTRAINT}"
  if [[ -n "${PXM_NVIDIA_VISIBLE_DEVICES:-}" ]]; then
    echo "  NVIDIA_VISIBLE_DEVICES=${PXM_NVIDIA_VISIBLE_DEVICES}"
  fi
  echo "  PXM_DEVICE=${PXM_DEVICE}"
  echo "  PXM_BATCH_SIZE=${PXM_BATCH_SIZE}"
  if [[ -n "${PXM_EXTRA_ARGS:-}" ]]; then
    echo "  PXM_EXTRA_ARGS=${PXM_EXTRA_ARGS}"
  fi

  "${compose_run_cmd[@]}" pocketxmol
fi

if [[ "${DO_RESCORE}" == "1" ]]; then
  CONFIG_PREFIX="$(get_config_prefix)"
  TARGET_EXP_NAME="$(resolve_experiment_name "${OUTDIR}" "${EXP_NAME}" "${CONFIG_PREFIX}")"
  if [[ -z "${TARGET_EXP_NAME}" ]]; then
    echo "Could not resolve experiment in ${OUTDIR}." >&2
    echo "Please set --exp-name explicitly (exact directory name or prefix)." >&2
    exit 1
  fi
  if [[ -z "${RESCORE_DEVICE}" ]]; then
    RESCORE_DEVICE="${DEVICE}"
  fi

  echo "[run_pocketxmol_docker] Running tuned confidence rescoring"
  echo "  EXP_NAME=${TARGET_EXP_NAME}"
  echo "  SCORE_CONFIG=${SCORE_CONFIG}"
  echo "  RESCORE_DEVICE=${RESCORE_DEVICE}"
  echo "  RESCORE_BATCH_SIZE=${RESCORE_BATCH_SIZE}"

  "${compose_run_cmd[@]}" pocketxmol \
    python scripts/believe_use_pdb.py \
      --exp_name "${TARGET_EXP_NAME}" \
      --result_root "${OUTDIR}" \
      --config "${SCORE_CONFIG}" \
      --device "${RESCORE_DEVICE}" \
      --batch_size "${RESCORE_BATCH_SIZE}"

  echo "[run_pocketxmol_docker] Building sorted confidence ranking"
  "${compose_run_cmd[@]}" pocketxmol \
    python scripts/make_confidence_ranking.py \
      --exp_name "${TARGET_EXP_NAME}" \
      --result_root "${OUTDIR}" \
      --mode "${RANK_MODE}" \
      --output "${RANK_OUTPUT}"

  echo "[run_pocketxmol_docker] Done."
  echo "  Ranking file: ${OUTDIR}/${TARGET_EXP_NAME}/${RANK_OUTPUT}"
fi
