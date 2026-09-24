from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.runtime.af3_adapter import (
    build_af3_json,
    collect_chain_msa_paths,
    load_unpaired_msa,
    parse_yaml_for_af3,
)
from backend.runtime.protenix_adapter import (
    apply_protein_msa_paths,
    parse_yaml_for_protenix,
)
from backend.runtime.run_single_prediction import (
    MSA_CACHE_CONFIG,
    cache_msa_files_from_temp_dir,
    generate_msa_for_sequences,
    get_sequence_hash,
)
from backend.services.common_utils import (
    ProteinMsaMode,
    classify_protein_msa,
    extract_protein_msa_policies,
)


_DISABLED_MSA_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFG
      msa: empty
"""

_MIXED_MSA_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFG
      msa: empty
  - protein:
      id: B
      sequence: ACDEFG
"""

_EXTERNAL_MSA_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFG
"""

_MULTI_TYPE_YAML = """\
version: 1
sequences:
  - protein:
      id: P
      sequence: ACDEFG
  - dna:
      id: D
      sequence: ACGT
  - rna:
      id: R
      sequence: ACGU
  - ligand:
      id: L
      smiles: CCO
"""


class ProteinMsaPolicyTests(unittest.TestCase):
    def test_policy_classification_is_explicit(self) -> None:
        policies = extract_protein_msa_policies(_MIXED_MSA_YAML)

        self.assertEqual(
            [policy.mode for policy in policies],
            [ProteinMsaMode.DISABLED, ProteinMsaMode.EXTERNAL],
        )
        self.assertIs(classify_protein_msa(True), ProteinMsaMode.EXTERNAL)
        self.assertIs(classify_protein_msa(False), ProteinMsaMode.DISABLED)
        self.assertIs(classify_protein_msa("input.a3m"), ProteinMsaMode.PROVIDED)

    def test_af3_keeps_different_msa_policies_as_separate_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            msa_path = Path(temp_dir) / "B_msa.a3m"
            msa_path.write_text(">B\nACDEFG\n")

            prep = parse_yaml_for_af3(_MIXED_MSA_YAML)
            chain_paths = collect_chain_msa_paths(prep, temp_dir)
            unpaired_msa = load_unpaired_msa(prep, chain_paths)
            payload = build_af3_json(prep, unpaired_msa)

        proteins = {
            tuple(entry["protein"]["id"]): entry["protein"]
            for entry in payload["sequences"]
            if "protein" in entry
        }
        self.assertEqual(set(proteins), {("A",), ("B",)})
        self.assertEqual(proteins[("A",)]["unpairedMsa"], "")
        self.assertEqual(proteins[("A",)]["pairedMsa"], "")
        self.assertIn("ACDEFG", proteins[("B",)]["unpairedMsa"])

    def test_af3_msa_collection_uses_the_protein_policy_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            msa_path = Path(temp_dir) / "P_msa.a3m"
            msa_path.write_text(">P\nACDEFG\n")

            prep = parse_yaml_for_af3(_MULTI_TYPE_YAML)
            chain_paths = collect_chain_msa_paths(prep, temp_dir)

        self.assertEqual(set(prep.chain_id_to_sequence), {"P", "D", "R", "L"})
        self.assertEqual(set(prep.chain_id_to_msa_mode), {"P"})
        self.assertEqual(set(chain_paths), {"P"})

    @mock.patch("backend.runtime.run_single_prediction.request_msa_from_server")
    def test_disabled_msa_does_not_call_the_server(self, request_msa: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertTrue(generate_msa_for_sequences(_DISABLED_MSA_YAML, temp_dir))
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

        request_msa.assert_not_called()

    def test_cache_accepts_only_the_declared_a3m_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir) / "work"
            cache_dir = Path(temp_dir) / "cache"
            work_dir.mkdir()
            (work_dir / "metadata.csv").write_text("name,value\nA,ACDEFG\n")

            config = {
                "enable_cache": True,
                "cache_dir": str(cache_dir),
            }
            with mock.patch.dict(MSA_CACHE_CONFIG, config, clear=False):
                cache_msa_files_from_temp_dir(str(work_dir), _EXTERNAL_MSA_YAML)
                self.assertEqual(list(cache_dir.glob("*.a3m")), [])

                (work_dir / "A_msa.a3m").write_text(">A\nACDEFG\n")
                cache_msa_files_from_temp_dir(str(work_dir), _EXTERNAL_MSA_YAML)

            expected_path = cache_dir / f"msa_{get_sequence_hash('ACDEFG')}.a3m"
            self.assertTrue(expected_path.is_file())
            self.assertEqual(expected_path.read_text(), ">A\nACDEFG\n")

    def test_protenix_assigns_msa_only_to_enabled_entities(self) -> None:
        prep = parse_yaml_for_protenix(_MIXED_MSA_YAML)

        assigned = apply_protein_msa_paths(
            prep,
            {"B": "/workspace/msa/B.a3m"},
            disabled_msa_path="/workspace/msa/disabled.a3m",
        )

        protein_entries = [
            entry["proteinChain"]
            for entry in prep.payload[0]["sequences"]
            if "proteinChain" in entry
        ]
        self.assertEqual(assigned, 1)
        self.assertEqual(
            protein_entries[0]["unpairedMsaPath"],
            "/workspace/msa/disabled.a3m",
        )
        self.assertEqual(
            protein_entries[1]["unpairedMsaPath"],
            "/workspace/msa/B.a3m",
        )


if __name__ == "__main__":
    unittest.main()
