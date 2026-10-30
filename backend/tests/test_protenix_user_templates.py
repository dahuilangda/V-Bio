"""Protenix user-template staging: synthetic IDs + per-task release_dates overlay.

The root mechanism under test: Protenix filters templates by hit ID against
release_dates and fetches structures as {mmcif_dir}/{id}.cif. User templates
lack PDB registry metadata, so the backend assigns deterministic synthetic
IDs and supplies the release_dates overlay per task — no ID guessing, no
fallbacks.
"""

import json
from types import SimpleNamespace

import pytest

from backend.runtime.run_single_prediction import (
    _PROTENIX_TEMPLATE_RELEASE_DATE,
    _stage_protenix_user_templates,
)


def _prep(entity_chain_ids, entity_seq_positions):
    """Duck-typed stand-in for ProtenixPreparation's entity mapping fields."""
    return SimpleNamespace(
        entity_chain_ids=entity_chain_ids,
        entity_kinds={eidx: "protein" for eidx in entity_chain_ids},
        entity_seq_positions=entity_seq_positions,
    )


def _single_chain_cif(tmp_path, chain_id: str = "A", n_res: int = 12) -> str:
    """Template cif via the production conversion path (PDB → single-chain mmCIF).

    Uses convert_structure_to_single_chain_mmcif — the exact converter
    prepare_template_payloads runs — so the fixture exercises the same
    '.'-typed chem_comp output that production staging must complete.
    PDB ATOM lines follow strict fixed columns (name 13-16, resName 18-20,
    chainID 22, resSeq 23-26, xyz 31-54).
    """
    from backend.runtime.run_single_prediction import (
        convert_structure_to_single_chain_mmcif,
    )

    lines = []
    for i in range(1, n_res + 1):
        lines.append(
            f"ATOM  {i:5d}  CA  ALA {chain_id}{i:4d}    "
            f"{float(i):8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          C"
        )
    pdb_text = "\n".join(lines) + "\nEND\n"
    src = tmp_path / f"src_{chain_id}_{n_res}.pdb"
    src.write_text(pdb_text)
    cif_out = tmp_path / f"src_{chain_id}_{n_res}.cif"
    _, cif_text, _, _ = convert_structure_to_single_chain_mmcif(
        src, chain_id, cif_out
    )
    return cif_text


def _payload(tmp_path, chain: str = "A", n_res: int = 12):
    return {
        "target_chain_ids": [chain],
        "mmcif": _single_chain_cif(tmp_path, chain, n_res),
    }


@pytest.fixture()
def staged(tmp_path):
    """Two templates both targeting chain A; entity index != sequences position
    (production maps entity_chain_ids keys through entity_seq_positions)."""
    protenix_json = [
        {
            "name": "task",
            "sequences": [
                {"proteinChain": {"sequence": "A" * 12, "count": 1}},
                {"proteinChain": {"sequence": "G" * 20, "count": 1}},
            ],
        }
    ]
    prep = _prep({1: ["A"], 2: ["B"]}, {1: 0, 2: 1})
    payloads = [
        _payload(tmp_path, "A"),
        _payload(tmp_path, "A", n_res=10),
    ]
    staged_ok = _stage_protenix_user_templates(
        payloads, protenix_json, str(tmp_path), prep
    )
    return staged_ok, protenix_json, tmp_path


def test_two_pass_staging_marks_only_matched_entities(staged):
    staged_ok, protenix_json, tmp_path = staged
    assert staged_ok is True

    seqs = protenix_json[0]["sequences"]
    assert seqs[0]["proteinChain"]["templatesPath"].startswith(
        "/workspace/protenix_input/template_mmcif/"
    )
    assert seqs[0]["proteinChain"]["templatesPath"].endswith(".a3m")
    assert "templatesPath" not in seqs[1]["proteinChain"]


def test_synthetic_ids_are_hit_format_compliant(staged):
    """Hit 名必须满足 get_pdb_id_and_chain: ([a-zA-Z\\d]{4})_(chain)；落盘
    cif 的 chem_comp.type 已按注册表补全（quote 修复的回归点）。"""
    import re

    staged_ok, _, tmp_path = staged
    template_dir = tmp_path / "template_mmcif"
    cifs = sorted(p.name for p in template_dir.glob("*.cif"))
    assert cifs == ["t001.cif", "t002.cif"]

    for cif_name in cifs:
        content = (template_dir / cif_name).read_text()
        assert "'L-peptide linking'" in content, (
            f"{cif_name} 的 chem_comp.type 未补全")

    a3m_files = list(template_dir.glob("*.a3m"))
    assert len(a3m_files) == 1  # only entity 0 matched; single aggregated a3m
    content = a3m_files[0].read_text()
    hits = [line for line in content.splitlines() if line.startswith(">")]
    assert len(hits) == 2  # both templates target chain A -> one a3m, two hits
    for hit in hits:
        m = re.match(r"^>?([a-zA-Z\d]{4})_([a-zA-Z0-9.]+)/1-\d+ protein length:\d+ mol:protein", hit)
        assert m, f"hit name violates protenix format: {hit}"
        assert f"{m.group(1)}.cif" in cifs  # hit 引用的 cif 必须已落盘


def test_release_dates_overlay_covers_every_template(staged):
    staged_ok, _, tmp_path = staged
    overlay_path = tmp_path / "template_release_dates.json"
    assert overlay_path.exists()
    overlay = json.loads(overlay_path.read_text())
    assert set(overlay) == {"t001", "t002"}
    for entry in overlay.values():
        assert entry["release_date"] < "2021-09-30"  # protenix leakage cutoff
    assert _PROTENIX_TEMPLATE_RELEASE_DATE


def test_unmatched_target_chain_raises(tmp_path):
    """链不匹配是配置错误：直接抛 ValueError 并列出可用蛋白链，不静默跳过。"""
    import pytest

    protenix_json = [
        {"name": "task", "sequences": [{"proteinChain": {"sequence": "A" * 5}}]}
    ]
    prep = _prep({0: ["A"]}, {0: 0})
    with pytest.raises(ValueError, match="未匹配任何输入蛋白实体"):
        _stage_protenix_user_templates(
            [{"target_chain_ids": ["Z"], "mmcif": _single_chain_cif(tmp_path)}],
            protenix_json,
            str(tmp_path),
            prep,
        )
    assert "templatesPath" not in protenix_json[0]["sequences"][0]["proteinChain"]
    assert not list(tmp_path.glob("template_mmcif/*.a3m"))


def test_empty_payload_raises(tmp_path):
    """空内容是上游转换契约违约：抛错而非跳过。"""
    import pytest

    protenix_json = [
        {"name": "task", "sequences": [{"proteinChain": {"sequence": "A" * 5}}]}
    ]
    with pytest.raises(ValueError, match="内容为空"):
        _stage_protenix_user_templates(
            [{"target_chain_ids": ["A"], "mmcif": ""}],
            protenix_json,
            str(tmp_path),
            _prep({0: ["A"]}, {0: 0}),
        )


def test_empty_payloads_noop(tmp_path):
    protenix_json = [
        {"name": "task", "sequences": [{"proteinChain": {"sequence": "A" * 5}}]}
    ]
    assert (
        _stage_protenix_user_templates(
            [], protenix_json, str(tmp_path), _prep({0: ["A"]}, {0: 0})
        )
        is False
    )
    assert not (tmp_path / "template_mmcif").exists()
