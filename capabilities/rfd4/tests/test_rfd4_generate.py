"""rfd4_generate 封装的单元测试：链摘要、override 组装、产物校验。

运行环境需 gemmi（引擎镜像内自带）；GPU 端到端由 scripts/run_smoke.sh 覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

gemmi = pytest.importorskip("gemmi")

from rfd4_generate import (  # noqa: E402
    _build_overrides,
    _chain_summary,
    _collect_cifs,
    _write_summary,
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
