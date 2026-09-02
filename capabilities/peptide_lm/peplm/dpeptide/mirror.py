"""Structure mirroring for D-peptide design (validated protocol).

Mirror algebra (eight rounds of experiments in /data/Boltz2Score/dpeptide_test):
    design goal        L-target + D-peptide
    equivalent problem D-target + L-peptide   (= mirror of the goal)
    answer reference   mirror(deposited complex)

The mirror operation x -> -x is a geometrically exact enantiomerization:
|phi_L + phi_D| = 0, |psi_L + psi_D| = 0, CA chiral volumes flip sign.

D-amino-acid CCD names are NOT in the Boltz token vocabulary — they must be
renamed to their L counterparts whenever a D-residue structure is fed to a
model (chirality is then carried purely by coordinates).
"""

from __future__ import annotations

from dataclasses import dataclass

import gemmi
import numpy as np

# terminal caps without model tokens — removed on ingest
UNMAPPED_CAP_RESIDUES = {"NH2", "NHE", "ACE", "NMA", "FOR", "ACB"}

WATER_RESIDUES = {"HOH", "WAT", "H2O"}
def mirror_structure(structure: gemmi.Structure) -> gemmi.Structure:
    """In-place x -> -x enantiomerization of every atom (exact, validated)."""
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    pos = atom.pos
                    atom.pos = gemmi.Position(-pos.x, pos.y, pos.z)
    return structure
@dataclass
class ChiralityReport:
    """Chirality summary for one chain."""

    chain: str
    n_scored: int
    n_positive: int
    mean_volume: float

    @property
    def is_d(self) -> bool:
        return self.n_scored > 0 and self.mean_volume < 0

    @property
    def is_l(self) -> bool:
        return self.n_scored > 0 and self.mean_volume > 0


def ca_chiral_volumes(structure: gemmi.Structure, chain: str) -> list[float]:
    """det[N-CA, C-CA, CB-CA] per residue (L ~ +2.5, D ~ -2.5)."""
    volumes: list[float] = []
    target = structure[0][chain]
    for residue in target:
        names = {a.name: np.array([a.pos.x, a.pos.y, a.pos.z]) for a in residue}
        if not {"N", "CA", "C", "CB"} <= names.keys():
            continue
        ca = names["CA"]
        volumes.append(
            float(np.linalg.det(np.stack([names["N"] - ca, names["C"] - ca, names["CB"] - ca])))
        )
    return volumes


def chirality_report(structure: gemmi.Structure, chain: str) -> ChiralityReport:
    volumes = ca_chiral_volumes(structure, chain)
    if not volumes:
        return ChiralityReport(chain=chain, n_scored=0, n_positive=0, mean_volume=float("nan"))
    return ChiralityReport(
        chain=chain,
        n_scored=len(volumes),
        n_positive=sum(v > 0 for v in volumes),
        mean_volume=float(np.mean(volumes)),
    )
