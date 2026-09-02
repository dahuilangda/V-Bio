"""Benchmark target registry (Schrodinger FEP benchmark sets shipped with Boltz2Score)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from halo import REPO_ROOT

DATA_ROOT = REPO_ROOT / "data"  # V-Bio data dir; benchmark targets are optional

# name -> (protein file, target chain)  ligands.sdf assumed alongside
_TARGET_REGISTRY: dict[str, tuple[str, str]] = {
    "cdk8": ("5hnb-chainA-prepared.pdb", "A"),
    "cmet": ("4r1y-chainA-prepared.pdb", "A"),
    "eg5": ("3l9h-chainA-prepared.pdb", "A"),
    "hif2a": ("5tbm-chainA-prepared.pdb", "A"),
    "pfkfb3": ("6hvi-chainA-prepared.pdb", "A"),
    "shp2": ("5ehr-chainA-prepared.pdb", "A"),
    "syk": ("4pv0-chainA-prepared.pdb", "A"),
    "tnks2": ("4ui5-chainA-prepared.pdb", "A"),
}

# targets prepared inside this package (e.g. CDK2 from the public FEP+ benchmark)
_EXTRA_REGISTRY: dict[str, tuple[str, str]] = {
    "cdk2": ("1h1q-chainA-prepared.pdb", "A"),
}


@dataclass
class Target:
    name: str
    protein_pdb: Path
    ligands_sdf: Path
    target_chain: str = "A"
    ligand_chain: str = "L"
    extra: dict = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.protein_pdb.parent


def get_target(name: str) -> Target:
    name = name.lower()
    if name in _TARGET_REGISTRY:
        protein, chain = _TARGET_REGISTRY[name]
        d = DATA_ROOT / name
        t = Target(name, d / protein, d / "ligands.sdf", chain)
    elif name in _EXTRA_REGISTRY:
        protein, chain = _EXTRA_REGISTRY[name]
        d = DATA_ROOT / name
        t = Target(name, d / protein, d / "ligands.sdf", chain)
    else:
        raise KeyError(f"unknown target {name!r}; available: {sorted(_TARGET_REGISTRY) + sorted(_EXTRA_REGISTRY)}")
    if not t.protein_pdb.exists():
        raise FileNotFoundError(f"protein missing for {name}: {t.protein_pdb}")
    if not t.ligands_sdf.exists():
        raise FileNotFoundError(f"ligands missing for {name}: {t.ligands_sdf}")
    return t


def available_targets() -> list[str]:
    out = []
    for name in list(_TARGET_REGISTRY) + list(_EXTRA_REGISTRY):
        try:
            get_target(name)
            out.append(name)
        except FileNotFoundError:
            pass
    return out
