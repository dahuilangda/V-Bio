"""CPU tests for the strict MSA resolution and SMILES canonicalization.

Run: <protenix venv> -m pytest tests/ -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_resolve_msa_without_server_returns_query_only(tmp_path):
    """No MSA server == the caller opted out: the official N_msa=1 contract
    (a query-only a3m), matching Protenix's optional-MSA semantics."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.input_prep import resolve_msa

    out = resolve_msa("MKTLLLTLVV", "A", None, None, tmp_path)
    assert Path(out).exists()
    assert Path(out).read_text() == ">query\nMKTLLLTLVV\n"


def test_resolve_msa_length_mismatched_cache_raises(tmp_path):
    """A cached a3m whose first sequence disagrees with the query is data
    corruption and must fail loudly, not be silently reused (only checked
    when a cache dir is supplied)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.input_prep import resolve_msa

    cache = tmp_path / "cache"
    cache.mkdir()
    bad = ">x\nMKTAYIAKQR\n"
    from core.input_prep import _md5

    (cache / f"msa_{_md5('MKT')}.a3m").write_text(bad)
    with pytest.raises(ValueError, match="does not match"):
        # explicit env tier: its cache name (msa_<h>.a3m) is the one written above
        resolve_msa("MKT", "A", cache, None, tmp_path / "scratch", msa_mode="env")


def test_fetch_msa_unrecognized_payload_raises(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.input_prep import _fetch_msa_from_server

    class FakeRequests:
        @staticmethod
        def post(url, data=None, timeout=None):
            class R:
                status_code = 200

                def json(self):
                    return {"id": "t1"}

            return R()

        @staticmethod
        def get(url, timeout=None):
            class R:
                status_code = 200
                content = b"\x00\x01binary-garbage-not-a3m"

                def json(self):
                    return {"status": "COMPLETE"}

            return R()

    import types

    fake = types.ModuleType("requests")
    fake.post = FakeRequests.post
    fake.get = FakeRequests.get
    real = sys.modules.get("requests")
    sys.modules["requests"] = fake
    try:
        with pytest.raises(ValueError, match="not recognized"):
            _fetch_msa_from_server("MKT", "http://x", timeout=5)
    finally:
        if real is not None:
            sys.modules["requests"] = real


def test_canonicalize_smiles_strips_cx_annotations():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from generate_structure_shards import canonicalize_smiles

    assert canonicalize_smiles("CCO") == "CCO"
    assert canonicalize_smiles("CCO |w:11.11|") == "CCO"
    with pytest.raises(ValueError):
        canonicalize_smiles("!!not smiles!!")


def _mini_staged_pdb(tmp_path: Path) -> Path:
    """3-residue receptor (A) + 2-residue peptide (B), ideal-ish geometry."""
    lines = ["HEADER    MINI"]
    # chain A: ALA-ALA-ALA stacked along +z (bonds ~1.5 A)
    atoms_a = [
        ("N", 1, (0.0, 0.0, 0.0), "N"),
        ("CA", 1, (1.5, 0.0, 0.0), "C"),
        ("C", 1, (1.5, 1.5, 0.0), "C"),
        ("N", 2, (1.5, 2.6, 0.0), "N"),
        ("CA", 2, (1.5, 4.1, 0.0), "C"),
        ("C", 2, (1.5, 5.6, 0.0), "C"),
        ("N", 3, (1.5, 6.7, 0.0), "N"),
        ("CA", 3, (1.5, 8.2, 0.0), "C"),
    ]
    # chain B: SER-TYR peptide 6 A off chain A in x; TYR OH 1.4 A from A:CA1 (hard clash)
    ox = 7.0
    atoms_b = [
        ("N", 1, (ox, 0.0, 0.0), "N"),
        ("CA", 1, (ox + 1.5, 0.0, 0.0), "C"),
        ("C", 1, (ox + 1.5, 1.5, 0.0), "C"),
        ("CB", 1, (ox + 2.9, -0.4, 0.0), "C"),
        ("OG", 1, (ox + 4.0, 0.3, 0.0), "O"),
        ("N", 2, (ox + 1.5, 2.6, 0.0), "N"),
        ("CA", 2, (ox + 1.5, 4.1, 0.0), "C"),
        ("C", 2, (ox + 1.5, 5.6, 0.0), "C"),
        ("CZ", 2, (ox + 0.1, 4.1, 0.0), "C"),
        ("OH", 2, (ox - 1.2, 4.1, 0.0), "O"),  # 1.3 A from A:CA2 at x=1.5
    ]
    serial = 1
    for chain, residues in (("A", atoms_a), ("B", atoms_b)):
        for name, resi, (x, y, z), elem in residues:
            lines.append(
                f"ATOM  {serial:5d} {name:>4s} {'ALA' if chain == 'A' else ('SER' if resi == 1 else 'TYR'):>3s} {chain}{resi:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 50.00          {elem:>2s}")
            serial += 1
        lines.append("TER")
    lines.append("END")
    path = tmp_path / "mini.pdb"
    path.write_text("\n".join(lines) + "\n")
    return path


def _mini_table():
    """Assembled table (info, coords, mask) matching align_complex_init_coords.

    Chain A (entity 0): 3 residues x backbone; chain B (entity 1): SER-TYR
    with full side chains; the TYR OH sits 1.3 A from A:CA2 (hard clash).
    """
    import numpy as np

    asym = [1] * 8 + [2] * 10
    res_id = [1, 1, 1, 2, 2, 2, 3, 3] + [1, 1, 1, 1, 1, 2, 2, 2, 2, 2]
    atom_names = (
        ["N", "CA", "C", "N", "CA", "C", "N", "CA"]
        + ["N", "CA", "C", "CB", "OG", "N", "CA", "C", "CZ", "OH"]
    )
    elements = ["N", "C", "C", "N", "C", "C", "N", "C"] + [
        "N", "C", "C", "C", "O", "N", "C", "C", "C", "O"]
    xyz = (
        [(0.0, 0.0, 0.0), (1.5, 0.0, 0.0), (1.5, 1.5, 0.0),
         (1.5, 2.6, 0.0), (1.5, 4.1, 0.0), (1.5, 5.6, 0.0),
         (1.5, 6.7, 0.0), (1.5, 8.2, 0.0)]
        + [(7.0, 0.0, 0.0), (8.5, 0.0, 0.0), (8.5, 1.5, 0.0),
           (9.9, -0.4, 0.0), (11.0, 0.3, 0.0), (8.5, 2.6, 0.0),
           (8.5, 4.1, 0.0), (8.5, 5.6, 0.0), (7.1, 4.1, 0.0), (5.8, 4.1, 0.0)]
    )
    comps = (["ALA"] * 3 + ["ALA"] * 0) + ["ALA"] * 0  # chain A all ALA
    comps = ["ALA", "ALA", "ALA", "ALA", "ALA", "ALA", "ALA", "ALA",
             "SER", "SER", "SER", "SER", "SER", "TYR", "TYR", "TYR", "TYR", "TYR"]
    info = {
        "asym": np.asarray(asym),
        "res_id": np.asarray(res_id),
        "atom_names": np.asarray(atom_names),
        "elements": np.asarray(elements),
        "comp_ids": np.asarray(comps),
        "asym_to_entity": {1: 0, 2: 1},
        "entity_chain_names": ["A", "B"],
    }
    coords = np.asarray(xyz, dtype=np.float32)
    mask = np.ones(len(asym), dtype=np.float32)
    return info, coords, mask




def test_ligand_covalent_bands_from_rdkit_graph(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from core.input_prep import compute_ligand_covalent_bands

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    mol = Chem.RemoveHs(mol)
    rows = np.arange(10, 13)  # assembled rows arbitrary
    out = compute_ligand_covalent_bands(rows, mol)
    assert out is not None
    idx, up, lo = out
    assert len(idx) == 2  # C-C + C-O
    assert np.isin(idx, rows).all()
    assert lo.min() >= 0.5
    assert compute_ligand_covalent_bands(np.array([]), mol) is None



def test_ligand_bands_strip_hydrogens_before_graph():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from core.input_prep import compute_ligand_covalent_bands

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    heavy = Chem.RemoveHs(mol)
    rows = np.arange(10, 10 + heavy.GetNumAtoms())
    out = compute_ligand_covalent_bands(rows, mol)  # H-carrying input
    assert out is not None
    idx, up, lo = out
    assert len(idx) == 2 and np.isin(idx, rows).all()
    # a molecule whose heavy count mismatches the rows is rejected
    assert compute_ligand_covalent_bands(rows[:-1], mol) is None



def _mini_table():
    """Assembled table (info, coords, mask) matching align_complex_init_coords.

    Chain A (entity 0): 3 residues x backbone; chain B (entity 1): SER-TYR
    with full side chains; the TYR OH sits 1.3 A from A:CA2 (hard clash).
    """
    import numpy as np

    asym = [1] * 8 + [2] * 10
    res_id = [1, 1, 1, 2, 2, 2, 3, 3] + [1, 1, 1, 1, 1, 2, 2, 2, 2, 2]
    atom_names = (
        ["N", "CA", "C", "N", "CA", "C", "N", "CA"]
        + ["N", "CA", "C", "CB", "OG", "N", "CA", "C", "CZ", "OH"]
    )
    elements = ["N", "C", "C", "N", "C", "C", "N", "C"] + [
        "N", "C", "C", "C", "O", "N", "C", "C", "C", "O"]
    comps = ["ALA"] * 8 + ["SER"] * 5 + ["TYR"] * 5
    xyz = (
        [(0.0, 0.0, 0.0), (1.5, 0.0, 0.0), (1.5, 1.5, 0.0),
         (1.5, 2.6, 0.0), (1.5, 4.1, 0.0), (1.5, 5.6, 0.0),
         (1.5, 6.7, 0.0), (1.5, 8.2, 0.0)]
        + [(7.0, 0.0, 0.0), (8.5, 0.0, 0.0), (8.5, 1.5, 0.0),
           (9.9, -0.4, 0.0), (11.0, 0.3, 0.0), (8.5, 2.6, 0.0),
           (8.5, 4.1, 0.0), (8.5, 5.6, 0.0), (7.1, 4.1, 0.0), (5.8, 4.1, 0.0)]
    )
    info = {
        "asym": np.asarray(asym),
        "res_id": np.asarray(res_id),
        "atom_names": np.asarray(atom_names),
        "elements": np.asarray(elements),
        "comp_ids": np.asarray(comps),
        "asym_to_entity": {1: 0, 2: 1},
        "entity_chain_names": ["A", "B"],
    }
    coords = np.asarray(xyz, dtype=np.float32)
    mask = np.ones(len(asym), dtype=np.float32)
    return info, coords, mask


def test_geometry_bonds_follow_the_chemical_graph():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from core.input_prep import compute_free_chain_tfg_constraints

    info, coords, mask = _mini_table()
    out = compute_free_chain_tfg_constraints(info, coords, mask, free_entities={1})
    assert out is not None
    idx = out["pairwise_distance_index"]
    assert idx.shape[0] == 2  # [2, M]

    pairs = {frozenset((int(idx[0, k]), int(idx[1, k])))
             for k in range(idx.shape[1])}
    bonds = {frozenset((int(idx[0, k]), int(idx[1, k])))
             for k in range(idx.shape[1])
             if out["pairwise_distance_is_bond"][k] == 1}
    angles = {frozenset((int(idx[0, k]), int(idx[1, k])))
              for k in range(idx.shape[1])
              if out["pairwise_distance_is_angle"][k] == 1}

    expected_bonds = {
        frozenset((8, 9)), frozenset((9, 10)),      # SER N-CA, CA-C
        frozenset((9, 11)), frozenset((11, 12)),    # SER CA-CB, CB-OG
        frozenset((13, 14)), frozenset((14, 15)),   # TYR N-CA, CA-C
        frozenset((16, 17)),                         # TYR CZ-OH
        frozenset((10, 13)),                         # peptide C-N
    }
    assert expected_bonds <= bonds, f"missing bonds: {expected_bonds - bonds}"
    assert angles, "no angle constraints derived"
    assert bonds | angles == pairs  # every pair is bond or angle


def test_geometry_bands_cover_disulfide_crosslink():
    """S-S (2.05 A) between non-consecutive residues must be a bond."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from core.input_prep import compute_free_chain_tfg_constraints

    xyz = {
        ("B", 1, "N"): (0.0, 0.0, 0.0), ("B", 1, "CA"): (1.5, 0.0, 0.0),
        ("B", 1, "C"): (1.5, 1.5, 0.0), ("B", 1, "O"): (1.5, 2.6, 0.0),
        ("B", 1, "CB"): (2.9, -0.5, 0.0), ("B", 1, "SG"): (4.3, -0.6, 0.0),
        ("B", 2, "N"): (1.5, 2.6, 0.0), ("B", 2, "CA"): (1.5, 4.1, 0.0),
        ("B", 2, "C"): (1.5, 5.6, 0.0), ("B", 2, "O"): (1.5, 6.7, 0.0),
        ("B", 3, "N"): (1.5, 6.7, 0.0), ("B", 3, "CA"): (1.5, 8.2, 0.0),
        ("B", 3, "C"): (1.5, 9.7, 0.0), ("B", 3, "O"): (1.5, 10.8, 0.0),
        ("B", 3, "CB"): (2.9, 8.6, 0.0), ("B", 3, "SG"): (4.3, 1.45, 0.0),
    }
    keys = sorted(xyz)
    info = {"asym": np.asarray([2] * len(keys)),
            "res_id": np.asarray([k[1] for k in keys]),
            "atom_names": np.asarray([k[2] for k in keys]),
            "elements": np.asarray(["N", "C", "C", "O", "C", "S",
                                    "N", "C", "C", "O",
                                    "N", "C", "C", "O", "C", "S"]),
            "comp_ids": np.asarray(["CYS"] * 6 + ["GLY"] * 4 + ["CYS"] * 6),
            "asym_to_entity": {2: 1}, "entity_chain_names": ["A", "B"]}
    coords = np.asarray([xyz[k] for k in keys], dtype=np.float32)
    mask = np.ones(len(keys), dtype=np.float32)
    out = compute_free_chain_tfg_constraints(info, coords, mask, free_entities={1})
    idx = out["pairwise_distance_index"]
    sg1 = keys.index(("B", 1, "SG"))
    sg3 = keys.index(("B", 3, "SG"))
    found = next(k for k in range(idx.shape[1])
                 if {int(idx[0, k]), int(idx[1, k])} == {sg1, sg3})
    assert out["pairwise_distance_is_bond"][found] == 1


