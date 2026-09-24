"""Distance-cutoff selection for polymer vs small-molecule ligand chains.

A polymer "ligand" (peptide/protein binder) is tokenized as one CB/CA atom per
residue; the atom-level 5 A heavy-atom cutoff then matches almost nothing and
collapses ipsae to a few token pairs (seen on protenix peptide binders: 4 pairs
→ ipsae 0.38 despite iptm 0.92). Residue-level ligand tokens must widen the
cutoff to the 10 A CA/CB contact proxy; atom-level (HETATM) ligands keep 5 A.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from metrics.ligand_ipsae import (
    RESIDUE_LIGAND_DIST_CUTOFF,
    compute_ligand_ipsae_from_files,
)

CIF_HEAD = """data_test
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_seq_id
_atom_site.auth_seq_id
_atom_site.pdbx_PDB_ins_code
_atom_site.label_asym_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.label_entity_id
_atom_site.auth_asym_id
_atom_site.auth_comp_id
_atom_site.B_iso_or_equiv
_atom_site.pdbx_PDB_model_num
"""


def _atom_row(group: str, atom_id: int, name: str, comp: str, seq: str,
              chain: str, x: float, y: float) -> str:
    entity = "1" if group == "ATOM" else "2"
    return (
        f"{group} {atom_id} C {name} . {comp} {seq} {seq} ? {chain} "
        f"{x} {y} 0 1.0 {entity} {chain} {comp} 0 1"
    )


def _write_case(tmp_path: Path, ligand_rows: list[str], pae: np.ndarray) -> Path:
    protein_rows = [
        _atom_row("ATOM", 1, "N", "ALA", "1", "A", 0.0, 0.0),
        _atom_row("ATOM", 2, "CA", "ALA", "1", "A", 1.5, 0.0),
        _atom_row("ATOM", 3, "CB", "ALA", "1", "A", 1.9, 1.2),
        _atom_row("ATOM", 4, "N", "GLY", "2", "A", 3.0, 0.0),
        _atom_row("ATOM", 5, "CA", "GLY", "2", "A", 4.5, 0.0),
    ]
    cif_path = tmp_path / "case.cif"
    cif_path.write_text(CIF_HEAD + "\n".join(protein_rows + ligand_rows) + "\n#\n")
    np.savez(tmp_path / "case_pae.npz", pae=pae)
    (tmp_path / "case_conf.json").write_text(json.dumps({"model_ligand_chain_id": "B"}))
    return cif_path


def _run(tmp_path: Path, cif_path: Path) -> dict:
    return compute_ligand_ipsae_from_files(
        confidence_path=tmp_path / "case_conf.json",
        cif_path=cif_path,
        pae_path=tmp_path / "case_pae.npz",
        pae_cutoff=12.0,
        dist_cutoff=5.0,
    )


def test_small_molecule_ligand_keeps_atom_cutoff(tmp_path: Path) -> None:
    # tokens: 0 ALA(CB) 1 GLY(CA) | ligand atoms 2 C1 3 C2 4 O1
    ligand_rows = [
        _atom_row("HETATM", 6, "C1", "LIG", ".", "B", 1.6, 1.3),
        _atom_row("HETATM", 7, "C2", "LIG", ".", "B", 2.6, 1.3),
        _atom_row("HETATM", 8, "O1", "LIG", ".", "B", 2.1, 2.2),
    ]
    pae = np.array(
        [[0.5, 3.0, 0.6, 1.2, 2.0],
         [3.0, 0.5, 0.7, 1.5, 2.0],
         [0.6, 0.7, 0.5, 0.5, 0.5],
         [1.2, 1.5, 0.5, 0.5, 0.5],
         [2.0, 2.0, 0.5, 0.5, 0.5]],
        dtype=np.float32,
    )
    cif_path = _write_case(tmp_path, ligand_rows, pae)
    result = _run(tmp_path, cif_path)
    assert result["ligand_token_granularity"] == "atom"
    assert result["dist_cutoff"] == pytest.approx(5.0)


def test_polymer_ligand_widens_to_residue_cutoff(tmp_path: Path) -> None:
    # Chain B is a peptide: standard residues are one token per residue (CB),
    # so the token layout matches the small-molecule case — only the kind differs.
    ligand_rows = [
        _atom_row("ATOM", 6, "N", "LYS", "1", "B", 1.6, 1.3),
        _atom_row("ATOM", 7, "CB", "LYS", "1", "B", 2.6, 1.3),
        _atom_row("ATOM", 8, "N", "HIS", "2", "B", 3.6, 2.2),
    ]
    pae = np.array(
        [[0.5, 3.0, 0.6, 1.2],
         [3.0, 0.5, 0.7, 1.5],
         [0.6, 0.7, 0.5, 0.5],
         [1.2, 1.5, 0.5, 0.5]],
        dtype=np.float32,
    )
    cif_path = _write_case(tmp_path, ligand_rows, pae)
    result = _run(tmp_path, cif_path)
    assert result["ligand_token_granularity"] == "residue"
    assert result["dist_cutoff"] == pytest.approx(RESIDUE_LIGAND_DIST_CUTOFF)


def test_polymer_ligand_cutoff_never_narrows_request(tmp_path: Path) -> None:
    # An explicit larger requested cutoff must be preserved, not clamped down.
    ligand_rows = [
        _atom_row("ATOM", 6, "CB", "LYS", "1", "B", 1.6, 1.3),
        _atom_row("ATOM", 7, "CB", "HIS", "2", "B", 2.6, 2.2),
    ]
    pae = np.array(
        [[0.5, 3.0, 0.6, 1.2],
         [3.0, 0.5, 0.7, 1.5],
         [0.6, 0.7, 0.5, 0.5],
         [1.2, 1.5, 0.5, 0.5]],
        dtype=np.float32,
    )
    cif_path = _write_case(tmp_path, ligand_rows, pae)
    result = compute_ligand_ipsae_from_files(
        confidence_path=tmp_path / "case_conf.json",
        cif_path=cif_path,
        pae_path=tmp_path / "case_pae.npz",
        pae_cutoff=12.0,
        dist_cutoff=14.0,
    )
    assert result["ligand_token_granularity"] == "residue"
    assert result["dist_cutoff"] == pytest.approx(14.0)
