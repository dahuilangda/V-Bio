from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core import celery_app as celery_module
from backend.runtime import candidate_pool, rfd4_strategy, run_single_prediction
from backend.runtime.design_strategy import Candidate, Rfd4Strategy

TARGET = [{"name": "target.cif", "content": "data_target\n"}]


def generated_cif(residues=("CYS", "ALA", "NLE", "CYS", "GLY", "CYS")) -> str:
    rows = []
    atom_id = 0
    for index, name in enumerate(residues, start=1):
        for atom_name, symbol in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            atom_id += 1
            rows.append(
                f"ATOM {atom_id} {symbol} {atom_name} . {name} B 1 {index} ? "
                f"0.000 0.000 {index:.3f} 1.00 20.00 {index} {name} B {atom_name} 1")
    tags = ["group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id", "label_comp_id",
            "label_asym_id", "label_entity_id", "label_seq_id", "pdbx_PDB_ins_code", "Cartn_x",
            "Cartn_y", "Cartn_z", "occupancy", "B_iso_or_equiv", "auth_seq_id", "auth_comp_id",
            "auth_asym_id", "auth_atom_id", "pdbx_PDB_model_num"]
    return "\n".join(["data_g", "#", "loop_"] + [f"_atom_site.{tag}" for tag in tags] + rows) + "\n"


