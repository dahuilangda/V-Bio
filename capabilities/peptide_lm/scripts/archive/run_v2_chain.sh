#!/bin/bash
# Tier1-v2 -> Tier2-v2 pipeline: corpus (FIM + tags + binders) -> retrain ->
# sanity -> lead-opt + denovo benchmarks -> summary.
set -u
cd /data/V-Bio/capabilities/peptide_lm
PY=/data/Boltz2Score/.venv/bin/python

echo "[v2-chain] waiting for PDB binder mining ..."
while [ ! -f runs/data_pdb/binder_peptides.txt ]; do sleep 30; done
sleep 5
echo "[v2-chain] binders: $(wc -l < runs/data_pdb/binder_peptides.txt) peptides"

echo "[v2-chain] building corpus v2"
$PY -m peplm.data.build_corpus_v2 --n_segments 12000000 --out_dir runs/data_v2 \
  | tail -4

echo "[v2-chain] training tier1 v2"
$PY -u scripts/train_tier1.py --data runs/data_v2 --out runs/prior_v2 \
  --device cuda:0 --epochs 2 | grep -v transformers

echo "[v2-chain] sanity sampling check"
$PY - <<'EOF'
import sys
sys.path.insert(0, '.')
import numpy as np
from peplm.models.train import load_prior

model, vocab = load_prior('runs/prior_v2/prior.pt', device='cuda:0')
outs = model.sample_with_prompt(
    ["<sol_h>", "<syn_h>", "<liab_h>", "<L15>", "<lin>"], 50, 'cuda:0',
    temperature=1.0, return_tokens=True,
    ban_tokens=["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>", "<syn_m>",
                "<syn_l>", "<liab_h>", "<liab_m>", "<liab_l>", "<cont>",
                "<mask>", "<pre>", "<suf>", "<mid>", "<lin>", "<cyc>"]
    + [f"<L{5*k}>" for k in range(1, 10)])
lens = [len([t for t in o if not t.startswith('<')]) for o in outs]
print(f"sanity: n={len(lens)} len p50={np.percentile(lens,50):.0f} max={max(lens)}")
assert np.percentile(lens, 90) < 70, "sampling does not terminate"
print("[v2-chain] sampling OK")
EOF
if [ $? -ne 0 ]; then echo "[v2-chain] SANITY FAILED"; exit 1; fi

echo "[v2-chain] lead-opt benchmark (LM v2)"
$PY -u scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm lm \
  --setting leadopt --gpus 1 2 3 --rounds 8 --pop 16 --budget 16 --ncaa 1 3 \
  --rl_lr 1e-4 --prior runs/prior_v2/prior.pt --out runs/bench_leadopt_v2 \
  --device cuda:0 2>&1 | grep -E "target|peptidelm|best|r [0-9]"

echo "[v2-chain] denovo benchmark (LM v2)"
$PY -u scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm lm \
  --gpus 1 2 3 --rounds 8 --pop 16 --budget 16 --ncaa 1 3 \
  --rl_lr 1e-4 --prior runs/prior_v2/prior.pt --out runs/bench_v2 \
  --device cuda:0 2>&1 | grep -E "target|peptidelm|best|r [0-9]"

# merge GA results into the v2 reports for side-by-side tables
$PY - <<'EOF'
import json
from pathlib import Path
for root, src in (("runs/bench_leadopt_v2", "runs/bench_leadopt"),
                  ("runs/bench_v2", "runs/bench")):
    p = Path(root) / "report.json"
    report = json.loads(p.read_text()) if p.exists() else {}
    for t in ("mdm2", "keap1", "bclxl"):
        g = Path(src) / f"{t}_ga" / "ga_results.json"
        if g.exists():
            report.setdefault(t, {})["ga"] = json.loads(g.read_text())["summary"]
    p.write_text(json.dumps(report, indent=1, default=str))
print("[v2-chain] reports merged")
EOF

echo "[v2-chain] summary"
$PY scripts/summarize_bench.py runs/bench/report.json runs/bench_leadopt/report.json \
  runs/bench_v2/report.json runs/bench_leadopt_v2/report.json || true
echo "[v2-chain] ALL DONE"
