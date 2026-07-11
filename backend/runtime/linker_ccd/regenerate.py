"""Regenerate the bicyclic-linker CCD resources (SEZ/29N/BS3 .cif + linker_mols.pkl).

Source of truth is Boltz's CCD cache (ccd.pkl): its Mols carry the atom ``name`` props that
``run_single_prediction.BICYCLIC_LINKER_ATOM_MAP`` references, so the generated mmcif matches
Boltz's geometry exactly. Re-run after a Boltz CCD update or an RDKit pickle-format change:

    python -m backend.runtime.linker_ccd.regenerate /data/boltz_cache/ccd.pkl
"""

import pickle
import sys
from pathlib import Path

import numpy as np
from rdkit import Chem

from backend.runtime.custom_ccd_builder import build_ligand_ccd_mmcif

LINKERS = ("SEZ", "29N", "BS3")


def main(ccd_pkl: str) -> None:
    with open(ccd_pkl, "rb") as handle:
        ccd = pickle.load(handle)
    out_dir = Path(__file__).resolve().parent
    mols = {}
    for code in LINKERS:
        if code not in ccd:
            raise KeyError(f"CCD {code} not found in {ccd_pkl}")
        mol = ccd[code]
        (out_dir / f"{code}.cif").write_text(
            build_ligand_ccd_mmcif(code, mol, label=f"{code} bicyclic linker")
        )
        # Protenix resolves ligand CCDs from the rdkit_mol.pkl and expects the same custom
        # attributes its own cache builder sets (scripts/gen_ccd_cache.py / json_parser.py):
        # atom_map, ref_conf_id, ref_mask. Boltz's CCD-cache Mols carry only atom "name" props.
        mol.atom_map = {atom.GetProp("name"): atom.GetIdx() for atom in mol.GetAtoms()}
        mol.ref_conf_id = 0
        mol.ref_mask = np.ones(mol.GetNumAtoms(), dtype=bool)
        mols[code] = mol
    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
    with (out_dir / "linker_mols.pkl").open("wb") as handle:
        pickle.dump(mols, handle)
    print(f"Regenerated {', '.join(LINKERS)} -> {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/data/boltz_cache/ccd.pkl")
