"""mol1_predict 的单元测试：输入转换、连接子寻址、口袋/界面几何。

不依赖 GPU 与 Mol-1 包（仅 rdkit/yaml）；引擎的 Mol-1 调用路径由
E2E 冒烟覆盖（README）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mol1_predict import (  # noqa: E402
    MolAtom,
    _to_mol_bonds,
    _to_mol_sequences,
    build_dock_summary,
    collect_summaries,
    interface_metrics,
    parse_pocket_res,
    parse_structure,
    pocket_contact_report,
    prepare_custom_linkers,
    yaml_to_mol_jobs,
)


# ------------------------------------------------------------ yaml 转换

def test_cyclic_yaml_adds_headtail_bond(tmp_path):
    yaml_text = """
name: t
sequences:
  - protein:
      id: A
      sequence: IVGGWECEK
  - protein:
      id: B
      sequence: ACDEFGHIKL
      cyclic: true
constraints:
  - bond:
      atom1: [B, 2, N]
      atom2: [A, 189, OD1]
"""
    p = tmp_path / "t.yaml"
    p.write_text(yaml_text)
    job = yaml_to_mol_jobs(p)[0]
    bonds = job["covalent_bonds"]
    # 环肽头尾键：entity 2, pos 1 N <-> pos 10 C
    assert {"entity1": "2", "copy1": 1, "position1": "1", "atom1": "N",
            "entity2": "2", "copy2": 1, "position2": "10", "atom2": "C"} in bonds
    # 跨链键 entity 换算
    assert {"entity1": "2", "copy1": 1, "position1": "2", "atom1": "N",
            "entity2": "1", "copy2": 1, "position2": "189", "atom2": "OD1"} in bonds


def test_cyclic_without_explicit_id_gets_auto_chain(tmp_path):
    yaml_text = """
name: t
sequences:
  - protein:
      sequence: ACDEFGHIKL
      cyclic: true
"""
    p = tmp_path / "t.yaml"
    p.write_text(yaml_text)
    job = yaml_to_mol_jobs(p)[0]
    entity = job["sequences"][0]["proteinChain"]
    assert entity["id"] == ["A"]
    assert job["covalent_bonds"][0]["entity1"] == "1"


def test_modifications_get_ccd_prefix(tmp_path):
    yaml_text = """
name: t
sequences:
  - protein:
      id: B
      sequence: ACDEFGHIKL
      cyclic: true
      modifications:
        - ccd: PCA
          position: 1
