"""Covalent geometry reference tables (PDB CCD / Engh & Huber).

Single source of truth for residue bond graphs, ideal bond lengths,
and ring bond angles. All constraint builders import from here.
"""
from __future__ import annotations

import math

# Standard amino acid side-chain bond graphs (heavy atoms, CA-CB included;
# backbone N-CA / CA-C / C-O added separately by the constraint builder).
STD_AA_BONDS = {
    'ALA': [('CA', 'CB')],
    'ARG': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'NE'),
            ('NE', 'CZ'), ('CZ', 'NH1'), ('CZ', 'NH2')],
    'ASN': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'OD1'), ('CG', 'ND2')],
    'ASP': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'OD1'), ('CG', 'OD2')],
    'CYS': [('CA', 'CB'), ('CB', 'SG')],
    'GLN': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'OE1'),
            ('CD', 'NE2')],
    'GLU': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'OE1'),
            ('CD', 'OE2')],
    'GLY': [],
    'HIS': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'ND1'), ('CG', 'CD2'),
            ('ND1', 'CE1'), ('CD2', 'NE2'), ('CE1', 'NE2')],
    'ILE': [('CA', 'CB'), ('CB', 'CG1'), ('CB', 'CG2'), ('CG1', 'CD1')],
    'LEU': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2')],
    'LYS': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'CE'),
            ('CE', 'NZ')],
    'MET': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'SD'), ('SD', 'CE')],
    'PHE': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'CE1'), ('CD2', 'CE2'), ('CE1', 'CZ'), ('CE2', 'CZ'),
            ('CZ', 'CG')],
    'PRO': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'N')],
    'SER': [('CA', 'CB'), ('CB', 'OG')],
    'THR': [('CA', 'CB'), ('CB', 'OG1'), ('CB', 'CG2')],
    'TRP': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'NE1'), ('CD2', 'CE2'), ('CD2', 'CE3'), ('NE1', 'CE2'),
            ('CE2', 'CZ2'), ('CE3', 'CZ3'), ('CZ2', 'CH2'), ('CH2', 'CZ3')],
    'TYR': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'CE1'), ('CD2', 'CE2'), ('CE1', 'CZ'), ('CE2', 'CZ'),
            ('CZ', 'CG'), ('CZ', 'OH')],
    'VAL': [('CA', 'CB'), ('CB', 'CG1'), ('CB', 'CG2')],
}

# Element-pair bond lengths (generic fallback when no residue-specific
# value exists). Side-chain carbonyls and aromatic bonds must use
# RESIDUE_BOND below — the element-pair table cannot distinguish them.
IDEAL_BOND = {
    ('C', 'C'): 1.52, ('C', 'N'): 1.47, ('C', 'O'): 1.43,
    ('C', 'SE'): 1.96, ('C', 'S'): 1.81, ('S', 'S'): 2.05,
    ('N', 'O'): 1.40, ('C', 'P'): 1.84, ('O', 'P'): 1.60,
    ('N', 'P'): 1.70, ('S', 'P'): 2.05,
}

# Residue-specific bond length overrides keyed (residue, {atom_a, atom_b}).
# Entries under 'ALL' apply to every residue.
RESIDUE_BOND: dict[tuple[str, frozenset], float] = {}

def _register(res: str, pairs: list[tuple[str, str, float]]) -> None:
    for a, b, length in pairs:
        RESIDUE_BOND[(res, frozenset((a, b)))] = length

_register('ALL', [
    ('C', 'N', 1.33),    # peptide amide
    ('C', 'O', 1.23),    # carbonyl
    ('N', 'CA', 1.46),
    ('C', 'OXT', 1.31),  # C-terminal
])
_register('ASP', [('CB', 'CG', 1.52), ('CG', 'OD1', 1.25), ('CG', 'OD2', 1.25)])
_register('ASN', [('CB', 'CG', 1.52), ('CG', 'OD1', 1.23), ('CG', 'ND2', 1.33)])
_register('GLU', [('CG', 'CD', 1.52), ('CD', 'OE1', 1.25), ('CD', 'OE2', 1.25)])
_register('GLN', [('CG', 'CD', 1.52), ('CD', 'OE1', 1.23), ('CD', 'NE2', 1.33)])
_register('PHE', [
    ('CB', 'CG', 1.51), ('CG', 'CD1', 1.39), ('CG', 'CD2', 1.39),
    ('CD1', 'CE1', 1.39), ('CD2', 'CE2', 1.39), ('CE1', 'CZ', 1.39),
    ('CE2', 'CZ', 1.39), ('CZ', 'CG', 1.39)])
_register('TYR', [
    ('CB', 'CG', 1.51), ('CG', 'CD1', 1.39), ('CG', 'CD2', 1.39),
    ('CD1', 'CE1', 1.39), ('CD2', 'CE2', 1.39), ('CE1', 'CZ', 1.39),
    ('CE2', 'CZ', 1.39), ('CZ', 'CG', 1.39), ('CZ', 'OH', 1.38)])
