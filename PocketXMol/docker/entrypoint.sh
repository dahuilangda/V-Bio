#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" = "0" ] && [ "${PXM_USER_SWITCHED:-0}" != "1" ]; then
  TARGET_UID="${DOCKER_UID:-$(stat -c '%u' /workspace)}"
  TARGET_GID="${DOCKER_GID:-$(stat -c '%g' /workspace)}"

  if [ "${TARGET_UID}" != "0" ] || [ "${TARGET_GID}" != "0" ]; then
    export PXM_USER_SWITCHED=1
    exec gosu "${TARGET_UID}:${TARGET_GID}" "$0" "$@"
  fi
fi

if [ -z "${HOME:-}" ] || [ ! -w "${HOME:-/}" ]; then
  export HOME="/tmp/pocketxmol-home-$(id -u)"
fi
mkdir -p "${HOME}"

source /opt/conda/etc/profile.d/conda.sh
conda activate pxm_cu128

CKPT="data/trained_models/pxm/checkpoints/pocketxmol.ckpt"
if [ ! -f "${CKPT}" ]; then
  if [ -f "weights/model_weights.tar.gz" ]; then
    echo "[entrypoint] Extracting model weights from weights/model_weights.tar.gz"
    tar -xzf weights/model_weights.tar.gz
  elif [ -f "model_weights.tar.gz" ]; then
    echo "[entrypoint] Extracting model weights from model_weights.tar.gz"
    tar -xzf model_weights.tar.gz
  else
    echo "[entrypoint] Warning: model weights not found. Expected ${CKPT}" >&2
  fi
fi

exec "$@"
