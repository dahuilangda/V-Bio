"""Utilities for reference-guided partial docking setup.

This module prepares an aligned target ligand from a target SMILES and a
reference ligand pose, then returns common-scaffold atom indices that can be
used with `task.transform.fix_some.atom` in docking mode.
"""

import os
from typing import Dict, List

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem import rdFMCS
from rdkit.Chem import rdMolAlign


def _load_reference_mol(path: str) -> Chem.Mol:
    """Load a single reference molecule with 3D coordinates."""
    if path.endswith('.sdf'):
        supplier = Chem.SDMolSupplier(path, removeHs=False)
        mol = None
        for item in supplier:
            if item is not None:
                mol = item
                break
    elif path.endswith('.pdb'):
        mol = Chem.MolFromPDBFile(path, removeHs=False)
    else:
        raise ValueError(f'Unsupported reference ligand format: {path}')

    if mol is None:
        raise ValueError(f'Failed to read reference ligand: {path}')
    mol = Chem.RemoveHs(mol)
    if mol.GetNumConformers() == 0:
        raise ValueError('Reference ligand has no 3D conformer.')
    return mol


def _build_target_mol_from_smiles(target_smiles: str, random_seed: int = 2024) -> Chem.Mol:
    """Build a 3D target molecule from SMILES."""
    target = Chem.MolFromSmiles(target_smiles)
    if target is None:
        raise ValueError(f'Invalid target SMILES: {target_smiles}')

    target = Chem.AddHs(target)
    params = AllChem.ETKDGv3()
    params.randomSeed = int(random_seed)
    status = AllChem.EmbedMolecule(target, params)
    if status != 0:
        status = AllChem.EmbedMolecule(target, useRandomCoords=True, randomSeed=int(random_seed))
    if status != 0:
        raise ValueError('Failed to generate 3D conformer for target SMILES.')

    try:
        AllChem.UFFOptimizeMolecule(target, maxIters=500)
    except Exception:
        pass

    target = Chem.RemoveHs(target)
    return target


def _find_best_mcs_alignment(
    ref_mol: Chem.Mol,
    target_mol: Chem.Mol,
    min_common_atoms: int = 6,
    timeout: int = 20,
    max_substruct_matches: int = 128,
) -> Dict:
    """Find MCS and return best atom mapping/aligned target among all matches."""
    mcs_params = rdFMCS.MCSParameters()
    mcs_params.Timeout = int(timeout)
    mcs_params.AtomTyper = rdFMCS.AtomCompare.CompareElements
    mcs_params.BondTyper = rdFMCS.BondCompare.CompareOrder
    mcs_params.AtomCompareParameters.MatchValences = True
    mcs_params.AtomCompareParameters.MatchChiralTag = False
    mcs_params.AtomCompareParameters.RingMatchesRingOnly = True
    mcs_params.AtomCompareParameters.CompleteRingsOnly = True
    mcs_params.BondCompareParameters.RingMatchesRingOnly = True
    mcs_params.BondCompareParameters.CompleteRingsOnly = True

    mcs_result = rdFMCS.FindMCS([ref_mol, target_mol], mcs_params)
    if mcs_result.canceled:
        raise ValueError('MCS search timed out. Increase timeout or simplify molecules.')
    if mcs_result.numAtoms < int(min_common_atoms):
        raise ValueError(
            f'MCS too small ({mcs_result.numAtoms} atoms). '
            f'Require at least {min_common_atoms} common atoms.'
        )

    mcs_query = Chem.MolFromSmarts(mcs_result.smartsString)
    if mcs_query is None:
        raise ValueError('Failed to parse MCS SMARTS.')

    ref_matches = ref_mol.GetSubstructMatches(
        mcs_query,
        uniquify=True,
        maxMatches=int(max_substruct_matches),
    )
    target_matches = target_mol.GetSubstructMatches(
        mcs_query,
        uniquify=True,
        maxMatches=int(max_substruct_matches),
    )
    if len(ref_matches) == 0 or len(target_matches) == 0:
        raise ValueError('No substructure match found for MCS on reference/target.')

    best = None
    best_rmsd = None
    for t_match in target_matches:
        for r_match in ref_matches:
            atom_map = list(zip(t_match, r_match))
            probe = Chem.Mol(target_mol)
            rmsd = rdMolAlign.AlignMol(probe, ref_mol, atomMap=atom_map)
            if (best is None) or (rmsd < best_rmsd):
                best = {
                    'aligned_target': probe,
                    'target_match': list(map(int, t_match)),
                    'ref_match': list(map(int, r_match)),
                    'atom_map': [(int(a), int(b)) for a, b in atom_map],
                }
                best_rmsd = float(rmsd)

    if best is None:
        raise ValueError('Failed to align target to reference by MCS.')

    # Make scaffold atoms exactly match reference coordinates (for hard fixing).
    conf_ref = ref_mol.GetConformer()
    conf_tgt = best['aligned_target'].GetConformer()
    for idx_t, idx_r in best['atom_map']:
        conf_tgt.SetAtomPosition(idx_t, conf_ref.GetAtomPosition(idx_r))

    best.update(
        {
            'mcs_smarts': mcs_result.smartsString,
            'mcs_num_atoms': int(mcs_result.numAtoms),
            'alignment_rmsd': float(best_rmsd),
        }
    )
    return best


def build_aligned_ligand_from_reference(
    reference_ligand_path: str,
    target_smiles: str,
    output_sdf_path: str,
    min_common_atoms: int = 6,
    mcs_timeout: int = 20,
    random_seed: int = 2024,
    max_substruct_matches: int = 128,
) -> Dict:
    """Prepare aligned target ligand and fixed-atom indices for partial docking.

    Returns a dictionary with:
      - output_ligand_path
      - fixed_atom_indices
      - mcs_smarts
      - mcs_num_atoms
      - alignment_rmsd
      - reference_center (xyz list)
      - target_smiles_canonical
    """
    ref_mol = _load_reference_mol(reference_ligand_path)
    target_mol = _build_target_mol_from_smiles(target_smiles, random_seed=random_seed)

    alignment = _find_best_mcs_alignment(
        ref_mol=ref_mol,
        target_mol=target_mol,
        min_common_atoms=min_common_atoms,
        timeout=mcs_timeout,
        max_substruct_matches=max_substruct_matches,
    )
    aligned_target = alignment['aligned_target']

    outdir = os.path.dirname(output_sdf_path)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    Chem.MolToMolFile(aligned_target, output_sdf_path)

    ref_conf = ref_mol.GetConformer()
    ref_pos = ref_conf.GetPositions()
    ref_center = ref_pos.mean(axis=0).tolist()

    fixed_atom_indices = sorted(set(alignment['target_match']))
    return {
        'output_ligand_path': output_sdf_path,
        'fixed_atom_indices': fixed_atom_indices,
        'mcs_smarts': alignment['mcs_smarts'],
        'mcs_num_atoms': alignment['mcs_num_atoms'],
        'alignment_rmsd': alignment['alignment_rmsd'],
        'reference_center': [float(v) for v in ref_center],
        'target_smiles_canonical': Chem.MolToSmiles(Chem.MolFromSmiles(target_smiles)),
    }
