"""引擎能力矩阵契约测试：嵌入字典与 configs JSON 不得漂移。"""
from __future__ import annotations

import json
from pathlib import Path

from backend.core.engine_capabilities import EMBEDDED_ENGINES, MATRIX_PATH, active_engine_ids

REQUIRED_FEATURES = {"pocket", "custom_ccd", "msa", "ncaa"}
VALID_STATUSES = {"active", "deprecated"}
VALID_WORKFLOWS = {"prediction", "peptide_design", "virtual_screening", "lead_optimization"}


def test_matrix_file_matches_embedded():
    data = json.loads(MATRIX_PATH.read_text())["engines"]
    assert data == EMBEDDED_ENGINES, (
        "configs/engine_capabilities.json 与 engine_capabilities.py 嵌入副本漂移，请同步更新两处")


def test_matrix_schema():
    for engine_id, engine in EMBEDDED_ENGINES.items():
        assert engine["status"] in VALID_STATUSES, engine_id
        assert set(engine["features"]) == REQUIRED_FEATURES, engine_id
        for wf in engine["workflows"]:
            assert wf in VALID_WORKFLOWS, f"{engine_id}: 未知 workflow {wf}"
        for ch in engine["chirality"]:
            assert ch in ("l", "d"), f"{engine_id}: 非法手性 {ch}"


def test_active_engines_cover_frontend_options():
    # 前端下拉引用的引擎 id 必须都是 active（或 deprecated 但显式隐藏）
    active = set(active_engine_ids())
    for engine_id in ("protenix2dock", "mol1", "rfd4", "protenix", "boltz", "alphafold3"):
        assert engine_id in active, engine_id
