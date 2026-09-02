#!/usr/bin/env bash
# Speed benchmark: full prediction vs boltz2score modes vs protenix2dock modes.
#
# Target: CDK2 1H1Q chain A (297 aa) + 5-bromo pyrazolopyrimidine (25 heavy atoms),
# single RTX 4090.  Times wall clock per mode (sequential, GPU 0).
#
# Usage:  bash benchmarks/bench_modes.sh  [<cdk2_chainA.pdb> <ligands.sdf> <out_dir>]
# Results appended to <out_dir>/BENCH_RESULTS.txt.  The p2d rows use the
# protenix2dock runtime image directly (docker), the boltz-predict row uses the
# installed boltz CLI with the MSA server.
#
# Measured (2026-08-24, venv boltz 2.2.1 / vbio-protenix-v2-runtime:2.0.0):
#   full(200x5,R3,bf16) 63.1s | b2s score 53.9s | pose 92.2* | refine 59.0
#   interface 59.0 | dock(160/200/16) 123.9s | p2d score 360.4(冷)/37.9(热)
#   p2d dock 129.4(热 44.3) | b2s 批量5配体单进程 79.3s (15.9s/配体)
#   (*pose 为模块缓存冷写噪声；模型加载缓存后：b2s 启动~12s、boltz 完整预测 31s→1s、
#    p2d 构造 83s→3s —— 见 docs/deployment/model-services.md)
set -u
B2S_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROT="${1:-/data/Boltz2Score/data/cdk2/1h1q-chainA-prepared.pdb}"
LIG="${2:-/data/Boltz2Score/data/cdk2/ligands.sdf}"
OUT="${3:-/tmp/bench_modes}"
SMILES="Brc1cccc(Nc2nc(OCC3CCCCC3)c3nc[nH]c3n2)c1"
PY="${BOLTZ2_VENV_PYTHON:-/data/Boltz2Score/.venv/bin/python}"
MSA="${MSA_SERVER_URL:-http://172.17.3.200:8080}"
mkdir -p "$OUT"

run_timed() {
  local name="$1"; shift
  local t0 t1 wall
  t0=$(date +%s.%N); echo "=== BENCH START $name $(date -u +%H:%M:%S)"
  "$@" > "$OUT/$name.log" 2>&1; local rc=$?
  t1=$(date +%s.%N); wall=$(python3 -c "print(f'{float('$t1') - float('$t0'):.1f}')")
  echo "=== BENCH DONE $name rc=$rc wall=${wall}s $(date -u +%H:%M:%S)" | tee -a "$OUT/BENCH_RESULTS.txt"
}

# full prediction baseline (boltz predict CLI; needs BOLTZ_CACHE with mols.tar + boltz2_conf.ckpt)
# full prediction baseline (boltz predict CLI; needs BOLTZ_CACHE with mols.tar + boltz2_conf.ckpt)
FULL_SEQ="$(grep -v '^>' /tmp/cdk2_chainA.fasta 2>/dev/null | tr -d '\n')"
if [ -n "${FULL_SEQ:-}" ] && [ ! -f "$OUT/cdk2_complex.yaml" ]; then
  printf 'version: 1\nsequences:\n  - protein:\n      id: A\n      sequence: "%s"\n      count: 1\n  - ligand:\n      id: L\n      smiles: "%s"\n      count: 1\n' \
    "$FULL_SEQ" "$SMILES" > "$OUT/cdk2_complex.yaml"
fi
run_timed full \
  env CUDA_VISIBLE_DEVICES=0 "$(dirname "$PY")/boltz" predict "$OUT/cdk2_complex.yaml" \
  --out_dir "$OUT/full" --use_msa_server --msa_server_url "$MSA" \
  --recycling_steps 3 --sampling_steps 200 --diffusion_samples 5 \
  --accelerator gpu --devices 1 --output_format mmcif

# boltz2score five modes
for mode in score pose refine interface; do
  run_timed "b2s_$mode" \
    env CUDA_VISIBLE_DEVICES=0 "$PY" "$B2S_REPO/boltz2score.py" --mode "$mode" \
    --protein_file "$PROT" --ligand_file "$LIG" --ligand_indices 1 \
    --output_dir "$OUT/b2s_$mode" --compute_ipsae --enable_affinity \
    --target_chain A --ligand_chain L --seed 42
done

run_timed b2s_dock \
  env CUDA_VISIBLE_DEVICES=0 "$PY" "$B2S_REPO/boltz2score.py" --mode dock \
  --protein_file "$PROT" --ligand_smiles "$SMILES" \
  --center_x 0.89 --center_y 27.45 --center_z 8.07 \
  --output_dir "$OUT/b2s_dock" --compute_ipsae --enable_affinity \
  --target_chain A --ligand_chain L --seed 42

# protenix2dock (docker; P2D_IMAGE env, model/cache mounts per worker config)
P2D_IMAGE="${P2D_IMAGE:-vbio-protenix-v2-runtime:2.0.0}"
P2D_MODEL_DIR="${P2D_MODEL_DIR:-/data/protenix/model}"
P2D_CACHE_DIR="${P2D_CACHE_DIR:-/data/protenix/common_cache}"
mkdir -p /tmp/p2d_bench && cp "$PROT" /tmp/p2d_bench/protein.pdb
"$PY" -c "
from rdkit import Chem
m = Chem.SDMolSupplier('$LIG', removeHs=True)[0]
w = Chem.SDWriter('/tmp/p2d_bench/ligand.sdf'); w.write(m); w.close()"
p2d_run() {
  local mode="$1" out="$2"
  docker run --rm --entrypoint= --gpus device=0 --shm-size 16g \
    --volume "$B2S_REPO/..:/workspace/vbio:ro" \
    --volume /tmp/p2d_bench:/tmp/p2d_bench \
    --volume "$P2D_MODEL_DIR:/workspace/model:ro" \
    --volume "$P2D_CACHE_DIR:/cache/common:ro" \
    --volume /dev/shm:/dev/shm \
    --env PYTHONPATH=/workspace/vbio/vendor/protenix-source \
    --env PROTENIX_ROOT_DIR=/cache \
    --env PROTENIX_MODULE_CACHE_DIR=/cache/module_cache \
    "$P2D_IMAGE" /usr/local/micromamba/envs/protenix/bin/python \
    /workspace/vbio/capabilities/protenix2dock/protenix2dock.py \
    --mode "$mode" --protein_file /tmp/p2d_bench/protein.pdb \
    $(if [ "$mode" = "dock" ]; then
        echo --ligand_smiles "$SMILES" --center_x 0.89 --center_y 27.45 --center_z 8.07 --size_x 20 --size_y 20 --size_z 20
      else
        echo --ligand_file /tmp/p2d_bench/ligand.sdf
      fi) \
    --output_dir /tmp/p2d_bench/$out --work_dir /tmp/p2d_bench/work_$out \
    --msa_server_url "$MSA" --seed 42 --checkpoint_dir /workspace/model
}
run_timed p2d_score p2d_run score out_score
run_timed p2d_dock p2d_run dock out_dock
echo "ALL BENCH DONE"
