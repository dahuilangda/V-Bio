"""Contract tests for custom CCD blocks appended to Protenix components.cif.

Ground truth: biotite ``structure.io.pdbx.convert.get_component`` (Protenix's CCD
reader) accepts a category that is absent entirely (single-ion entries like NA/ZN in
the official RCSB components.cif) or present with >=1 data row; a header-only loop
raises ``biotite.DeserializationError`` ("Array must contain at least one element").
Regression context: task 79332a4f died on exactly that shape for the BS3 (Bi3+)
bicyclic linker CCD.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.runtime.ccd_contract import CCDContractError, validate_ccd_additions  # noqa: E402
from backend.runtime.custom_ccd_builder import (  # noqa: E402
    _build_custom_ccd_mol,
    _custom_ccd_mol_to_cif_block,
)

LINKER_DIR = REPO_ROOT / "backend" / "runtime" / "linker_ccd"


HEADER_ONLY_BOND_LOOP = """data_BS3
#
_chem_comp.id BS3
_chem_comp.name 'BS3 bicyclic linker'
_chem_comp.type 'NON-POLYMER'
_chem_comp.formula 'Bi'
loop_
_chem_comp_atom.comp_id
_chem_comp_atom.atom_id
_chem_comp_atom.type_symbol
BS3 BI Bi
#
loop_
_chem_comp_bond.comp_id
_chem_comp_bond.atom_id_1
_chem_comp_bond.atom_id_2
_chem_comp_bond.value_order
_chem_comp_bond.pdbx_aromatic_flag
#
"""


def test_regenerated_linker_files_are_contract_valid() -> None:
    texts = [
        (LINKER_DIR / f"{code}.cif").read_text() for code in ("SEZ", "29N", "BS3")
    ]
    validate_ccd_additions(*texts)


def test_single_atom_ligand_bsx_has_no_empty_bond_category() -> None:
    bs3_text = (LINKER_DIR / "BS3.cif").read_text()
    assert "_chem_comp_bond" not in bs3_text, (
        "BS3 is a single Bi atom; per PDB convention it must omit the bond category "
        "entirely instead of emitting a header-only empty loop."
    )


def test_header_only_loop_is_rejected_with_named_ccd() -> None:
    with pytest.raises(CCDContractError) as excinfo:
        validate_ccd_additions(HEADER_ONLY_BOND_LOOP)
    message = str(excinfo.value)
    assert "BS3" in message and "zero data rows" in message


def test_builder_normal_residue_emits_bond_rows_and_passes() -> None:
    mol = _build_custom_ccd_mol("N[C@@H](C)C(=O)O", kind="residue")
    block = _custom_ccd_mol_to_cif_block("XAL", mol, kind="residue", base_residue="A")
    assert "_chem_comp_bond.atom_id_1" in block
    validate_ccd_additions(block)


def test_builder_single_atom_ion_omits_bond_category() -> None:
    mol = _build_custom_ccd_mol("[Mg+2]", kind="ligand")
    for atom in mol.GetAtoms():
        if not atom.HasProp("name"):
            atom.SetProp("name", atom.GetSymbol().upper())
        atom.SetBoolProp("leaving_atom", False)
    block = _custom_ccd_mol_to_cif_block("MGX", mol, kind="ligand")
    assert "_chem_comp_bond" not in block
    validate_ccd_additions(block)


def test_builder_rejects_dot_disconnected_smiles_with_named_ccd() -> None:
    mol = _build_custom_ccd_mol("CC.CC", kind="ligand")
    for index, atom in enumerate(mol.GetAtoms()):
        atom.SetProp("name", f"{atom.GetSymbol().upper()}{index + 1}")
        atom.SetBoolProp("leaving_atom", False)
    with pytest.raises(ValueError) as excinfo:
        _custom_ccd_mol_to_cif_block("FRAG", mol, kind="ligand")
    assert "FRAG" in str(excinfo.value)


def test_validator_rejects_disconnected_species_over_defined_atoms() -> None:
    two_islands = (
        "data_ISL2\n#\n"
        "loop_\n"
        "_chem_comp_atom.comp_id\n_chem_comp_atom.atom_id\n_chem_comp_atom.type_symbol\n"
        "ISL2 CA C\nISL2 CB C\nISL2 CX C\nISL2 CY C\n#\n"
        "loop_\n"
        "_chem_comp_bond.comp_id\n_chem_comp_bond.atom_id_1\n_chem_comp_bond.atom_id_2\n"
        "_chem_comp_bond.value_order\n_chem_comp_bond.pdbx_aromatic_flag\n"
        "ISL2 CA CB SING N\nISL2 CX CY SING N\n#\n"
    )
    with pytest.raises(CCDContractError) as excinfo:
        validate_ccd_additions(two_islands)
    assert "disconnected" in str(excinfo.value)


def test_validator_accepts_single_atom_component_without_bond_category() -> None:
    single = (
        "data_ONE\n#\n"
        "loop_\n"
        "_chem_comp_atom.comp_id\n_chem_comp_atom.atom_id\n_chem_comp_atom.type_symbol\n"
        "ONE PT PT\n#\n"
    )
    validate_ccd_additions(single)


def test_validator_rejects_undefined_bond_atom() -> None:
    undefined_atom = (
        "data_BAD\n#\n"
        "loop_\n"
        "_chem_comp_atom.comp_id\n_chem_comp_atom.atom_id\n_chem_comp_atom.type_symbol\n"
        "BAD CA C\n#\n"
        "loop_\n"
        "_chem_comp_bond.comp_id\n_chem_comp_bond.atom_id_1\n_chem_comp_bond.atom_id_2\n"
        "_chem_comp_bond.value_order\n_chem_comp_bond.pdbx_aromatic_flag\n"
        "BAD CA CZ SING N\n#\n"
    )
    with pytest.raises(CCDContractError) as excinfo:
        validate_ccd_additions(undefined_atom)
    assert "CZ" in str(excinfo.value)


def test_validator_rejects_duplicate_component_codes() -> None:
    block = (
        "data_DUP\n#\n"
        "loop_\n"
        "_chem_comp_atom.comp_id\n_chem_comp_atom.atom_id\n_chem_comp_atom.type_symbol\n"
        "DUP CA C\n#\n"
    )
    with pytest.raises(CCDContractError) as excinfo:
        validate_ccd_additions(block, block)
    assert "twice" in str(excinfo.value)


def test_merge_hook_wired_into_run_single_prediction() -> None:
    import backend.runtime.run_single_prediction as rsp  # noqa: F401

    assert callable(rsp.validate_ccd_additions)
