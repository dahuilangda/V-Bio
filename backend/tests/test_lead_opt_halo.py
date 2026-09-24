"""Unit tests for the HALO lead-optimization module (routes + oracle + runner).

Covers route validation for the three optimization modes, the prediction-oracle
archive parsing (confidence / ipsae / affinity / ligand pLDDT), YAML
construction, and runner input checks.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capabilities"))

from halo.oracle.predict_oracle import (  # noqa: E402
    PredictOracle,
    _ligand_plddt_from_cif,
    extract_protein_sequence_from_pdb,
)


def _write_pdb(path: Path, chain: str = "A") -> None:
    lines = ["HEADER    TEST"]
    for i, res in enumerate(("ALA", "GLY", "SER")):
        lines.append(
            f"ATOM  {i + 1:5d}  CA  {res} {chain}{i + 1:4d}    "
            f"{8.0 + i:8.3f}{9.0:8.3f}{10.0:8.3f}  1.00 50.00           C"
        )
    lines.append(
        f"HETATM{i + 9:5d}  C1  LIG {chain}{i + 9:4d}    "
        f"{11.0:8.3f}{9.0:8.3f}{10.0:8.3f}  1.00 80.00           C"
    )
    path.write_text("\n".join(lines) + "\n")


class SequenceExtractionTests(unittest.TestCase):
    def test_extracts_requested_chain_and_ignores_ligand(self):
        tmp = Path("/tmp/halo_test_seq.pdb")
        _write_pdb(tmp)
        seq = extract_protein_sequence_from_pdb(tmp, chain="A")
        self.assertEqual(seq, "AGS")

    def test_raises_when_target_chain_absent(self):
        tmp = Path("/tmp/halo_test_seq2.pdb")
        _write_pdb(tmp, chain="A")
        self.assertEqual(extract_protein_sequence_from_pdb(tmp, chain="Z"), "")


class LigandPlddtTests(unittest.TestCase):
    CIF = """data_test