_register('TRP', [
    ('CB', 'CG', 1.50), ('CG', 'CD1', 1.36), ('CD1', 'NE1', 1.37),
    ('NE1', 'CE2', 1.38), ('CE2', 'CD2', 1.43), ('CD2', 'CG', 1.44),
    ('CD2', 'CE3', 1.40), ('CE3', 'CZ3', 1.39), ('CZ3', 'CH2', 1.39),
    ('CH2', 'CZ2', 1.39), ('CZ2', 'CE2', 1.40), ('CZ2', 'CG', 1.42)])
_register('HIS', [
    ('CB', 'CG', 1.51), ('CG', 'ND1', 1.38), ('CG', 'CD2', 1.35),
    ('ND1', 'CE1', 1.38), ('CE1', 'NE2', 1.38), ('NE2', 'CD2', 1.39)])

# Ring bond angles keyed (residue, centre_atom, {neighbour_a, neighbour_b}).
# Residue keying is required: the same atom name can be an sp2 aromatic
# carbon in one residue and a fused-ring junction in another.
RING_ANGLES: dict[tuple[str, str, frozenset], float] = {}

def _ring(res: str, centre: str, a: str, b: str, deg: float) -> None:
    RING_ANGLES[(res, centre, frozenset((a, b)))] = deg

# phenylalanine / tyrosine 6-ring
for _r in ('PHE', 'TYR'):
    _ring(_r, 'CG', 'CD1', 'CD2', 120.0)
    _ring(_r, 'CG', 'CB', 'CD1', 120.0)
    _ring(_r, 'CG', 'CB', 'CD2', 120.0)
    _ring(_r, 'CD1', 'CG', 'CE1', 120.0)
    _ring(_r, 'CD2', 'CG', 'CE2', 120.0)
    _ring(_r, 'CE1', 'CD1', 'CZ', 120.0)
    _ring(_r, 'CE2', 'CD2', 'CZ', 120.0)
    _ring(_r, 'CZ', 'CE1', 'CE2', 120.0)
    _ring(_r, 'CZ', 'CE1', 'OH', 120.0)  # TYR only; PHE won't query OH
# histidine imidazole 5-ring
for _c, _a, _b, _d in [
    ('CG', 'ND1', 'CD2', 106.0), ('CG', 'CB', 'ND1', 122.0),
    ('CG', 'CB', 'CD2', 122.0), ('ND1', 'CG', 'CE1', 108.0),
    ('CD2', 'CG', 'NE2', 110.0), ('CE1', 'ND1', 'NE2', 108.0),
    ('NE2', 'CE1', 'CD2', 108.0),
]:
    _ring('HIS', _c, _a, _b, _d)
# tryptophan indole (fused 6+5)
for _c, _a, _b, _d in [
    ('CG', 'CD1', 'CD2', 106.0), ('CG', 'CB', 'CD1', 126.0),
    ('CG', 'CB', 'CD2', 130.0), ('CD1', 'CG', 'NE1', 110.0),
    ('CD2', 'CG', 'CE2', 107.0), ('CD2', 'CG', 'CE3', 126.0),
    ('CD2', 'CE2', 'CE3', 128.0), ('NE1', 'CD1', 'CE2', 108.0),
    ('CE2', 'NE1', 'CZ2', 122.0), ('CE2', 'CD2', 'CZ2', 112.0),
    ('CE2', 'CD2', 'CE3', 130.0), ('CE3', 'CD2', 'CZ3', 120.0),
    ('CZ3', 'CE3', 'CH2', 120.0), ('CH2', 'CZ3', 'CZ2', 120.0),
    ('CZ2', 'CH2', 'CE2', 120.0), ('CZ2', 'CH2', 'CG', 120.0),
]:
    _ring('TRP', _c, _a, _b, _d)

# Backbone bond angles (residue-independent)
# sp2 terminal / ring-centre side-chain angles (Engh & Huber 2001 +
# idealised aromatic geometry). Before this table the engine constrained
# carboxylates toward 111 deg (chemistry: 123.3) and aromatic interiors
# toward 111 (chemistry: 120) -- under docking stress GLU/ASP carboxyls
# overshot to 164 deg and rings squashed to 86-92 deg interiors.
SIDECHAIN_ANGLES = {
    # carboxylates: ASP flank on CG, GLU flank on CD (audit: the GLU
    # entries previously keyed CG-OE1 which is not a bond -- dead keys,
    # GLU silently fell to the 111 default and its carboxyls overshot to
    # 164 deg under stress). Values from crystal measurement:
    # ASP CB-CG-OD1/OD2 119.1/117.6, GLN CG-CD-OE1/NE2 120.9/116.3.
    ("OE1", "CD", "OE2"): 123.3, ("OD1", "CG", "OD2"): 123.3,
    # ASP flanks (sp2 sums with the centre: 119.1+117.6+123.3 = 360)
    ("CB", "CG", "OD1"): 119.1, ("CB", "CG", "OD2"): 117.6,
    # GLU carboxylate vs GLN amide share the CG-CD-OE1/2 flanking
    # geometry (measured delta <= 1.3 deg) -- one shared value per
    # triplet; sp2 sums close at 360 for both centres
    ("CG", "CD", "OE1"): 120.3, ("CG", "CD", "OE2"): 117.0,
    # amides (Asn / Gln)
    ("OD1", "CG", "ND2"): 123.1, ("OE1", "CD", "NE2"): 123.1,
    # ASP vs ASN share CB-CG-OD1 (delta 2.3 deg) -- shared value
    ("CB", "CG", "OD1"): 120.3, ("CB", "CG", "ND2"): 116.0,
    ("CG", "CD", "NE2"): 116.3,
}

