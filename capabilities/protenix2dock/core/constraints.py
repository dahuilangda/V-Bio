"""TFG constraint builders for protenix2dock.

Computes covalent bond/angle distance constraints, steric-shell
pairs, bond contact pairs, and VDW shell constraints — all in the
official TFG PairwiseDistancePotential array format.
"""
from __future__ import annotations

import math
from itertools import combinations
from pathlib import Path
from typing import Any

import os
import numpy as np

from core.geometry_tables import (
    STD_AA_BONDS,
    ideal_bond_length,
    ideal_bond_length_residue,
    ideal_bond_angle,
)

# pre-split private aliases: the constraint body predates the
# geometry_tables extraction and keeps its original call names
_STD_AA_BONDS = STD_AA_BONDS
_ideal_bond_length_residue = ideal_bond_length_residue
_ideal_bond_angle = ideal_bond_angle

# Rigid-ring atom sets (W/H/Y/F + PRO): intra-ring pairs beyond true bonds
# become ANGLE-family bands pinned to the CCD ideal, keeping rings rigid
# through the state projection (see the emission-site note for why
# rings-only and not full-residue).
_RING_ATOM_SETS: dict[str, tuple[tuple[str, ...], ...]] = {
    "PHE": (("CG", "CD1", "CD2", "CE1", "CE2", "CZ"),),
    "TYR": (("CG", "CD1", "CD2", "CE1", "CE2", "CZ"),),
    "HIS": (("CG", "ND1", "CD2", "CE1", "NE2"),),
    "TRP": (
        ("CG", "CD1", "NE1", "CE2", "CD2"),            # pyrrole
        ("CD2", "CE2", "CE3", "CZ3", "CH2", "CZ2"),    # benzene
    ),
    "PRO": (("N", "CA", "CB", "CG", "CD"),),
}
_RESIDUE_PAIR_BAND = 0.12


def backbone_shape_pairs(
    info: dict[str, Any],
    mask: np.ndarray,
    free_entities: set[int] | None = None,
) -> list[tuple[list[int], float, float]]:
    """CA-CA distance pairs that preserve local backbone shape.

    Returns (rows, lower, upper) tuples. Three families:
    - i,i+1: virtual bond [3.5, 4.2] — omega dihedral reporter
    - i,i+2: [4.8, 7.2] — helix ~5.4, extended ~6.8, turn 5-6.5;
      prevents extreme compression without locking secondary structure
    - i,i+3: [4.5, 10.2] — helix ~5.0, extended ~9.8; same rationale

    The clash guard pushes individual atoms along pair axes; without
    these shape pairs the 200-step accumulation gradually unwinds helices
 spread 1.7-9.4 A vs crystal
    4.8-5.8). Wide bands allow both helix and strand but block the
    degenerate compressions/overextensions the min-norm solver finds
    when it only sees bonds+angles.
    """
    asym = np.asarray(info["asym"])
    res_id = np.asarray(info["res_id"])
    atom_names = [str(a) for a in np.asarray(info["atom_names"]).astype(str)]
    asym_to_entity = info["asym_to_entity"]
    rows_by_res: dict[tuple[int, int], int] = {}
    for i in range(len(asym)):
        if mask[i] <= 0 or atom_names[i] != "CA":
            continue
        if free_entities is not None and asym_to_entity[int(asym[i])] not in free_entities:
            continue
        rows_by_res.setdefault((int(asym[i]), int(res_id[i])), i)
    by_chain: dict[int, list[tuple[int, int]]] = {}
    for (a, r), row in rows_by_res.items():
        by_chain.setdefault(a, []).append((r, row))
    result: list[tuple[list[int], float, float]] = []
    for rows in by_chain.values():
        rows.sort()
        cas = [row for _, row in rows]
        for i in range(len(cas)):
            if i + 1 < len(cas):
                result.append(([cas[i], cas[i + 1]], 3.50, 4.20))
            if i + 2 < len(cas):
                result.append(([cas[i], cas[i + 2]], 4.80, 7.20))
            if i + 3 < len(cas):
                result.append(([cas[i], cas[i + 3]], 4.50, 10.20))
    return result