"""
    p = tmp_path / "t.yaml"
    p.write_text(yaml_text)
    job = yaml_to_mol_jobs(p)[0]
    assert job["sequences"][0]["proteinChain"]["modifications"] == [
        {"ptmType": "CCD_PCA", "ptmPosition": 1}]


def test_ligand_and_ion_mapping():
    seqs = _to_mol_sequences([
        {"ligand": {"id": "L", "ccd": "ATP"}},
        {"ligand": {"id": "S", "smiles": "CCO"}},
        {"ion": {"id": "I", "ccd": "MG"}},
    ])
    assert seqs[0]["ligand"]["ligand"] == "CCD_ATP"
    assert seqs[1]["ligand"]["ligand"] == "CCO"
    assert seqs[2]["ion"]["ion"] == "MG"


# ------------------------------------------------------------ 双环连接子

def test_prepare_custom_linkers_converts_cif_to_sdf(tmp_path):
    cif_src = Path("/data/V-Bio/backend/runtime/linker_ccd/SEZ.cif")
    if not cif_src.exists():
        pytest.skip("linker ccd fixtures 不在本机")
    pdbeccdutils = pytest.importorskip("pdbeccdutils")
    work = tmp_path / "work"
    work.mkdir()
    jobs = [{"name": "t", "sequences": [
        {"ligand": {"ligand": "CCD_SEZ", "count": 1}}], "covalent_bonds": []}]
    prepare_custom_linkers(jobs, work, "/data/V-Bio/backend/runtime/linker_ccd")
    lig = jobs[0]["sequences"][0]["ligand"]["ligand"]
    assert lig.startswith("FILE_") and lig.endswith("SEZ.sdf")
    assert Path(lig[len("FILE_"):]).exists()


def test_bicyclic_bond_payload_passthrough():
    bonds = _to_mol_bonds([
        {"entity1": 2, "copy1": 1, "position1": 1, "atom1": "SG",
         "entity2": 3, "copy2": 1, "position2": 1, "atom2": "CD"}])
    assert bonds[0]["atom1"] == "SG" and bonds[0]["entity2"] == "3"


# ------------------------------------------------------------ 几何与汇总

def _atom(chain, resid, name, elem, x, y, z):
    return MolAtom(chain, resid, "ALA", name, elem, x, y, z)


def test_pocket_contact_report():
    atoms = [
        _atom("A", 18, "CA", "C", 0.0, 0.0, 0.0),
        _atom("A", 99, "CA", "C", 50.0, 0.0, 0.0),
        _atom("B", 1, "CA", "C", 1.0, 0.0, 0.0),
    ]
    rep = pocket_contact_report(atoms, "B", [("A", 18)], 6.0)
    assert rep["pocket_min"] == pytest.approx(1.0, abs=1e-6)
    assert rep["pocket_pass"] is True
    rep_far = pocket_contact_report(atoms, "B", [("A", 99)], 6.0)
    assert rep_far["pocket_pass"] is False


def test_interface_metrics_counts_clash_and_hbond():
    atoms = [
        # 受体
        _atom("A", 1, "O", "O", 0.0, 0.0, 0.0),
        _atom("A", 2, "CA", "C", 8.0, 0.0, 0.0),
        # 肽：一个 1.0Å 的 O-O 接触（clash 且计氢键）、一个 3.0Å 的 N-O、一个远处
        _atom("B", 1, "O", "O", 1.0, 0.0, 0.0),
        _atom("B", 2, "N", "N", 3.0, 0.0, 0.0),
        _atom("B", 3, "CA", "C", 30.0, 0.0, 0.0),
    ]
    m = interface_metrics(atoms, "B")
    assert m["interface_min_dist"] == pytest.approx(1.0, abs=1e-6)
    assert m["interface_clash_pairs"] == 1
    assert m["n_interface_hbonds"] == 2


def test_parse_pocket_res():
    assert parse_pocket_res("A:18, A:76") == [("A", 18), ("A", 76)]
    assert parse_pocket_res("") == []


def test_build_dock_summary_shape(tmp_path):
    conf = {"ranking_score": 0.9, "iptm": 0.8, "ptm": 0.7, "plddt": 90.0,
            "sample": 0}
    out = tmp_path / "out" / "job" / "seed_101" / "predictions"
    out.mkdir(parents=True)
    cif = out / "job_sample_0.cif"
    cif.write_text("data_x\n")
    conf_file = out / "job_summary_confidence_sample_0.json"
    conf_file.write_text(json.dumps(conf))
    summaries = collect_summaries(tmp_path / "out")
    assert len(summaries) == 1
    summary = build_dock_summary(summaries, tmp_path / "out", "peptide",
                                 [("A", 18)], 6.0)
    assert summary["engine"] == "mol1dock"
    assert summary["n_confidences"] == 1
    row = summary["best"]
    assert row["ranking_score"] == 0.9
    assert "interface_min_dist" in row and "pocket" in row


def test_apply_d_chirality_conversion(tmp_path):
    """D 肽逐位 D-CCD 修饰：G 跳过、已有修饰保留、受体链不动。"""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mol1_predict import apply_d_chirality  # noqa: E402

    jobs = [{"name": "t", "sequences": [
        {"proteinChain": {"sequence": "AGKLMNPQRSTVWYEDC", "count": 1,
                          "id": ["B"],
                          "modifications": [{"ptmType": "CCD_PCA",
                                             "ptmPosition": 2}]}},
        {"proteinChain": {"sequence": "IVGG", "count": 1, "id": ["A"]}},
    ], "covalent_bonds": []}]
    apply_d_chirality(jobs, "B")
    mods = {m["ptmPosition"]: m["ptmType"]
            for m in jobs[0]["sequences"][0]["proteinChain"]["modifications"]}
    assert mods[1] == "CCD_DAL"          # A -> DAL
    assert mods[2] == "CCD_PCA"          # 用户修饰保留
    assert mods[5] == "CCD_MED"          # M -> MED
    assert mods[6] == "CCD_DSG"          # N -> DSG（非 DSN）
    assert mods[8] == "CCD_DGN"          # Q -> DGN
    assert mods[17] == "CCD_DCY"         # C -> DCY
    # 序列 AGKLMNPQRSTVWYEDC 中 G 在位 2，被 PCA 修饰覆盖保留；
    # 其余 G 无（逐位断言见上）
    # 受体链 A 不转换
    a_mods = jobs[0]["sequences"][1]["proteinChain"].get("modifications")
    assert not a_mods
