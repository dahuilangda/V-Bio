"""引擎能力矩阵测试：schema 与前端引用的引擎 id 覆盖。"""
from __future__ import annotations

from backend.core.engine_capabilities import ENGINES, active_engine_ids

REQUIRED_FEATURES = {"pocket", "custom_ccd", "msa", "ncaa"}
VALID_STATUSES = {"active", "deprecated"}
VALID_WORKFLOWS = {"prediction", "peptide_design", "virtual_screening", "lead_optimization"}


def test_matrix_schema():
    for engine_id, engine in ENGINES.items():
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
