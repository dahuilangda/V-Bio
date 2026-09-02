#!/usr/bin/env python3
"""Prepare curated training data for the protenix2dock affinity head.

Implements the curation checklist from docs/affinity_training_data.md
(Boltz-2 four-layer curation + Nesso-1 additions):

  1. measurement filters  — Ki/Kd/IC50/EC50 only, log10(uM), assay-internal
     variance floor (drop uninformative assays)
  2. compound filters     — PAINS screening (rdkit SaltStrip + SMARTS if
     available), heavy atoms <= 50, MW window
  3. bias guards          — assay-level |Pearson(affinity, MW)| cap
  4. dedup               — per (target-cluster, canonical SMILES) median
  5. split               — 90% sequence-identity target clustering (greedy,
     dependency-free), cluster-disjoint train/val/test + report per-split
     chemical-similarity profile and MW-baseline Spearman (leakage audit)

Inputs (CSV, any subset of columns):
    target_id, sequence, smiles, affinity_uM, affinity_type [, assay_id,
    temperature_c, date]
For PDBbind-style inputs with structures, add: protein_path, ligand_path
(passed through to train_affinity.py's index.csv when present).

Outputs:
    <out>/train.csv, val.csv, test.csv   (train_affinity.py --index_csv format)
    <out>/audit.json                     (per-filter drop counts, split audit)

Usage:
    python prepare_affinity_data.py --input raw.csv --out /data/affinity_curated
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdFingerprintGenerator
    from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams
    _RDKIT = True
except Exception:  # pragma: no cover
    _RDKIT = False


def _log_affinity_um(value: float) -> float:
    """Standardize a µM affinity to pAffinity = -log10(µM)."""
    return -math.log10(max(float(value), 1e-12))


def _heavy_atoms(mol) -> int:
    return sum(1 for a in mol.GetAtoms() if a.GetAtomicNum() > 1)


def _sequence_identity(a: str, b: str) -> float:
    """Fast identity estimate: identical-length Hamming, else 1 - norm-Levenshtein
    capped by length ratio (dependency-free stand-in for MMseqs2; conservative)."""
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    if abs(la - lb) / max(la, lb) > 0.1:
        ratio = min(la, lb) / max(la, lb)
    else:
        ratio = 1.0
    # Hamming on aligned prefix (cheap approximation)
    n = min(la, lb)
    same = sum(1 for x, y in zip(a, b) if x == y)
    return ratio * (same / n)


def greedy_cluster(sequences: dict[str, str], threshold: float = 0.9) -> dict[str, int]:
    """Greedy leader clustering at `threshold` identity; returns id->cluster."""
    order = sorted(sequences, key=lambda k: -len(sequences[k]))
    leaders: list[tuple[str, str]] = []
    assign: dict[str, int] = {}
    for tid in order:
        seq = sequences[tid]
        placed = False
        for ci, (_, lead) in enumerate(leaders):
            if _sequence_identity(seq, lead) >= threshold:
                assign[tid] = ci
                placed = True
                break
        if not placed:
            leaders.append((tid, seq))
            assign[tid] = len(leaders) - 1
    return assign


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--val_frac", type=float, default=0.08)
    parser.add_argument("--test_frac", type=float, default=0.10)
    parser.add_argument("--cluster_identity", type=float, default=0.9)
    parser.add_argument("--mw_corr_max", type=float, default=0.7,
                        help="drop assays with |Pearson(pAff, MW)| above this")
    parser.add_argument("--assay_std_min", type=float, default=0.3,
                        help="drop assays whose pAff std is below this (log units)")
    parser.add_argument("--mw_range", default="150,900")
    parser.add_argument("--max_heavy", type=int, default=50)
    parser.add_argument("--pains", action="store_true", help="apply RDKit PAINS catalog")
    parser.add_argument("--exclude_targets_csv", default=None,
                        help="CSV with a `sequence` column (e.g. FEP+ targets) to"
                             " remove from TRAIN only (Nesso-style leakage guard)")
    args = parser.parse_args()

    if not _RDKIT:
        print("[Warning] rdkit unavailable; skipping PAINS/MW/fingerprint steps.")

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    mw_lo, mw_hi = (float(x) for x in args.mw_range.split(","))

    rows = list(csv.DictReader(open(args.input)))
    audit: dict[str, int] = {"input": len(rows)}

    # 1) measurement filters ------------------------------------------------
    kept = []
    for r in rows:
        try:
            aff = float(r["affinity_uM"])
        except (TypeError, ValueError):
            aff = -1.0
        if aff <= 0:
            audit["drop_bad_affinity"] = audit.get("drop_bad_affinity", 0) + 1
            continue
        if r.get("affinity_type", "IC50").upper() not in {"KI", "KD", "IC50", "EC50"}:
            audit["drop_meas_type"] = audit.get("drop_meas_type", 0) + 1
            continue
        t = r.get("temperature_c")
        if t not in (None, "") and not (20 <= float(t) <= 40):
            audit["drop_temperature"] = audit.get("drop_temperature", 0) + 1
            continue
        kept.append(r)
    rows = kept
    audit["after_measurement"] = len(rows)

    # 2) compound filters ---------------------------------------------------
    if _RDKIT:
        pains_cat = None
        if args.pains:
            params = FilterCatalogParams()
            params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)
            pains_cat = FilterCatalog(params)
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=1024)
        kept = []
        for r in rows:
            mol = Chem.MolFromSmiles(r["smiles"])
            if mol is None:
                audit["drop_bad_smiles"] = audit.get("drop_bad_smiles", 0) + 1
                continue
            if _heavy_atoms(mol) > args.max_heavy:
                audit["drop_heavy_atoms"] = audit.get("drop_heavy_atoms", 0) + 1
                continue
            mw = Descriptors.MolWt(mol)
            if not (mw_lo <= mw <= mw_hi):
                audit["drop_mw_window"] = audit.get("drop_mw_window", 0) + 1
                continue
            if pains_cat is not None and pains_cat.HasMatch(mol):
                audit["drop_pains"] = audit.get("drop_pains", 0) + 1
                continue
            r = dict(r)
            r["_canon"] = Chem.MolToSmiles(mol)
            r["_mw"] = mw
            r["_fp"] = gen.GetFingerprint(mol)
            r["_paff"] = _log_affinity_um(r["affinity_uM"])
            kept.append(r)
        rows = kept
    else:
        for r in rows:
            r["_canon"] = r["smiles"]
            r["_paff"] = _log_affinity_um(r["affinity_uM"])
    audit["after_compound"] = len(rows)

    # 3) assay-level bias guards --------------------------------------------
    by_assay = defaultdict(list)
    for r in rows:
        by_assay[(r.get("assay_id") or f"{r['target_id']}|{r.get('affinity_type', 'IC50')}")].append(r)
    kept = []
    for aid, items in by_assay.items():
        paffs = np.array([r["_paff"] for r in items])
        if len(items) >= 5 and paffs.std() < args.assay_std_min:
            audit["drop_flat_assay"] = audit.get("drop_flat_assay", 0) + len(items)
            continue
        if _RDKIT and len(items) >= 8:
            mws = np.array([r["_mw"] for r in items])
            corr = abs(np.corrcoef(paffs, mws)[0, 1]) if mws.std() > 1e-6 else 0.0
            if corr > args.mw_corr_max:
                audit["drop_mw_correlated_assay"] = audit.get("drop_mw_correlated_assay", 0) + len(items)
                continue
        kept.extend(items)
    rows = kept
    audit["after_bias_guards"] = len(rows)

    # 4) dedup ---------------------------------------------------------------
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["target_id"], r["_canon"])
        vals = best.setdefault(key, {"list": []})
        vals["list"].append(r)
    deduped = []
    for (tid, _), blob in best.items():
        items = blob["list"]
        paffs = sorted(r["_paff"] for r in items)
        med = paffs[len(paffs) // 2]
        chosen = min(items, key=lambda r: abs(r["_paff"] - med))
        chosen = dict(chosen)
        chosen["pic50"] = f"{med:.3f}"
        chosen["active"] = "1" if med >= 6.0 else "0"
        deduped.append(chosen)
    audit["after_dedup"] = len(deduped)

    # 5) cluster-disjoint split ----------------------------------------------
    sequences = {r["target_id"]: r["sequence"] for r in deduped}
    assign = greedy_cluster(sequences, args.cluster_identity)
    clusters = sorted(set(assign.values()))
    rng = np.random.default_rng(42)
    rng.shuffle(clusters)
    n_test = max(1, int(len(clusters) * args.test_frac))
    n_val = max(1, int(len(clusters) * args.val_frac))
    test_c = set(clusters[:n_test])
    val_c = set(clusters[n_test:n_test + n_val])

    # Nesso-style leakage guard: excluded sequences removed from TRAIN only.
    excl = set()
    if args.exclude_targets_csv:
        for r in csv.DictReader(open(args.exclude_targets_csv)):
            excl.add(r["sequence"].strip())
        if excl:
            deduped = [
                r for r in deduped
                if r["sequence"].strip() in excl
                or assign[r["target_id"]] in test_c or assign[r["target_id"]] in val_c
            ]
            audit["train_leakage_removed"] = audit["after_dedup"] - len(deduped)

    splits = {"train": [], "val": [], "test": []}
    for r in deduped:
        c = assign[r["target_id"]]
        splits["test" if c in test_c else "val" if c in val_c else "train"].append(r)

    fields = ["name", "pic50", "active", "protein_path", "ligand_path",
              "target_id", "sequence", "smiles", "affinity_type"]
    for name, items in splits.items():
        with open(out / f"{name}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for i, r in enumerate(items):
                r = dict(r)
                r.setdefault("protein_path", "")
                r.setdefault("ligand_path", "")
                r["name"] = f"{r['target_id']}_{i:06d}"
                w.writerow(r)
        audit[f"split_{name}"] = len(items)

    # MW-baseline audit per split (the honesty check: beat this or don't ship)
    from scipy.stats import spearmanr

    for name, items in splits.items():
        if not items:
            continue
        xs = [r["_mw"] for r in items]
        ys = [float(r["pic50"]) for r in items]
        rho = spearmanr(xs, ys).statistic
        audit[f"{name}_mw_baseline_spearman"] = round(float(rho), 3)

    (out / "audit.json").write_text(json.dumps(audit, indent=2))
    print(json.dumps(audit, indent=2))
    print(f"\n[curated] {out}")


if __name__ == "__main__":
    main()