def write_archive(path: Path, files: dict[str, str | bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def linear_archive(path: Path) -> None:
    summary = {"outputs": [{
        "file": "runs/design_0/model.cif",
        "chains": {
            "A": {"n_residues": 20, "sequence": "T" * 20},
            "B": {"n_residues": 8, "sequence": "CREEECWA"},
        },
    }]}
    write_archive(path, {"design_0/model.cif": "data_design\n",
                         "summary.json": json.dumps(summary)})


class FinishedResult:
    def __init__(self, task_id, result):
        self.id = task_id
        self.state = "SUCCESS"
        self.result = result


def make_strategy(tmp_path, design_mode="linear", options=None) -> Rfd4Strategy:
    return Rfd4Strategy(
        temp_dir=str(tmp_path),
        yaml_content="version: 1\nsequences: []\n",
        design_mode=design_mode,
        options=options or {},
        target_sequence="T" * 20,
        pocket_author_contacts=[("A", 41)],
        template_inputs=TARGET,
        spec=None,
        parent_task_id="parent-1",
        queue="cap.rfd4.default")


@pytest.fixture
def dispatched(monkeypatch):
    sent = []
    monkeypatch.setattr(candidate_pool, "register_subtask", lambda *args: None)

    def send_task(name, args, queue):
        payload = args[0]
        archive = Path(payload["candidate_dir"]) / "generation.zip"
        sent.append((name, payload, queue))
        return SimpleNamespace(id=f"cand-{len(sent)}", archive=archive)

    monkeypatch.setattr(celery_module.celery_app, "send_task", send_task)

    def drain(items, submit, parent_task_id, on_done, policy, **kwargs):
        for item in items:
            result = submit(item)
            on_done(item, {"archive_path": str(result.archive)}, "SUCCESS")
        return list(items)

    monkeypatch.setattr(candidate_pool, "drain", drain)
    return sent


def test_linear_generation_runs_as_one_subtask_and_reads_summary_candidates(tmp_path, dispatched):
    strategy = make_strategy(tmp_path)
    linear_archive(tmp_path / "rfd4_generation" / "gen_001" / "linear" / "generation.zip")
    candidates = list(strategy.propose(1, [{"sequence": "IGNORED"}], 3))
    name, payload, queue = dispatched[0]
    assert (name, queue) == ("tasks.peptide_candidate_worker_task", "cap.rfd4.default")
    assert payload["backend"] == "rfd4"
    assert payload["parent_task_id"] == "parent-1"
    assert payload["predict_args"] == {
        "rfd4_spec": None, "rfd4_num_designs": 3, "template_inputs": TARGET}
    assert candidates == [Candidate("CREEECWA", [], [], "rfd4")]
    assert strategy.artifacts() == {"rfd4_generated/linear/model.cif": b"data_design\n"}


def test_bicyclic_candidates_carry_modifications_and_anchors(tmp_path, dispatched, monkeypatch):
    job = {"dir": tmp_path / "bicyc_none_L12", "archive": tmp_path / "bicyc_none_L12" / "generation.zip",
           "spec": "spec: 1", "template_inputs": TARGET, "num_designs": 2}
    write_archive(job["archive"], {"design_0/model_generated.cif": generated_cif()})
    monkeypatch.setattr(rfd4_strategy, "generation_jobs", lambda **kwargs: [job])
    strategy = make_strategy(tmp_path, design_mode="bicyclic")
    assert list(strategy.propose(1, [], 2)) == [
        Candidate("CALCGC", [{"position": 3, "ccd": "NLE", "baseResidue": "L"}], [1, 4, 6], "rfd4")]
    assert dispatched[0][1]["predict_args"]["rfd4_spec"] == "spec: 1"
    assert set(strategy.artifacts()) == {"rfd4_generated/bicyc_none_L12/model_generated.cif"}


def test_rfd4_strategy_does_not_rank_or_learn(tmp_path):
    strategy = make_strategy(tmp_path)
    assert strategy.prior_score("AAAA") is None
    assert strategy.learn([{"sequence": "AAAA"}], [{"sequence": "AAAA"}]) is None


def test_linear_generation_job(tmp_path):
    jobs = rfd4_strategy.generation_jobs(
        temp_dir=str(tmp_path), design_mode="linear", options={}, target_sequence="T" * 20,
        pocket_author_contacts=[], template_inputs=TARGET, spec="spec: 1", generation=1, num_designs=4)
    assert jobs == [{
        "dir": tmp_path / "rfd4_generation" / "gen_001" / "linear",
        "archive": tmp_path / "rfd4_generation" / "gen_001" / "linear" / "generation.zip",
        "spec": "spec: 1", "template_inputs": TARGET, "num_designs": 4}]


@pytest.mark.parametrize("options, message", [
    ({}, "需要种子序列"),
    ({"peptideSequenceMask": "AARFC"}, "不含 C"),
    ({"peptideSequenceMask": "AARFKVWW"}, "peptideLengthMin/Max"),
    ({"peptideSequenceMask": "AARFKVWW", "peptideLengthMin": 10, "peptideLengthMax": 16},
     "种子 AARFKVWW"),
])
def test_bicyclic_generation_rejects_invalid_options(tmp_path, options, message):
    with pytest.raises(ValueError, match=message):
        rfd4_strategy.generation_jobs(
            temp_dir=str(tmp_path), design_mode="bicyclic", options=options, target_sequence="T" * 20,
            pocket_author_contacts=[], template_inputs=TARGET, spec=None, generation=1, num_designs=2)


def test_bicyclic_generation_requires_structure_and_pocket(tmp_path):
    options = {"peptideSequenceMask": "AARFKVWW", "peptideLengthMin": 16, "peptideLengthMax": 16}
    with pytest.raises(ValueError, match="带坐标靶标结构"):
        rfd4_strategy.generation_jobs(
            temp_dir=str(tmp_path), design_mode="bicyclic", options=options, target_sequence="T" * 20,
            pocket_author_contacts=[], template_inputs=None, spec=None, generation=1, num_designs=2)
    with pytest.raises(ValueError, match="口袋定位"):
        rfd4_strategy.generation_jobs(
            temp_dir=str(tmp_path), design_mode="bicyclic", options=options, target_sequence="T" * 20,
            pocket_author_contacts=[], template_inputs=TARGET, spec=None, generation=1, num_designs=2)


def test_candidate_worker_runs_rfd4_generation_with_the_requested_budget(tmp_path, monkeypatch):
    calls = []
    candidate_dir = tmp_path / "cand"
    archive = candidate_dir / "generation.zip"
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")

    def run_rfd4_backend(**kwargs):
        calls.append(kwargs)
        archive.write_bytes(b"PK")

    monkeypatch.setattr(run_single_prediction, "run_rfd4_backend", run_rfd4_backend)
    monkeypatch.setattr("gpu_manager.acquire_gpu_for_peptide_worker", lambda **kwargs: 2)
    monkeypatch.setattr("gpu_manager.release_gpu", lambda **kwargs: None)
    payload = {
        "__peptide_candidate_worker__": True,
        "temp_dir": str(candidate_dir),
        "yaml_content": "version: 1\nsequences: []\n",
        "output_archive_path": str(archive),
        "predict_args": {"rfd4_spec": "spec: 1", "rfd4_num_designs": 3, "template_inputs": TARGET},
        "model_name": None,
        "backend": "rfd4",
        "__peptide_worker_acquire_gpu__": True,
        "__peptide_worker_task_id__": "cand-1",
    }
    args_path = tmp_path / "args.json"
    args_path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys, "argv", ["run_single_prediction", str(args_path)])
    run_single_prediction.main()
    assert len(calls) == 1
    generation = calls[0]
    assert (generation["num_designs"], generation["rfd4_spec"]) == (3, "spec: 1")
    assert generation["template_inputs"] == TARGET
    assert generation["task_id"] == "cand-1"


def test_bicyclic_candidate_without_three_anchors_is_skipped(tmp_path, dispatched, monkeypatch, capsys):
    job = {"dir": tmp_path / "bicyc_none_L12", "archive": tmp_path / "bicyc_none_L12" / "generation.zip",
           "spec": None, "template_inputs": TARGET, "num_designs": 2}
    write_archive(job["archive"], {
        "design_0/model_generated.cif": generated_cif(("CYS", "ALA", "NLE", "CYS")),
        "design_1/model_generated.cif": generated_cif(),
    })
    monkeypatch.setattr(rfd4_strategy, "generation_jobs", lambda **kwargs: [job])
    strategy = make_strategy(tmp_path, design_mode="bicyclic")
    assert [c.sequence for c in strategy.propose(1, [], 2)] == ["CALCGC"]
    assert "恰好 3 个 Cys 锚点" in capsys.readouterr().err
