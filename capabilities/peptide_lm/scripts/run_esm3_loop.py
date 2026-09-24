#!/usr/bin/env python
"""ESM3 RL binder-design loop against the Protenix oracle.

Usage (staging template from a production run, 4 dock GPUs):
  PY scripts/run_esm3_loop.py --template /path/to/staged_complex.pdb \
      --pocket A:162,A:163,A:170 --peptide_len 14 \
      --gpus 0 1 2 3 --rounds 8

The template supplies the receptor, its numbering (the pocket selection
uses the STAGED per-chain 1..N numbering production writes -- raw crystal
numbering silently guides the wrong residues) and the peptide chain
slots; the loop rewrites only the peptide identity per candidate. An
optional masked-SFT warm-up grounds the receptor-conditioned distribution
before RL (the open ESM3 checkpoint is per-protein pretrained; binder
infilling is otherwise out-of-distribution -- docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peplm.residues import ONE_OF as THREE2ONE

# Modified residues protenix2dock maps to standard letters (core/structure.py
# _TO_STANDARD): the conditioned receptor sequence must keep them or every
# residue after the modification shifts.
MODIFIED_TO_ONE = {
    "MSE": "M", "SEC": "U", "CSO": "C", "CSD": "C", "CME": "C", "OCS": "C",
    "HYP": "P", "MLY": "K", "FME": "M",
}


def receptor_sequence_from_template(template: Path) -> str:
    """Longest polymer chain as one-letter sequence, modified residues
    mapped the same way protenix2dock maps them."""
    import gemmi
    st = gemmi.read_structure(str(template))
    chains = [c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3]
    chains.sort(key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    lookup = {**THREE2ONE, **MODIFIED_TO_ONE}
    seq = ""
    for res in chains[0]:
        if res.het_flag == "H" and res.name.upper() not in MODIFIED_TO_ONE:
            continue
        aa = lookup.get(res.name.upper())
        if aa:
            seq += aa
    if not seq:
        raise ValueError(f"no standard residues on receptor chain of {template}")
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True,
                    help="production staging complex pdb (receptor + peptide chain)")
    ap.add_argument("--pocket", required=True,
                    help="pocket as comma-separated chain:resnum tokens on the "
                         "receptor, e.g. A:162,A:163,A:170 (staged per-chain "
                         "1..N numbering; range syntax is rejected)")
    ap.add_argument("--peptide_len", type=int, default=14)
    ap.add_argument("--cyclic", action="store_true", default=True)
    ap.add_argument("--linear", dest="cyclic", action="store_false")
    ap.add_argument("--peptide_chain", default="B")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--gpus", nargs="+", type=int, default=[0, 1, 2, 3])
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--oracle_batch", type=int, default=12)
    ap.add_argument("--oversample", type=int, default=48)
    ap.add_argument("--refills_per_parent", type=int, default=3)
    ap.add_argument("--seed", type=int, default=101)
    ap.add_argument("--min_entropy", type=float, default=2.2,
                    help="sequence diversity floor for oracle eligibility")
    ap.add_argument("--sft_epochs", type=int, default=0,
                    help="masked-SFT warm-up epochs before RL (0 = skip)")
    ap.add_argument("--adapter_in", default=None,
                    help="resume from a saved adapter directory (LoRA shape "
                         "must match --lora_r/--lora_alpha)")
    ap.add_argument("--lora_r", type=int, default=32)
    ap.add_argument("--lora_alpha", type=int, default=16)
    ap.add_argument("--dock_timeout", type=int, default=2700,
                    help="per-dock oracle timeout (shared GPUs need ~30 min)")
    ap.add_argument("--run_dir", default=None)
    args = ap.parse_args()

    from peplm.paths import default_run_dir
    run_root = Path(args.run_dir) if args.run_dir else default_run_dir(
        "esm3_loop")
    run_root.mkdir(parents=True, exist_ok=True)

    from peplm.loop.esm3_loop import ESM3Loop, LoopBudget
    from peplm.models.esm3_policy import ESM3Policy
    from peplm.models.esm3_sft import load_pairs, sft_warmup
    from peplm.oracle.esm3_oracle import OracleConfig

    rec_seq = receptor_sequence_from_template(Path(args.template))
    print(f"[run] receptor {len(rec_seq)} aa from {args.template}")

    policy = ESM3Policy(device=args.device, lora_r=args.lora_r,
                         lora_alpha=args.lora_alpha)
    if args.adapter_in:
        policy.load_adapter(Path(args.adapter_in))
        print(f"[run] resumed adapter from {args.adapter_in}")
    if args.sft_epochs > 0:
        pairs = load_pairs()
        print(f"[run] SFT warm-up: {len(pairs)} pairs, {args.sft_epochs} epochs")
        stats = sft_warmup(policy, pairs, epochs=args.sft_epochs)
        print(f"[run] SFT done: {stats}")

    import peplm.score.peptide_reward as _pr
    _orig_elig = _pr.sequence_is_eligible
    _pr.sequence_is_eligible = lambda s, **kw: _orig_elig(s, min_entropy=args.min_entropy)
    loop = ESM3Loop(
        policy,
        OracleConfig(template=Path(args.template),
                     pocket=args.pocket,
                     peptide_chain=args.peptide_chain,
                     cyclic=args.cyclic,
                     timeout_s=args.dock_timeout),
        rec_seq,
        run_root,
        pep_len=args.peptide_len,
        budget=LoopBudget(oracle_batch=args.oracle_batch,
                          oversample=args.oversample,
                          refills_per_parent=args.refills_per_parent),
        gpus=tuple(args.gpus),
        seed=args.seed,
    )
    for r in range(1, args.rounds + 1):
        summary = loop.round(r)
        if summary.get("skipped"):
            break
    print(f"[run] history -> {run_root / 'history.jsonl'}")


if __name__ == "__main__":
    main()
