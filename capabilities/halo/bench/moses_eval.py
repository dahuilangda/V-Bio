"""MOSES-style generative-quality evaluation for chemical LMs.

Metrics: validity, unique@k (samples & scaffolds), internal diversity
(Tanimoto), novelty vs the training set, and Fréchet ChemNet Distance (FCD)
against a reference sample (via fcd_torch on GPU when available).
"""

from __future__ import annotations

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit import DataStructs
from rdkit.Chem.Scaffolds import MurckoScaffold

RDLogger.DisableLog("rdApp.*")

_fp = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, countSimulation=False)


def _fps(smiles_list):
    out = []
    for s in smiles_list:
        m = Chem.MolFromSmiles(s)
        if m is not None:
            out.append(_fp.GetFingerprint(m))
    return out


def evaluate_samples(samples: list[str], train_sample: list[str], reference: list[str] | None = None,
                     k: int = 1000, fcd_reference: list[str] | None = None, device="cuda") -> dict:
    n = len(samples)
    valid = [s for s in samples if Chem.MolFromSmiles(s) is not None]
    validity = len(valid) / max(n, 1)
    uniq = len(set(valid)) / max(len(valid), 1)

    # unique@k on the first k valid
    firstk = valid[:k]
    unique_at_k = len(set(firstk)) / max(len(firstk), 1)
    scaffs = {MurckoScaffold.MurckoScaffoldSmiles(mol=Chem.MolFromSmiles(s)) for s in firstk
              if Chem.MolFromSmiles(s) is not None}
    scaffold_uniq = len(scaffs) / max(len(firstk), 1)

    fps = _fps(valid[:2000])
    if len(fps) >= 2:
        sims = []
        rng = np.random.RandomState(0)
        for _ in range(500):
            i, j = rng.randint(0, len(fps), 2)
            if i != j:
                sims.append(DataStructs.TanimotoSimilarity(fps[i], fps[j]))
        int_div = float(np.mean(sims))
    else:
        int_div = float("nan")

    train_set = set(train_sample)
    novel = sum(1 for s in valid if s not in train_set) / max(len(valid), 1)

    out = {
        "n": n, "validity": round(validity, 4), "unique": round(uniq, 4),
        f"unique@{k}": round(unique_at_k, 4), "scaffold_unique": round(scaffold_uniq, 4),
        "internal_diversity": round(int_div, 4), "novelty_vs_train_sample": round(novel, 4),
    }

    if fcd_reference:
        try:
            from fcd_torch import FCD

            fcd_calc = FCD(device=device if str(device).startswith("cuda") else "cpu", n_jobs=8)
            out["FCD"] = round(float(fcd_calc(fcd_reference, valid)), 3)
        except Exception as e:
            out["FCD"] = f"unavailable ({type(e).__name__})"
    return out


def sample_reference(corpus_path, n=10000, seed=0):
    import random as _random
    from pathlib import Path

    lines = Path(corpus_path).read_text().splitlines()
    rng = _random.Random(seed)
    return [l.split("\t")[0].split()[0] for l in rng.sample(lines, min(n, len(lines)))]
