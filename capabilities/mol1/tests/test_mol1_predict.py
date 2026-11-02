"""mol1_predict 的单元测试：输入转换、键校验、MSA 缓存与汇总。

不依赖 GPU 与 Mol-1 包（仅 yaml/json）；引擎的 Mol-1 调用路径由
E2E 冒烟覆盖（README）。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mol1_predict import (  # noqa: E402
    _cached_a3m_matches,
    _to_mol_bonds,
    _to_mol_modifications,
    _to_mol_sequences,
    collect_summaries,
    fill_missing_msa,
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


# ------------------------------------------------------------ 键与修饰校验

def test_bicyclic_bond_payload_passthrough():
    bonds = _to_mol_bonds([
        {"entity1": 2, "copy1": 1, "position1": 1, "atom1": "SG",
         "entity2": 3, "copy2": 1, "position2": 1, "atom2": "CD"}])
    assert bonds[0]["atom1"] == "SG" and bonds[0]["entity2"] == "3"


def test_bond_missing_required_key_raises():
    with pytest.raises(ValueError, match="missing required keys"):
        _to_mol_bonds([{"entity1": 2, "position1": 1, "atom1": "SG",
                        "entity2": 3, "position2": 1}])


def test_modification_missing_field_raises():
    with pytest.raises(ValueError, match="missing ccd/position"):
        _to_mol_modifications([{"position": 1}])
    with pytest.raises(ValueError, match="must be a mapping"):
        _to_mol_modifications(["PCA"])


def test_cyclic_multi_id_raises(tmp_path):
    yaml_text = """
name: t
sequences:
  - protein:
      id: [B, C]
      count: 2
      sequence: ACDEFGHIKL
      cyclic: true
"""
    p = tmp_path / "t.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ValueError, match="exactly one id"):
        yaml_to_mol_jobs(p)


# ------------------------------------------------------------ MSA 缓存

def _write_a3m(path: Path, first_seq: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# a3m\n>query\n{first_seq}\n>seq1\n{first_seq}\n")


def test_cached_a3m_requires_content_match(tmp_path):
    seq = "IVGGWECEKHSQPWQAV"
    good = tmp_path / "good.a3m"
    _write_a3m(good, seq)
    assert _cached_a3m_matches(good, seq)
    # 小写插入列剔除后比对
    with_ins = tmp_path / "ins.a3m"
    with_ins.write_text(">query\n" + "".join(ch + "i" for ch in seq) + "\n")
    assert _cached_a3m_matches(with_ins, seq)
    # 同长不同序列：长度校验放行的错缓存，内容比对必须拒绝
    other = tmp_path / "other.a3m"
    _write_a3m(other, "IVGGWECEKHSQPWQAA")
    assert not _cached_a3m_matches(other, seq)


def test_msa_search_failure_raises(tmp_path, monkeypatch):
    fake = types.ModuleType("mol.runner.msa_search")
    fake.msa_search = lambda seqs, out: []
    monkeypatch.setitem(sys.modules, "mol.runner.msa_search", fake)
    jobs = [{"name": "t", "sequences": [
        {"proteinChain": {"sequence": "IVGGWECEKHSQPWQAVYSHGWAHCGGV",
                          "count": 1, "id": ["A"]}}], "covalent_bonds": []}]
    with pytest.raises(RuntimeError, match="msa_search returned no result"):
        fill_missing_msa(jobs, tmp_path, None)


def test_short_chain_uses_self_a3m(tmp_path):
    jobs = [{"name": "t", "sequences": [
        {"proteinChain": {"sequence": "ACDEFGHIKL", "count": 1,
                          "id": ["B"]}}], "covalent_bonds": []}]
    fill_missing_msa(jobs, tmp_path, None)
    path = Path(jobs[0]["sequences"][0]["proteinChain"]["unpairedMsaPath"])
    assert path.read_text() == ">query_0\nACDEFGHIKL\n"


# ------------------------------------------------------------ 汇总

def test_collect_summaries_reports_skipped(tmp_path):
    out = tmp_path / "out" / "job" / "seed_101" / "predictions"
    out.mkdir(parents=True)
    conf = {"ranking_score": 0.9, "iptm": 0.8, "ptm": 0.7, "plddt": 90.0}
    (out / "job_sample_0.cif").write_text("data_x\n")
    (out / "job_summary_confidence_sample_0.json").write_text(json.dumps(conf))
    broken = out / "job_summary_confidence_sample_1.json"
    broken.write_text("{not json")
    summaries, skipped = collect_summaries(tmp_path / "out")
    assert len(summaries) == 1
    assert summaries[0]["confidence"]["ranking_score"] == 0.9
    assert summaries[0]["cif"].endswith("job_sample_0.cif")
    assert skipped == [str(broken)]
