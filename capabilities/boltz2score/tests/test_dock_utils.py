"""CPU regression tests for the boltz2score dock pipeline.

Covers: SMILES parsing, pose-ensemble generation (diversity + placement),
ensemble record pruning, and the shared interface pose-ranking metric.
GPU-dependent paths (fixed-polymer generation sampling) are validated by the
GPU dock regression in docs/docking.md.

Run:  <boltz2score venv> -m pytest tests/test_dock_utils.py -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest
from rdkit import Chem

from core.results import interface_rank_score_from_payload
from utils.docking import (
    PocketCenter,
    parse_smiles_input,
    prepare_dock_ligands,
)

SMILES = "Brc1cccc(Nc2nc(OCC3CCCCC3)c3nc[nH]c3n2)c1"
CENTER = PocketCenter(1.0, 27.0, 8.0)


def _write_smi(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def test_parse_smiles_input_direct() -> None:
    entries = parse_smiles_input(smiles=SMILES, smiles_file=None)
    assert len(entries) == 1
    assert entries[0].name == "ligand_1"
    assert entries[0].smiles == SMILES


def test_parse_smiles_input_smi_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        f = _write_smi(Path(td) / "lib.smi", [f"{SMILES}\tlig1", "# comment", "  ", "c1ccccc1  benzene"])
        entries = parse_smiles_input(smiles=None, smiles_file=f)
    assert [e.name for e in entries] == ["lig1", "benzene"]
    assert [e.smiles for e in entries] == [SMILES, "c1ccccc1"]


def test_prepare_dock_ligands_single_pose_legacy_name() -> None:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        sdf = prepare_dock_ligands(
            parse_smiles_input(smiles=SMILES, smiles_file=None),
            CENTER, seed=42, work_dir=wd, n_poses=1,
        )
        mols = [m for m in Chem.SDMolSupplier(str(sdf), removeHs=False) if m]
    assert len(mols) == 1
    assert mols[0].GetProp("_Name") == "ligand_1"
    assert mols[0].GetProp("_DockSMILES") == SMILES
    xyz = np.array([list(mols[0].GetConformer().GetAtomPosition(i)) for i in range(mols[0].GetNumAtoms())])
    assert np.allclose(xyz.mean(0), [1.0, 27.0, 8.0], atol=1e-3)


def test_prepare_dock_ligands_ensemble_naming_and_placement() -> None:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        sdf = prepare_dock_ligands(
            parse_smiles_input(smiles=SMILES, smiles_file=None),
            CENTER, seed=42, work_dir=wd, n_poses=6,
        )
        mols = [m for m in Chem.SDMolSupplier(str(sdf), removeHs=False) if m]
        prep = json.loads((wd / "dock_preparation.json").read_text())
    names = [m.GetProp("_Name") for m in mols]
    assert names == [f"ligand_1__pose{k:02d}" for k in range(6)]
    assert prep["n_poses_per_ligand"] == 6
    assert prep["pose_groups"]["ligand_1"] == names
    # every pose placed at the pocket centre
    xyzs = [
        np.array([list(m.GetConformer().GetAtomPosition(i)) for i in range(m.GetNumAtoms())])
        for m in mols
    ]
    for xyz in xyzs:
        assert np.allclose(xyz.mean(0), [1.0, 27.0, 8.0], atol=1e-3)
    # genuine orientation/conformer diversity
    pairwise = [
        float(np.sqrt(((xyzs[i] - xyzs[j]) ** 2).sum(1)).mean())
        for i in range(len(xyzs)) for j in range(i + 1, len(xyzs))
    ]
    assert max(pairwise) > 2.0


def test_interface_rank_score_shared_metric() -> None:
    good = {
        "ligand_ipsae_max": 0.65, "ipsae_dom": 0.71, "iptm": 0.969, "ligand_iptm": 0.9,
        "ligand_plddt_mean": 83.4, "ligand_atom_plddt_p10": 70.0, "ligand_atom_plddt_min": 40.0,
        "ligand_atom_plddt_fraction_ge_50": 0.95, "ligand_atom_plddt_fraction_ge_70": 0.8,
        "confidence_score": 0.95, "mean_interface_pae": 4.0, "mean_interface_distance": 4.0,
    }
    bad = {**good, "ligand_ipsae_max": 0.15, "ipsae_dom": 0.1, "iptm": 0.6}
    assert interface_rank_score_from_payload(good) > interface_rank_score_from_payload(bad)


def _make_record(out: Path, name: str, score: float) -> None:
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "selected_model": "best_model",
        "models": [{"model_stem": "best_model", "interface_rank_score": score}],
    }
    (d / f"best_sample_{name}.json").write_text(json.dumps(payload))
    (d / f"confidence_{name}_model_0.json").write_text("{}")
    (d / f"{name}_model_0.cif").write_text("x")


def test_ensemble_winner_pruning() -> None:
    from core.flexible_optimization import _select_dock_ensemble_winners

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        rid = "prot__0001_ligand_1"
        _make_record(out, f"{rid}__pose00", 0.70)
        _make_record(out, f"{rid}__pose01", 0.60)
        _make_record(out, f"{rid}__pose02", 0.55)
        _make_record(out, "prot__0002_ligand_2", 0.0)  # not an ensemble record

        _select_dock_ensemble_winners(out)

        remaining = sorted(p.name for p in out.iterdir())
        assert remaining == sorted([f"{rid}__pose00", "prot__0002_ligand_2", "dock_ensemble_selection.json"])
        sel = json.loads((out / "dock_ensemble_selection.json").read_text())
        assert sel[rid]["winner"] == f"{rid}__pose00"
        assert len(sel[rid]["ranked_poses"]) == 3


def test_single_records_untouched_by_ensemble_selection() -> None:
    """Records without the __poseNN suffix are not dock ensembles and must
    survive selection untouched."""
    from core.flexible_optimization import _select_dock_ensemble_winners

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        _make_record(out, "prot__0001_ligand_1", 0.7)
        _make_record(out, "prot__0002_ligand_2", 0.0)
        _select_dock_ensemble_winners(out)
        remaining = sorted(p.name for p in out.iterdir())
        assert remaining == ["prot__0001_ligand_1", "prot__0002_ligand_2"]


def test_ensemble_record_without_rerank_summary_raises() -> None:
    """A pose record missing its rerank summary is a scoring gap, not a
    silently-ignored pose."""
    from core.flexible_optimization import _select_dock_ensemble_winners

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        _make_record(out, "prot__0001_ligand_1__pose00", 0.7)
        (out / "prot__0001_ligand_1__pose00" / "best_sample_prot__0001_ligand_1__pose00.json").unlink()
        with pytest.raises(FileNotFoundError):
            _select_dock_ensemble_winners(out)


def test_conformer_generation_rejects_mmff_less_smiles() -> None:
    """Chemistry MMFF94 cannot parameterize must fail, not yield an
    unrelaxed conformer silently."""
    from utils.docking import generate_conformer_from_smiles

    # [Fe]-containing metallocene: valid SMILES, no MMFF parameters
    with pytest.raises(ValueError):
        generate_conformer_from_smiles("c1ccc2[Fe]3(c1ccc2)cccc3", seed=42)


def test_conformer_generation_rejects_bad_smiles() -> None:
    from utils.docking import generate_conformer_from_smiles

    with pytest.raises(ValueError, match="cannot parse"):
        generate_conformer_from_smiles("not-a-smiles!!")
