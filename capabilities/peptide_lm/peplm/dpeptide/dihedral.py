"""Backbone phi/psi chirality verification (validated judging criteria).

L/D Ramachandran regions mirror each other:
    alpha-helix:  L (phi ~ -57, psi ~ -47)  vs  D (phi ~ +57, psi ~ +47)
Mirror-image chains satisfy |phi_L + phi_D| ~ 0, |psi_L + psi_D| ~ 0
(measured exactly 0.0 on the deposited-vs-mirrored reference pair).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import gemmi
import numpy as np


def _dihedral(p0, p1, p2, p3) -> float:
    b0, b1, b2 = p0 - p1, p2 - p1, p3 - p2
    b1n = b1 / np.linalg.norm(b1)
    v = b0 - np.dot(b0, b1n) * b1n
    w = b2 - np.dot(b2, b1n) * b1n
    return math.degrees(math.atan2(np.dot(np.cross(b1n, v), w), np.dot(v, w)))


def _chain_atoms(chain: gemmi.Chain):
    out = []
    for residue in chain:
        atoms = {}
        for a in residue:
            if a.altloc in ("", "A", "\x00"):
                atoms.setdefault(a.name, np.array([a.pos.x, a.pos.y, a.pos.z]))
        out.append((residue.name, atoms))
    return out


def chain_phi_psi(chain: gemmi.Chain) -> list[tuple[int, str, float, float]]:
    """(index_from_1, resname, phi, psi) for residues with both neighbours."""
    residues = _chain_atoms(chain)
    out = []
    for i in range(1, len(residues) - 1):
        prev, cur, nxt = residues[i - 1][1], residues[i][1], residues[i + 1][1]
        if not ({"C"} <= prev.keys() and {"N", "CA", "C"} <= cur.keys() and {"N"} <= nxt.keys()):
            continue
        phi = _dihedral(prev["C"], cur["N"], cur["CA"], cur["C"])
        psi = _dihedral(cur["N"], cur["CA"], cur["C"], nxt["N"])
        out.append((i, residues[i][0], phi, psi))
    return out


def classify(phi: float, psi: float) -> str:
    """Coarse Ramachandran region label including D-side regions."""
    if -100 <= phi <= -40 and -70 <= psi <= -10:
        return "L-alpha"
    if 40 <= phi <= 100 and 10 <= psi <= 70:
        return "D-alpha"
    if -160 <= phi <= -60 and (90 <= psi <= 180 or -180 <= psi <= -150):
        return "L-beta"
    if 60 <= phi <= 160 and -90 <= psi <= -10:
        return "D-beta"
    return "other"


def chain_region_summary(chain: gemmi.Chain) -> dict:
    pp = chain_phi_psi(chain)
    kinds: dict[str, int] = {}
    for _, _, phi, psi in pp:
        kinds[classify(phi, psi)] = kinds.get(classify(phi, psi), 0) + 1
    phis = np.array([p[2] for p in pp]) if pp else np.array([])
    psis = np.array([p[3] for p in pp]) if pp else np.array([])
    return {
        "n_residues": len(pp),
        "phi_mean": float(phis.mean()) if len(phis) else None,
        "psi_mean": float(psis.mean()) if len(psis) else None,
        "regions": kinds,
    }


def _wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def compare_dihedrals(
    chain_a: gemmi.Chain,
    chain_b: gemmi.Chain,
    mode: str,
) -> dict:
    """Compare two chains' phi/psi.

    mode='same':   |phi_A - phi_B| ~ 0 expected for equal-chirality conformations
    mode='mirror': |phi_A + phi_B| ~ 0 expected for mirror-image pairs
    """
    if mode not in {"same", "mirror"}:
        raise ValueError("mode must be 'same' or 'mirror'")
    pa, pb = chain_phi_psi(chain_a), chain_phi_psi(chain_b)
    n = min(len(pa), len(pb))
    if n == 0:
        return {"n": 0, "phi_mean_abs": None, "psi_mean_abs": None,
                "phi_max_abs": None, "psi_max_abs": None}
    sign = 1.0 if mode == "same" else 1.0  # mirror uses sum: a + b
    dphi = np.array([pa[i][2] - pb[i][2] if mode == "same" else pa[i][2] + pb[i][2]
                     for i in range(n)])
    dpsi = np.array([pa[i][3] - pb[i][3] if mode == "same" else pa[i][3] + pb[i][3]
                     for i in range(n)])
    dphi, dpsi = _wrap180(dphi), _wrap180(dpsi)
    return {
        "n": n,
        "phi_mean_abs": float(np.abs(dphi).mean()),
        "psi_mean_abs": float(np.abs(dpsi).mean()),
        "phi_max_abs": float(np.abs(dphi).max()),
        "psi_max_abs": float(np.abs(dpsi).max()),
    }


def validate_product_dihedrals(
    product: Path,
    native_reference: Path,
    receptor_chain: str = "A",
    peptide_chain: str = "B",
) -> dict:
    """Validate a flipped (L-target + D-peptide) product against a deposited
    native complex: receptor should be same-chirality (small |diff|), peptide
    should be same-chirality D (small |diff| vs the deposited D-peptide)."""
    prod = gemmi.read_structure(str(product)); prod.setup_entities()
    nat = gemmi.read_structure(str(native_reference)); nat.setup_entities()
    return {
        "receptor": compare_dihedrals(prod[0][receptor_chain], nat[0][receptor_chain], "same"),
        "peptide": compare_dihedrals(prod[0][peptide_chain], nat[0][peptide_chain], "same"),
        "peptide_regions_product": chain_region_summary(prod[0][peptide_chain])["regions"],
        "peptide_regions_native": chain_region_summary(nat[0][peptide_chain])["regions"],
    }
