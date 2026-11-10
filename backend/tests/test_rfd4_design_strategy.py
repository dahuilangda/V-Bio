"""RFD4 设计策略的纯函数单测：候选提取与靶标序列解析。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

rsp = importlib.import_module("backend.runtime.run_single_prediction")

_dpeptide_target_sequence = rsp._dpeptide_target_sequence
_rfd4_candidates_from_summary = rsp._rfd4_candidates_from_summary


def test_candidates_pick_shortest_polymer_per_output():
    summary = {
        "outputs": [
            {"file": "runs/design_0/model.cif", "chains": {
                "A": {"n_residues": 115, "sequence": "NAFTVTVPKDLYVVEYGSNM"},
                "B": {"n_residues": 16, "sequence": "TEREEALTEMFDKLSE"},
            }},
            {"file": "runs/design_1/model.cif", "chains": {
                "A": {"n_residues": 115, "sequence": "NAFTVTVPKDLYVVEYGSNM"},
                "B": {"n_residues": 14, "sequence": "ACDEFGHIKLMNQR"},
            }},
        ]
    }
    candidates = _rfd4_candidates_from_summary(summary)
    assert [c["label"] for c in candidates] == ["model", "model"]
    assert candidates[0]["sequence"] == "TEREEALTEMFDKLSE"
    assert candidates[1]["sequence"] == "ACDEFGHIKLMNQR"


def test_candidates_skip_nonstandard_residue_chains():
    summary = {
        "outputs": [
            {"file": "runs/x/model.cif", "chains": {
                "A": {"n_residues": 20, "sequence": "AAA[SEZ]AAA"},
                "B": {"n_residues": 12, "sequence": "CREEECHWLANR"},
            }},
        ]
    }
    candidates = _rfd4_candidates_from_summary(summary)
    assert len(candidates) == 1
    assert candidates[0]["sequence"] == "CREEECHWLANR"


def test_candidates_empty_when_no_outputs():
    assert _rfd4_candidates_from_summary({"outputs": []}) == []
    assert _rfd4_candidates_from_summary({}) == []


def test_target_sequence_prefers_requested_chain():
    yaml_data = {"sequences": [
        {"protein": {"id": "A", "sequence": "TARGETSEQ115"}},
        {"protein": {"id": "B", "sequence": "SHORTPEPTIDE"}},
    ]}
    assert _dpeptide_target_sequence(yaml_data, "A") == "TARGETSEQ115"
    assert _dpeptide_target_sequence(yaml_data, None) == "TARGETSEQ115"


def test_target_sequence_raises_without_protein():
    import pytest

    with pytest.raises(ValueError):
        _dpeptide_target_sequence({"sequences": []}, "A")
