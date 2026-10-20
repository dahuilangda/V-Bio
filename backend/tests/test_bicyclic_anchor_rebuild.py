"""单原子双环链接器锚点重建(BS3=Bi)与锚点自碰撞门禁。

模型对单原子配体没有化学先验,可能把 Bi 埋进肽骨架(生产事故:Bi 距
Asp13 N 0.81 A 而 Bi-SG 键长全部合格)。修复 = 确定性重建:从三个匹配的
Cys-SG 的三角形外夹角中心以 Bi-S rest 长度(2.55 A)重摆锚点,选对自身肽
清空更大的一侧,再做锚点+SG 的受限微松弛;门禁同时检查键长与锚点自碰撞。
"""
from __future__ import annotations

import sys
from pathlib import Path

import gemmi
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.runtime.run_single_prediction as rsp  # noqa: E402


def _build_bicyclic_test_structure(
    path: Path,
    *,
    anchor_offset: np.ndarray,
    sg_points: list[np.ndarray],
    linker_name: str = "BS3",
    anchor_name: str = "BI",
    anchor_element: str = "Bi",
) -> None:
    """最小双环复合物: 链 A=受体(1 残基), 链 B=3×Cys 肽, 链 L=单原子链接器。

    锚点初始位置由 anchor_offset 指定(远离/埋入均可构造)。
    """
    st = gemmi.Structure()
    st.add_model(gemmi.Model("1"))
    model = st[0]

    receptor = gemmi.Chain("A")
    res = gemmi.Residue()
    res.name = "ALA"
    res.seqid.num = 1
    res.het_flag = "A"
    ca = gemmi.Atom()
    ca.name = "CA"
    ca.element = gemmi.Element("C")
    ca.pos = gemmi.Position(40.0, 40.0, 40.0)
    res.add_atom(ca)
    receptor.add_residue(res)
    model.add_chain(receptor)

    peptide = gemmi.Chain("B")
    for idx, sg_pos in enumerate(sg_points, start=1):
        residue = gemmi.Residue()
        residue.name = "CYS"
        residue.seqid.num = idx
        residue.het_flag = "A"
        for atom_name, pos in (
            ("N", sg_pos + np.array([-1.5, 0.0, 0.0])),
            ("CA", sg_pos + np.array([-0.5, 1.0, 0.0])),
            ("C", sg_pos + np.array([0.5, 1.0, 0.0])),
            ("O", sg_pos + np.array([1.5, 1.5, 0.0])),
            ("CB", sg_pos + np.array([0.0, 0.3, -1.2])),
            ("SG", sg_pos),
        ):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element("N" if atom_name == "N" else "S" if atom_name == "SG" else "C")
            atom.pos = gemmi.Position(*(pos.tolist()))
            residue.add_atom(atom)
        peptide.add_residue(residue)
    model.add_chain(peptide)

    linker = gemmi.Chain("L")
    linker_res = gemmi.Residue()
    linker_res.name = linker_name
    linker_res.seqid.num = 1
    linker_res.het_flag = "H"
    anchor = gemmi.Atom()
    anchor.name = anchor_name
    anchor.element = gemmi.Element(anchor_element)
    triangle_centroid = np.mean(sg_points, axis=0)
    anchor.pos = gemmi.Position(*(np.asarray(anchor_offset, dtype=float).tolist()))
    del triangle_centroid
    linker_res.add_atom(anchor)
    linker.add_residue(linker_res)
    model.add_chain(linker)

    st.setup_entities()
    if path.suffix == ".pdb":
        st.write_pdb(str(path))
    else:
        st.make_mmcif_document().write_file(str(path))


def _triangle_points() -> list[np.ndarray]:
    # 外向三角形: 三个 SG 朝 +z 张开; xy 半径 1.5, 锚点在 (0,0,2.0) 时
    # Bi-S = sqrt(1.5^2+2.0^2) = 2.5 A(理想配位窗口内)
    return [np.array([1.5, 0.0, 0.0]), np.array([-0.75, 1.3, 0.0]), np.array([-0.75, -1.3, 0.0])]


