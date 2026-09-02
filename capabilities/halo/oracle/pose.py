"""Pose construction for novel candidates: embed + MMFF + MCS-align into the pocket.

Fast 'inplace' protocol: ETKDGv3 conformers are aligned onto the reference
co-crystal ligand via a maximum-common-substructure map; the best-aligned
conformer is kept. Candidates without a usable MCS fall back to shape/centroid
placement (flagged as low-confidence poses).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem, rdFMCS, rdMolAlign
from rdkit.Geometry import Point3D

rdBase.DisableLog("rdApp.warning")


def embed_conformers(smiles: str, n_confs: int = 4, seed: int = 0xC0FFEE) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    params.useSmallRingTorsions = True
    try:
        codes = AllChem.EmbedMultipleConfs(mol, numConfs=n_confs, params=params)
    except Exception:
        return None
    if not list(codes):
        return None
    try:
        AllChem.MMFFOptimizeMoleculeConfs(mol, maxIters=500)
    except Exception:
        pass
    return mol


def _mcs_align(mol: Chem.Mol, ref: Chem.Mol) -> tuple[Chem.Mol, float] | None:
    """Align every conformer of mol onto ref via MCS; return best (mol, rmsd)."""
    try:
        mcs = rdFMCS.FindMCS(
            [mol, ref], timeout=10,
            bondCompare=rdFMCS.BondCompare.CompareOrder,
            atomCompare=rdFMCS.AtomCompare.CompareElements,
            ringMatchesRingOnly=True, completeRingsOnly=True,
        )
    except Exception:
        return None
    if mcs.numAtoms < 5:
        return None
    patt = Chem.MolFromSmarts(mcs.smartsString)
    if patt is None:
        return None
    ref_match = ref.GetSubstructMatch(patt)
    if not ref_match:
        return None
    best, best_rms = None, float("inf")
    for conf_id in range(mol.GetNumConformers()):
        for cand_match in mol.GetSubstructMatches(patt, uniquify=False, maxMatches=16):
            if len(set(cand_match)) != len(cand_match):
                continue
            cmap = list(zip(cand_match, ref_match))
            try:
                rms = rdMolAlign.AlignMol(mol, ref, prbCid=conf_id, atomMap=cmap, reflect=False, maxIters=64)
            except Exception:
                continue
            if rms < best_rms:
                best_rms = rms
                best = (conf_id, cmap)
    if best is None:
        return None
    conf_id, cmap = best
    # rewrite single-conformer mol in aligned frame
    out = Chem.Mol(mol)
    conf = out.GetConformer(conf_id)
    aligned = Chem.Mol(out, False, conf_id)  # copy with that conformer only
    _ = conf
    return aligned, best_rms


def _centroid_place(mol: Chem.Mol, ref: Chem.Mol, seed: int = 7) -> Chem.Mol:
    """Fallback: centroid + principal-axis alignment onto the reference."""
    ref_conf = ref.GetConformer()
    ref_pos = np.array(ref_conf.GetPositions())
    ref_c = ref_pos.mean(0)
    out = Chem.Mol(mol)
    # principal axes of reference
    rc = ref_pos - ref_c
    _, _, vt = np.linalg.svd(rc, full_matrices=False)
    for conf in out.GetConformers():
        pos = np.array(conf.GetPositions())
        c = pos.mean(0)
        pc = pos - c
        _, _, vp = np.linalg.svd(pc, full_matrices=False)
        R = vp.T @ vt.T
        new = pc @ R + ref_c
        for i, p in enumerate(new):
            conf.SetAtomPosition(i, Point3D(*map(float, p)))
    return out


def build_poses(
    smiles_list: list[str],
    reference: Chem.Mol,
    out_sdf: Path,
    n_confs: int = 4,
    mcs_rmsd_max: float = 3.5,
) -> list[dict]:
    """Write aligned poses; returns per-smiles info {id, smiles, method, rmsd}."""
    writer = Chem.SDWriter(str(out_sdf))
    infos = []
    for i, smi in enumerate(smiles_list):
        mol = embed_conformers(smi, n_confs=n_confs)
        if mol is None:
            infos.append({"i": i, "smiles": smi, "method": "failed", "rmsd": None})
            continue
        res = _mcs_align(mol, reference)
        if res is not None and res[1] <= mcs_rmsd_max:
            posed, rms = res
            method = "mcs"
        else:
            posed = _centroid_place(mol, reference)
            method = "centroid"
            rms = None
        posed.SetProp("_Name", f"cand{i:05d}")
        try:
            writer.write(posed)
        except Exception:
            infos.append({"i": i, "smiles": smi, "method": "failed", "rmsd": None})
            continue
        infos.append({"i": i, "smiles": smi, "method": method, "rmsd": rms})
    writer.close()
    return infos


def reference_ligand_with_conformer(sdf_path: Path) -> Chem.Mol:
    mols = [m for m in Chem.SDMolSupplier(str(sdf_path), removeHs=False) if m and m.GetNumConformers()]
    if not mols:
        raise ValueError(f"no ligand with conformer in {sdf_path}")
    return mols[0]
