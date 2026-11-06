#!/usr/bin/env bash
# RFD4-Proteina 部署烟测：四个实测用例（binder / 短肽 / NCAA motif / SEZ 双环）
# 逐个生成并断言结构产物存在。GPU 单卡规范：一次只跑一个用例。
#
# 用法: GPU=0 bash scripts/run_smoke.sh
# 前提: 镜像已构建（rfd4-proteina-engine:cu13）。
#       HF 权重缓存挂载在 /workspace/hf_cache，首次运行会拉取约 1.7GB，
#       之后再跑不再下载。
set -euo pipefail

GPU="${GPU:-0}"
IMAGE="${IMAGE:-rfd4-proteina-engine:cu13}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail=0
run_case() {
  local name="$1" spec="$2" host_out="$3"
  echo "[$(date +%H:%M:%S)] $name 开始"
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
    echo "$name 无结构产物" >&2
    fail=1
  else
    echo "$name 结构产物 $cif_count 个"
  fi
  echo "[$(date +%H:%M:%S)] $name 完成"
}

run_case pdl1_binder  examples/pdl1_binder.yaml          runs/smoke/pdl1
run_case shortpep     examples/shortpep.yaml             runs/smoke/shortpep
run_case ncaa_motif   examples/ncaa_phosphopeptide.yaml  runs/smoke/ncaa
run_case sez_bicyclic examples/sez_bicyclic.yaml         runs/smoke/sez

if [ "$fail" -ne 0 ]; then
  echo "烟测失败" >&2
  exit 1
fi
echo "烟测全部通过"
