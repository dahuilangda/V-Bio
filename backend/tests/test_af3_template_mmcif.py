from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import gemmi

from backend.runtime.run_single_prediction import (
    convert_structure_to_single_chain_mmcif,
    extract_chain_sequences_from_structure,
    prepare_template_payloads,
)


_PDB_WITH_OFFSET_NUMBERING = """\
ATOM      1  N   ASN A 241      11.104  13.207   9.551  1.00 20.00           N
ATOM      2  CA  ASN A 241      12.560  13.207   9.551  1.00 20.00           C
ATOM      3  C   ASN A 241      13.020  14.650   9.551  1.00 20.00           C
ATOM      4  O   ASN A 241      12.300  15.600   9.551  1.00 20.00           O
ATOM      5  N   GLY A 242      14.300  14.800   9.551  1.00 20.00           N
ATOM      6  CA  GLY A 242      14.900  16.120   9.551  1.00 20.00           C
ATOM      7  C   GLY A 242      16.410  16.050   9.551  1.00 20.00           C
ATOM      8  O   GLY A 242      17.020  15.000   9.551  1.00 20.00           O
TER
END
"""


class AlphaFoldTemplateMmcifTests(unittest.TestCase):
    def test_offset_residue_numbers_are_normalized_for_af3_scheme_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "offset.pdb"
            output_path = Path(temp_dir) / "template.cif"
            source_path.write_text(_PDB_WITH_OFFSET_NUMBERING)

            _, cif_text, chain_id, sequence = convert_structure_to_single_chain_mmcif(
                source_path,
                "A",
                output_path,
            )

            structure = gemmi.read_structure(str(output_path))
            residues = list(structure[0][chain_id])
            self.assertEqual(sequence, "NG")
            self.assertEqual([residue.seqid.num for residue in residues], [1, 2])
            self.assertEqual([residue.label_seq for residue in residues], [1, 2])
            self.assertEqual(len(structure.entities[0].full_sequence), 2)

            block = gemmi.cif.read_string(cif_text).sole_block()
            self.assertEqual(set(block.find_values("_atom_site.label_seq_id")), {"1", "2"})
            self.assertEqual(set(block.find_values("_atom_site.auth_seq_id")), {"1", "2"})
            self.assertEqual(set(block.find_values("_entity_poly_seq.num")), {"1", "2"})

    def test_mmcif_without_b_factor_still_exposes_chain_sequences(self) -> None:
        structure = gemmi.read_pdb_string(_PDB_WITH_OFFSET_NUMBERING)
        document = structure.make_mmcif_document()
        block = document.sole_block()
        block.find_loop("_atom_site.B_iso_or_equiv").erase()

        sequences = extract_chain_sequences_from_structure(document.as_string(), "cif")

        self.assertEqual(sequences, {"A": "NG"})

    def test_template_preparation_accepts_mmcif_without_b_factor(self) -> None:
        structure = gemmi.read_pdb_string(_PDB_WITH_OFFSET_NUMBERING)
        document = structure.make_mmcif_document()
        document.sole_block().find_loop("_atom_site.B_iso_or_equiv").erase()
        content = document.as_string()
        yaml_content = """\
version: 1
sequences:
  - protein:
      id: T
      sequence: NG
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            _, templates = prepare_template_payloads(
                yaml_content,
                [
                    {
                        "file_name": "template.cif",
                        "format": "cif",
                        "template_chain_id": "A",
                        "target_chain_ids": ["T"],
                        "content_base64": base64.b64encode(content.encode()).decode(),
                    }
                ],
                temp_dir,
            )

        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["target_chain_ids"], ["T"])
        self.assertEqual(templates[0]["queryIndices"], [0, 1])
        self.assertEqual(templates[0]["templateIndices"], [0, 1])


if __name__ == "__main__":
    unittest.main()
