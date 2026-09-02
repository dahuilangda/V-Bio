"""Peptidic developability-liability oracle.

Extends the V-Bio production liability penalty (composition ratios,
homopolymer runs, repeated triples) with chemical-stability liabilities
standard in therapeutic-peptide optimization: deamidation (N followed by
G/S/T), Asp isomerization (D-G), oxidation-prone long Met/Trp runs, and
N-terminal Gln/Glu cyclization tendency. Returns a *score* in [0,1]
(1 = clean; the production code returns a penalty — this is 1 - penalty).
"""

from __future__ import annotations

from collections import Counter

HYDROPHOBIC = set("AILMFWYV")
CHARGED = set("DEKRH")


def liability_score(tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    seq = [t[1:-1] if t.startswith("[") else t for t in tokens]  # CCD names for NCAAs
    n = len(seq)
    counts = Counter(seq)
    penalty = 0.0

    # composition liabilities (V-Bio production thresholds)
    hydrophobic_ratio = sum(counts.get(aa, 0) for aa in HYDROPHOBIC) / n
    charged_ratio = sum(counts.get(aa, 0) for aa in CHARGED) / n
    pro_gly_ratio = sum(counts.get(aa, 0) for aa in "PG") / n
    if hydrophobic_ratio > 0.58:
        penalty += min(0.12, (hydrophobic_ratio - 0.58) * 0.6)
    if charged_ratio > 0.45:
        penalty += min(0.08, (charged_ratio - 0.45) * 0.5)
    if pro_gly_ratio > 0.35:
        penalty += min(0.08, (pro_gly_ratio - 0.35) * 0.5)

    # homopolymer runs >= 4 and repeated triples (V-Bio production rules)
    run = 1
    for i in range(1, n):
        run = run + 1 if seq[i] == seq[i - 1] else 1
        if run == 4:
            penalty += 0.04
    triples = Counter("".join(seq[i:i + 3]) for i in range(n - 2))
    penalty += 0.03 * sum(1 for c in triples.values() if c >= 3)

    # chemical stability liabilities
    deamid = sum(1 for i in range(n - 1) if seq[i] == "N" and seq[i + 1] in "GST")
    penalty += min(0.06, 0.02 * deamid)
    isomer = sum(1 for i in range(n - 1) if seq[i] == "D" and seq[i + 1] == "G")
    penalty += min(0.04, 0.02 * isomer)
    oxidation = sum(1 for i in range(n - 1) if {seq[i], seq[i + 1]} & {"M", "W"}
                    and seq[i] in "MW" and seq[i + 1] in "MW")
    penalty += min(0.04, 0.02 * oxidation)
    if seq[0] in ("Q", "E", "PCA"):
        penalty += 0.02  # N-terminal cyclization tendency

    return float(min(1.0, max(0.0, 1.0 - min(penalty, 0.5))))
