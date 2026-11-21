"""自定义 CCD 编码冲突自动重命名：与标准集合冲突时改 Z 编码并同步 yaml。"""
import unittest

from backend.runtime.custom_ccd_builder import (
    apply_custom_ccd_code_remap,
    resolve_custom_ccd_code_collisions,
)


class ResolveCustomCcdCodeCollisionsTests(unittest.TestCase):
    def test_colliding_codes_renamed_to_z_pool(self):
        molecules = [
            {"ccd": "ALA", "smiles": "CC(N)C(=O)O", "kind": "residue"},
            {"ccd": "DLY", "smiles": "CC(C)CC(N)C(=O)O", "kind": "residue"},
        ]
        remapped, remap = resolve_custom_ccd_code_collisions(
            molecules, {"ALA", "ARG", "DLY"}, protected_codes={"ALA"})
        self.assertEqual(remap, {"DLY": "Z001"})
        self.assertEqual(molecules[1]["ccd"], "Z001")
        self.assertEqual(molecules[0]["ccd"], "ALA")

    def test_z_pool_skips_occupied_slots(self):
        taken = {"ALA", "Z001", "Z002"}
        remapped, remap = resolve_custom_ccd_code_collisions(
            [{"ccd": "ALA"}], taken)
        self.assertEqual(remap, {"ALA": "Z003"})

    def test_empty_base_codes_fail_open(self):
        molecules = [{"ccd": "DLY", "smiles": "C"}]
        remapped, remap = resolve_custom_ccd_code_collisions(molecules, set())
        self.assertEqual(remap, {})
        self.assertEqual(molecules[0]["ccd"], "DLY")

    def test_yaml_remap_updates_entity_and_modifications(self):
        yaml_doc = (
            "sequences:\n"
            "  - protein:\n"
            "      id: [A]\n"
            "      sequence: AGHC\n"
            "      modifications:\n"
            "        - position: 3\n"
            "          ccd: DLY\n"
            "  - ligand:\n"
            "      id: [L]\n"
            "      ccd: TST1\n"
        )
        remapped = apply_custom_ccd_code_remap(yaml_doc, {"DLY": "Z001", "TST1": "Z002"})
        self.assertIn("ccd: Z001", remapped)
        self.assertIn("ccd: Z002", remapped)
        self.assertNotIn("DLY", remapped)
        self.assertNotIn("TST1", remapped)

    def test_yaml_remap_noop_without_entries(self):
        self.assertEqual(apply_custom_ccd_code_remap("sequences: []", {}), "sequences: []")


if __name__ == "__main__":
    unittest.main()