# residue-resolved ring interiors (same atom triplet, different ring:
# Phe/Tyr benzenoid 120 deg vs Trp indole 108 deg -- a flat dict would
# collide, so these ride the residue-keyed RING_ANGLES lookup)
_RING_INTERIOR = {
    ("PHE", "CG", ("CD1", "CD2")): 120.0,
    ("TYR", "CG", ("CD1", "CD2")): 120.0,
    ("PHE", "CD1", ("CG", "CE1")): 120.0,
    ("TYR", "CD1", ("CG", "CE1")): 120.0,
    ("PHE", "CD2", ("CG", "CE2")): 120.0,
    ("TYR", "CD2", ("CG", "CE2")): 120.0,
    ("PHE", "CE1", ("CD1", "CZ")): 120.0,
    ("TYR", "CE1", ("CD1", "CZ")): 120.0,
    ("PHE", "CE2", ("CD2", "CZ")): 120.0,
    ("TYR", "CE2", ("CD2", "CZ")): 120.0,
    ("PHE", "CZ", ("CE1", "CE2")): 120.0,
    ("TYR", "CZ", ("CE1", "CE2")): 120.0,
    ("HIS", "CG", ("ND1", "CD2")): 108.0,
    ("HIS", "ND1", ("CG", "CE1")): 108.0,
    ("HIS", "CD2", ("CG", "NE2")): 108.0,
    ("HIS", "CE1", ("ND1", "NE2")): 108.0,
    ("HIS", "NE2", ("CE1", "CD2")): 108.0,
    ("TRP", "CG", ("CD1", "CD2")): 108.0,
    ("TRP", "CD1", ("CG", "NE1")): 108.0,
    ("TRP", "CD2", ("CE2", "CE3")): 120.0,
    ("TRP", "CE2", ("CD2", "CZ2")): 120.0,
    ("TRP", "CE3", ("CD2", "CZ3")): 120.0,
    ("TRP", "CZ3", ("CE3", "CH2")): 120.0,
    ("TRP", "CH2", ("CZ3", "CZ2")): 120.0,
    ("TRP", "CZ2", ("CH2", "CE2")): 120.0,
}
for (_res, _c, (_a, _b)), _v in _RING_INTERIOR.items():
    RING_ANGLES[(_res, _c, frozenset((_a, _b)))] = _v

BACKBONE_ANGLES = {
    ('CA', 'C', 'N'): 116.1,
    ('C', 'N', 'CA'): 121.7,
    ('O', 'C', 'N'): 123.0,
    ('CA', 'C', 'O'): 120.8,
    ('N', 'CA', 'C'): 111.2,
    ('N', 'CA', 'CB'): 110.5,
    ('C', 'CA', 'CB'): 110.1,
}


def ideal_bond_length(elem_a: str, elem_b: str) -> float:
    """Generic element-pair bond length."""
    key = tuple(sorted((elem_a.upper(), elem_b.upper())))
    return IDEAL_BOND.get(key, 1.50)


def ideal_bond_length_residue(
    res_name: str, elem_a: str, elem_b: str, atom_a: str, atom_b: str,
) -> float:
    """Residue-specific bond length; falls back to element-pair."""
    r = res_name.upper()
    key = frozenset((atom_a.upper(), atom_b.upper()))
    v = RESIDUE_BOND.get((r, key))
    if v is not None:
        return v
    v = RESIDUE_BOND.get(('ALL', key))
    if v is not None:
        return v
    return ideal_bond_length(elem_a, elem_b)


def ideal_bond_angle(
    a: str, centre: str, b: str, residue: str | None = None,
) -> float:
    """Bond angle at `centre` in degrees.

    Amide sp2 backbone angles are planar (116-122 deg); aromatic ring
    centres are ~120 deg; sp3 falls through to ~111 deg."""
    a = a.upper(); centre = centre.upper(); b = b.upper()
    triplet = (a, centre, b)
    # try both orderings for asymmetric lookups
    angle = BACKBONE_ANGLES.get(triplet)
    if angle is None:
        angle = BACKBONE_ANGLES.get((b, centre, a))
    if angle is None:
        angle = SIDECHAIN_ANGLES.get(triplet)
    if angle is None:
        angle = SIDECHAIN_ANGLES.get((b, centre, a))
    if angle is not None:
        return angle
    if residue is not None:
        key = (residue.upper(), centre, frozenset((a, b)))
        ring = RING_ANGLES.get(key)
        if ring is not None:
            return ring
    return 111.0
