#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/scripts/common.sh
source "${SCRIPT_DIR}/common.sh"

ROLE="${1:-all}"

require_cmd docker
require_cmd bash

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin not available." >&2
  exit 1
fi

echo "[OK] docker + docker compose"

case "${ROLE}" in
  gpu|all)
    if command -v nvidia-smi >/dev/null 2>&1; then
      echo "[OK] nvidia-smi detected"
      nvidia-smi -L || true
    else
      echo "WARN: nvidia-smi not found. GPU worker may not run."
    fi
    ;;
  cpu|central)
    ;;
  *)
    echo "Usage: $0 [all|central|gpu|cpu]" >&2
    exit 1
    ;;
esac

echo "[OK] preflight completed for role=${ROLE}"
