#!/bin/bash
# CD73 bicyclic rerun: Protenix backend, user layout (Cys at position 1,
# interior anchor, terminal), ADAPTIVE length 12-25, NCAAs allowed 0-3.
# 12 rounds x 16 candidates = 192 oracle calls (parity with prior runs).
set -u
cd /data/V-Bio/capabilities/peptide_lm
PY=/data/Boltz2Score/.venv/bin/python

$PY -u scripts/run_closed_loop.py \
  --protein AWELTILHTNDVHSRLEQTSEDSSKCVNASRCMGGVARLFTKVQQIRRAEPNVLLLDAGDQYQGTIWFTVYKGAEVAHFMNALRYDAMALGNHEFDNGVEGLIEPLLKEAKFPILSANIKAKGPLASQISGLYLPYKVLPVGDEVVGIVG \
  --prior runs/prior_v2/prior.pt \
  --run_dir runs/cd73_protenix \
  --rounds 12 --budget 16 --peptide_len 12 25 --ncaa 0 3 \
  --design_mode bicyclic --cys_positions 7 --linker SEZ \
  --bicyclic_layout first_last \
  --backend protenix \
  --gpus 1 2 3 --device cuda:1 --seed 0

echo "CD73 PROTENIX DONE"
$PY - <<'EOF'
import json
from pathlib import Path
rows = [json.loads(l) for l in Path('runs/cd73_protenix/scored.jsonl').read_text().splitlines()]
rows = [r for r in rows if r.get('composite') is not None]
rows.sort(key=lambda r: -r['composite'])
print(f"total scored: {len(rows)}")
print("TOP 10 (production composite):")
for r in rows[:10]:
    print(f"  {r['composite']:.4f} len={len(r['seq'])} {r['seq']} "
          f"iptm={r.get('iptm')} pair={r.get('pair_iptm')} "
          f"ipsae={r.get('ipsae_dom')} plddt={r.get('binder_avg_plddt')}")
lens = [len(r['seq']) for r in rows]
import collections
print("length distribution:", dict(sorted(collections.Counter(lens).items())))
EOF
