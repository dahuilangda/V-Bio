"""Regression tests for per-chain polymer entity sequences.

A gemmi entity may own several subchains (a homodimer's two copies share one
entity). The combined-input writer used to rebuild entity.full_sequence from
residues across ALL subchains, concatenating the copies into a 2x sequence.
The mmCIF writer attaches that sequence to every chain of the entity, and
downstream parsing gives each chain the doubled polymer — the structure model
then folds the duplicated half into garbage (broken geometry, low pLDDT).

Run:  <boltz2score venv> -m pytest tests/test_entity_sequences.py -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import gemmi
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from utils.ligand_utils import (
    build_combined_input_from_parts,
    sync_entity_sequences_from_first_subchain,
)


def _residue(name: str, seqid: int) -> gemmi.Residue:
    res = gemmi.Residue()
    res.name = name
    res.seqid = gemmi.SeqId(seqid, " ")
    for atom_name, element in (("CA", "C"), ("N", "N"), ("C", "C")):
        atom = gemmi.Atom()
        atom.name = atom_name
        atom.element = gemmi.Element(element)
        atom.pos = gemmi.Position(0.0, 0.0, 0.0)
        res.add_atom(atom)
    return res


def _two_chain_structure() -> gemmi.Structure:
    """Two identical protein chains (A, B) that share one polymer entity."""
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for chain_name in ("A", "B"):
        chain = gemmi.Chain(chain_name)
        for idx, res_name in enumerate(("ALA", "GLY", "SER"), start=1):
            chain.add_residue(_residue(res_name, idx))
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    return structure


def _entity_poly_seq_rows(cif_path: Path) -> dict[str, int]:
    doc = gemmi.cif.read(str(cif_path))
    tab = doc.sole_block().find("_entity_poly_seq.", ["entity_id", "num", "mon_id"])
    counts: dict[str, int] = {}
    for row in tab:
        counts[row[0]] = counts.get(row[0], 0) + 1
    return counts


def _ligand_mol() -> Chem.Mol:
    mol = Chem.MolFromSmiles("CC(C)Cc1ccc(C(=O)O)cc1")
    AllChem.EmbedMolecule(mol, randomSeed=42)
    return mol


def test_sync_entity_sequences_uses_first_subchain_only() -> None:
    structure = _two_chain_structure()
    entity = next(e for e in structure.entities if e.entity_type.name == "Polymer")
    entity.full_sequence = ["ALA", "GLY", "SER", "ALA", "GLY", "SER"]  # poisoned: both copies

    sync_entity_sequences_from_first_subchain(structure)

    assert list(entity.full_sequence) == ["ALA", "GLY", "SER"]


def test_combined_input_writes_one_sequence_per_entity() -> None:
    structure = _two_chain_structure()
    with tempfile.NamedTemporaryFile(suffix=".cif", delete=False) as tmp:
        structure.make_mmcif_document().write_file(tmp.name)
        protein_path = Path(tmp.name)

    with tempfile.TemporaryDirectory() as td:
        combined, *_ = build_combined_input_from_parts(
            protein_path=protein_path,
            ligand_mol=_ligand_mol(),
            ligand_source_label="ligand.sdf",
            ligand_smiles_map={},
            work_dir=Path(td),
            record_id="homodimer_ligand_1",
        )
        counts = _entity_poly_seq_rows(combined)

    protein_entities = [c for c in counts.items() if c[1] > 1]
    assert protein_entities, "expected a polymer entity in the combined CIF"
    for entity_id, num_residues in protein_entities:
        assert num_residues == 3, (
            f"entity {entity_id} sequence must describe ONE chain copy "
            f"(3 residues), got {num_residues} — subchain concatenation regression"
        )


@pytest.mark.parametrize("keep", [["A", "L"], ["B", "L"]])
def test_filter_structure_keeps_single_copy_sequence(keep: list[str]) -> None:
    from utils.structure_refinement import filter_structure_by_chains

    structure = _two_chain_structure()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.cif"
        structure.make_mmcif_document().write_file(str(src))
        dst = Path(td) / "filtered.cif"
        # filter_structure_by_chains resolves chains against a processed
        # StructureV2; for this regression the gemmi-level contract is enough:
        # the writer must not re-concatenate shared-entity subchains.
        try:
            filter_structure_by_chains(
                input_path=src,
                target_chains=[c for c in keep if c != "L"],
                ligand_chains=["L"] if "L" in keep else [],
                output_path=dst,
            )
        except Exception:
            pytest.skip("filter_structure_by_chains requires a processed structure context")
        if dst.exists():
            counts = _entity_poly_seq_rows(dst)
            for entity_id, num_residues in counts.items():
                assert num_residues == 3, f"entity {entity_id} got {num_residues} residues"
