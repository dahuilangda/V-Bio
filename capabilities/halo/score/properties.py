"""Cheap property computation + hard filters (RDKit): descriptors, PAINS, drug-likeness."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from rdkit import Chem, RDConfig
from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors
from rdkit.Chem import FilterCatalog

# Ertl synthetic-accessibility score ships in RDKit Contrib
_CONTRIB = Path(RDConfig.RDContribDir) / "SA_Score"
if str(_CONTRIB) not in sys.path:
    sys.path.insert(0, str(_CONTRIB))
try:
    import sascorer  # noqa: F401  (module from RDKit Contrib)

    def sas_score(mol) -> float:
        return float(sascorer.calculateScore(mol))
except Exception:  # pragma: no cover - fallback heuristic
    def sas_score(mol) -> float:
        rings = mol.GetRingInfo().GetNumRings()
        rot = rdMolDescriptors.CalcNumRotatableBonds(mol)
        sp3 = rdMolDescriptors.CalcFractionCSP3(mol)
        return float(np.clip(2.0 + 0.6 * rings + 0.15 * rot - sp3, 1.0, 10.0))


# ---- PAINS / aggregators catalog (built once lazily) ------------------------
_catalogs: FilterCatalog.FilterCatalog | None = None


def _get_catalog() -> FilterCatalog.FilterCatalog:
    global _catalogs
    if _catalogs is None:
        params = FilterCatalog.FilterCatalogParams()
        for c in (
            FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS_A,
            FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS_B,
            FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS_C,
        ):
            params.AddCatalog(c)
        _catalogs = FilterCatalog.FilterCatalog(params)
    return _catalogs


def is_pains(smiles: str) -> bool:
    m = Chem.MolFromSmiles(smiles)
    return _get_catalog().HasMatch(m) if m else True


DESCRIPTOR_KEYS = (
    "mw", "clogp", "tpsa", "hbd", "hba", "rotb", "n_rings", "n_arom_rings",
    "frac_csp3", "n_heavy", "n_atoms", "qed", "sas", "n_stereo",
)


def compute_descriptors(mol: Chem.Mol) -> dict:
    return {
        "mw": Descriptors.MolWt(mol),
        "clogp": Crippen.MolLogP(mol),
        "tpsa": rdMolDescriptors.CalcTPSA(mol),
        "hbd": rdMolDescriptors.CalcNumHBD(mol),
        "hba": rdMolDescriptors.CalcNumHBA(mol),
        "rotb": rdMolDescriptors.CalcNumRotatableBonds(mol),
        "n_rings": mol.GetRingInfo().NumRings(),
        "n_arom_rings": rdMolDescriptors.CalcNumAromaticRings(mol),
        "frac_csp3": rdMolDescriptors.CalcFractionCSP3(mol),
        "n_heavy": mol.GetNumHeavyAtoms(),
        "n_atoms": mol.GetNumAtoms(),
        "qed": Chem.QED.qed(mol),
        "sas": sas_score(mol),
        "n_stereo": rdMolDescriptors.CalcNumAtomStereoCenters(mol),
    }


def descriptor_vector(mol: Chem.Mol) -> np.ndarray:
    d = compute_descriptors(mol)
    return np.array([d[k] for k in DESCRIPTOR_KEYS], dtype=np.float32)


# default lead-opt property window (human editable at runtime via feedback)
DEFAULT_WINDOW = {
    "mw": (150.0, 560.0),
    "clogp": (-1.0, 5.5),
    "tpsa": (30.0, 160.0),
    "hbd": (0, 6),
    "hba": (0, 13),
    "rotb": (0, 12),
    "sas": (0.0, 6.5),
    "n_heavy": (10, 60),
}


def passes_window(desc: dict, window: dict | None = None) -> bool:
    window = window or DEFAULT_WINDOW
    for k, (lo, hi) in window.items():
        v = desc.get(k)
        if v is None or not (lo <= v <= hi):
            return False
    return True


def canonical_smiles(smiles: str) -> str | None:
    m = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(m) if m else None
