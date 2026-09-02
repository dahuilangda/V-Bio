"""Ligand set loading: SMILES + experimental activity (pIC50 / deltaG)."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from rdkit import Chem

RT_KCAL = 0.0019872041 * 298.15  # kcal/mol at 298.15 K


def _pic50_to_dg(pic50: float) -> float:
    """pIC50 (M) -> binding free energy in kcal/mol (approximation)."""
    return -RT_KCAL * pic50 * math.log(10) / 1.0  # = -RT*ln(10)*pIC50


def load_ligand_table(sdf_path: Path) -> pd.DataFrame:
    """Load a benchmark ligand SDF with an IC50[uM] property.

    Returns rows: smiles (canonical), mol_name, ic50_um, activity_pic50, activity_dg,
    plus 3D coords availability.
    """
    rows = []
    supp = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
    for i, mol in enumerate(supp):
        if mol is None:
            continue
        props = mol.GetPropsAsDict()
        ic50 = props.get("IC50[uM]")
        smiles = Chem.MolToSmiles(Chem.RemoveHs(mol)) if mol.GetNumAtoms() else None
        row = {
            "index": i,
            "mol_name": str(props.get("Name", i)),
            "smiles": smiles,
            "has_conformer": mol.GetNumConformers() > 0,
        }
        if isinstance(ic50, (int, float)) and ic50 > 0:
            row["ic50_um"] = float(ic50)
            row["activity_pic50"] = 6.0 - math.log10(float(ic50))
            row["activity_dg"] = _pic50_to_dg(row["activity_pic50"])
        else:
            row["ic50_um"] = math.nan
            row["activity_pic50"] = math.nan
            row["activity_dg"] = math.nan
        rows.append(row)
    return pd.DataFrame(rows)


def load_reference_ligand(sdf_path: Path, prefer_best_activity: bool = True) -> Chem.Mol:
    """Pocket reference ligand: the most potent ligand with a conformer (crystal pose)."""
    supp = [m for m in Chem.SDMolSupplier(str(sdf_path), removeHs=False) if m is not None]
    with_conf = [m for m in supp if m.GetNumConformers() > 0]
    pool = with_conf or supp
    if prefer_best_activity:
        def ic50(m):
            v = m.GetPropsAsDict().get("IC50[uM]")
            return v if isinstance(v, (int, float)) and v > 0 else math.inf
        pool = sorted(pool, key=ic50)
    return pool[0]


def load_chembl_tsv(path: Path, limit: int | None = None) -> list[str]:
    """Load ChEMBL raw tsv (chembl_id<TAB>canonical_smiles<TAB>inchi...) with drug-like filtering."""
    out = []
    for line in Path(path).read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        smi = parts[1].strip()
        m = Chem.MolFromSmiles(smi)
        if m is None:
            continue
        try:
            frag = Chem.GetMolFrags(m, asMols=True, sanitizeFrags=True)
            if not frag:
                continue
            m = max(frag, key=lambda x: x.GetNumAtoms())
            n_heavy = m.GetNumHeavyAtoms()
            if not (10 <= n_heavy <= 60):
                continue
            out.append(Chem.MolToSmiles(m))
        except Exception:
            continue
        if limit and len(out) >= limit:
            break
    return list(dict.fromkeys(out))


def load_smiles_corpus(path: Path, limit: int | None = None) -> list[str]:
    """Load a .smi file; keep the largest fragment, canonicalize, drop >60 heavy atoms."""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        smi = line.split("\t")[0].split()[0]
        m = Chem.MolFromSmiles(smi)
        if m is None:
            continue
        try:
            frag = Chem.GetMolFrags(m, asMols=True, sanitizeFrags=True)
            if not frag:
                continue
            m = max(frag, key=lambda x: x.GetNumAtoms())
            if m.GetNumHeavyAtoms() > 60:
                continue
            out.append(Chem.MolToSmiles(m))
        except Exception:
            continue
        if limit and len(out) >= limit:
            break
    return list(dict.fromkeys(out))
