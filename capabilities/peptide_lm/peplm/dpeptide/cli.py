"""Single-candidate D-oracle CLI: mirror-space dock/score in ONE process.

Executed inside the engine runtime container (protenix or boltz image via
the platform's docker task skeleton) — this module owns ALL heavy state:
model loading, sampler patching, input prep, inference, and product flip.

Contract:
  python -m peplm.dpeptide.cli \
      --staged INPUT.pdb        # D-target + placed L-peptide complex
      --mode fixed              # fixed-receptor inpainting docking
      --pocket-box 6.0          # anchoring box radius (0 disables)
      --out-dir OUT             # outputs written here

Outputs:
  OUT/confidence.json           best-sample metrics (+ structure_dir)
Exit code non-zero on failure; no fallbacks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", required=True)
    ap.add_argument("--mode", choices=["fixed", "score"], default="fixed")
    ap.add_argument("--pocket_box", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out_dir", required=True)
    args = ap.parse_args(argv)

    peplm_root = Path(__file__).resolve().parents[3]
    for p in (str(peplm_root), "/data/Boltz2Score"):
        if p not in sys.path:
            sys.path.insert(0, p)

    from peplm.dpeptide.scoring import (
        dock_peptide,
        load_model_once,
        score_complex,
        set_fixed_receptor_config,
    )

    if args.mode == "fixed":
        set_fixed_receptor_config(
            enabled=True,
            peptide_init="input",
            pocket_box_radius=float(args.pocket_box),
        )
        model_module = load_model_once()
        conf = dock_peptide(
            Path(args.staged), Path(args.out_dir), model_module=model_module,
            seed=args.seed, pocket_box=float(args.pocket_box),
        )
    else:
        # Route-one validation: score mode — the confidence head scores the
        # staged coordinates as-is (0.000 A pass-through, no re-diffusion).
        conf = score_complex(Path(args.staged), Path(args.out_dir), seed=args.seed)

    out_dir = Path(args.out_dir)
    (out_dir / "confidence.json").write_text(json.dumps(conf, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