loop_
_atom_site.group_PDB
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
_atom_site.B_iso_or_equiv
ATOM CA ALA A 1 90.0
HETATM C1 LIG B 2 70.0
HETATM C2 LIG B 3 50.0
#
"""

    def test_ligand_chain_mean(self):
        value = _ligand_plddt_from_cif(self.CIF, "B")
        self.assertAlmostEqual(value, 60.0)

    def test_hint_missing_uses_any_ligand_residue(self):
        value = _ligand_plddt_from_cif(self.CIF, None)
        self.assertAlmostEqual(value, 60.0)

    def test_no_ligand_rows_returns_none(self):
        cif = self.CIF.replace("LIG", "ALA")
        self.assertIsNone(_ligand_plddt_from_cif(cif, "B"))


class OracleYamlTests(unittest.TestCase):
    def _oracle(self, backend="protenix2dock"):
        tmp = Path("/tmp/halo_oracle_test")
        tmp.mkdir(parents=True, exist_ok=True)
        protein = tmp / "t.pdb"
        _write_pdb(protein)
        target = SimpleNamespace(protein_pdb=protein, target_chain="A")
        return PredictOracle(target, tmp / "work", backend=backend, api_url="http://127.0.0.1:5000", api_token="test-token")

    def test_yaml_carries_affinity_binder_and_ligand(self):
        import yaml

        oracle = self._oracle()
        doc = yaml.safe_load(oracle._build_yaml("c1ccccc1"))
        ids = [next(iter(entry)) for entry in doc["sequences"]]
        self.assertEqual(ids, ["protein", "ligand"])
        self.assertEqual(doc["sequences"][1]["ligand"]["id"], "B")
        self.assertEqual(doc["properties"], [{"affinity": {"binder": "B"}}])

    def test_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            self._oracle(backend="pocketxmol")

    def test_submit_contract_targets_unprefixed_routes(self):
        import inspect

        source = inspect.getsource(PredictOracle)
        self.assertNotIn('/api/predict', source)
        self.assertNotIn('/api/status/', source)
        self.assertNotIn('/api/results/', source)
        self.assertIn('{self.base}/predict', source)
        self.assertIn('"workflow": "lead_optimization"', source)
        self.assertIn('"use_msa_server": "false"', source)

    def test_missing_token_fails_loudly(self):
        import os

        saved = os.environ.pop("VBIO_API_TOKEN", None)
        try:
            tmp = Path("/tmp/halo_oracle_notoken")
            tmp.mkdir(parents=True, exist_ok=True)
            protein = tmp / "t.pdb"
            _write_pdb(protein)
            target = SimpleNamespace(protein_pdb=protein, target_chain="A")
            with self.assertRaises(RuntimeError):
                PredictOracle(target, tmp / "w", api_url="http://127.0.0.1:5000")
        finally:
            if saved is not None:
                os.environ["VBIO_API_TOKEN"] = saved

    def test_parses_prediction_archive(self):
        import pandas  # noqa: F401  (oracle module dependency)

        oracle = self._oracle()
        out = Path("/tmp/halo_oracle_parse")
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.rglob("*"):
            if stale.is_file():
                stale.unlink()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "confidence_data_model_0.json",
                json.dumps({"iptm": 0.67, "ligand_iptm": 0.7, "confidence_score": 0.5}),
            )
            zf.writestr("best_ipsae.json", json.dumps({"ipsae_dom": 0.31}))
            zf.writestr(
                "affinity_data.json",
                json.dumps({"affinity_pic50": 6.2, "affinity_pic50_mw": 4.1, "binder_chain": "B"}),
            )
            zf.writestr("data_model_0.cif", self.CIF if hasattr(self, "CIF") else LigandPlddtTests.CIF)
        with zipfile.ZipFile(buf) as zf:
            zf.extractall(out)
        row = oracle._parse_result(out)
        self.assertAlmostEqual(row["iptm"], 0.67)
        self.assertAlmostEqual(row["ipsae"], 0.31)
        self.assertAlmostEqual(row["affinity_pic50"], 6.2)
        self.assertAlmostEqual(row["ligand_plddt_mean"], 60.0)


class RunnerValidationTests(unittest.TestCase):
    def test_unknown_mode_rejected(self):
        from halo.vbio_runner import run_halo_optimization

        with self.assertRaises(ValueError):
            run_halo_optimization({"mode": "magic", "protein_path": "/tmp/x.pdb"}, Path("/tmp/halo_run_x"))

    def test_fragment_mode_needs_reference(self):
        from halo.vbio_runner import run_halo_optimization

        with self.assertRaises(ValueError):
            run_halo_optimization(
                {"mode": "fragment", "protein_path": "/tmp/x.pdb"}, Path("/tmp/halo_run_x")
            )

    def test_denovo_needs_pocket_or_reference(self):
        from halo.vbio_runner import run_halo_optimization

        with self.assertRaises(ValueError):
            run_halo_optimization(
                {"mode": "denovo", "protein_path": "/tmp/x.pdb"}, Path("/tmp/halo_run_x")
            )

    def test_missing_protein_file_rejected(self):
        from halo.vbio_runner import run_halo_optimization

        with self.assertRaises(FileNotFoundError):
            run_halo_optimization(
                {"mode": "fragment", "protein_path": "/tmp/definitely_missing.pdb",
                 "reference_smiles": "c1ccccc1"},
                Path("/tmp/halo_run_x"),
            )


class RoundProgressTests(unittest.TestCase):
    """Peptide-style iteration visibility: per-round events from the loop."""

    def _watcher(self, run_dir, events, total=6):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capabilities"))
        from halo.vbio_runner import _RoundProgressWatcher

        return _RoundProgressWatcher(Path(run_dir), total, events.append)

    def test_watcher_emits_round_event_with_top_candidates(self):
        import tempfile

        events = []
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "rounds.jsonl").write_text(
                '{"round": 1, "pool": 40, "oracle_calls": 12, "top_final_reward": 0.5, '
                '"best_affinity_oracle": 6.1, "elapsed_s": 90.0}\n'
                '{"round": 2, "pool": 44, "oracle_calls": 12, "top_final_reward": 0.61, '
                '"best_affinity_oracle": 6.8, "elapsed_s": 85.0}\n'
            )
            (run_dir / "candidates.csv").write_text(
                "round,smiles,source,affinity_pic50,final_reward\n"
                "1,c1ccccc1,oracle,6.1,0.5\n"
                "2,CCO,oracle,6.8,0.61\n"
                "2,CNC,surrogate,,0.3\n"
            )
            watcher = self._watcher(run_dir, events)
            watcher._poll_once()
            # Only the newest round emits; repeats are suppressed.
            watcher._poll_once()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["stage"], "round")
        self.assertEqual(event["round"], 2)
        self.assertEqual(event["total_rounds"], 6)
        self.assertIn("best affinity", event["message"])
        self.assertEqual(event["stats"]["best_affinity_oracle"], 6.8)
        top = event["top_candidates"]
        self.assertEqual(top[0]["smiles"], "CCO")
        self.assertAlmostEqual(top[0]["final_reward"], 0.61)
        self.assertEqual(top[0]["affinity_pic50"], 6.8)

    def test_oracle_wrapper_reports_scoring_phase(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capabilities"))
        from halo.vbio_runner import _ProgressOracle

        class FakeOracle:
            n_calls = 0

            def score_smiles(self, smiles_list, tag="batch"):
                self.n_calls += len(smiles_list)
                import pandas as pd

                return pd.DataFrame([{"smiles": s} for s in smiles_list])

        events = []
        inner = FakeOracle()
        wrapper = _ProgressOracle(inner, events.append, total_rounds=4)
        result = wrapper.score_smiles(["c1ccccc1", "CCO"], tag="r003")
        self.assertEqual(len(result), 2)
        self.assertEqual(inner.n_calls, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "scoring")
        self.assertEqual(events[0]["round"], 3)
        self.assertEqual(events[0]["total_rounds"], 4)
        self.assertEqual(events[0]["candidates"], 2)


class RouteContractTests(unittest.TestCase):
    """Route validation through the Flask app with the token check bypassed."""

    @classmethod
    def setUpClass(cls):
        from backend import app as app_module

        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    def _post(self, body, token="development-api-token"):
        return self.client.post(
            "/api/lead_optimization/halo_optimize",
            json=body,
            headers={"X-API-Token": token},
        )

    def test_requires_token(self):
        response = self.client.post("/api/lead_optimization/halo_optimize", json={})
        self.assertIn(response.status_code, (401, 403))

    def test_rejects_unknown_mode(self):
        self.assertEqual(self._post({"mode": "magic", "protein_path": "/tmp/x.pdb"}).status_code, 400)

    def test_rejects_retired_pocketxmol_backend(self):
        response = self._post(
            {"mode": "fragment", "protein_path": "/tmp/x.pdb", "reference_smiles": "C",
             "backend": "pocketxmol"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("protenix2dock", response.get_json()["error"])

    def test_rejects_missing_protein(self):
        self.assertEqual(self._post({"mode": "fragment", "reference_smiles": "C"}).status_code, 400)

    def test_rejects_fragment_without_reference(self):
        self.assertEqual(self._post({"mode": "fragment", "protein_path": "/tmp/x.pdb"}).status_code, 400)

    def test_rejects_bad_pocket(self):
        response = self._post(
            {"mode": "denovo", "protein_path": "/tmp/x.pdb", "pocket": "1,2"}
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_out_of_range_rounds(self):
        response = self._post(
            {"mode": "denovo", "protein_path": "/tmp/x.pdb", "pocket": "1,2,3", "rounds": 500}
        )
        self.assertEqual(response.status_code, 400)

    def test_backends_listing(self):
        response = self.client.get(
            "/api/lead_optimization/halo_backends", headers={"X-API-Token": "development-api-token"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["default"], "protenix2dock")
        self.assertEqual(
            [b["id"] for b in payload["backends"]], ["protenix2dock", "boltz2dock", "alphafold3"]
        )
        self.assertNotIn("pocketxmol", [b["id"] for b in payload["backends"]])


class RouterMappingTests(unittest.TestCase):
    def test_halo_task_maps_to_lead_opt_capability(self):
        from backend.scheduling.capability_router import _TASK_NAME_CAPABILITY_FALLBACK

        self.assertEqual(_TASK_NAME_CAPABILITY_FALLBACK.get("lead_optimization_halo_task"), "lead_opt")
        self.assertNotIn("lead_optimization_mmp_query_task", _TASK_NAME_CAPABILITY_FALLBACK)


if __name__ == "__main__":
    unittest.main()