def test_geometry_bands_reject_chemically_impossible_pairs():
    """A deformed input (two backbone oxygens 1.5 A apart) is not a bond."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from core.input_prep import compute_free_chain_tfg_constraints

    keys = [("B", 1, "C"), ("B", 1, "O"), ("B", 2, "N"), ("B", 2, "CA"),
            ("B", 2, "OXT")]
    xyz = [(0.0, 0.0, 0.0), (1.25, 0.0, 0.0),
           (1.5, 1.33, 0.0), (1.5, 2.9, 0.0), (1.9, -0.9, 0.0)]
    info = {"asym": np.asarray([2] * len(keys)),
            "res_id": np.asarray([k[1] for k in keys]),
            "atom_names": np.asarray([k[2] for k in keys]),
            "elements": np.asarray(["C", "O", "N", "C", "O"]),
            "comp_ids": np.asarray(["ASP"] * 5),
            "asym_to_entity": {2: 1}, "entity_chain_names": ["A", "B"]}
    coords = np.asarray(xyz, dtype=np.float32)
    mask = np.ones(len(keys), dtype=np.float32)
    out = compute_free_chain_tfg_constraints(info, coords, mask, free_entities={1})
    idx = out["pairwise_distance_index"]
    bond_pairs = {frozenset((int(idx[0, k]), int(idx[1, k])))
                  for k in range(idx.shape[1])
                  if out["pairwise_distance_is_bond"][k] == 1}
    o1 = keys.index(("B", 1, "O"))
    oxt = keys.index(("B", 2, "OXT"))
    c1 = keys.index(("B", 1, "C"))
    assert frozenset((o1, oxt)) not in bond_pairs, "O-O must not be a bond"
    assert frozenset((c1, o1)) in bond_pairs, "C=O must be a bond"


def test_ligand_bands_from_rdkit_graph():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from core.input_prep import compute_ligand_covalent_bands

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    heavy = Chem.RemoveHs(mol)
    rows = np.arange(10, 10 + heavy.GetNumAtoms())
    idx, up, lo = compute_ligand_covalent_bands(rows, mol)
    assert len(idx) == 2 and np.isin(idx, rows).all()
    assert compute_ligand_covalent_bands(np.array([]), mol) is None


def test_ligand_bands_reject_mismatched_heavy_atom_count():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from core.input_prep import compute_ligand_covalent_bands

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    rows = np.arange(10, 13)  # 3 rows vs 3 heavy atoms after RemoveHs -> OK
    assert compute_ligand_covalent_bands(rows, mol) is not None
    assert compute_ligand_covalent_bands(rows[:2], mol) is None  # mismatch
