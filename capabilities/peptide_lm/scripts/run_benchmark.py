#!/usr/bin/env python
"""Head-to-head benchmark: PeptideLM closed loop vs V-Bio production GA.

Same target, same Boltz oracle settings, same candidate+oracle budget per arm.
Reported metric: the production composite (0.58*interface + 0.22*binder +
0.12*pair_ipTM + 0.08*developability) — directly comparable to V-Bio numbers.

Usage:
  PY scripts/run_benchmark.py --targets mdm2 keap1 --gpus 0 1 2 3 \
      --prior models/prior.pt --rounds 8
Output defaults to the unified run root (VBIO_RUNS_DIR, else
/data/vbio_runs), outside the repository — pass --out to override.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peplm.bench.ga_baseline import GABaseline
from peplm.bench.targets import load_target
from peplm.candidate import Candidate
from peplm.loop.config import LoopConfig
from peplm.loop.engine import PeptideLoop
from peplm.models.train import load_prior
from peplm.oracle.peptide_boltz import PeptideBoltzOracle
from peplm.paths import default_run_dir
from peplm.vocab import parse_tokens


def run_peptidelm(target, prior_path, run_dir, args):
    prior, vocab = load_prior(prior_path, device=args.device)
    oracle = PeptideBoltzOracle(target["receptor"], work_dir=run_dir,
                                gpus=tuple(args.gpus), log=print)
    L = len(target["seed_peptide"])
    leadopt = args.setting == "leadopt"
    cfg = LoopConfig(
        n_rounds=args.rounds, oracle_budget=args.budget,
        n_agent=args.pop - 8, n_edit=8, n_mut=8,
        len_range=(max(6, L - 4), min(30, L + 6)),
        ncaa_range=tuple(args.ncaa), device=args.device,
        gpus=tuple(args.gpus), seed=args.seed,
        rl_lr=args.rl_lr, anchor_seed=leadopt,
        use_surrogate=not args.no_surrogate)
    loop = PeptideLoop(cfg, target["receptor"], prior, vocab, oracle,
                       run_dir=run_dir)
    loop.reward_fn.seeds = [target["seed_peptide"]]
    # seed elite = the co-crystal peptide (round 1 edit/mut parent)
    from peplm.vocab import parse_tokens as _pt

    seed_cand = Candidate(
        tokens=_pt(("<cyc>" if cfg.cyclic else "<lin>") + target["seed_peptide"]),
        origin="seed")
    if leadopt:
        loop.anchor_elites.append(seed_cand)
    loop.elites.append(seed_cand)
    t0 = time.time()
    loop.run()
    comps = [c.metrics.get("composite") for c in loop.elites
             if c.metrics.get("composite") is not None]
    all_rows = []
    # collect every scored candidate across rounds from elites + best
    for c in loop.elites + (loop.best or []):
        if c.metrics.get("composite") is not None:
            all_rows.append(c)
    uniq = {c.key for c in all_rows}
    rows = sorted((c.metrics["composite"] for c in all_rows), reverse=True)
    return {
        "best_composite": rows[0] if rows else None,
        "top5_mean": sum(rows[:5]) / min(5, len(rows)) if rows else None,
        "unique_scored": len(uniq),
        "oracle_calls": oracle.n_calls,
        "wall_s": time.time() - t0,
        "rounds": loop.rounds_log,
    }


def run_ga(target, run_dir, args):
    oracle = PeptideBoltzOracle(target["receptor"], work_dir=run_dir,
                                gpus=tuple(args.gpus), log=print)
    L = len(target["seed_peptide"])
    ga = GABaseline(oracle, run_dir, peptide_length=L,
                    population=args.pop, elite_size=4,
                    generations=args.rounds,
                    ncaa_pool=list(args.ga_ncaa_pool) if args.ga_ncaa_pool else None,
                    ncaa_min=args.ncaa[0], ncaa_max=args.ncaa[1],
                    initial_sequence=(target["seed_peptide"]
                                      if args.setting == "leadopt" else None),
                    seed=args.seed)
    return ga.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", nargs="+", default=["mdm2"])
    ap.add_argument("--prior", default="models/prior.pt")
    ap.add_argument("--out", default=None,
                    help="output root; default: unified run root "
                         "(VBIO_RUNS_DIR, else /data/vbio_runs)")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--pop", type=int, default=16,
                    help="candidates per round, both arms")
    ap.add_argument("--budget", type=int, default=16)
    ap.add_argument("--ncaa", nargs=2, type=int, default=[0, 6])
    ap.add_argument("--ga_ncaa_pool", nargs="*", default=None,
                    help="NCAA CCD pool for the GA arm (production default: none)")
    ap.add_argument("--gpus", nargs="+", type=int, default=[0, 1, 2, 3])
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--no_surrogate", action="store_true")
    ap.add_argument("--arm", choices=["both", "lm", "ga"], default="both")
    ap.add_argument("--seed", type=int, default=0,
                    help="varies GA rng / loop sampling for repeat runs")
    ap.add_argument("--setting", choices=["denovo", "leadopt"], default="denovo",
                    help="denovo: no lead knowledge; leadopt: both arms start "
                         "from the co-crystal peptide (production "
                         "peptideInitialSequence behaviour)")
    ap.add_argument("--rl_lr", type=float, default=3e-5)
    args = ap.parse_args()

    out_root = Path(args.out) if args.out else default_run_dir("bench")
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "report.json"
    report = {}
    if report_path.exists():  # merge across separately-run arms
        try:
            report = json.loads(report_path.read_text())
        except Exception:
            report = {}
    for tname in args.targets:
        target = load_target(tname)
        print(f"===== target {tname} ({target['pdb']}) receptor "
              f"{len(target['receptor'])}aa seed {target['seed_peptide']} =====")
        entry = {}
        if args.arm in ("both", "lm"):
            entry["peptidelm"] = run_peptidelm(
                target, args.prior, out_root / f"{tname}_lm", args)
        if args.arm in ("both", "ga"):
            entry["ga"] = run_ga(target, out_root / f"{tname}_ga", args)
        report.setdefault(tname, {}).update(entry)
        print(f"[{tname}]", json.dumps(
            {k: {kk: vv for kk, vv in v.items() if kk != "rounds"}
             for k, v in entry.items()}, indent=1, default=str))
    (out_root / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print("\n===== summary (production composite) =====")
    for tname, entry in report.items():
        for arm, r in entry.items():
            print(f"{tname:8s} {arm:10s} best={r['best_composite']:.4f} "
                  f"top5={r['top5_mean']:.4f} unique={r['unique_scored']} "
                  f"oracle={r['oracle_calls']} wall={r['wall_s']:.0f}s"
                  if r.get("best_composite") is not None
                  else f"{tname:8s} {arm:10s} no scores")


if __name__ == "__main__":
    main()
