from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from backend.runtime.protenix_adapter import parse_yaml_for_protenix
from backend.runtime.run_single_prediction import (
    _remap_constraints_by_template_alignment,
    _sanitize_constraints_for_chain_lengths,
)

# Shape mirrors a bicyclic peptide-design candidate: receptor (A, uploaded
# target), 20-residue binder (B), SEZ linker ligand (C), three SG bonds and a
# user pocket on the receptor.
_CANDIDATE_YAML = """
version: 1
sequences:
  - protein:
      id: A
      sequence: MKTAYIAKQRQISFVKSHFSRQLEERLIKNAAQFKQAG
  - protein:
      id: B
      sequence: CCCCCCCCCCC
      msa: empty
  - ligand:
      id: C
      ccd: SEZ
constraints:
  - bond:
      atom1: [B, 1, SG]
      atom2: [C, 1, CD]
  - bond:
      atom1: [B, 5, SG]
      atom2: [C, 1, C1]
  - bond:
      atom1: [B, 10, SG]
      atom2: [C, 1, C2]
  - pocket:
      binder: B
      max_distance: 8.0
      contacts:
        - [A, 12]
        - [A, 13]
"""


class ProtenixPocketConstraintTest(unittest.TestCase):
    def test_pocket_entities_point_at_binder_and_receptor(self) -> None:
        prep = parse_yaml_for_protenix(_CANDIDATE_YAML)
        constraint = prep.payload[0]["constraint"]

        # entity 1 = receptor, entity 2 = binder, entity 3 = linker ligand
        self.assertEqual(
            constraint["pocket"]["binder_chain"], {"entity": 2, "copy": 1}
        )
        self.assertEqual(
            [c["entity"] for c in constraint["pocket"]["contact_residues"]],
            [1, 1],
        )
        self.assertEqual(
            [c["copy"] for c in constraint["pocket"]["contact_residues"]],
            [1, 1],
        )
        # positions pass through unchanged; author->sequence translation is
        # applied upstream by _remap_constraints_by_template_alignment
        self.assertEqual(
            [c["position"] for c in constraint["pocket"]["contact_residues"]],
            [12, 13],
        )

    def test_covalent_bond_entities_unchanged(self) -> None:
        prep = parse_yaml_for_protenix(_CANDIDATE_YAML)
        bonds = prep.payload[0]["covalent_bonds"]
        self.assertEqual(len(bonds), 3)
        for bond in bonds:
            self.assertEqual(bond["entity1"], 2)
            self.assertEqual(bond["copy1"], 1)
            self.assertEqual(bond["entity2"], 3)
            self.assertEqual(bond["copy2"], 1)
        self.assertEqual([b["position1"] for b in bonds], [1, 5, 10])

    def test_unknown_pocket_chain_fails_loud(self) -> None:
        data = yaml.safe_load(_CANDIDATE_YAML)
        data["constraints"][-1]["pocket"]["binder"] = "Z"
        with self.assertRaises(ValueError):
            parse_yaml_for_protenix(yaml.safe_dump(data))


class TemplateNumberingRemapTest(unittest.TestCase):
    """Pocket contacts arrive in author numbering; both engines number
    polymer residues 1..N over the input sequence, so contacts must be
    translated via the uploaded template before reaching the engine."""

    def _write_template(self, root: Path, first_resnum: int, n_residues: int) -> Path:
        path = root / "template.pdb"
        lines = []
        for i in range(n_residues):
            resnum = first_resnum + i
            lines.append(
                "ATOM  {:5d}  CA  ALA {:1s}{:4d}    "
                "{:8.3f}{:8.3f}{:8.3f}  1.00 20.00           C".format(
                    i + 1, "A", resnum, 0.0, 0.0, float(i)
                )
            )
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_contacts_shifted_by_template_offset(self) -> None:
        with TemporaryDirectory() as tmp:
            template = self._write_template(Path(tmp), first_resnum=26, n_residues=40)
            data = yaml.safe_load(_CANDIDATE_YAML)
            data["templates"] = [
                {"pdb": str(template), "target_chain_ids": ["A"], "chain_id": ["A"]}
            ]
            data["constraints"][-1]["pocket"]["contacts"] = [
                [c[0], c[1] + 25] for c in data["constraints"][-1]["pocket"]["contacts"]
            ]
            remapped = _remap_constraints_by_template_alignment(
                yaml.safe_dump(data)
            )
            pocket = yaml.safe_load(remapped)["constraints"][-1]["pocket"]
            # author 37 = first template residue offset 26 -> sequence index 12
            self.assertEqual(
                [c[1] for c in pocket["contacts"]], [12, 13]
            )

    def test_sanitize_rejects_out_of_range_position(self) -> None:
        data = yaml.safe_load(_CANDIDATE_YAML)
        data["constraints"][-1]["pocket"]["contacts"] = [[c[0], 999] for c in
                                                         data["constraints"][-1]["pocket"]["contacts"]]
        with self.assertRaises(ValueError):
            _sanitize_constraints_for_chain_lengths(yaml.safe_dump(data))


