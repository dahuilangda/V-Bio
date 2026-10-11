from __future__ import annotations

import unittest

import yaml

from backend.runtime.run_single_prediction import (
    BICYCLIC_LINKER_ATOM_MAP,
    _build_peptide_candidate_yaml,
)


def _bonds(yaml_text: str) -> list[tuple]:
    data = yaml.safe_load(yaml_text)
    return [tuple(c["bond"].values()) for c in data.get("constraints") or []]


class BicyclicCandidateYamlTests(unittest.TestCase):
    BASE = {"sequences": [{"protein": {"id": "A", "sequence": "MKTLLLILAV"}}]}

    def _build(self, sequence, cys_positions=None):
        return _build_peptide_candidate_yaml(
            dict(self.BASE),
            binder_chain_id="B",
            binder_sequence=sequence,
            design_mode="bicyclic",
            linker_ccd="SEZ",
            linker_chain_id="L",
            linker_atom_map=BICYCLIC_LINKER_ATOM_MAP,
            modifications=[],
            backend="boltz",
            cys_positions=cys_positions,
        )

    def test_bonds_hit_explicit_anchor_positions(self):
        # Cys at 0-based 2/7/11 -> bonds reference 1-based residues 3/8/12
        text = self._build("AWCGHIKCLMNCQ", cys_positions=[2, 7, 11])
        bonds = _bonds(text)
        self.assertEqual(len(bonds), 3)
        self.assertEqual(
            sorted(bond[0] for bond in bonds),
            [["B", 3, "SG"], ["B", 8, "SG"], ["B", 12, "SG"]],
        )
        self.assertEqual(sorted(bond[1] for bond in bonds),
                         [["L", 1, "C1"], ["L", 1, "C2"], ["L", 1, "CD"]])

    def test_extra_cys_outside_anchors_is_not_bonded(self):
        # stray Cys at position 5 stays free; only the anchors bond
        text = self._build("AWCGHCKCLMNCQ", cys_positions=[2, 7, 11])
        bonds = _bonds(text)
        self.assertEqual(sorted(bond[0] for bond in bonds),
                         [["B", 3, "SG"], ["B", 8, "SG"], ["B", 12, "SG"]])

    def test_anchor_without_cysteine_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "does not hold a cysteine"):
            self._build("AWCGHIKCLMNAQ", cys_positions=[2, 7, 11])

    def test_scan_fallback_still_requires_three_cys(self):
        text = self._build("CWAGHIKCLMNCP")  # auto mode: no explicit anchors
        self.assertEqual(len(_bonds(text)), 3)
        with self.assertRaisesRegex(ValueError, "exactly 3 cysteine"):
            self._build("CWAGHIKXLMNAP")

    def test_supported_linkers_are_product_form_only(self):
        # SEZ = mesitylene (TBMB product form), 29N = triazinane tripropanone (LFI
        # product form). BS3 is the Bi(III) ion linker: all three Cys-SG coordinate to
        # the same atom ("BI"), unlike the covalent linkers' three distinct anchors.
        self.assertEqual(sorted(BICYCLIC_LINKER_ATOM_MAP), ["29N", "BS3", "SEZ"])
        for code, atoms in BICYCLIC_LINKER_ATOM_MAP.items():
            self.assertEqual(len(atoms), 3, code)
        self.assertEqual(len(set(BICYCLIC_LINKER_ATOM_MAP["BS3"])), 1)
        for code in ("SEZ", "29N"):
            self.assertEqual(len(set(BICYCLIC_LINKER_ATOM_MAP[code])), 3, code)


def _identity_resolve(length, fixed, cys_positions, cys_layout):
    import sys
    from pathlib import Path
    peplm_root = str(Path(__file__).resolve().parents[2] / "capabilities" / "peptide_lm")
    if peplm_root not in sys.path:
        sys.path.insert(0, peplm_root)
    from peplm.loop.constraints import resolve_bicyclic_anchors
    return resolve_bicyclic_anchors(length, fixed, cys_positions, cys_layout)


class RunnerGateImportTests(unittest.TestCase):
    """The finalize path calls plddt_gate_for_chirality at runtime; a missing
    import survived the test suite until 18 live tasks crashed at the gate
    (v15, 2026-10-11). Import-time reachability is the cheap guard."""

    def test_plddt_gate_for_chirality_reachable_in_runner(self):
        from backend.runtime import run_single_prediction as runner
        self.assertTrue(callable(runner.plddt_gate_for_chirality))
        self.assertEqual(runner.plddt_gate_for_chirality("l") >= 0, True)
        self.assertEqual(runner.plddt_gate_for_chirality("d") >= 0, True)


class StampBicyclicCysteinesTests(unittest.TestCase):
    """ESM3 proposals carry no topology: the stamp must produce a sequence
    the exactly-3-Cys YAML builder accepts (v15 gen-1 crash regression)."""

    def _stamp(self, sequence, **overrides):
        from backend.runtime.run_single_prediction import _stamp_bicyclic_cysteines
        params = dict(
            explicit_positions=[],
            cys_layout=None,
            sequence_mask="",
            allow_extra_cys=False,
            resolve_anchors=_identity_resolve,
        )
        params.update(overrides)
        return _stamp_bicyclic_cysteines(sequence, **params)

    def test_auto_layout_stamps_three_anchors(self):
        stamped, anchors = self._stamp("VGKRDKRGKRNPAKK")
        self.assertEqual(len(anchors), 3)
        self.assertEqual(stamped.count("C"), 3)
        for pos in anchors:
            self.assertEqual(stamped[pos], "C")
        # stamped output passes the candidate YAML builder unchanged
        text = self._build_yaml(stamped)
        self.assertEqual(len(_bonds(text)), 3)

    def test_stray_cys_demoted_outside_anchors(self):
        stamped, anchors = self._stamp("CGKRDKCGKRNPACK")
        self.assertEqual(stamped.count("C"), 3)

    def test_explicit_positions_win(self):
        stamped, anchors = self._stamp(
            "AGKRDKRGKRNPAKA", explicit_positions=[1, 8, 14])
        self.assertEqual(anchors, [1, 8, 14])
        self.assertEqual(stamped[1], "C")
        self.assertEqual(stamped[8], "C")
        self.assertEqual(stamped[14], "C")

    def test_mask_fixed_residue_not_demoted(self):
        mask = "C" + "X" * 13 + "C"
        stamped, anchors = self._stamp(
            "CGKRDKRGKRNPAK" + "C", sequence_mask=mask)
        # masked C at index 0 and 14 kept; stamp resolves interior anchor
        self.assertEqual(stamped[0], "C")
        self.assertEqual(stamped[14], "C")
        self.assertEqual(len(anchors), 3)

    def test_short_sequence_returned_untouched(self):
        seq = "AGK"
        stamped, anchors = self._stamp(seq)
        self.assertEqual(stamped, seq)
        self.assertEqual(anchors, [])

    @staticmethod
    def _build_yaml(sequence: str) -> str:
        from backend.runtime.run_single_prediction import _build_peptide_candidate_yaml
        return _build_peptide_candidate_yaml(
            {"sequences": [{"protein": {"id": "A", "sequence": "MKTLLLILAV"}}]},
            binder_chain_id="B",
            binder_sequence=sequence,
            design_mode="bicyclic",
            linker_ccd="SEZ",
            linker_chain_id="L",
            linker_atom_map=BICYCLIC_LINKER_ATOM_MAP,
            modifications=[],
            backend="boltz",
            cys_positions=None,
        )


if __name__ == "__main__":
    unittest.main()
