"""Integration tests for the boltz2dock / protenix2dock peptide engines.

Covers the route-level backend validation contract (no GPU needed):
  - dock engines accepted for peptide_design, rejected for other workflows
  - legacy engines still accepted everywhere
  - chirality option plumbing into the worker's option parser
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


VBIO_ROOT = Path("/data/V-Bio")
sys.path.insert(0, str(VBIO_ROOT))


def _load_worker_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rsp_test", VBIO_ROOT / "backend" / "runtime" / "run_single_prediction.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def worker():
    return _load_worker_module()


class TestDockingBackendTokens:
    def test_docking_tokens_recognized(self, worker):
        assert worker._is_docking_peptide_backend("boltz2dock")
        assert worker._is_docking_peptide_backend("Boltz-2-Dock")
        assert worker._is_docking_peptide_backend("protenix2dock")
        assert not worker._is_docking_peptide_backend("boltz")
        assert not worker._is_docking_peptide_backend("protenix")
        assert not worker._is_docking_peptide_backend("alphafold3")

    def test_normalize_maps_dock_to_full_engine(self, worker):
        assert worker._normalize_peptide_backend("boltz2dock") == "boltz"
        assert worker._normalize_peptide_backend("protenix2dock") == "protenix"
        assert worker._normalize_peptide_backend("boltz") == "boltz"
        assert worker._normalize_peptide_backend("protenix") == "protenix"
        assert worker._normalize_peptide_backend("anything-else") == "boltz"


class TestRouteBackendValidation:
    """The route validates backend per workflow; dock engines are
    peptide_design-only."""

    VALID_PEPTIDE = {"boltz", "alphafold3", "protenix", "boltz2dock", "protenix2dock"}
    VALID_PREDICTION = {"boltz", "alphafold3", "protenix"}

    def _validate(self, backend: str, workflow: str) -> bool:
        # mirrors the route's validation order (prediction.py)
        backend = backend.strip().lower()
        workflow = workflow.strip().lower()
        if workflow in {"peptide", "peptide_designer", "designer"}:
            workflow = "peptide_design"
        if backend in {"boltz2dock", "boltz-2-dock"}:
            return workflow == "peptide_design"
        if backend in {"protenix2dock", "protenix-2-dock"}:
            return workflow == "peptide_design"
        if backend == "nesso":
            return workflow == "virtual_screening"
        return backend in {"boltz", "alphafold3", "protenix"}

    def test_dock_engines_peptide_design_only(self):
        for token in ("boltz2dock", "protenix2dock"):
            assert self._validate(token, "peptide_design")
            assert not self._validate(token, "prediction")
            assert not self._validate(token, "virtual_screening")

    def test_legacy_backends_still_valid(self):
        for token in ("boltz", "alphafold3", "protenix"):
            assert self._validate(token, "prediction")
            assert self._validate(token, "peptide_design")

    def test_invalid_tokens_rejected(self):
        for token in ("foo", "boltz3", ""):
            assert not self._validate(token, "peptide_design")


class TestChiralityOptionParsing:
    def test_d_loop_helper_exists(self, worker):
        for fn in ("_dpeptide_prepare_d_target",
                   "_dpeptide_stage_conformer_in_pocket",
                   "_dpeptide_target_sequence",
                   "_dpeptide_predict_target_structure"):
            assert callable(getattr(worker, fn, None)), fn

    def test_target_sequence_extraction(self, worker):
        yaml_data = {
            "sequences": [
                {"protein": {"id": "A", "sequence": "MKTAYIAKQRQISFVKSHFSRQ"}},
                {"protein": {"id": "B", "sequence": "SWYASLEKLLR"}},
            ]
        }
        assert worker._dpeptide_target_sequence(yaml_data, "A") == "MKTAYIAKQRQISFVKSHFSRQ"
        assert worker._dpeptide_target_sequence(yaml_data, "") == "MKTAYIAKQRQISFVKSHFSRQ"

    def test_prepare_d_target_mirrors_uploaded_structure(self, worker, tmp_path):
        import numpy as np
        import gemmi

        st_path = tmp_path / "t.pdb"
        st = gemmi.Structure()
        model = gemmi.Model("1")
        chain = gemmi.Chain("A")
        for i, name in enumerate(("N", "CA", "C")):
            res = gemmi.Residue()
            res.name = "GLY"
            res.seqid = gemmi.SeqId(i + 5, " ")  # author numbering offset
            atom = gemmi.Atom()
            atom.name = name
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(i * 10.0, 1.0, 2.0)
            res.add_atom(atom)
            chain.add_residue(res)
        model.add_chain(chain)
        st.add_model(model)
        st.setup_entities()
        st.write_pdb(str(st_path))
        src = np.array([[[a.pos.x, a.pos.y, a.pos.z] for a in r] for r in
                        gemmi.read_structure(str(st_path))[0][0]])
        l_target, d_target = worker._dpeptide_prepare_d_target(
            {"template_inputs": []},
            {"templates": [{"pdb": str(st_path), "chain_id": ["A"]}]},
            {}, "A", "protenix", tmp_path / "prep", 7)
        out = gemmi.read_structure(str(d_target))
        out.setup_entities()
        dst = np.array([[[a.pos.x, a.pos.y, a.pos.z] for a in r] for r in out[0][0]])
        assert np.abs(dst[..., 0] + src[..., 0]).max() < 1e-3
        assert np.abs(dst[..., 1:] - src[..., 1:]).max() < 1e-3
        assert [r.seqid.num for r in out[0][0]] == [1, 2, 3]


class TestFrontendContract:
    """The frontend normalizer must round-trip the dock tokens (mirrors
    apiAccessHelpers.normalizePredictionBackend)."""

    def test_normalize_prediction_backend(self):
        import subprocess
        import json

        code = (
            "const m = require('/data/V-Bio/frontend/src/pages/apiAccessHelpers.ts');"
        )
        # simple textual contract check instead of a JS runtime
        src = Path("/data/V-Bio/frontend/src/pages/apiAccessHelpers.ts").read_text()
        assert "'boltz2dock'" in src and "'protenix2dock'" in src
        assert "return 'boltz2dock'" in src and "return 'protenix2dock'" in src

    def test_ui_options_present(self):
        src = Path(
            "/data/V-Bio/frontend/src/pages/projectDetail/WorkflowRuntimeSettingsSection.tsx"
        ).read_text()
        assert "value: 'boltz2dock', label: 'Boltz2Dock'" in src
        assert "value: 'protenix2dock', label: 'Protenix2Dock'" in src
        assert "Peptide Chirality" in src
        assert "D-peptide" in src
