#!/bin/bash
# Chain the lead-optimization benchmark arms after the denovo run finishes.
set -u
cd /data/V-Bio/capabilities/peptide_lm
PY=/data/Boltz2Score/.venv/bin/python
NCAA_POOL="AIB NLE NVA ORN CIT HSE HCY MSE SEC HYP PCA SEP TPO PTR CSO MLY DAL"

echo "[chain] waiting for denovo LM benchmark to finish ..."
while pgrep -f "run_benchmark.py --targets mdm2 keap1 bclxl --arm lm" > /dev/null; do
  sleep 60
done

echo "[chain] GA leadopt"
$PY -u scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm ga \
  --setting leadopt --gpus 1 2 3 --rounds 8 --pop 16 --ncaa 1 3 --seed 0 \
  --ga_ncaa_pool $NCAA_POOL --out runs/bench_leadopt

echo "[chain] LM leadopt"
$PY -u scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm lm \
  --setting leadopt --gpus 1 2 3 --rounds 8 --pop 16 --budget 16 --ncaa 1 3 \
  --rl_lr 1e-4 --prior runs/prior/prior.pt --out runs/bench_leadopt --device cuda:0

echo "[chain] all done"