class StagedSpacePocketContactsTest(unittest.TestCase):
    """The staged placement consumes sequence-numbered contacts; translation
    from the user's author numbering must be exact and loud on failure."""

    def _base_yaml(self, template: Path) -> Dict[str, Any]:
        seq = "A" * 40
        return {
            "sequences": [{"protein": {"id": "A", "sequence": seq}}],
            "templates": [{"pdb": str(template), "chain_id": ["A"]}],
        }

    def _write_template(self, root: Path, first_resnum: int, n_residues: int) -> Path:
        path = root / "template.pdb"
        lines = []
        for i in range(n_residues):
            resnum = first_resnum + i
            lines.append(
                "ATOM  {:5d}  CA  ALA {:1s}{:4d}    "
                "{:8.3f}{:8.3f}{:8.3f}  1.00 20.00           C".format(
                    i + 1, "A", resnum, 0.0, 0.0, float(i)
                )
            )
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_author_contacts_translate_to_sequence_positions(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with TemporaryDirectory() as tmp:
            template = self._write_template(Path(tmp), first_resnum=100, n_residues=40)
            author, sequence = _pocket_contacts_for_staged_space(
                self._base_yaml(template),
                {"peptidePocketResidues": "A:106,A:107"},
            )
            self.assertEqual(author, [("A", 106), ("A", 107)])
            self.assertEqual(sequence, [("A", 7), ("A", 8)])

    def test_unresolvable_residue_fails_loud(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with TemporaryDirectory() as tmp:
            template = self._write_template(Path(tmp), first_resnum=100, n_residues=10)
            with self.assertRaises(ValueError):
                _pocket_contacts_for_staged_space(
                    self._base_yaml(template),
                    {"peptidePocketResidues": "A:999"},
                )

    def test_missing_templates_fail_loud(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with self.assertRaises(ValueError):
            _pocket_contacts_for_staged_space(
                {"sequences": [{"protein": {"id": "A", "sequence": "ACDE"}}]},
                {"peptidePocketResidues": "A:2"},
            )

    def test_explicit_center_selects_surrounding_residues(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with TemporaryDirectory() as tmp:
            # residues laid out along z: index i at z=i, author 26+i
            template = self._write_template(Path(tmp), first_resnum=26, n_residues=40)
            author, sequence = _pocket_contacts_for_staged_space(
                self._base_yaml(template),
                {"peptidePocketCenter": "0.0,0.0,5.0"},
            )
            self.assertTrue(author)
            # residues within 6 A of z=5 are author 26+0..26+11 clipped to
            # |i-5|<=6 -> author 25..37 exclusive of unresolvable ones
            self.assertTrue(all(26 <= n <= 37 for _, n in author))
            translated = {n for _, n in sequence}
            self.assertEqual(len(translated), len(sequence))

    def test_center_radius_honors_pocket_box_option(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with TemporaryDirectory() as tmp:
            # residues at z=i (1 A spacing), author 26+i; radius 4 around z=5
            # selects |i-5|<=4 -> author 27..35 only
            template = self._write_template(Path(tmp), first_resnum=26, n_residues=40)
            author, _ = _pocket_contacts_for_staged_space(
                self._base_yaml(template),
                {"peptidePocketCenter": "0.0,0.0,5.0", "peptidePocketBox": 4},
            )
            self.assertTrue(author)
            self.assertTrue(all(27 <= n <= 35 for _, n in author))

    def test_plain_positions_without_template_are_sequence_contacts(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        author, sequence = _pocket_contacts_for_staged_space(
            {"sequences": [{"protein": {"id": "A", "sequence": "ACDEFGHIKL"}}]},
            {"peptidePocketResidues": "3,4,5"},
            "A",
        )
        self.assertEqual(author, [("A", 3), ("A", 4), ("A", 5)])
        self.assertEqual(sequence, [("A", 3), ("A", 4), ("A", 5)])

    def test_plain_positions_default_to_first_protein_chain(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        author, sequence = _pocket_contacts_for_staged_space(
            {"sequences": [{"protein": {"id": "Z", "sequence": "ACDE"}}]},
            {"peptidePocketResidues": "2"},
        )
        self.assertEqual(author, [("Z", 2)])
        self.assertEqual(sequence, [("Z", 2)])

    def test_plain_positions_out_of_range_fail_loud(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with self.assertRaises(ValueError):
            _pocket_contacts_for_staged_space(
                {"sequences": [{"protein": {"id": "A", "sequence": "ACDE"}}]},
                {"peptidePocketResidues": "9"},
                "A",
            )

    def test_plain_positions_with_template_translate_to_author(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with TemporaryDirectory() as tmp:
            template = self._write_template(Path(tmp), first_resnum=100, n_residues=40)
            author, sequence = _pocket_contacts_for_staged_space(
                self._base_yaml(template),
                {"peptidePocketResidues": "7"},
                "A",
            )
            # sequence position 7 lives at author 106; the YAML constraint
            # keeps author numbering and the engines remap it back
            self.assertEqual(author, [("A", 106)])
            self.assertEqual(sequence, [("A", 7)])

    def test_chain_prefixed_residues_still_require_template(self) -> None:
        from backend.runtime.run_single_prediction import _pocket_contacts_for_staged_space
        with self.assertRaises(ValueError):
            _pocket_contacts_for_staged_space(
                {"sequences": [{"protein": {"id": "A", "sequence": "ACDE"}}]},
                {"peptidePocketResidues": "A:2"},
                "A",
            )


class PocketContactReportTest(unittest.TestCase):
    def test_min_distance_and_gate_semantics(self) -> None:
        import gemmi
        import numpy as np
        from backend.runtime.run_single_prediction import (
            POCKET_CONTACT_MAX_A,
            _pocket_contact_report,
        )
        st = gemmi.Model("1")
        chain = gemmi.Chain("A")
        for idx, (name, z) in enumerate((("ALA", 0.0), ("GLY", 10.0))):
            res = gemmi.Residue()
            res.name = name
            res.seqid = gemmi.SeqId(idx + 1, " ")
            res.het_flag = "A"
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(0.0, 0.0, z)
            res.add_atom(atom)
            chain.add_residue(res)
        st.add_chain(chain)
        pep = gemmi.Chain("B")
        res = gemmi.Residue()
        res.name = "LYS"
        res.seqid = gemmi.SeqId(1, " ")
        res.het_flag = "A"
        atom = gemmi.Atom()
        atom.name = "CA"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(0.0, 0.0, 4.0)
        res.add_atom(atom)
        pep.add_residue(res)
        st.add_chain(pep)
        structure = gemmi.Structure()
        structure.add_model(st)
        structure.setup_entities()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "complex.pdb"
            structure.write_pdb(str(path))
            report = _pocket_contact_report(path, [("A", 1)])
            self.assertAlmostEqual(report["pocket_min_distance"], 4.0, places=3)
            self.assertGreaterEqual(POCKET_CONTACT_MAX_A, report["pocket_min_distance"])
            far = _pocket_contact_report(path, [("A", 2)])
            self.assertAlmostEqual(far["pocket_min_distance"], 6.0, places=3)

    def test_boltz_chain_suffix_matches(self) -> None:
        """boltz2 renames processed chains A -> A1; pocket matching must be
        suffix-insensitive or every refined pose fails the gate."""
        import gemmi
        from backend.runtime.run_single_prediction import _pocket_contact_report
        st = gemmi.Model("1")
        rec = gemmi.Chain("A1")
        for idx in range(3):
            res = gemmi.Residue()
            res.name = "GLY"
            res.seqid = gemmi.SeqId(idx + 1, " ")
            res.het_flag = "A"
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(0.0, 0.0, float(idx))
            res.add_atom(atom)
            rec.add_residue(res)
        st.add_chain(rec)
        pep = gemmi.Chain("B1")
        res = gemmi.Residue()
        res.name = "LYS"
        res.seqid = gemmi.SeqId(1, " ")
        res.het_flag = "A"
        atom = gemmi.Atom()
        atom.name = "CA"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(0.0, 0.0, 3.5)
        res.add_atom(atom)
        pep.add_residue(res)
        st.add_chain(pep)
        structure = gemmi.Structure()
        structure.add_model(st)
        structure.setup_entities()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "refined.cif"
            structure.make_mmcif_document().write_file(str(path))
            report = _pocket_contact_report(path, [("A", 1)])
            self.assertAlmostEqual(report["pocket_min_distance"], 3.5, places=3)


class StagedBicyclicLinkRestoreTest(unittest.TestCase):
    """gemmi 0.7.5's write_pdb drops connections; the staged PDB must regain
    its SG<->anchor LINK records after every rewrite or the refine diffusion
    breaks the ring."""

    def _write_staged(self, root: Path) -> Path:
        import gemmi
        model = gemmi.Model("1")
        rec = gemmi.Chain("A")
        for idx in range(30):
            res = gemmi.Residue()
            res.name = "GLY"
            res.seqid = gemmi.SeqId(idx + 1, " ")
            res.het_flag = "A"
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(0.0, 0.0, float(idx % 10))
            res.add_atom(atom)
            rec.add_residue(res)
        model.add_chain(rec)
        pep = gemmi.Chain("B")
        for idx, name in enumerate(("CYS", "GLY", "GLY", "CYS", "GLY", "CYS")):
            res = gemmi.Residue()
            res.name = name
            res.seqid = gemmi.SeqId(idx + 1, " ")
            res.het_flag = "A"
            for atom_name, element in (("N", "N"), ("CA", "C"), ("SG", "S")):
                if name != "CYS" and atom_name == "SG":
                    continue
                atom = gemmi.Atom()
                atom.name = atom_name
                atom.element = gemmi.Element(element)
                atom.pos = gemmi.Position(10.0 + idx, 0.0, 0.0 if atom_name != "SG" else 2.0)
                res.add_atom(atom)
            pep.add_residue(res)
        model.add_chain(pep)
        lnk = gemmi.Chain("L")
        res = gemmi.Residue()
        res.name = "SEZ"
        res.seqid = gemmi.SeqId(1, " ")
        res.het_flag = "H"
        for atom_name in ("CD", "C1", "C2"):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(12.0, 0.0, 2.5)
            res.add_atom(atom)
        lnk.add_residue(res)
        model.add_chain(lnk)
        structure = gemmi.Structure()
        structure.add_model(model)
        structure.setup_entities()
        path = root / "staged.pdb"
        structure.write_pdb(str(path))
        return path

    def test_links_restored_after_gemmi_rewrite(self) -> None:
        import gemmi
        from backend.runtime.run_single_prediction import _append_staged_bicyclic_links
        with TemporaryDirectory() as tmp:
            staged = self._write_staged(Path(tmp))
            self.assertFalse(
                any(l.startswith("LINK") for l in staged.read_text().splitlines()))
            added = _append_staged_bicyclic_links(
                staged, cys_positions=[0, 3, 5], linker_ccd="SEZ")
            self.assertEqual(added, 3)
            st = gemmi.read_structure(str(staged))
            st.setup_entities()
            self.assertEqual(len(st.connections), 3)
            self.assertTrue(all(c.type.name == "Covale" for c in st.connections))
            pairs = sorted(
                (c.partner1.atom_name, c.partner2.atom_name)
                for c in st.connections
            )
            self.assertEqual(pairs, [("SG", "C1"), ("SG", "C2"), ("SG", "CD")])
            # idempotent: a second append is a no-op
            self.assertEqual(_append_staged_bicyclic_links(staged, [0, 3, 5], "SEZ"), 0)

    def test_anchor_count_mismatch_fails_loud(self) -> None:
        from backend.runtime.run_single_prediction import _append_staged_bicyclic_links
        with TemporaryDirectory() as tmp:
            staged = self._write_staged(Path(tmp))
            with self.assertRaises(ValueError):
                _append_staged_bicyclic_links(staged, cys_positions=[0], linker_ccd="SEZ")


if __name__ == "__main__":
    unittest.main()


class StructureIntegrityGateTest(unittest.TestCase):
    """CA-CB bond integrity gate: a CB pushed tens of A away passes the
    chirality sign gate, so this is the only guard that catches it."""

    def _write(self, root, ca_cb_dist):
        import gemmi
        st = gemmi.Model("1")
        ch = gemmi.Chain("B")
        res = gemmi.Residue()
        res.name = "LEU"
        res.seqid = gemmi.SeqId(4, " ")
        res.het_flag = "A"
        for name, xyz in (("N", (1.3, 0.0, 0.0)), ("CA", (0.0, 0.0, 0.0)),
                          ("C", (-0.5, 1.4, 0.0)), ("CB", (-0.4, -0.5, ca_cb_dist))):
            atom = gemmi.Atom()
            atom.name = name
            atom.element = gemmi.Element("C" if name != "N" else "N")
            atom.pos = gemmi.Position(*xyz)
            res.add_atom(atom)
        ch.add_residue(res)
        st.add_chain(ch)
        structure = gemmi.Structure()
        structure.add_model(st)
        path = Path(root) / "complex.pdb"
        structure.write_pdb(str(path))
        return path

    def test_intact_bond_passes(self) -> None:
        from backend.runtime.run_single_prediction import _structure_integrity_report
        with TemporaryDirectory() as tmp:
            path = self._write(tmp, 1.53)
            report = _structure_integrity_report(path)
            self.assertTrue(report["all_intact"])
            self.assertEqual(report["broken_bonds"], [])

    def test_pushed_cb_is_caught(self) -> None:
        from backend.runtime.run_single_prediction import _structure_integrity_report
        with TemporaryDirectory() as tmp:
            path = self._write(tmp, 30.4)
            report = _structure_integrity_report(path)
            self.assertFalse(report["all_intact"])
            self.assertEqual(len(report["broken_bonds"]), 1)
            self.assertEqual(report["broken_bonds"][0]["resnum"], 4)
            self.assertAlmostEqual(report["broken_bonds"][0]["ca_cb"], 30.4, places=1)
