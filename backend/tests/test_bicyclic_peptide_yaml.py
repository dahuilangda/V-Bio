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


if __name__ == "__main__":
    unittest.main()
