"""Run-local Boltz cache with NCAA residue CCDs registered.

Boltz reads custom residue CCDs as RDKit Mol pickles in {cache}/mols/{CCD}.pkl.
We build an isolated cache: top-level assets (ccd.pkl, checkpoints) and every
base component pickle are symlinked from the shared cache, then the NCAA mols
are written with the production builder (backend.runtime.custom_ccd_builder) so
atom naming / backbone perception match the V-Bio pipeline exactly. The shared
cache is never modified.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

from peplm.residues import NCAA_PRESETS

DEFAULT_BASE_CACHE = "/data/boltz_cache"


def prepare_run_cache(work_dir: Path, base_cache: str = DEFAULT_BASE_CACHE,
                      extra_molecules: list[dict] | None = None) -> Path:
    base = Path(base_cache)
    cache = Path(work_dir) / "boltz_cache"
    cache.mkdir(parents=True, exist_ok=True)
    for entry in base.iterdir():
        if entry.name == "mols":
            continue
        link = cache / entry.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry)
    mols = cache / "mols"
    mols.mkdir(exist_ok=True)
    src_mols = base / "mols"
    if src_mols.is_dir():
        for p in src_mols.glob("*.pkl"):
            link = mols / p.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(p)

    # register NCAA CCDs with the production mol builder
    sys.path.insert(0, "/data/V-Bio")
    try:
        from rdkit import Chem

        from backend.runtime.custom_ccd_builder import (
            _boltz_custom_ccd_aliases,
            _build_custom_ccd_mol,
        )
        items = [{"ccd": ccd, "smiles": meta["smiles"], "kind": "residue",
                  "base_residue": meta["base"], "label": meta["label"]}
                 for ccd, meta in NCAA_PRESETS.items()]
        items += [m for m in (extra_molecules or []) if m.get("ccd") and m.get("smiles")]
        Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
        for item in items:
            mol = _build_custom_ccd_mol(item["smiles"], kind=item.get("kind") or "residue")
            mol.name = item["ccd"]
            for alias in _boltz_custom_ccd_aliases(item["ccd"]):
                out = mols / f"{alias}.pkl"
                if out.exists() or out.is_symlink():
                    out.unlink()
                with out.open("wb") as fh:
                    pickle.dump(mol, fh)
    finally:
        sys.path.remove("/data/V-Bio")
    return cache
