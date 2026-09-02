"""Net-charge computation (Henderson-Hasselbalch at pH 7.4).

Only residues with pKa near physiological range contribute: K/R/H/ORN/MLY
positive, D/E negative. Cys (8.3) and Tyr (10.1) stay ~neutral at 7.4 and are
intentionally excluded."""
from __future__ import annotations

PKA_N_TERM = 9.6
PKA_C_TERM = 2.3
SIDE_PKA = {"K": 10.5, "R": 12.5, "H": 6.0, "[ORN]": 10.5, "[MLY]": 10.1,
            "D": 3.9, "E": 4.1}


def net_charge(tokens: list[str], pH: float = 7.4) -> float:
    q = 0.0
    for tok in tokens:
        pka = SIDE_PKA.get(tok)
        if pka is None:
            continue
        if pka > 7.0:  # basic side chain
            q += 1.0 / (1.0 + 10 ** (pH - pka))
        else:          # acidic side chain
            q -= 1.0 / (1.0 + 10 ** (pka - pH))
    q += 1.0 / (1.0 + 10 ** (pH - PKA_N_TERM))
    q -= 1.0 / (1.0 + 10 ** (PKA_C_TERM - pH))
    return q
