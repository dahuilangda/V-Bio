"""Interface analysis for protein-ligand complexes."""
from __future__ import annotations

from io import StringIO
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from Bio.PDB import MMCIFParser, PDBParser
from Bio.PDB.SASA import ShrakeRupley

PROBE_RADIUS = 1.4
SASA_POINTS = 960
BURIAL_CUTOFF = 1.0
HBOND_CUTOFF = 3.5
SALT_BRIDGE_CUTOFF = 4.0
HYDROPHOBIC_CUTOFF = 4.5
MIN_SIDE_RESIDUES = 3

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "AIB": "A", "NLE": "L", "HYP": "P",
}
POLAR_ELEMENTS = {"N", "O", "S"}
POSITIVE_RESIDUES = {"ARG", "LYS", "HIS"}
NEGATIVE_RESIDUES = {"ASP", "GLU"}
POSITIVE_SIDECHAIN = {"NZ", "NH1", "NH2", "ND1", "NE2"}
NEGATIVE_SIDECHAIN = {"OD1", "OD2", "OE1", "OE2"}
HIS_RING = {"ND1", "NE2"}
HYDROPHOBIC_RESIDUES = {"ALA", "VAL", "LEU", "ILE", "MET", "PHE", "TRP", "PRO"}


def _side_atoms(model, chains):
    """Heavy atoms of standard residues in the given chains.

    Returns [(atom, residue, chain_id)] for the matching chains only.
    """
    out = []
    for chain in model:
        if chain.id not in chains:
            continue
        for residue in chain:
            if residue.id[0] != " ":
                continue
            for atom in residue:
                if atom.element == "H":
                    continue
                out.append((atom, residue, chain.id))
    return out


def analyze_interface(cif_text: str, fmt: str,
                      side_one_chains: Sequence[str],
                      side_two_chains: Sequence[str]) -> Dict[str, Any]:
    """Interface metrics between two chain groups of one complex.

    Returns dict with shape_complementarity, buried_sasa per side,
    interface residues, hbond/salt-bridge/hydrophobic contact counts.
    """
    structure = _parse(cif_text, fmt)
    model = structure[0]

    all_chains = {c.id for c in model}
    side_one, side_two = set(side_one_chains), set(side_two_chains)
    unknown = (side_one | side_two) - all_chains
    if unknown:
        raise ValueError(f"missing chains in structure: {sorted(unknown)}")
    if side_one & side_two:
        raise ValueError("interface sides overlap")

    side_one_atoms = _side_atoms(model, side_one)
    side_two_atoms = _side_atoms(model, side_two)
    if not side_one_atoms or not side_two_atoms:
        raise ValueError("one side has no heavy atoms")

    # Shape complementarity
    from shape_complementarity import compute_sc
    sc = compute_sc(
        [tuple(atom.coord.tolist()) for atom, _res, _chain in side_one_atoms],
        [atom.name for atom, _res, _chain in side_one_atoms],
        [_res.resname.strip() for atom, _res, _chain in side_one_atoms],
        [tuple(atom.coord.tolist()) for atom, _res, _chain in side_two_atoms],
        [atom.name for atom, _res, _chain in side_two_atoms],
        [_res.resname.strip() for atom, _res, _chain in side_two_atoms],
    )

    # Cross-side contact pairs
    hbonds = []
    salt_bridges = []
    hydrophobic = []
    for atom_a, res_a, chain_a in side_one_atoms:
        for atom_b, res_b, chain_b in side_two_atoms:
            d = float(np.linalg.norm(atom_a.coord - atom_b.coord))
            if d > max(3.5, 4.0, 4.5):
                continue
            is_polar = atom_a.element in POLAR_ELEMENTS and atom_b.element in POLAR_ELEMENTS
            if is_polar and d <= 3.5:
                hbonds.append({
                    "a": f"{chain_a}/{res_a.resname}{res_a.id[1]}/{atom_a.name}",
                    "b": f"{chain_b}/{res_b.resname}{res_b.id[1]}/{atom_b.name}",
                    "dist": round(d, 2),
                })
            if _is_hydrophobic_residue_pair(res_a, res_b) and d <= 4.5:
                hydrophobic.append({
                    "a": f"{chain_a}/{res_a.resname}{res_a.id[1]}",
                    "b": f"{chain_b}/{res_b.resname}{res_b.id[1]}",
                    "dist": round(d, 2),
                })
            if _is_salt_bridge(atom_a, res_a, atom_b, res_b) and d <= 4.0:
                salt_bridges.append({
                    "a": f"{chain_a}/{res_a.resname}{res_a.id[1]}/{atom_a.name}",
                    "b": f"{chain_b}/{res_b.resname}{res_b.id[1]}/{atom_b.name}",
                    "dist": round(d, 2),
                })

    # Interface residues (contact-based, <=5A)
    iface_res = set()
    for atom_a, res_a, chain_a in side_one_atoms:
        for atom_b, res_b, chain_b in side_two_atoms:
            if float(np.linalg.norm(atom_a.coord - atom_b.coord)) <= 5.0:
                iface_res.add((chain_a, res_a.id[1], res_a.resname))
                iface_res.add((chain_b, res_b.id[1], res_b.resname))
                break

    return {
        "shape_complementarity": round(sc.sc, 4),
        "median_distance": round(sc.median_distance, 2),
        "n_hbonds": len(hbonds),
        "n_salt_bridges": len(salt_bridges),
        "n_hydrophobic_contacts": len(hydrophobic),
        "n_interface_residues": len({(c, n) for c, n, _r in iface_res}),
        "interface_residues": [
            {"chain": c, "res_num": n, "res_name": r}
            for c, n, r in sorted(iface_res)
        ],
        "hbonds": hbonds[:20],
        "salt_bridges": salt_bridges[:10],
        "hydrophobic_contacts": hydrophobic[:20],
    }


def _is_salt_bridge(atom_a, res_a, atom_b, res_b) -> bool:
    pa = _sidechain_polarity(atom_a, res_a)
    pb = _sidechain_polarity(atom_b, res_b)
    return pa * pb == -1


def _sidechain_polarity(atom, residue) -> int:
    res = residue.resname.strip().upper()
    name = atom.name.strip().upper()
    if res in POSITIVE_RESIDUES and name in POSITIVE_SIDECHAIN:
        return 1
    if res in NEGATIVE_RESIDUES and name in NEGATIVE_SIDECHAIN:
        return -1
    return 0


def _is_hydrophobic_residue_pair(res_a, res_b) -> bool:
    return (res_a.resname.strip().upper() in HYDROPHOBIC_RESIDUES
            and res_b.resname.strip().upper() in HYDROPHOBIC_RESIDUES)


def _parse(cif_text: str, fmt: str):
    parser = PDBParser(QUIET=True) if fmt == "pdb" else MMCIFParser(QUIET=True)
    return parser.get_structure("cx", StringIO(cif_text))


def _heavy_atoms(model, chains: Set[str]):
    """(atom, residue, chain_id) triples of standard-residue heavy atoms."""
    for chain in model:
        if chain.id not in chains:
            continue
        for residue in chain:
            if residue.id[0] != " ":
                continue
            for atom in residue:
                if atom.element != "H":
                    yield atom, residue, chain
