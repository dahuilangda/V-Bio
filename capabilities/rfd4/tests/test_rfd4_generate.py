"""rfd4_generate 封装的单元测试：链摘要、override 组装、产物校验。

运行环境需 gemmi（引擎镜像内自带）；GPU 端到端由 scripts/run_smoke.sh 覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rfd4_generate import (  # noqa: E402
    _build_overrides,
    _chain_summary,
    _collect_cifs,
    _resolve_spec,
    _write_summary,
)

pytestmark_chain_summary = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["find_spec"]).find_spec("gemmi") is None,
    reason="需要 gemmi",
)


def _write_minimal_cif(path: Path, sequence3: list[str]) -> Path:
    """写极简 PDB（每残基仅 CA）→ gemmi 读入转 cif，供链摘要测试。"""
    import gemmi

    pdb_path = path.with_suffix(".pdb")
    rows = []
    for idx, name in enumerate(sequence3, start=1):
        rows.append(
            f"ATOM  {idx:5d}  CA  {name:>3} A{idx:4d}    "
            f"{10.0 * idx:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           "
        )
    pdb_path.write_text("\n".join(rows) + "\nEND\n")

    st = gemmi.read_structure(str(pdb_path))
    st.setup_entities()
    doc = st.make_mmcif_document()
    doc.write_file(str(path))
    return path


def test_build_overrides_contains_core_fields():
    overrides = _build_overrides(
        type("A", (), {"spec": "d.yaml", "preset": "quickstart",
                       "num_designs": 3, "seed": 7, "extra": [],
                       "resolved_spec": "d_resolved.yaml"}),
        Path("/workspace/runs/x"),
    )
    assert "inputs=d_resolved.yaml" in overrides
    assert "num_replicates=3" in overrides
    assert "seed=7" in overrides
    assert "compile_lengths=null" in overrides
    assert any(o.startswith("out_dir=") for o in overrides)


def test_build_overrides_appends_extra():
    overrides = _build_overrides(
        type("A", (), {"spec": "d.yaml", "preset": "quickstart",
                       "num_designs": 1, "seed": 0,
                       "extra": ["nsteps=80"],
                       "resolved_spec": "d_resolved.yaml"}),
        Path("/workspace/runs/x"),
    )
    assert "nsteps=80" in overrides


def test_chain_summary_maps_three_letter_codes(tmp_path: Path):
    cif = _write_minimal_cif(tmp_path / "t.cif", ["ALA", "GLY", "CYS"])
    chains = _chain_summary(cif)
    assert list(chains) == ["A"]
    assert chains["A"]["sequence"] == "AGC"
    assert chains["A"]["n_residues"] == 3


def test_collect_cifs_raises_on_empty(tmp_path: Path):
    with pytest.raises(RuntimeError, match="没有任何结构产物"):
        _collect_cifs(tmp_path)


def test_write_summary_shape(tmp_path: Path):
    cif = _write_minimal_cif(tmp_path / "t.cif", ["GLY"])
    args = type("A", (), {"spec": "t.yaml", "preset": "quickstart",
                          "num_designs": 1, "seed": 0})
    summary_path = _write_summary(args, tmp_path, [cif], 12.5)
    data = json.loads(summary_path.read_text())
    assert data["engine"] == "rfd4-proteina"
    assert data["outputs"][0]["chains"]["A"]["sequence"] == "G"


# ------------------------------------------------------------ spec 路径解析

def _write_spec(tmp_path: Path, input_value: str) -> Path:
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "design:\n"
        f"  input: {input_value}\n"
        "  contig: \"10-20\"\n")
    return spec


def test_resolve_spec_cwd_hit(tmp_path, monkeypatch):
    """input 相对 cwd 可命中时按 cwd 解析。"""
    import os
    (tmp_path / "target.cif").write_text("data_t\n")
    spec = _write_spec(tmp_path, "target.cif")
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_spec(spec, tmp_path / "out")
    data = __import__("yaml").safe_load(resolved.read_text())
    assert data["design"]["input"] == str((tmp_path / "target.cif").resolve())


def test_resolve_spec_spec_dir_hit(tmp_path, monkeypatch):
    """cwd 不命中时回退 spec 目录解析。"""
    monkeypatch.chdir(tmp_path)  # cwd 无 target
    sub = tmp_path / "inputs"
    sub.mkdir()
    (sub / "target.cif").write_text("data_t\n")
    spec = _write_spec(sub, "target.cif")
    resolved = _resolve_spec(spec, tmp_path / "out")
    data = __import__("yaml").safe_load(resolved.read_text())
    assert data["design"]["input"] == str((sub / "target.cif").resolve())


def test_resolve_spec_absolute_passthrough(tmp_path, monkeypatch):
    """绝对路径原样透传。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "abs.cif"
    target.write_text("data_t\n")
    spec = _write_spec(tmp_path, str(target))
    resolved = _resolve_spec(spec, tmp_path / "out")
    data = __import__("yaml").safe_load(resolved.read_text())
    assert data["design"]["input"] == str(target.resolve())


def test_resolve_spec_missing_input_raises(tmp_path, monkeypatch):
    """两级都未命中的 input 显式报错，不静默写坏 spec。"""
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, "nonexistent.cif")
    with pytest.raises(FileNotFoundError, match="input 不存在"):
        _resolve_spec(spec, tmp_path / "out")
