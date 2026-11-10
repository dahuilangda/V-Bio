"""RFD4 verify 结果的一致性评估：折叠构象对生成构象的 DockQ / LRMSD。

verify 任务名形如 "RFD4 verify mol1 <label> <seq>"，label 同时是生成归档里
inference_samples/<label>/<label>_generated.cif 的候选目录名；生成任务名由
label 去掉末段序号拼出（"RFD4 gen uhrf1_4gy5_L18"）。评估器复用
capabilities/boltz2score/metrics/dockq.py（FadA campaign 同款），按链长自动
判定 binder（最短链）与其余 receptor 链。结果注入 view 归档的
mol1/rfd4_consistency.json，并持久化到任务行 confidence，之后零成本复用。
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import gemmi
import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
BOLTZ2SCORE_ROOT = REPO_ROOT / "capabilities" / "boltz2score"

VERIFIED_ENGINES = {"mol1", "protenix", "alphafold3"}


def _log(gateway, message: str) -> None:
    try:
        gateway.logger.info("rfd4-consistency: %s", message)
    except Exception:
        pass


def parse_verify_name(name: str) -> Optional[Tuple[str, str]]:
    "RFD4 verify <engine> <label> <seq> -> (label, gen_task_name)"
    tokens = str(name or "").split()
    if len(tokens) < 5 or tokens[0] != "RFD4" or tokens[1] != "verify":
        return None
    if tokens[2].lower() not in VERIFIED_ENGINES:
        return None
    label = tokens[3]
    if "_" not in label:
        return None
    gen_short = label.rsplit("_", 1)[0]
    return label, f"RFD4 gen {gen_short}"


def _load_structure(cif_text: str):
    structure = gemmi.read_structure_string(cif_text)
    structure.setup_entities()
    if len(structure) == 0:
        structure.add_model(gemmi.Model("1"))
    return structure


def _split_binder_receptor(structure) -> Tuple[str, Tuple[str, ...]]:
    # 残基数 <5 的链是连接子 CCD（如 SEZ），不参与 binder/receptor 角色
    chains = sorted(
        ((chain.name, sum(1 for _ in chain)) for chain in structure[0]),
        key=lambda item: (item[1], item[0]),
    )
    chains = [item for item in chains if item[1] >= 5] or chains
    if len(chains) < 2:
        raise ValueError("structure has fewer than two chains")
    binder = chains[0][0]
    receptor = tuple(name for name, _ in chains[1:])
    return binder, receptor


def compute_consistency(reference_cif: str, model_cif: str) -> Dict[str, Any]:
    if str(BOLTZ2SCORE_ROOT) not in sys.path:
        sys.path.insert(0, str(BOLTZ2SCORE_ROOT))
    from metrics.dockq import dockq_score, load_model

    result: Dict[str, Any] = {}
    for role, cif_text in (("reference", reference_cif), ("model", model_cif)):
        structure = _load_structure(cif_text)
        binder, receptor = _split_binder_receptor(structure)
        rec_atoms, pep_atoms = load_model(
            cif_text, receptor=receptor, peptide=binder, is_text=True
        )
        result[role] = (rec_atoms, pep_atoms)

    score = dockq_score(*result["reference"], *result["model"])
    if score is None:
        raise ValueError("too few shared backbone atoms between the two complexes")

    def _finite(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None

    payload: Dict[str, Any] = {}
    for key in ("dockq", "fnat", "irmsd", "lrmsd"):
        value = _finite(score.get(key))
        if value is not None:
            payload[key] = round(value, 4)
    if "dockq" not in payload:
        raise ValueError("dockq score unavailable")
    return payload


def _candidate_binder_sequence(cif_text: str) -> str:
    structure = _load_structure(cif_text)
    binder, _receptor = _split_binder_receptor(structure)
    for chain in structure[0]:
        if chain.name == binder:
            return "".join(residue.name for residue in chain)
    return ""



def _fetch_reference_cif(
    gateway, gen_task_id: str, label: str, binder_sequence: str
) -> Optional[str]:
    """先按 label 精确路径取；聚合编号（verify 名与 gen 候选号不一致）时
    按 binder 序列前缀匹配归档内的候选。"""
    proxy = gateway.runtime_proxy
    response = proxy.session.get(
        f"{proxy.runtime_api_base_url}/results/{gen_task_id}",
        headers={"X-API-Token": proxy.runtime_api_token, "Accept": "application/json"},
        timeout=(10, 300),
    )
    response.raise_for_status()
    archive_bytes = response.content
    exact = f"inference_samples/{label}/{label}_generated.cif"
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        if exact in names:
            return archive.read(exact).decode("utf-8")
        prefix = str(binder_sequence or "").strip().upper()[:16]
        if not prefix:
            return None
        for name in names:
            if not name.endswith("_generated.cif"):
                continue
            candidate = archive.read(name).decode("utf-8", "replace")
            if _candidate_binder_sequence(candidate).startswith(prefix):
                return candidate
    return None


def _extract_model_cif(view_bytes: bytes) -> Optional[str]:
    with zipfile.ZipFile(io.BytesIO(view_bytes)) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".cif"):
                return archive.read(name).decode("utf-8")
    return None


def _inject_member(view_bytes: bytes, member: str, payload: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(view_bytes)) as source, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr(member, payload)
    return buffer.getvalue()


def maybe_enrich_view(
    gateway,
    task_id: str,
    project_id: str,
    upstream: requests.Response,
) -> Optional[bytes]:
    """verify 任务的 view 归档注入 DockQ/LRMSD；返回 None 表示原样透传。"""
    try:
        content_type = str(upstream.headers.get("Content-Type") or "")
        if "zip" not in content_type:
            return None
        row = _load_task_row(gateway, task_id, project_id)
        if row is None:
            return None
        if _is_d_peptide_task(row):
            return None
        cached = row.get("confidence")
        if isinstance(cached, dict) and isinstance(cached.get("rfd4_consistency"), dict):
            payload = json.dumps(cached["rfd4_consistency"], ensure_ascii=False)
            return _inject_member(
                upstream.content, "mol1/rfd4_consistency.json", payload
            )
        parsed_name = parse_verify_name(str(row.get("name") or ""))
        if parsed_name is None:
            return None
        label, gen_name = parsed_name
        gen_task_id = _find_gen_task_id(gateway, project_id, gen_name)
        if not gen_task_id:
            return None
        binder_sequence = str(row.get("name") or "").split()[4] if len(str(row.get("name") or "").split()) > 4 else ""
        reference_cif = _fetch_reference_cif(gateway, gen_task_id, label, binder_sequence)
        model_cif = _extract_model_cif(upstream.content)
        if not model_cif:
            return None
        consistency = compute_consistency(reference_cif, model_cif)
        consistency.update({"label": label, "gen_task_id": gen_task_id})
        _persist(gateway, row, consistency)
        _log(gateway, f"{task_id} label={label} -> {consistency.get('dockq')}")
        return _inject_member(
            upstream.content,
            "mol1/rfd4_consistency.json",
            json.dumps(consistency, ensure_ascii=False),
        )
    except Exception as exc:
        _log(gateway, f"{task_id} enrich skipped: {exc}")
        return None


def _is_d_peptide_task(row: Dict[str, Any]) -> bool:
    properties = row.get("properties")
    if not isinstance(properties, dict):
        return False
    chirality = str(
        properties.get("peptideChirality") or properties.get("peptide_chirality") or ""
    ).strip().lower()
    return chirality == "d"


def _load_task_row(gateway, task_id: str, project_id: str) -> Optional[Dict[str, Any]]:
    rows = gateway.task_store.postgrest.request(
        "GET",
        "project_tasks",
        query={
            "select": "id,name,backend,task_state,confidence,properties",
            "task_id": f"eq.{task_id}",
            "project_id": f"eq.{project_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _find_gen_task_id(gateway, project_id: str, gen_name: str) -> Optional[str]:
    rows = gateway.task_store.postgrest.request(
        "GET",
        "project_tasks",
        query={
            "select": "task_id",
            "name": f"eq.{gen_name}",
            "project_id": f"eq.{project_id}",
            "backend": "eq.rfd4",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0]["task_id"] if rows else None


def _persist(gateway, row: Dict[str, Any], consistency: Dict[str, Any]) -> None:
    row_id = str(row.get("id") or "")
    if not row_id or row_id.startswith("alias:"):
        return
    confidence = row.get("confidence")
    merged = dict(confidence) if isinstance(confidence, dict) else {}
    merged["rfd4_consistency"] = consistency
    gateway.task_store.postgrest.request(
        "PATCH",
        "project_tasks",
        query={"id": f"eq.{row_id}"},
        payload={"confidence": merged},
        headers={"Prefer": "return=minimal"},
        expect_json=False,
    )


def supports(name: str, backend: str) -> bool:
    return str(name or "").startswith("RFD4 verify") and str(backend or "") in VERIFIED_ENGINES


__all__ = ["maybe_enrich_view", "parse_verify_name", "compute_consistency", "supports"]
