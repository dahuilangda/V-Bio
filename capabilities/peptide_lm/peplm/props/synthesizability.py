"""SPPS synthesizability oracle.

Rule engine over solid-phase peptide synthesis practice:
  * length: routine to ~35 aa, possible to 50, hard beyond (user-pool NCAAs
    are all catalog Fmoc building blocks; Se/beta residues couple slower)
  * difficult-sequence penalties: long hydrophobic runs and beta-sheet prone
    stretches cause on-resin aggregation (the classic SPPS failure mode)
  * specialty monomers (phospho, Se, beta) add mild cost, capped so a
    well-placed phospho residue is not over-penalized
Output in [0,1], higher = easier.
"""

from __future__ import annotations

import math

from peplm.residues import (
    BETA_RESIDUES,
    HYDROPATHY,
    SPECIALTY_SPPS,
)

HYDROPHOBIC = set("AILMFWYV") | {"[AIB]", "[NLE]", "[NVA]", "[MSE]"}
BETA_PRONE = set("VFITIYW") | {"[NVA]", "[NLE]"}


def _longest_run(tokens, members) -> int:
    best = cur = 0
    for t in tokens:
        cur = cur + 1 if t in members else 0
        best = max(best, cur)
    return best


def synthesizability_score(tokens: list[str], cyclic: bool = False) -> float:
    if not tokens:
        return 0.0
    n = len(tokens)
    # length term: 1.0 <=35, falls to ~0.2 at 60
    length_term = 1.0 / (1.0 + math.exp((n - 35) / 7.0))
    # aggregation-prone stretches
    hydro_run = _longest_run(tokens, HYDROPHOBIC)
    beta_run = _longest_run(tokens, BETA_PRONE)
    run_term = max(0.0, 1.0 - 0.12 * max(0, hydro_run - 4)) \
        * max(0.0, 1.0 - 0.10 * max(0, beta_run - 5))
    # mean hydrophobicity drives coupling difficulty
    mean_kd = sum(HYDROPATHY.get(t, 0.0) for t in tokens) / n
    kd_term = 1.0 - min(1.0, max(0.0, (mean_kd + 4.5) / 9.0)) * 0.4
    # specialty monomers: sublinear cost
    specialty = sum(1 for t in tokens if t.lstrip("[").rstrip("]") in SPECIALTY_SPPS)
    spec_term = max(0.55, 1.0 - 0.08 * math.sqrt(specialty))
    beta_count = sum(1 for t in tokens if t in BETA_RESIDUES)
    beta_term = max(0.6, 1.0 - 0.10 * beta_count)

    score = (0.35 * length_term + 0.30 * run_term + 0.15 * kd_term
             + 0.10 * spec_term + 0.10 * beta_term)
    return float(min(1.0, max(0.0, score)))