def compute_free_chain_tfg_constraints(
    info: dict[str, Any],
    coords: np.ndarray,
    mask: np.ndarray,
    free_entities: set[int] | None = None,
    bond_band: float = 0.15,
    cyclic_chains: bool = False,
    ring_pairs: list[list] | None = None,
) -> dict[str, np.ndarray] | None:
    """Bond and angle constraints for the free chains in official TFG format.

    Follows extract_pairwise_distance_bounds_from_mol semantics
    (protenix/data/core/geometry_featurizer.py): bonds from the residue
    chemical graph with ideal element-pair lengths, angles from bond
    triples (pairs sharing a bonded centre). The output feeds the TFG
    PairwiseDistancePotential, which projects x0 with the official
    angles-then-bonds ordering and a minimum-norm solve.

    Standard residues use _STD_AA_BONDS (chemistry, independent of input
    deformation); non-standard components use distance detection on the
    assembled coordinates (CCD conformer geometry is intact). The peptide
    C(i)-N(i+1) link and the terminal C-OXT bond always apply; with
    cyclic_chains=True the head-tail C(last)-N(first) amide bond is added
    as a TRUE bond (is_bond=1) with automatic flanking angles — the
    boltz2 `protein.cyclic` analogue for the protenix path.

    Returns the pairwise_distance_* arrays or None.
    """
    asym = info["asym"]
    res_id = info["res_id"]
    comp_of = np.asarray(info["comp_ids"])
    atom_names = [str(a) for a in np.asarray(info["atom_names"]).astype(str)]
    elements = np.char.upper(np.asarray(info["elements"]).astype(str))
    asym_to_entity = info["asym_to_entity"]

    # Aromatic side chains are hands-off here too (TFG runs): the projected channel's corrections on ring atoms are
    # what buckled F/Y six-rings into boats wherever TFG ran. Backbone
    # atoms of aromatic residues stay.
    _aro = {"PHE", "TYR", "TRP", "HIS"}

    def _is_aro_side(i: int) -> bool:
        if str(comp_of[i]).upper() not in _aro:
            return False
        return atom_names[i].lstrip("0123456789") not in (
            "N", "CA", "C", "O", "OXT")

    groups: dict[tuple[int, int], list[int]] = {}
    order: dict[int, list[int]] = {}
    for i in range(len(asym)):
        if mask[i] <= 0 or _is_aro_side(i):
            continue
        asym_v, rid = int(asym[i]), int(res_id[i])
        if free_entities is not None and asym_to_entity[asym_v] not in free_entities:
            continue
        groups.setdefault((asym_v, rid), []).append(i)
        seq = order.setdefault(asym_v, [])
        if rid not in seq:
            seq.append(rid)

    index: list[list[int]] = []
    upper: list[float] = []
    lower: list[float] = []
    is_bond: list[int] = []
    is_angle: list[int] = []

    for asym_v, rids in order.items():
        residues: list[tuple[int, str, dict[str, int]]] = []
        for rid in rids:
            rows = groups.get((asym_v, rid), [])
            comp = str(comp_of[rows[0]]).upper() if rows else ""
            residues.append((rid, comp, {atom_names[i]: i for i in rows}))

        chain_bonds: set[tuple[int, int]] = set()

        def bond(i: int, j: int, rest: float | None = None) -> None:
            key = (min(i, j), max(i, j))
            if key in chain_bonds:
                return
            chain_bonds.add(key)
            if rest is None:
                rest = _ideal_bond_length_residue(
                    comp, elements[i], elements[j],
                    atom_names[i], atom_names[j])
            index.append([i, j])
            upper.append(rest + bond_band)
            lower.append(max(rest - bond_band, 0.5))
            is_bond.append(1)
            is_angle.append(0)

        # peptide amide bond length (Engh & Huber): NOT the generic C-N
        # single bond 1.47 — using 1.47 for backbone links leaves the
        # projection band [1.32,1.62] which the denoiser fills to 1.5-1.6
        AMIDE = 1.33

        for rid, comp, atoms in residues:
            graph = _STD_AA_BONDS.get(comp)
            if graph is not None:
                for a, b in graph:
                    if a in atoms and b in atoms:
                        bond(atoms[a], atoms[b])
            else:
                names = sorted(atoms)
                for x in range(len(names)):
                    for y in range(x + 1, len(names)):
                        i, j = atoms[names[x]], atoms[names[y]]
                        d = float(np.linalg.norm(coords[i] - coords[j]))
                        has_S = 'S' in (elements[i], elements[j])
                        has_SE = 'SE' in (elements[i], elements[j])
                        cutoff = 2.2 if (has_S or has_SE) else 1.95
                        if d <= cutoff:
                            # CCD-conformer distance IS the ideal rest —
                            # using a generic element-pair default here
                            # crushes Se bonds (1.96 to 1.50) and stretches
                            # NCAA carbonyls (1.21 to 1.43)
                            bond(i, j, rest=float(np.clip(d, 0.8, 2.4)))
            if graph is not None:
                # alpha-amino acid backbone: graph confirms this is standard
                for a, b in (('N', 'CA'), ('CA', 'C'), ('C', 'O'), ('C', 'OXT')):
                    if a in atoms and b in atoms:
                        bond(atoms[a], atoms[b])
            if graph is not None and ring_pairs is not None:
                # Ring-rigidity pairs (intra-ring distances beyond true
                # bonds) pinned to CCD ideals. They ride the STATE-GUARD
                # PROJECTION sidecar ONLY: in the engine's shared feats the
                # energy channel's mu-ascent gets them too, and distance-
                # band ascent is handedness-blind -- the 3LNJ crystal
                # redock regressed to 4.45 A RMSD with peptide chirality
                # -1.36 (flipped CAs) with them present. Projection-only
                # keeps rings rigid through clash pushes (v14: rings OK,
                # backbone chirality intact) without touching the pose
                # quality path.
                _rings = _RING_ATOM_SETS.get(comp)
                if _rings:
                    from core.sidechain import _load_ccd_template
                    tpl = _load_ccd_template(comp) or {}
                    for ring in _rings:
                        members = [n for n in ring if n in atoms and n in tpl]
                        for x in range(len(members)):
                            for y in range(x + 1, len(members)):
                                a, b = members[x], members[y]
                                i, j = atoms[a], atoms[b]
                                key = (min(i, j), max(i, j))
                                if key in chain_bonds:
                                    continue
                                ideal = float(np.linalg.norm(tpl[a] - tpl[b]))
                                ring_pairs[0].append([i, j])
                                ring_pairs[1].append(ideal + _RESIDUE_PAIR_BAND)
                                ring_pairs[2].append(max(ideal - _RESIDUE_PAIR_BAND, 0.5))
            # beta-amino acids (BALA) and other non-standard topologies:
            # the fallback distance detection above already found the real
            # bonds — a by-name CA-C here would crush a 2.48 A true
            # separation into a 1.52 A constraint
        for pos in range(len(residues) - 1):
            c_atom = residues[pos][2].get('C')
            n_atom = residues[pos + 1][2].get('N')
            if c_atom is not None and n_atom is not None:
                bond(c_atom, n_atom, rest=AMIDE)
        # head-tail cyclization bond C(last)-N(first): a TRUE amide bond
        # (same 1.33 A ideal as consecutive peptide links), not a soft
        # upper bound — the old 2.2 A contact-band approximation left
        # the cyclization seam at 2.0-2.2 A and stretched the flanking
        # adjacent peptide bonds. Feeding it through the same
        # bond() path gets is_bond=1 treatment (x0 projection priority,
        # vdw clamping) AND automatic flanking angle constraints from
        # the neighbours pass below.
        if cyclic_chains and len(residues) >= 3:
            c_last = residues[-1][2].get('C')
            n_first = residues[0][2].get('N')
            if c_last is not None and n_first is not None:
                # true amide bond with the peptide rest length; flanking
                # angles auto-generate from the neighbours pass below
                bond(c_last, n_first, rest=AMIDE)
        # disulfide: CYS SG-SG within bonding distance is chemistry
        sg_atoms = [(rid, atoms['SG']) for rid, comp, atoms in residues
                    if comp == 'CYS' and 'SG' in atoms]
        for x in range(len(sg_atoms)):
            for y in range(x + 1, len(sg_atoms)):
                i, j = sg_atoms[x][1], sg_atoms[y][1]
                if float(np.linalg.norm(coords[i] - coords[j])) <= 2.15:
                    bond(i, j)

        # angle pairs: two bonds sharing a centre, official
        # build_angle_triples_from_bonds semantics
        neighbours: dict[int, set[int]] = {}
        for a, b in chain_bonds:
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
        seen: set[tuple[int, int]] = set(chain_bonds)
        for shared, partners in neighbours.items():
            for i, j in combinations(sorted(partners), 2):
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                # residue-specific bond lengths for the 1-3 distance; each
                # leg uses the residue owning that bond ('ALL' for legs
                # crossing a peptide link). The previous code passed the
                # LAST residue's comp for the whole chain, applying e.g.
                # TRP ring lengths to every angle in every residue.
                same_i = (int(asym[i]) == int(asym[shared])
                          and int(res_id[i]) == int(res_id[shared]))
                same_j = (int(asym[j]) == int(asym[shared])
                          and int(res_id[j]) == int(res_id[shared]))
                l1 = _ideal_bond_length_residue(
                    str(comp_of[i]).upper() if same_i else 'ALL',
                    elements[i], elements[shared],
                    atom_names[i], atom_names[shared])
                l2 = _ideal_bond_length_residue(
                    str(comp_of[j]).upper() if same_j else 'ALL',
                    elements[shared], elements[j],
                    atom_names[shared], atom_names[j])
                theta = _ideal_bond_angle(
                    atom_names[i], atom_names[shared], atom_names[j],
                    str(comp_of[shared]))
                rest = math.sqrt(max(
                    l1 * l1 + l2 * l2
                    - 2 * l1 * l2 * math.cos(math.radians(theta)), 0.25))
                index.append([key[0], key[1]])
                # ±0.15 A in 1-3 distance ≈ ±15° in bond angle — the
                # stock ±0.40 allowed 40° deviation (amide CA-C-N
                # at 127° vs 116° ideal across all samples). Amide angles
                # especially need tight bands to preserve planarity.
                is_backbone_angle = (
                    {atom_names[key[0]], atom_names[shared], atom_names[key[1]]}
                    & {'N', 'CA', 'C', 'O'})
                band = 0.12 if is_backbone_angle else 0.20
                upper.append(rest + band)
                lower.append(max(rest - band, 0.5))
                is_bond.append(0)
                is_angle.append(1)

    # intra-chain steric floor: non-bonded heavy-atom pairs from
    # residues >= 2 apart get a repulsive lower bound. Without this the
    # peptide collapses onto itself (ARG:NH2 at
    # 1.77 A). Only pairs NOT already covered
    # by a bond or angle constraint get the floor.
    for asym_v, rids in order.items():
        chain_atoms = [(i, atom_names[i]) for i in range(len(asym))
                       if int(asym[i]) == asym_v and elements[i] != 'H']
        res_of = {i: int(res_id[i]) for i in range(len(asym)) if int(asym[i]) == asym_v}
        covered = set()
        for pair in index:
            covered.add((min(pair[0], pair[1]), max(pair[0], pair[1])))
        for x in range(len(chain_atoms)):
            i, ni = chain_atoms[x]
            for y in range(x + 1, len(chain_atoms)):
                j, nj = chain_atoms[y]
                if abs(res_of[i] - res_of[j]) < 2:
                    continue  # same or adjacent: bonds/angles handle
                if (min(i, j), max(i, j)) in covered:
                    continue  # already constrained
                d = float(np.linalg.norm(coords[i] - coords[j]))
                if d < 3.2:
                    # add repulsive band only for currently-close pairs
                    index.append([i, j])
                    upper.append(1e3)
                    lower.append(3.0)  # heavy-atom non-bonded floor
                    is_bond.append(0)
                    is_angle.append(0)

    if not index:
        return None
    return {
        "pairwise_distance_index": np.asarray(index, dtype=np.int64).T,
        "pairwise_distance_upper_bound": np.asarray(upper, dtype=np.float32),
        "pairwise_distance_lower_bound": np.asarray(lower, dtype=np.float32),
        "pairwise_distance_is_bond": np.asarray(is_bond, dtype=np.float32),
        "pairwise_distance_is_angle": np.asarray(is_angle, dtype=np.float32),
    }



