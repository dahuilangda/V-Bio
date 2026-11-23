import unittest

from backend.runtime.backends import structure_backend
from backend.runtime.backends.base import BackendRunContext
from backend.runtime.backends.request import PredictionRequest

YAML_LINEAR_BOND = """
sequences:
  - protein:
      id: B
      sequence: ACDEFGHIKLMNPQRST
      modifications:
        - position: 5
          ccd: DRG
constraints:
  - bond:
      atom1: [B, 1, N]
      atom2: [B, 17, C]
"""


class PredictionRequestTests(unittest.TestCase):
    def test_from_yaml_parses_entities_bonds_modifications(self):
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        self.assertEqual(len(request.entities), 1)
        entity = request.entities[0]
        self.assertEqual(entity.kind, "protein")
        self.assertEqual(entity.chain_id, "B")
        self.assertEqual(len(entity.sequence), 17)
        self.assertEqual(entity.modifications[0].ccd, "DRG")
        self.assertEqual(len(request.bonds), 1)
        bond = request.bonds[0]
        self.assertEqual((bond.chain1, bond.residue1, bond.atom1), ("B", 1, "N"))
        self.assertEqual((bond.chain2, bond.residue2, bond.atom2), ("B", 17, "C"))

    def test_to_yaml_round_trip(self):
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        rebuilt = PredictionRequest.from_yaml(request.to_yaml())
        self.assertEqual(rebuilt.entities[0].sequence, request.entities[0].sequence)
        self.assertEqual(rebuilt.bonds, request.bonds)

    def test_sequence_lengths(self):
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        self.assertEqual(request.sequence_lengths(), {"B": 17})


class EsmFold2BackendTests(unittest.TestCase):
    def test_bond_supported_contact_rejected(self):
        backend = structure_backend("esmfold2")
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        backend.validate(request)
        request.other_constraints.append({"contact": {"token1": [1, 3]}})
        with self.assertRaises(ValueError):
            backend.validate(request)

    def test_unknown_bond_chain_rejected(self):
        backend = structure_backend("esmfold2")
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        request.bonds[0].chain1 = "Z"
        with self.assertRaises(ValueError):
            backend.validate(request)

    def test_bond_residue_out_of_range_rejected(self):
        backend = structure_backend("esmfold2")
        request = PredictionRequest.from_yaml(YAML_LINEAR_BOND)
        request.bonds[0].residue2 = 99
        with self.assertRaises(ValueError):
            backend.validate(request)

    def test_registry_returns_esmfold2(self):
        backend = structure_backend("esmfold2")
        self.assertEqual(backend.name, "esmfold2")
        with self.assertRaises(ValueError):
            structure_backend("nesso")


if __name__ == "__main__":
    unittest.main()


class ModificationSupportTests(unittest.TestCase):
    def test_mol1_supports_standard_ccd_and_custom_upload(self):
        from backend.core.engine_capabilities import engine_modification_support
        support = engine_modification_support("mol1")
        self.assertTrue(support["standard_ccd"])
        self.assertTrue(support["custom_residue_upload"])

    def test_boltz_protenix_accept_custom_upload(self):
        from backend.core.engine_capabilities import engine_modification_support
        for name in ("boltz", "protenix"):
            self.assertTrue(engine_modification_support(name)["custom_residue_upload"])

    def test_esmfold2_declares_both(self):
        from backend.core.engine_capabilities import engine_modification_support
        support = engine_modification_support("esmfold2")
        self.assertTrue(support["standard_ccd"])
        self.assertTrue(support["custom_residue_upload"])
