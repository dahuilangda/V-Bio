"""RFD4 verify view 归档的一致性 enrich：参考取生成归档，结果注入并持久化。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from management_api.rfd4_consistency import (
    compute_consistency,
    maybe_enrich_view,
    parse_verify_name,
)

SAMPLE_CIF = (
    "data_x\n"
    "_atom_site.pdbx_PDB_model_num\n"
    "loop_\n"
    "_atom_site.group_PDB\n"
    "_atom_site.id\n"
    "_atom_site.Cartn_x\n"
    "_atom_site.Cartn_y\n"
    "_atom_site.Cartn_z\n"
    "# real structures are read by gemmi; the stub below is only for shape tests\n"
)


def _complex_cif(binder_offset: float) -> str:
    import gemmi

    structure = gemmi.Structure()
    structure.add_model(gemmi.Model("1"))
    for chain_name, count in (("A", 12), ("B", 8)):
        chain = gemmi.Chain(chain_name)
        for i in range(count):
            residue = gemmi.Residue()
            residue.name = "ALA" if chain_name == "A" else "GLY"
            residue.seqid = gemmi.SeqId(i + 1, " ")
            for atom_name in ("N", "CA", "C", "O"):
                atom = gemmi.Atom()
                atom.name = atom_name
                atom.element = gemmi.Element(atom_name)
                x = i * 3.8 + (binder_offset if chain_name == "B" else 0.0)
                atom.pos = gemmi.Position(x, 0.0, 0.0)
                residue.add_atom(atom)
            chain.add_residue(residue)
        structure[0].add_chain(chain)
    structure.setup_entities()
    return structure.make_mmcif_document().as_string()


def test_parse_verify_name_extracts_label_and_gen_name():
    assert parse_verify_name("RFD4 verify mol1 uhrf1_4gy5_L18_15 CREEEC") == (
        "uhrf1_4gy5_L18_15",
        "RFD4 gen uhrf1_4gy5_L18",
        "CREEEC",
    )
    assert parse_verify_name("mol1 plain task") is None


def test_compute_consistency_prefers_ranking_and_geometry():
    reference = _complex_cif(0.0)
    model = _complex_cif(0.0)
    score = compute_consistency(reference, model)
    assert score["dockq"] > 0.9
    assert score["lrmsd"] < 0.5


def _fake_gateway(rows_by_query, gen_zip_bytes, view_bytes):
    postgrest = SimpleNamespace(
        request=lambda method, table, **kwargs: rows_by_query.get(
            (method, table, kwargs.get("query", {}).get("name") or kwargs.get("query", {}).get("task_id") or "row"),
            [],
        )
    )
    task_store = SimpleNamespace(postgrest=postgrest)
    session = SimpleNamespace(
        get=lambda url, headers, timeout: SimpleNamespace(
            raise_for_status=lambda: None, content=gen_zip_bytes
        )
    )
    gateway = SimpleNamespace(
        task_store=task_store,
        runtime_proxy=SimpleNamespace(
            session=session,
            runtime_api_base_url="http://runtime",
            runtime_api_token="tok",
        ),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    upstream = SimpleNamespace(
        headers={"Content-Type": "application/zip"},
        status_code=200,
        content=view_bytes,
    )
    return gateway, upstream


def test_maybe_enrich_injects_and_persists(tmp_path):
    label = "traf6_1lb5_L16_9"
    reference = _complex_cif(0.0)
    gen_buffer = io.BytesIO()
    with zipfile.ZipFile(gen_buffer, "w") as zf:
        zf.writestr(f"inference_samples/{label}/{label}_generated.cif", reference)
    view_buffer = io.BytesIO()
    model = _complex_cif(0.0)
    with zipfile.ZipFile(view_buffer, "w") as zf:
        zf.writestr("mol1/output/input/seed_1/predictions/input_sample_0.cif", model)

    patches = []
    row = {
        "id": "row-9",
        "name": f"RFD4 verify mol1 {label} CREEEC",
        "backend": "mol1",
        "task_state": "SUCCESS",
        "confidence": {},
    }

    def fake_request(method, table, **kwargs):
        query = kwargs.get("query") or {}
        if method == "GET" and "name" in query:
            return [{"task_id": "gen-task-1"}]
        if method == "GET":
            return [row]
        if method == "PATCH":
            patches.append(kwargs["payload"])
        return []

    gateway = SimpleNamespace(
        task_store=SimpleNamespace(postgrest=SimpleNamespace(request=fake_request)),
        runtime_proxy=SimpleNamespace(
            session=SimpleNamespace(
                get=lambda url, headers, timeout: SimpleNamespace(
                    raise_for_status=lambda: None, content=gen_buffer.getvalue()
                )
            ),
            runtime_api_base_url="http://runtime",
            runtime_api_token="tok",
        ),
        logger=SimpleNamespace(info=lambda *a: None),
    )

    enriched = maybe_enrich_view(gateway, "task-1", "proj-1", upstream := SimpleNamespace(
        headers={"Content-Type": "application/zip"}, status_code=200, content=view_buffer.getvalue()
    ))
    assert enriched is not None
    members = zipfile.ZipFile(io.BytesIO(enriched)).namelist()
    assert "mol1/rfd4_consistency.json" in members
    payload = json.loads(zipfile.ZipFile(io.BytesIO(enriched)).read("mol1/rfd4_consistency.json"))
    assert payload["dockq"] > 0.9
    assert payload["label"] == label
    assert patches and patches[0]["confidence"]["rfd4_consistency"]["dockq"] > 0.9


def test_maybe_enrich_skips_d_peptide_task(tmp_path):
    view_buffer = io.BytesIO()
    model = _complex_cif(0.0)
    with zipfile.ZipFile(view_buffer, "w") as zf:
        zf.writestr("mol1/output/input/seed_1/predictions/input_sample_0.cif", model)

    row = {
        "id": "row-d",
        "name": "RFD4 verify mol1 traf6_1lb5_L16_9 CREEEC",
        "backend": "mol1",
        "task_state": "SUCCESS",
        "confidence": {},
        "properties": {"peptideChirality": "d"},
    }

    def fake_request(method, table, **kwargs):
        query = kwargs.get("query") or {}
        if method == "GET" and "name" in query:
            return [{"task_id": "gen-task-1"}]
        if method == "GET":
            return [row]
        return []

    gateway = SimpleNamespace(
        task_store=SimpleNamespace(postgrest=SimpleNamespace(request=fake_request)),
        runtime_proxy=SimpleNamespace(
            session=SimpleNamespace(
                get=lambda url, headers, timeout: SimpleNamespace(
                    raise_for_status=lambda: None, content=b""
                )
            ),
            runtime_api_base_url="http://runtime",
            runtime_api_token="tok",
        ),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    upstream = SimpleNamespace(
        headers={"Content-Type": "application/zip"},
        status_code=200,
        content=view_buffer.getvalue(),
    )

    enriched = maybe_enrich_view(gateway, "task-d", "proj-1", upstream)
    assert enriched is None
