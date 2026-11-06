#!/usr/bin/env bash
# RFD4-Proteina deployment smoke: four validated cases (binder / short
# peptide / NCAA motif / SEZ bicyclic), generated sequentially with
# structure-count assertions. GPU contract: one case at a time.
#
# Usage: GPU=0 bash scripts/run_smoke.sh
# Requires the wrapper image (rfd4-proteina-engine:cu13). The entry point
# is bound to /app/rfd4_generate.py; the upstream image rfd4-proteina:cu13
# has no /app and cannot be swapped in via IMAGE=.
# HF weights cache is mounted at /workspace/hf_cache; the first run pulls
# ~1.7 GB, later runs reuse it.
set -euo pipefail

GPU="${GPU:-0}"
IMAGE="${IMAGE:-rfd4-proteina-engine:cu13}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail=0
run_case() {
  local name="$1" spec="$2" host_out="$3"
  rm -rf "/data/rfd4-proteina/$host_out"  # stale artifacts would satisfy the count
  echo "[$(date +%H:%M:%S)] $name start"
  docker run --rm --runtime nvidia --gpus "device=$GPU" \
    -e HF_HOME=/workspace/hf_cache \
    -e HF_ENDPOINT=https://hf-mirror.com \
    -e HTTP_PROXY="${RFD4_HTTP_PROXY:-}" -e HTTPS_PROXY="${RFD4_HTTP_PROXY:-}" \
    -e NO_PROXY=localhost,127.0.0.1 \
    -v "$ROOT/examples:/workspace/examples:ro" \
    -v /data/rfd4-proteina/hf_cache:/workspace/hf_cache \
    -v /data/rfd4-proteina/runs:/workspace/runs \
    -w /workspace \
    --entrypoint python3 \
    "$IMAGE" /app/rfd4_generate.py \
      --spec "$spec" --out-dir "$host_out" \
      --num-designs 1 --seed 101 --timeout-min 60
  local cif_count
  cif_count=$(find "/data/rfd4-proteina/$host_out" -name "*.cif" 2>/dev/null | wc -l)
  if [ "$cif_count" -eq 0 ]; then
    echo "$name produced no structures" >&2
    fail=1
  else
    echo "$name structures: $cif_count"
  fi
  echo "[$(date +%H:%M:%S)] $name done"
}

run_case pdl1_binder  examples/pdl1_binder.yaml          runs/smoke/pdl1
run_case shortpep     examples/shortpep.yaml             runs/smoke/shortpep
run_case ncaa_motif   examples/ncaa_phosphopeptide.yaml  runs/smoke/ncaa
run_case sez_bicyclic examples/sez_bicyclic.yaml         runs/smoke/sez

if [ "$fail" -ne 0 ]; then
  echo "smoke failed" >&2
  exit 1
fi
echo "smoke passed"
