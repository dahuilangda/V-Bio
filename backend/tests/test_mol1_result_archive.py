"""mol1 归档走 protenix 解析契约：view 归档按 ranking_score 配对结构+置信度。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.result_archive import ResultArchiveService


def _mol1_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for sample in range(2):
            seed_dir = "mol1/output/input/seed_1/predictions"
            confidence = {
                "plddt": 90.0 + sample,
                "iptm": 0.70 + 0.01 * sample,
                "ranking_score": 0.70 + 0.01 * sample,
            }
            structure = (
                "data_model\n"
                "_atom_site.pdbx_PDB_model_num P\n"
                f"# sample {sample}\n"
            )
            zf.writestr(f"{seed_dir}/input_sample_{sample}.cif", structure)
            zf.writestr(
                f"{seed_dir}/input_summary_confidence_sample_{sample}.json",
                json.dumps(confidence),
            )
        zf.writestr("mol1/output/summary.json", json.dumps({"engine": "mol1"}))
        zf.writestr("mol1/input.json", json.dumps({}))
    return buffer.getvalue()


def _service() -> ResultArchiveService:
    return ResultArchiveService(
        app=SimpleNamespace(config={}),
        celery_app=None,
        logger=SimpleNamespace(debug=lambda *a: None, warning=lambda *a: None),
        get_redis_client_fn=lambda: None,
    )


def test_view_archive_pairs_best_sample_with_confidence(tmp_path):
    source = tmp_path / "mol1_results.zip"
    source.write_bytes(_mol1_zip_bytes())
    svc = _service()

    data = svc._build_view_archive_bytes(str(source))
    members = zipfile.ZipFile(io.BytesIO(data)).namelist()

    assert any(name.endswith("input_sample_1.cif") for name in members), members
    assert any(name.endswith("input_summary_confidence_sample_1.json") for name in members), members
    assert len([n for n in members if n.endswith(".cif")]) == 1


def test_is_protenix_detection_includes_mol1_prefix(tmp_path):
    source = tmp_path / "mol1_results.zip"
    source.write_bytes(_mol1_zip_bytes())
    svc = _service()

    # 修复前 mol1 前缀落入 boltz 分支（结构固定选第一个样本）；现在走 protenix 分支，
    # ranking_score 最高的样本（sample_1）被选中，证明 mol1/output/ 被识别。
    data = svc._build_view_archive_bytes(str(source))
    members = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(name.endswith("input_sample_1.cif") for name in members)
