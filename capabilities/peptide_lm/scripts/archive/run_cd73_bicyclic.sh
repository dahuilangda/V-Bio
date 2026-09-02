#!/bin/bash
# CD73 bicyclic-peptide discovery with PeptideLM v2 (the real-world
# validation: the production GA topped out at 0.797 on this project).
# Parity with the old run: length 17, Cys anchors 3/8/terminal, SEZ linker,
# 12 rounds x 16 candidates = 192 oracle calls, production composite metric.
set -u
cd /data/V-Bio/capabilities/peptide_lm
PY=/data/Boltz2Score/.venv/bin/python

$PY -u scripts/run_closed_loop.py \
  --protein AWELTILHTNDVHSRLEQTSEDSSKCVNASRCMGGVARLFTKVQQIRRAEPNVLLLDAGDQYQGTIWFTVYKGAEVAHFMNALRYDAMALGNHEFDNGVEGLIEPLLKEAKFPILSANIKAKGPLASQISGLYLPYKVLPVGDEVVGIVG \
  --prior runs/prior_v2/prior.pt \
  --run_dir runs/cd73_bicyclic \
  --rounds 12 --budget 16 --peptide_len 17 17 --ncaa 0 3 \
  --design_mode bicyclic --cys_positions 2 7 --linker SEZ \
  --gpus 0 1 2 3 --device cuda:0

echo "CD73 DONE"
$PY - <<'EOF'
import json
from pathlib import Path
rows = [json.loads(l) for l in Path('runs/cd73_bicyclic/scored.jsonl').read_text().splitlines()]
rows = [r for r in rows if r.get('composite') is not None]
rows.sort(key=lambda r: -r['composite'])
print(f"total scored: {len(rows)}")
print("TOP 10 (production composite):")
for r in rows[:10]:
    print(f"  {r['composite']:.4f} {r['seq']} iptm={r.get('iptm')} "
          f"pair={r.get('pair_iptm')} ipsae={r.get('ipsae_dom')} "
          f"plddt={r.get('binder_avg_plddt')}")
print(f"\nGA baseline (production, same budget): best 0.797")
EOF