def test_rebuild_places_anchor_at_rest_length_outside(tmp_path):
    sg = _triangle_points()
    path = tmp_path / "bicyclic.pdb"
    # 锚点初始在远离 SG 的一角(模型乱放)
    _build_bicyclic_test_structure(path, anchor_offset=np.array([8.0, 8.0, 8.0]), sg_points=sg)

    info = rsp._rebuild_single_atom_linker_anchor(path)
    assert info is not None and info["linker"] == "BS3"
    assert all(2.2 <= d <= 2.9 for d in info["anchor_sg_a"]), info
    assert info["self_clash_min_a"] >= 2.0, info

    report = rsp._dpeptide_linker_bond_report(path)
    assert report["all_bonded"] is True
    assert report["self_clash"] is False


def test_rebuild_moves_buried_anchor_out_of_backbone(tmp_path):
    sg = _triangle_points()
    centroid = np.mean(sg, axis=0)
    path = tmp_path / "buried.pdb"
    # 锚点埋在三角形中心下方、贴着一个 SG 的 CB 侧(骨架形态)
    _build_bicyclic_test_structure(
        path, anchor_offset=centroid + np.array([0.0, 0.0, -0.5]), sg_points=sg)

    info = rsp._rebuild_single_atom_linker_anchor(path)
    assert info is not None
    # 修复后清空显著改善且键长入带
    assert info["self_clash_min_a"] >= 2.0, info
    assert all(2.2 <= d <= 2.9 for d in info["anchor_sg_a"]), info


def test_rebuild_skips_multi_atom_linker(tmp_path):
    sg = _triangle_points()
    path = tmp_path / "sez.pdb"
    _build_bicyclic_test_structure(
        path,
        anchor_offset=np.array([0.0, 0.0, 2.55]),
        sg_points=sg,
        linker_name="SEZ",
        anchor_name="CD",
        anchor_element="C",
    )
    # SEZ 有额外原子才算多原子——这里只有 1 个重原子,补一个邻居原子
    st = gemmi.read_structure(str(path))
    for chain in st[0]:
        for residue in chain:
            if residue.name == "SEZ":
                neighbor = gemmi.Atom()
                neighbor.name = "C1"
                neighbor.element = gemmi.Element("C")
                neighbor.pos = gemmi.Position(0.0, 0.0, 4.5)
                residue.add_atom(neighbor)
    st.setup_entities()
    st.write_pdb(str(path))

    assert rsp._rebuild_single_atom_linker_anchor(path) is None


def test_bond_report_flags_buried_anchor(tmp_path):
    sg = _triangle_points()
    centroid = np.mean(sg, axis=0)
    path = tmp_path / "buried_gate.pdb"
    # 锚点直接压在受体原子旁(0.5 A) —— 门禁必须判自碰撞
    _build_bicyclic_test_structure(
        path, anchor_offset=sg[0] + np.array([0.4, 0.0, 0.0]), sg_points=sg)
    # 把锚点旁放一个受体重原子制造 <2.0 A 接触
    st = gemmi.read_structure(str(path))
    res = st[0]["A"][0]
    near = gemmi.Atom()
    near.name = "CB"
    near.element = gemmi.Element("C")
    anchor_pos = sg[0] + np.array([0.4, 0.0, 0.0])
    near.pos = gemmi.Position(*(anchor_pos + np.array([1.0, 0.0, 0.0])).tolist())
    res.add_atom(near)
    st.setup_entities()
    st.write_pdb(str(path))

    report = rsp._dpeptide_linker_bond_report(path)
    assert report["self_clash"] is True
    assert report["all_bonded"] is False
    assert report["anchor_self_clash_min_a"] < 2.0


def test_bond_report_passes_ideal_coordination(tmp_path):
    sg = _triangle_points()
    path = tmp_path / "good.pdb"
    _build_bicyclic_test_structure(path, anchor_offset=np.array([0.0, 0.0, 2.6]), sg_points=sg)

    report = rsp._dpeptide_linker_bond_report(path)
    assert report["all_bonded"] is True
    assert report["self_clash"] is False
