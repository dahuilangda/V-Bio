from __future__ import annotations

import pytest

from backend.core.errors import UserInputError
from backend.runtime import peptide_design_backend
from backend.runtime.peptide_design_backend import run_peptide_design_backend


class ReachedEnvironmentSetup(Exception):
    pass


@pytest.fixture
def stop_before_environment_setup(monkeypatch):
    def stop(_yaml_data):
        raise ReachedEnvironmentSetup()

    monkeypatch.setattr(peptide_design_backend, "_extract_chain_ids_from_yaml", stop)


TARGET_YAML = "version: 1\nsequences:\n  - protein:\n      id: A\n      sequence: NLYIQWLKDGGPSSGRPPPS\n"
NLE = {"ccd": "NLE", "smiles": "N[C@@H](CCCCC)C(=O)O", "kind": "residue", "base_residue": "L"}


def dispatch(tmp_path, fixed_modifications, molecules, backend="protenix2dock"):
    return run_peptide_design_backend(
        temp_dir=str(tmp_path),
        yaml_content=TARGET_YAML,
        output_archive_path=str(tmp_path / "out.zip"),
        backend=backend,
        predict_args={},
        model_name=None,
        seed=1,
        options={"peptideFixedModifications": fixed_modifications},
        target_chain_id="A",
        progress_path=None,
        custom_ccd_molecules=molecules)


def test_dispatch_accepts_a_fixed_modification_defined_in_the_manifest(tmp_path, stop_before_environment_setup):
    with pytest.raises(ReachedEnvironmentSetup):
        dispatch(tmp_path, [{"position": 3, "ccd": "NLE"}], [NLE])


def test_dispatch_rejects_a_code_the_scorer_cannot_spell(tmp_path, stop_before_environment_setup):
    with pytest.raises(UserInputError):
        dispatch(tmp_path, [{"position": 3, "ccd": "NLE_TBA"}], [])


def test_dispatch_rejects_a_molecule_that_cannot_be_built(tmp_path, stop_before_environment_setup):
    amide = dict(NLE, smiles="N[C@@H](CCCCC)C(=O)N")
    with pytest.raises(ValueError, match="terminal carboxyl"):
        dispatch(tmp_path, [], [amide])