def compute_ligand_covalent_bands(
    ligand_rows: np.ndarray,
    ligand_mol: Any,
    band: float = 0.12,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Ligand covalent bonds from the RDKit graph as hard-anchor bands.

    Dock-mode counterpart of compute_covalent_bond_bands: the molecular
    graph gives the exact topology (no distance heuristics) and the input
    conformer the rest lengths. ligand_rows holds HEAVY-atom rows in order
    (the assembled table carries no hydrogens, and the align contract
    writes the conformer through those rows), so the molecule is stripped
    to heavy atoms before its graph is read; a molecule whose heavy-atom
    count does not match the rows has no valid row correspondence and is
    rejected (the alignment drops the ligand in that case as well).
    """
    if ligand_rows is None or len(ligand_rows) == 0 or ligand_mol is None:
        return None
    from rdkit import Chem

    mol = Chem.RemoveHs(ligand_mol) if any(
        a.GetAtomicNum() == 1 for a in ligand_mol.GetAtoms()) else ligand_mol
    if mol.GetNumAtoms() != len(ligand_rows):
        return None

    conf = mol.GetConformer()
    pairs: list[list[int]] = []
    rest: list[float] = []
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        p, q = conf.GetAtomPosition(i), conf.GetAtomPosition(j)
        d = float(np.linalg.norm(np.array(p) - np.array(q)))
        pairs.append([int(ligand_rows[i]), int(ligand_rows[j])])
        rest.append(d)
    if not pairs:
        return None
    index = np.asarray(pairs, dtype=np.int64)
    upper = np.asarray(rest, dtype=np.float32) + float(band)
    lower = np.maximum(np.asarray(rest, dtype=np.float32) - float(band), 0.5)
    return index, upper, lower



def _sasa_per_atom(coords: np.ndarray, elements: np.ndarray,
                   probe: float = 1.4) -> np.ndarray:
    """Shrake-Rupley SASA (A^2) per atom, heavy atoms only (H excluded by
    the caller). Neighbours via KD-tree; 92-point sphere."""
    from collections import defaultdict

    from scipy.spatial import cKDTree

    pts = _sasa_sphere_points(_SASA_POINTS)
    radii = np.array([_VDW_RADII_A.get(str(e).upper(), 1.70) + probe
                      for e in elements], dtype=np.float64)
    out = np.zeros(len(coords), dtype=np.float64)
    tree = cKDTree(coords)
    pairs = tree.query_pairs(2.0 * radii.max(), output_type="ndarray")
    nb: dict[int, list[int]] = defaultdict(list)
    for a, b in pairs:
        nb[a].append(b)
        nb[b].append(a)
    for i in range(len(coords)):
        surf = pts * radii[i] + coords[i]
        if not nb[i]:
            out[i] = 4.0 * np.pi * radii[i] ** 2
            continue
        nbr, rnb = coords[nb[i]], radii[nb[i]]
        d = np.linalg.norm(surf[:, None, :] - nbr[None, :, :], axis=-1)
        out[i] = (~(d < rnb[None, :]).any(axis=1)).sum() / _SASA_POINTS \
            * 4.0 * np.pi * radii[i] ** 2
    return out


def _sasa_sphere_points(n: int) -> np.ndarray:
    """Fibonacci sphere: near-uniform n points on S^2."""
    i = np.arange(n, dtype=np.float64) + 0.5
    phi = np.arccos(1.0 - 2.0 * i / n)
    theta = np.pi * (1.0 + 5.0 ** 0.5) * i
    return np.stack([np.cos(theta) * np.sin(phi),
                     np.sin(theta) * np.sin(phi),
                     np.cos(phi)], axis=1)







def compute_bond_contact_pairs(
    info: dict[str, Any],
    bond_pairs: list[tuple[tuple[str, int, str], tuple[str, int, str]]],
    upper: float,
    staged_to_auto: dict[str, str] | None = None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Covalent bond pairs (peptide atom, linker atom) as TFG contacts.

    Pair indices resolve through the assembled-atom table in `info`
    (returned by align_complex_init_coords) keyed by
    (chain letter, residue ordinal, atom name) — chain letters are the
    input.json auto ids ("A", "B", "C", ...) and residue ordinals are 1..n
    positions within the staged chain (sequence order, not author seqid;
    the production writer numbers the peptide 1..n). `staged_to_auto` maps
    the staged complex chain names (what the caller's bond_pairs
    references) to those auto letters; without it the pair chain letters
    are assumed to already be auto letters.
    The finite upper bound keeps the projection hard (the vendored TFG marks
    these pairs as the angle category so clash logic cannot drop them).
    """
    asym = info["asym"]
    res_id = info["res_id"]
    atom_names = info["atom_names"]
    asym_to_letter: dict[int, str] = {}
    for value in asym:
        v = int(value)
        if v not in asym_to_letter:
            asym_to_letter[v] = chr(ord("A") + len(asym_to_letter))
    atom_key_of: dict[tuple[str, int, str], int] = {}
    for i in range(len(asym)):
        atom_key_of[(asym_to_letter[int(asym[i])], int(res_id[i]), atom_names[i])] = i

    def _auto(ref: tuple[str, int, str]) -> tuple[str, int, str]:
        if staged_to_auto is None:
            return ref
        chain = staged_to_auto.get(ref[0], ref[0])
        return (chain, ref[1], ref[2])

    pairs: list[list[int]] = []
    for a1, a2 in bond_pairs:
        i1 = atom_key_of.get(_auto(a1))
        i2 = atom_key_of.get(_auto(a2))
        if i1 is None:
            print(f"[Warning] bond atom {a1} not found in the assembled atom table")
            continue
        if i2 is None:
            print(f"[Warning] bond atom {a2} not found in the assembled atom table")
            continue
        pairs.append([i1, i2])
    if not pairs:
        return None
    index = np.asarray(pairs, dtype=np.int64)
    elements = np.char.upper(np.asarray(info["elements"]).astype(str))
    upper_arr = np.empty(len(pairs), dtype=np.float32)
    for k, (i, j) in enumerate(pairs):
        pair_elems = {elements[i], elements[j]}
        if "S" in pair_elems or "SE" in pair_elems:
            # thioether/selenoether C-S ~1.81, C-Se ~1.96: the flat
            # upper over-compresses these by ~0.3 A every guided step
            upper_arr[k] = max(float(upper), 2.1)
        else:
            upper_arr[k] = float(upper)
    print(f"[Info] Bond TFG contacts prepared: {len(pairs)} pairs (upper={upper:.2f}A, "
          f"S/Se-aware: {(upper_arr > upper).sum()} pairs widened).")
    return index, upper_arr



def resolve_entity_atom_rows(
    info: dict[str, Any],
    entity_no: int,
    position: int,
    atom_names: list[str],
) -> list[int]:
    """Assembled atom rows for (entity ordinal position, atom names)."""
    rows = list(info["entity_rows"][entity_no])
    res_id = np.asarray(info["res_id"])
    atom_names_all = np.asarray(info["atom_names"]).astype(str)
    out = []
    for r in rows:
        if int(res_id[r]) == int(position):
            name = str(atom_names_all[r]).lstrip("0123456789")
            if name in atom_names or str(atom_names_all[r]) in atom_names:
                out.append(int(r))
    return out


def compute_vdw_shell_constraints(
    info: dict[str, Any],
    binder_rows: list[int] | np.ndarray,
    floor: float = 3.1,
    interchain_bond_pairs: list[tuple[int, int]] | None = None,
) -> dict[str, np.ndarray] | None:
    """Inter-chain VDW shell: every binder heavy atom keeps a lower-bound
    distance to every receptor heavy atom near the binding site.

    The shell feeds the ENERGY channel (flat-bottom penalty, k=1) and the
    FK resampling energy; the projection channel consumes these pairs only
    through the deep-burial guard (PairwiseDistancePotential projects
    clash pairs below PROTENIX_CLASH_PROJECT_FLOOR, 2.6 A -- catastrophic
    interpenetration only; the 2.6-3.4 A packing band belongs to the
    denoiser's interface prior, which the crystal bound-peptide envelope
    shows is physical down to 2.52 A contacts).

    Receptor set: every receptor heavy atom. upper=1e3 (inactive),
    is_bond=0, is_angle=0 — pure clash floor semantics; pairs covalently
    bonded across chains are excluded via ``interchain_bond_pairs``."""
    asym = np.asarray(info["asym"])
    elements = np.char.upper(np.asarray(info["elements"]).astype(str))
    atom_names = np.asarray(info["atom_names"]).astype(str)
    stripped = np.char.lstrip(atom_names, "0123456789")
    is_h = np.char.startswith(stripped, "H") | np.char.startswith(stripped, "D") \
        | (elements == "H")
    asym_to_letter: dict[int, str] = {}
    for v in asym:
        v = int(v)
        if v not in asym_to_letter:
            asym_to_letter[v] = chr(ord("A") + len(asym_to_letter))
    # the assembled table's first entity is the receptor (staging contract)
    rec_asym = {int(a) for a in np.unique(asym)} - {
        int(asym[r]) for r in binder_rows
    }
    rec_rows = np.array([
        i for i in range(len(asym))
        if int(asym[i]) in rec_asym and not is_h[i]
    ], dtype=np.int64)
    bnd_rows = np.array([int(r) for r in binder_rows if not is_h[r]], dtype=np.int64)
    if not len(rec_rows) or not len(bnd_rows):
        return None
    # atoms-first pairs ([2, N]) — the convention of the free-chain
    # constraints this merges with
    pair_index = np.stack([
        np.repeat(bnd_rows, len(rec_rows)),
        np.tile(rec_rows, len(bnd_rows)),
    ], axis=0)
    # pairs covalently bonded ACROSS chains must not be clash-constrained:
    # the shell would fight the bond (VinaStericPotential excludes the same
    # set); relevant whenever a linker/bicyclic chain bonds binder to receptor
    if interchain_bond_pairs:
        bonded = set()
        for a, b in interchain_bond_pairs:
            bonded.add((int(a), int(b)))
            bonded.add((int(b), int(a)))
        keep = np.array([
            (int(pair_index[0, c]), int(pair_index[1, c])) not in bonded
            for c in range(pair_index.shape[1])
        ])
        pair_index = pair_index[:, keep]
    n = pair_index.shape[1]
    return {
        "pairwise_distance_index": pair_index,
        "pairwise_distance_upper_bound": np.full(n, 1e3, dtype=np.float32),
        "pairwise_distance_lower_bound": np.full(n, float(floor), dtype=np.float32),
        "pairwise_distance_is_bond": np.zeros(n, dtype=np.int64),
        "pairwise_distance_is_angle": np.zeros(n, dtype=np.int64),
    }



def compute_ccd_bond_bands(
    info: dict[str, Any],
    coords: np.ndarray,
    mask: np.ndarray,
    free_entities: set[int] | None = None,
    band: float = 0.04,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Free-chain covalent bonds as tight rest-length bands.

    The CCD component templates define the rest length of every bond in a
    standard residue; the sampler moves atoms one at a time, so without
    this each steric adjustment can dislodge an atom off its residue
    instead of distributing through the bond network. The bands ride a damped-Jacobi projection applied
    after the guidance channels each step -- the official
    PairwiseDistancePotential ordering, angles then bonds last, so bond
    geometry wins the negotiation.

    Detection is all-pairs within each free chain with a chemistry filter
    (only element pairs that form bonds in biomolecular chemistry) so a
    deformed input's accidental close contacts are not latched onto. Rest
    length comes from the CCD template when the residue is standard, else
    from the detected distance. Returns (pairs [M,2], upper, lower) or None.
    """
    from core.sidechain import _load_ccd_template

    asym = np.asarray(info["asym"])
    asym_to_entity = info["asym_to_entity"]
    elements = np.char.upper(np.asarray(info["elements"]).astype(str))
    res_ids = np.asarray(info["res_id"])
    comp_ids = np.asarray(info["comp_ids"])
    atom_names = [str(a) for a in np.asarray(info["atom_names"]).astype(str)]

    plausible = {"CC", "CN", "CO", "CS", "SS", "CP", "OP", "NP", "SP"}
    # AROMATIC SIDE CHAINS ARE HANDS-OFF: with the TFG guidance off, the
    # raw model produces F/Y/W/H side chains with perfect CCD geometry
    # -- every distance projection we ran on
    # those atoms (bond bands, ring bands, junction angle bands, TFG's own
    # projected channel) only degraded them (boat rings to 0.53 A, OH 1.6 A
    # out of plane). The bands therefore cover the backbone and the
    # non-aromatic side chains only; aromatic side-chain atoms are excluded
    # from every constraint family.
    _aro_side = {"PHE", "TYR", "TRP", "HIS"}
    aro_side_rows: set[int] = set()
    for row in range(len(asym)):
        if mask[row] <= 0:
            continue
        if free_entities is not None and asym_to_entity[int(asym[row])] not in free_entities:
            continue
        if str(comp_ids[row]).upper() in _aro_side:
            nm = atom_names[row].lstrip("0123456789")
            if nm not in ("N", "CA", "C", "O", "OXT"):
                aro_side_rows.add(row)
    pairs: list[list[int]] = []
    rest: list[float] = []
    angle_marks: list[int] = []  # indices into pairs of junction ANGLE pairs
    # chi-dof groups for the steric-gradient torsion projection (engine
    # side): per aromatic residue, rows = [CA, CB, CG, <side-chain
    # atoms>]. The TFG mu-gradient on those atoms is re-expressed as a
    # pure chi1/chi2 rotation (least-squares onto the two torsion
    # velocity fields) so the steric push turns the side chain instead
    # of deforming the ring.
    _bb_keep = ("N", "CA", "C", "O", "OXT")
    aro_dof_rows: list[list[int]] = []
    _dof_by_res: dict[tuple[int, int], list[int]] = {}
    for row in range(len(asym)):
        if row not in aro_side_rows and not (
                str(comp_ids[row]).upper() in _aro_side
                and atom_names[row].lstrip("0123456789") in ("CA", "CB", "CG")
                and mask[row] > 0
                and (free_entities is None
                     or asym_to_entity[int(asym[row])] in free_entities)):
            continue
        if mask[row] <= 0:
            continue
        if free_entities is not None and asym_to_entity[int(asym[row])] not in free_entities:
            continue
        comp = str(comp_ids[row]).upper()
        if comp not in _aro_side:
            continue
        nm = atom_names[row].lstrip("0123456789")
        key = (int(asym[row]), int(res_ids[row]))
        if nm in ("CA", "CB", "CG"):
            _dof_by_res.setdefault(key, {})[nm] = row
        elif nm not in _bb_keep:
            _dof_by_res.setdefault(key, {}).setdefault("side", []).append(row)
    for key, d in _dof_by_res.items():
        if {"CA", "CB", "CG"} <= d.keys():
            aro_dof_rows.append([d["CA"], d["CB"], d["CG"]] + d.get("side", []))
    rows_by_asym: dict[int, list[int]] = {}
    for i in range(len(asym)):
        if mask[i] <= 0 or i in aro_side_rows:
            continue
        if free_entities is not None and asym_to_entity[int(asym[i])] not in free_entities:
            continue
        rows_by_asym.setdefault(int(asym[i]), []).append(i)

    for rows in rows_by_asym.values():
        pts = np.asarray(coords[rows], dtype=np.float64)
        elem = elements[rows]
        d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
        is_s = elem == "S"
        cutoff2 = np.where(is_s[:, None] | is_s[None, :], 2.15, 1.95) ** 2
        lo_e = np.where(elem[:, None] < elem[None, :], elem[:, None], elem[None, :])
        hi_e = np.where(elem[:, None] < elem[None, :], elem[None, :], elem[:, None])
        key = np.char.add(lo_e, hi_e)
        ok = np.isin(key, list(plausible))
        iu = np.triu_indices(len(rows), k=1)
        keep = (d2[iu] <= cutoff2[iu]) & ok[iu]
        for a, b in zip(iu[0][keep], iu[1][keep]):
            i, j = rows[a], rows[b]
            ideal = None
            if res_ids[i] == res_ids[j] and asym[i] == asym[j]:
                tpl = _load_ccd_template(str(comp_ids[i]).upper())
                if tpl:
                    ni = atom_names[i].lstrip("0123456789")
                    nj = atom_names[j].lstrip("0123456789")
                    if ni in tpl and nj in tpl:
                        ideal = float(np.linalg.norm(tpl[ni] - tpl[nj]))
            pairs.append([i, j])
            rest.append(ideal if ideal is not None
                        else float(np.linalg.norm(coords[i] - coords[j])))

    # Aromatic side-chain geometry. Distance bands (any width) CANNOT
    # enforce ring planarity: a buckled six-ring satisfies every pairwise
    # distance within the +-0.12 band while its atoms sit ~0.95 A off the
    # ring plane. Aromatic rings are therefore
    # carried as RIGID TEMPLATES: the sampler Kabsch-fits the CCD-ideal
    # ring onto the network's current ring each step and replaces the
    # internal coordinates -- placement and orientation stay the
    # network's decision, only the internal shape + planarity are pinned.
    # TRP's fused indole (both rings + the shared edge) is ONE rigid body;
    # TYR's phenol OH lies in the ring plane by sp2 chemistry and rides
    # the same body; PRO's pyrrolidine puckers physically and stays a
    # distance band.
    # Aromatic side-chain geometry. The sampler rebuilds each aromatic
    # side chain ANALYTICALLY every step (CCD template placed on the
    # network's N/CA/C frame, rotated to the network's chi1/chi2) -- the
    # AF3/protenix construction principle: side-chain internal geometry
    # comes from the CCD component template, never from pairwise
    # distances. A rebuilt side chain satisfies every bond AND angle
    # band exactly (the intersection point itself -- no Jacobi
    # negotiation, which permits boat-shaped rings).
    # chi1/chi2 stay fully network-owned; PRO's pyrrolidine puckers
    # physically and stays a distance band.
    _rigid_rings = {
        "PHE": (("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ"),),
        "TYR": (("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"),),
        "HIS": (("CB", "CG", "ND1", "CD2", "CE1", "NE2"),),
        "TRP": (("CB", "CG", "CD1", "NE1", "CE2", "CD2",
                 "CE3", "CZ3", "CH2", "CZ2"),),
    }
    _ring_sets = {
        "PRO": (("N", "CA", "CB", "CG", "CD"),),
    }
    bonded = {(min(i, j), max(i, j)) for i, j in pairs}
    name_by_res: dict[tuple[int, int], dict[str, int]] = {}
    for row in range(len(asym)):
        if free_entities is not None and asym_to_entity[int(asym[row])] not in free_entities:
            continue
        nm = atom_names[row].lstrip("0123456789")
        name_by_res.setdefault(
            (int(asym[row]), int(res_ids[row])), {})[nm] = row
    rigid_rows: list[list[int]] = []
    rigid_coords: list[np.ndarray] = []
    rigid_bb_rows: list[list[int]] = []
    rigid_bb_coords: list[np.ndarray] = []
    # Rigid templates + junction angle bands exist ONLY for the explicit
    # rebuild mode (PROTENIX_AROMATIC_REBUILD=1). The default route hands
    # aromatic side chains to the network entirely (see aro_side_rows) --
    # constraints on those atoms degrade the network output.
    _rebuild_mode = os.environ.get(
        "PROTENIX_AROMATIC_REBUILD", "").strip() in ("1", "true")
    for (a, r), rnames in name_by_res.items():
        comp = str(comp_ids[np.where(
            (asym == a) & (res_ids == r))[0][0]]).upper()
        tpl = _load_ccd_template(comp) or {}
        # rigid aromatic templates supersede any ring bands for this
        # residue -- exact internal geometry AND planarity in one body
        body_names: set[str] = set()
        for group in _rigid_rings.get(comp, ()) if _rebuild_mode else ():
            members = [n for n in group if n in rnames and n in tpl]
            if len(members) < 4:
                continue
            # analytic rebuild frame: the network's N/CA/C anchors the
            # template; chi1/chi2 are read from the network's current
            # side chain each step (generator side)
            bb = [rnames[n] for n in ("N", "CA", "C") if n in rnames]
            if len(bb) != 3 or not all(n in tpl for n in ("N", "CA", "C")):
                continue
            rigid_rows.append([rnames[n] for n in members])
            rigid_coords.append(
                np.asarray([tpl[n] for n in members], dtype=np.float32))
            rigid_bb_rows.append(bb)
            rigid_bb_coords.append(np.asarray(
                [tpl[n] for n in ("N", "CA", "C")], dtype=np.float32))
            body_names.update(members)
        # Junction ANGLE bands across the rigid-body boundary: the rigid
        # replacement fits the template onto the network's ring placement
        # (total-RMSD), which drags the anchor atom CG and leaves the
        # CA-CB-CG / CB-CG-CD1 angles free to wander. Pin every
        # body/non-body atom pair at template 1-3 distance (<= 2.8 A):
        # these distances are INVARIANT to both chi1 (rotation about
        # CA-CB) and chi2 (rotation about CB-CG), so the junction angles
        # lock while both torsions stay fully network-owned. Also the
        # backbone N-CB pair (angle N-CA-CB): without it the model's raw
        # backbone-angle noise shows at exactly these residues.
        # Angle pairs ride the regular bond width (band, +-0.04 A =
        # ~+-2.5 deg) -- the ring pair marker (negative) is reserved for
        # the +-0.12 planarisation width.
        if body_names and _rebuild_mode:
            res_heavy = [n for n in rnames if n in tpl]
            for xname in res_heavy:
                if xname in body_names:
                    continue          # x is outside, y inside -- each
                for yname in res_heavy:   # boundary pair added once
                    if yname not in body_names:
                        if (xname, yname) != ("N", "CB"):
                            continue    # outside-outside: only N-CB
                    rx, ry = rnames[xname], rnames[yname]
                    if (min(rx, ry), max(rx, ry)) in bonded:
                        continue
                    ideal13 = abs(float(np.linalg.norm(
                        tpl[xname] - tpl[yname])))
                    if ideal13 <= 2.8:
                        pairs.append([rx, ry])
                        rest.append(ideal13)
                        angle_marks.append(len(pairs) - 1)
        for ring in _ring_sets.get(comp, ()):
            members = [n for n in ring if n in rnames and n in tpl]
            if len(members) < 4:
                continue
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    ni, nj = members[x], members[y]
                    ri, rj = rnames[ni], rnames[nj]
                    if (min(ri, rj), max(ri, rj)) in bonded:
                        continue
                    pairs.append([ri, rj])
                    rest.append(-abs(float(np.linalg.norm(
                        tpl[ni] - tpl[nj]))))   # negative = ring pair marker

    # Inter-chain clash shell (separate return family so the sampler can
    # project it BEFORE the chemistry bands -- the official angles, clash,
    # bonds ordering; chemistry wins the negotiation).
    receptor_rows = [
        row for row in range(len(asym))
        if mask[row] > 0 and elements[row] != "H"
        and not (free_entities is None
                 or asym_to_entity[int(asym[row])] in free_entities)
    ]
    free_rows = [
        row for row in range(len(asym))
        if mask[row] > 0 and elements[row] != "H"
        and (free_entities is None
             or asym_to_entity[int(asym[row])] in free_entities)
    ]
    clash_pairs: list[list[int]] = []
    clash_floors: list[float] = []
    if receptor_rows and free_rows:
        # Full receptor x free-chain cross product, not a distance-filtered
        # subset: blind inpainting discards the staged pose, so the chain
        # may settle far from where the staging placed it and a
        # staged-neighbourhood pair set would not cover the actual site.
        for a in receptor_rows:
            for b in free_rows:
                clash_pairs.append([int(a), int(b)])
                clash_floors.append(2.6)

    # Intra-chain deep-burial guard: the inter-chain shell above and the
    # CCD bond bands cover everything EXCEPT non-bonded pairs inside the
    # free chain — observed as an LYS NZ jammed 1.5 A into an i+2 LEU
    # side chain. Real geometry keeps |i-j|>=2 heavy-atom pairs above
    # ~2.4 A (adjacent-residue pairs are valence-close and belong to the
    # bond bands; SG-SG can be a 2.05 A disulfide and is excluded).
    _res_id_arr = np.asarray(info["res_id"])
    _asym_arr = np.asarray(info["asym"])
    _elem_arr = np.char.upper(np.asarray(info["elements"]).astype(str))
    for _ia, a in enumerate(free_rows):
        for _ib, b in enumerate(free_rows):
            if _ib <= _ia:
                continue
            if _asym_arr[a] != _asym_arr[b]:
                continue
            if abs(int(_res_id_arr[a]) - int(_res_id_arr[b])) < 2:
                continue
            if _elem_arr[a] == "S" and _elem_arr[b] == "S":
                continue
            clash_pairs.append([int(a), int(b)])
            clash_floors.append(2.4)

    if not pairs:
        return None
    # Drop intra-template pairs from the DISTANCE bands: a pair with both
    # atoms inside the same rigid aromatic template is already exact (the
    # sampler re-places that template every step), and keeping it as a
    # band only hands the Jacobi sweep a drift DOF: the junction angle
    # corrections propagate through those bands and
    # buckle the six-rings into BOAT conformations (CG +0.39 A toward CB,
    # CD1/CD2 -0.25 A behind, every ring bond still inside its +-0.04
    # band: a boat satisfies all 1-2 distances). The fused 9-atom TRP
    # template resisted only through its stiffer bond network; removing
    # the intra-template pairs removes the propagation medium. Cross-
    # boundary pairs (CB-CG bond, CA-CG / CB-CD1 / CB-CD2 angles) stay.
    keep = np.ones(len(pairs), dtype=bool)
    if rigid_rows:
        row_sets = [set(r) for r in rigid_rows]
        for pi, (a, b) in enumerate(pairs):
            for rs in row_sets:
                if a in rs and b in rs:
                    keep[pi] = False
                    break
    angle_flags_all = np.zeros(len(pairs), dtype=bool)
    angle_flags_all[angle_marks] = True
    pairs = [p for p, k in zip(pairs, keep) if k]
    rest = [v for v, k in zip(rest, keep) if k]
    index = np.asarray(pairs, dtype=np.int64)
    rest_arr = np.asarray(rest, dtype=np.float32)
    angle_pair_flags = angle_flags_all[keep]
    widths = np.where(rest_arr < 0, np.float32(0.12), np.float32(band))
    rest_arr = np.abs(rest_arr)
    upper = rest_arr + widths
    lower = np.maximum(rest_arr - widths, 0.5)
    clash_index = (np.asarray(clash_pairs, dtype=np.int64)
                   if clash_pairs else None)
    # Guard floor, not a packing shell: 2.6 A matches the TFG projection
    # channel's deep-burial guard, so the emitted state and the denoiser's
    # clean estimate obey the same bound. The 2.6-3.4 A packing band stays
    # with the denoiser's interface prior — projecting the full 3.1 A
    # shell every step flattened binders into one-atom-thick pancakes.
    clash_lower = (np.asarray(clash_floors, dtype=np.float32)
                   if clash_pairs else None)
    # rigid aromatic templates as padded arrays: ring_rows [R, K] (-1 pad),
    # ring_coords [R, K, 3]; None when the free chains carry no aromatics
    if rigid_rows:
        k = max(len(rows) for rows in rigid_rows)
        rr = np.full((len(rigid_rows), k), -1, dtype=np.int64)
        rc = np.zeros((len(rigid_rows), k, 3), dtype=np.float32)
        for ri, (rows, txyz) in enumerate(zip(rigid_rows, rigid_coords)):
            rr[ri, :len(rows)] = rows
            rc[ri, :len(txyz)] = txyz
        rigid = (rr, rc, np.asarray(rigid_bb_rows, dtype=np.int64),
                 np.asarray(rigid_bb_coords, dtype=np.float32))
    else:
        rigid = None
    if aro_dof_rows:
        kk = max(len(r) for r in aro_dof_rows)
        ad = np.full((len(aro_dof_rows), kk), -1, dtype=np.int64)
        for ai, rows in enumerate(aro_dof_rows):
            ad[ai, :len(rows)] = rows
        aro_dof = ad
    else:
        aro_dof = None
    return index, upper, lower, clash_index, clash_lower, rigid, aro_dof
