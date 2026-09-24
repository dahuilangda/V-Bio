#!/usr/bin/env bash
# Start the ESM3 inference service (peptide design proposal engine).
#
# The service loads a 3B model once and serves propose/perplexity/learn
# requests over HTTP. Workers connect to ESM3_SERVER_URL (default
# http://localhost:9333).
#
# Usage:
#   ./start_esm3_service.sh              # foreground, GPU 0
#   ESM3_GPU_ID=2 ./start_esm3_service.sh  # foreground, GPU 2
#   ESM3_DAEMON=1 ./start_esm3_service.sh  # background with pidfile
#
# Required environment:
#   ESM3_VENV_PYTHON   Python with esm>=3.4 + torch+CUDA (no default —
#                       the script refuses to guess)
#   VBIO_ROOT           Repository root (default: parent of this script)
#   ESM3_GPU_ID         GPU device (default: 0)
#   ESM3_PORT           Listen port (default: 9333)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VBIO_ROOT="${VBIO_ROOT:-$(dirname "$(dirname "$SCRIPT_DIR")")}"
PEPTIDE_LM="$VBIO_ROOT/capabilities/peptide_lm"
WEIGHTS="$PEPTIDE_LM/models/external/esm3_sm_open_v1.pth"
SERVICE="$PEPTIDE_LM/peplm/integrate/_esm3_http_service.py"
LOG_DIR="${ESM3_LOG_DIR:-/tmp}"
PID_FILE="$LOG_DIR/esm3_service.pid"
LOG_FILE="$LOG_DIR/esm3_service.log"

ESM3_GPU_ID="${ESM3_GPU_ID:-0}"
ESM3_PORT="${ESM3_PORT:-9333}"
ESM3_VENV_PYTHON="${ESM3_VENV_PYTHON:-}"

if [[ -z "$ESM3_VENV_PYTHON" ]]; then
    echo "ERROR: ESM3_VENV_PYTHON is not set." >&2
    echo "Point it at a Python interpreter with esm>=3.4 and CUDA-enabled torch," >&2
    echo "e.g. ESM3_VENV_PYTHON=/opt/some-venv/bin/python $0" >&2
    exit 1
fi
if [[ ! -x "$ESM3_VENV_PYTHON" ]]; then
    echo "ERROR: ESM3_VENV_PYTHON not executable: $ESM3_VENV_PYTHON" >&2
    exit 1
fi
if [[ ! -f "$WEIGHTS" ]]; then
    echo "ERROR: ESM3 weights not found: $WEIGHTS" >&2
    echo "Provision them per capabilities/peptide_lm/models/MANIFEST.json" >&2
    exit 1
fi

if curl -sf "http://localhost:$ESM3_PORT/health" >/dev/null 2>&1; then
    echo "ESM3 service already running on port $ESM3_PORT"
    exit 0
fi

CMD=(
    env CUDA_VISIBLE_DEVICES="$ESM3_GPU_ID"
    PYTHONPATH="$PEPTIDE_LM"
    "$ESM3_VENV_PYTHON" "$SERVICE" "$ESM3_PORT"
)

if [[ "${ESM3_DAEMON:-0}" == "1" ]]; then
    "${CMD[@]}" >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (pid $(cat "$PID_FILE"), GPU $ESM3_GPU_ID, port $ESM3_PORT, log $LOG_FILE)"
    echo "Health: curl http://localhost:$ESM3_PORT/health"
else
    exec "${CMD[@]}"
fi
