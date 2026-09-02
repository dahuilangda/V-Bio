"""Peptide solubility oracle.

Transparent physical model (no fitted weights to hallucinate): mean Kyte-
Doolittle hydropathy, net charge magnitude at pH 7.4 (charged peptides resist
aggregation), aromatic content (pi-stacking drives aggregation), and a length
penalty (long peptides expose more hydrophobic surface). Output in [0,1],
higher = more soluble. Calibrated so typical therapeutic peptides (GLP-1-like,
somatostatin-like charge/hydropathy profiles) land in the 0.5-0.8 band and
membrane-spanning hydrophobins land near 0.
"""

from __future__ import annotations

import math

from peplm.props.charge import net_charge
from peplm.residues import HYDROPATHY

AROMATIC = set("FWY") | {"[PTR]"}


def solubility_score(tokens: list[str], cyclic: bool = False) -> float:
    if not tokens:
        return 0.0
    n = len(tokens)
    mean_kd = sum(HYDROPATHY.get(t, 0.0) for t in tokens) / n
    # hydropathy term: KD spans [-4.5, 4.5]; map to [0,1], 1 = fully polar
    hydro = 1.0 - min(1.0, max(0.0, (mean_kd + 4.5) / 9.0))

    charge = abs(net_charge(tokens))
    # charge term: |q| >= ~0.3/residue is strongly soluble; saturates
    charge_term = min(1.0, charge / max(1.0, 0.25 * n))

    arom = sum(1 for t in tokens if t in AROMATIC) / n
    arom_term = max(0.0, 1.0 - 3.0 * arom)  # >33% aromatic kills solubility

    # long + hydrophobic peptides aggregate; length alone is mild
    length_term = 1.0 / (1.0 + math.exp((n - 45) / 10.0))

    score = (0.45 * hydro + 0.30 * charge_term + 0.15 * arom_term + 0.10 * length_term)
    return float(min(1.0, max(0.0, score)))
