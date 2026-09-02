#!/usr/bin/env python
"""Tier-2 closed-loop optimization against a target.

Usage (MDM2, real Boltz oracle):
  PY scripts/run_closed_loop.py --target mdm2 --gpus 0 1 2 3 \
      --prior models/prior.pt --rounds 8
--run_dir defaults to a timestamped directory under the unified run root
(VBIO_RUNS_DIR, else /data/vbio_runs), outside the repository.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peplm.bench.targets import load_target
from peplm.candidate import Candidate
from peplm.loop.config import LoopConfig
from peplm.loop.engine import PeptideLoop
from peplm.models.train import load_prior
from peplm.oracle.peptide_boltz import MockPeptideOracle, PeptideBoltzOracle
from peplm.paths import default_run_dir
from peplm.vocab import parse_tokens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="mdm2",
                    help="bench target key (mdm2|keap1|bclxl)")
    ap.add_argument("--protein", default=None,
                    help="custom target sequence (overrides --target)")
    ap.add_argument("--prior", default="models/prior.pt")
    ap.add_argument("--run_dir", default=None,
                    help="run directory; default: <unified run root>/peptide_lm/"
                         "closed_loop/<target>_<timestamp>")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--gpus", nargs="+", type=int, default=[0, 1, 2, 3])
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--peptide_len", nargs="+", type=int, default=[8, 25],
                    help="1 value = fixed length; 2 values = adaptive range")
    ap.add_argument("--ncaa", nargs=2, type=int, default=[0, 0],
                    help="NCAA count window; requires --ncaa_pool")
    ap.add_argument("--ncaa_pool", nargs="*", default=[],
                    help="user-allowed NCAA CCD codes (e.g. AIB CIT NLE); "
                         "empty = pure natural design")
    ap.add_argument("--cyclic", action="store_true")
    ap.add_argument("--best_metric", choices=["composite", "ipSAE"],
                    default="composite",
                    help="primary ranking metric: production composite or "
                         "pure ipSAE interface evidence")
    ap.add_argument("--fixed_residue", action="append", default=[],
                    help="'POS:RESIDUE' e.g. '5:F' or '9:[AIB]' (repeatable; "
                         "production peptideSequenceMask letters semantics)")
    ap.add_argument("--seed_peptide", default=None)
    ap.add_argument("--budget", type=int, default=16)
    ap.add_argument("--design_mode", choices=["linear", "cyclic", "bicyclic"],
                    default="linear")
    ap.add_argument("--cys_positions", nargs="+", type=int, default=[7],
                    help="interior Cys anchor(s) for bicyclic")
    ap.add_argument("--linker", default="SEZ", choices=["SEZ", "29N"])
    ap.add_argument("--bicyclic_layout",
                    choices=["first_last", "interior_terminal"],
                    default="first_last")
    ap.add_argument("--backend", choices=["boltz", "protenix"], default="boltz")
    ap.add_argument("--user_residue", default=None,
                    help="JSON file: [{ccd, smiles, base, placement}, ...] — "
                         "arbitrary user amino acids beyond the presets")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--consistency_backend", choices=["none", "boltz", "protenix"],
                    default="none",
                    help="cross-backend self-consistency (upgrade 1): re-fold "
                         "top-k with an independent predictor")
    ap.add_argument("--consistency_topk", type=int, default=8)
    ap.add_argument("--ncaa_decode_bias", type=float, default=0.5,
                    help="decode-time soft bias toward the user NCAA pool")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    if not args.run_dir:
        run_name = "custom" if args.protein else args.target
        stamp = time.strftime("%Y%m%d-%H%M%S")
        args.run_dir = str(default_run_dir("closed_loop") / f"{run_name}_{stamp}")
        print(f"[run_dir] defaulting to {args.run_dir}", file=sys.stderr)

    user_residues = []
    if args.user_residue:
        user_residues = json.loads(Path(args.user_residue).read_text())

    # length: one value = fixed, two values = adaptive range
    if len(args.peptide_len) == 1:
        n = args.peptide_len[0]
        len_range = (n, n)
    else:
        len_range = tuple(sorted(args.peptide_len))

    # fixed-position residues: "POS:RESIDUE" (1-based); residue may be a
    # single letter or a [CCD] token (preset or user-registered)
    fixed_residues = []
    for spec in args.fixed_residue:
        pos_s, _, res = spec.partition(":")
        pos = int(pos_s.strip())
        res = res.strip().upper()
        if pos < 1:
            raise ValueError(f"fixed residue position must be >= 1: {spec}")
        if len(res) == 1 and res in "ACDEFGHIKLMNPQRSTVWY":
            fixed_residues.append({"position": pos, "residue": res})
        elif res.startswith("[") and res.endswith("]") and not res[1:-1].isdigit():
            fixed_residues.append({"position": pos, "residue": res})
        else:
            raise ValueError(f"unparsable fixed residue spec: {spec}")

    if args.protein:
        target = {"name": "custom", "receptor": args.protein.upper(),
                  "seed_peptide": args.seed_peptide}
    else:
        target = load_target(args.target)

    prior, vocab = load_prior(args.prior, device=args.device)
    bicyclic = ({"cys_positions": list(args.cys_positions),
                 "linker_ccd": args.linker}
                if args.design_mode == "bicyclic" else None)
    cfg = LoopConfig(
        n_rounds=args.rounds, oracle_budget=args.budget,
        len_range=len_range, ncaa_range=tuple(args.ncaa),
        ncaa_pool=tuple(args.ncaa_pool),
        cyclic=args.cyclic, gpus=tuple(args.gpus), device=args.device,
        design_mode=args.design_mode, cys_positions=tuple(args.cys_positions),
        linker_ccd=args.linker, bicyclic_layout=args.bicyclic_layout,
        user_residues=tuple(user_residues),
        fixed_residues=tuple(fixed_residues),
        best_metric=args.best_metric,
        ncaa_decode_bias=args.ncaa_decode_bias,
        consistency_topk=args.consistency_topk,
        seed=args.seed)
    if args.mock:
        oracle = MockPeptideOracle()
    elif args.backend == "protenix":
        from peplm.oracle.protenix import ProtenixOracle

        oracle = ProtenixOracle(target["receptor"], work_dir=args.run_dir,
                                gpus=tuple(args.gpus), bicyclic=bicyclic,
                                seed=args.seed, log=print)
    else:
        oracle = PeptideBoltzOracle(
            target["receptor"], work_dir=args.run_dir, gpus=tuple(args.gpus),
            bicyclic=bicyclic, extra_molecules=user_residues, log=print)
    oracle = _with_consistency_guard(oracle, args, target, bicyclic)
    loop = PeptideLoop(cfg, target["receptor"], prior, vocab, oracle,
                       run_dir=args.run_dir)
    if args.seed_peptide:
        seed = Candidate(tokens=parse_tokens(
            ("<cyc>" if args.cyclic else "<lin>") + args.seed_peptide.upper()),
            origin="seed")
        loop.elites.append(seed)
        loop.reward_fn.seeds = [args.seed_peptide.upper()]
    elif target.get("seed_peptide"):
        loop.reward_fn.seeds = [target["seed_peptide"]]
    res = loop.run()
    summary = {
        "target": target["name"],
        "best": max((r for r in loop.rounds_log if r["best_composite"] is not None),
                    key=lambda r: r["best_composite"], default=None),
        "oracle_calls": oracle.n_calls,
        "wall_s": res["total_s"],
    }
    (Path(args.run_dir) / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))




def _with_consistency_guard(oracle, args, target, bicyclic):
    """Wrap the primary oracle so the top-k get an independent second fold
    (boltz <-> protenix); none = plain oracle."""
    if args.consistency_backend == "none" or args.mock:
        return oracle
    from peplm.loop.consistency_guard import ConsistencyGuard
    from peplm.oracle.peptide_boltz import PeptideBoltzOracle
    from peplm.oracle.protenix import ProtenixOracle

    if args.consistency_backend == "protenix":
        secondary = ProtenixOracle(target["receptor"], work_dir=args.run_dir,
                                   gpus=tuple(args.gpus), bicyclic=bicyclic,
                                   seed=args.seed, log=print)
    else:
        secondary = PeptideBoltzOracle(
            target["receptor"], work_dir=args.run_dir, gpus=tuple(args.gpus),
            bicyclic=bicyclic, extra_molecules=[],
            log=print)
    return ConsistencyGuard(oracle, secondary, topk=args.consistency_topk)


if __name__ == "__main__":
    main()
