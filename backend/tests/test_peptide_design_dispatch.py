from __future__ import annotations

import inspect

import pytest
import yaml

from backend.core.errors import UserInputError
from backend.runtime import peptide_design_backend, design_strategy
from backend.runtime.backends import structure_backend
from backend.runtime.ccd import CcdCatalog
from backend.runtime.peptide_design_backend import (
    _candidate_ccd_probe_yaml,
    run_peptide_design_backend,
)


class ReachedEnvironmentSetup(Exception):
    pass


class StubCatalog(CcdCatalog):
    def __init__(self, codes) -> None:
        super().__init__()
        self._codes = frozenset(codes)

    def read(self) -> frozenset:
        return self._codes


@pytest.fixture(autouse=True)
def engine_dictionaries(monkeypatch):
    for name in ("protenix", "mol1"):
        monkeypatch.setattr(structure_backend(name), "_catalog", StubCatalog({"NLE"}))


@pytest.fixture
def stop_before_environment_setup(monkeypatch):
    def stop(_natural_pool, _design_mode):
        raise ReachedEnvironmentSetup()

    monkeypatch.setattr(peptide_design_backend, "_peptide_allowed_residues", stop)


TARGET_YAML = "version: 1\nsequences:\n  - protein:\n      id: A\n      sequence: NLYIQWLKDGGPSSGRPPPS\n"
NLE = {"ccd": "NLE", "smiles": "N[C@@H](CCCCC)C(=O)O", "kind": "residue", "base_residue": "L"}
ENGINES = ("protenix2dock", "mol1")


def dispatch(tmp_path, fixed_modifications, molecules, backend="protenix2dock", **options):
    return run_peptide_design_backend(
        temp_dir=str(tmp_path),
        yaml_content=TARGET_YAML,
        output_archive_path=str(tmp_path / "out.zip"),
        backend=backend,
        predict_args={},
        model_name=None,
        seed=1,
        options={"peptideFixedModifications": fixed_modifications, **options},
        target_chain_id="A",
        progress_path=None,
        custom_ccd_molecules=molecules)


@pytest.mark.parametrize("backend", ENGINES)
def test_dispatch_accepts_a_fixed_modification_defined_in_the_manifest(tmp_path, stop_before_environment_setup, backend):
    with pytest.raises(ReachedEnvironmentSetup):
        dispatch(tmp_path, [{"position": 3, "ccd": "NLE"}], [NLE], backend=backend)


@pytest.mark.parametrize("backend", ENGINES)
def test_dispatch_rejects_a_fixed_modification_the_engine_cannot_resolve(tmp_path, stop_before_environment_setup, backend):
    with pytest.raises(UserInputError, match="ZAC"):
        dispatch(tmp_path, [{"position": 1, "ccd": "ZAC"}], [], backend=backend)


def test_dispatch_rejects_stacked_fixed_modifications(tmp_path, stop_before_environment_setup):
    with pytest.raises(UserInputError, match="叠加了多个修饰"):
        dispatch(tmp_path, [{"position": 1, "ccd": "ZAC"}, {"position": 1, "ccd": "NLE"}], [NLE])


def test_dispatch_rejects_a_code_the_scorer_cannot_spell(tmp_path, stop_before_environment_setup):
    with pytest.raises(UserInputError):
        dispatch(tmp_path, [{"position": 3, "ccd": "NLE_TBA"}], [])


def test_dispatch_rejects_a_molecule_that_cannot_be_built(tmp_path, stop_before_environment_setup):
    amide = dict(NLE, smiles="N[C@@H](CCCCC)C(=O)N")
    with pytest.raises(ValueError, match="terminal carboxyl"):
        dispatch(tmp_path, [], [amide])


def test_probe_carries_every_modification_a_candidate_may_get():
    probe = _candidate_ccd_probe_yaml(
        {"sequences": [{"protein": {"id": "A", "sequence": "GGG"}}]}, "B", 4,
        [{"position": 9, "ccd": "NLE"}, {"position": 2, "ccd": "NLE"}, {"position": 4, "ccd": "MSE"}],
        [{"ccd": "PCA", "placement": "n_term"}, {"ccd": "AIB", "placement": "any"},
         {"ccd": "Z001", "placement": "c_term"}, {"ccd": "ORN", "placement": "any"}],
        {"MSE"})
    sequences = yaml.safe_load(probe)["sequences"]
    assert sequences[0] == {"protein": {"id": "A", "sequence": "GGG"}}
    assert sequences[1]["protein"]["sequence"] == "AAAAAA"
    assert [(m["position"], m["ccd"]) for m in sequences[1]["protein"]["modifications"]] == [
        (2, "NLE"), (4, "MSE"), (1, "PCA"), (5, "AIB"), (4, "Z001"), (6, "ORN")]


class ReachedStrategyConstruction(Exception):
    pass


@pytest.mark.parametrize("backend, options, owner, builder", [
    ("protenix2dock", {"peptideIterations": 2}, peptide_design_backend, "_build_esm3_strategy"),
    ("mol1", {"peptideProposerBackend": "rfd4"}, design_strategy.Rfd4Strategy, "__init__"),
])
def test_dispatch_builds_the_strategy_with_arguments_its_signature_accepts(
        tmp_path, monkeypatch, backend, options, owner, builder):
    signature = inspect.signature(getattr(owner, builder))
    built = {}

    def build(*args, **kwargs):
        signature.bind(*args, **kwargs)
        built.update(kwargs)
        raise ReachedStrategyConstruction()

    monkeypatch.setattr(owner, builder, build)
    with pytest.raises(ReachedStrategyConstruction):
        dispatch(tmp_path, [], [], backend=backend, **options)
    assert built["design_mode"] == "linear"
    if owner is design_strategy.Rfd4Strategy:
        assert built["queue"] == design_strategy.rfd4_generation_queue()
        assert built["template_inputs"] is None
