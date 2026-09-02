#!/bin/bash
# Wait for the epoch-0 prior checkpoint, sanity-check sampling, then launch
# the PeptideLM benchmark arm on GPUs 1-3.
set -u
cd /data/V-Bio/capabilities/peptide_lm
PY=/data/Boltz2Score/.venv/bin/python

echo "[watch] waiting for runs/prior/prior.pt ..."
while [ ! -f runs/prior/prior.pt ]; do sleep 30; done
sleep 10  # let the save complete
cp runs/prior/prior.pt runs/prior/prior_ep0.pt
echo "[watch] epoch-0 prior snapshotted"

$PY - <<'EOF'
import sys
sys.path.insert(0, '.')
import numpy as np
from peplm.models.train import load_prior
from peplm.models.gpt2 import PlacementMask

model, vocab = load_prior('runs/prior/prior_ep0.pt', device='cuda:0')
pm = PlacementMask(vocab, max_len=96, ncaa_max=3)
outs = model.sample_with_prompt(['<dev_hi>', '<lin>'], 100, 'cuda:0',
                                temperature=1.0, placement=pm, target_len=15,
                                return_tokens=True,
                                ban_tokens=['<dev_hi>','<dev_md>','<dev_lo>','<cont>','<mask>'])
lens, ncaas = [], []
for toks in outs:
    res = [t for t in toks if not t.startswith('<')]
    if res:
        lens.append(len(res))
        ncaas.append(sum(1 for t in res if t.startswith('[')))
print(f"[watch] sanity: n={len(lens)} len p10/50/90={np.percentile(lens,[10,50,90])} "
      f"ncaa_frac={np.mean([n>0 for n in ncaas]):.2f}")
assert np.percentile(lens, 90) < 60, "sampling still never stops!"
print("[watch] sampling terminates correctly")
EOF
if [ $? -ne 0 ]; then echo "[watch] SANITY FAILED, not launching"; exit 1; fi

echo "[watch] launching LM benchmark arm"
$PY -u scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm lm \
    --gpus 1 2 3 --rounds 8 --pop 16 --budget 16 --ncaa 1 3 \
    --prior runs/prior/prior_ep0.pt --out runs/bench --device cuda:0
echo "[watch] LM benchmark done"
