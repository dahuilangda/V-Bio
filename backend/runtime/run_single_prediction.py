# run_single_prediction.py
import sys
import os
import json
import uuid
import tempfile
import shutil
import traceback
import yaml
import hashlib
import glob
import zipfile
import shlex
import requests
import time
import tarfile
import io
import math
import re
import base64
import random
import copy
import pickle
import numpy as np
from pathlib import Path
from collections import Counter
from typing import Optional, List, Tuple, Dict, Any, Iterable, Callable
import subprocess
from backend.core.errors import TransientError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PROJECT_ROOT / "capabilities"






if CAPABILITIES_DIR.is_dir() and str(CAPABILITIES_DIR) not in sys.path:
    sys.path.insert(0, str(CAPABILITIES_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BOLTZ2SCORE_SCRIPT = "/workspace/vbio/capabilities/boltz2score/boltz2score.py"

sys.path.append(str(PROJECT_ROOT))
from backend.worker.docker_cmd import ensure_image, readable_docker_failure_diagnosis  # noqa: E402
from backend.runtime.design_constants import (  # noqa: E402
    BACKBONE_CA_CA_MIN_A,
    PRODUCT_BOUND_MAX_DISTANCE_A,
    REFINE_ABSOLUTE_PAIR_MIN_A,
    REFINE_INTERPENETRATION_MIN_A,
    REFINE_PACKING_BAND_A,
    plddt_gate_for_chirality,
)
from backend.core.config import (
    MSA_SERVER_URL,
    MSA_SERVER_TIMEOUT_SECONDS,
    COLABFOLD_JOBS_DIR,
    BOLTZ2_DOCKER_IMAGE,
    BOLTZ2_DOCKER_EXTRA_ARGS,
    BOLTZ2_DOCKER_SHM_SIZE,
    BOLTZ2_HOST_CACHE_DIR,
    BOLTZ2_CONTAINER_CACHE_DIR,
    ALPHAFOLD3_DOCKER_IMAGE,
    ALPHAFOLD3_MODEL_DIR,
    ALPHAFOLD3_DATABASE_DIR,
    ALPHAFOLD3_DOCKER_EXTRA_ARGS,
    PROTENIX_DOCKER_IMAGE,
    PROTENIX_MODEL_DIR,
    PROTENIX_MODEL_NAME,
    PROTENIX_SOURCE_DIR,
    PROTENIX_DOCKER_EXTRA_ARGS,
    PROTENIX_INFER_EXTRA_ARGS,
    PROTENIX_PYTHON_BIN,
    MOL1_DOCKER_IMAGE,
    RFD4_DOCKER_IMAGE,
    RFD4_DATA_DIR,
    RFD4_NUM_DESIGNS,
    RFD4_CONTIG,
    PROTENIX_USE_HOST_USER,
    PROTENIX_CONTAINER_APP_DIR,
    PROTENIX_CONTAINER_MODEL_DIR,
    PROTENIX_CONTAINER_CHECKPOINT_PATH,
    PROTENIX_COMMON_CACHE_DIR,
    PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS,
    PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX,
    RESULTS_BASE_DIR,
    VBIO_API_TOKEN,
    VBIO_GATEWAY_URL,
)
from backend.scheduling.capability_router import build_capability_queue
from backend.services.common_utils import (
    ProteinMsaMode,
    coerce_bool,
    extract_protein_msa_policies,
    infer_use_msa_server_from_yaml_text,
    is_msa_disabled,
)
from backend.runtime.linker_measurement import (
    BICYCLIC_LINKER_ATOM_MAP,
    _append_staged_bicyclic_links,
    _cyclic_headtail_bond_report,
    _dpeptide_linker_bond_report,
    _pdb_link_line,
    _rebuild_single_atom_linker_anchor,
)
_PROTENIX_INPUT_CONTAINER_DIR = "/workspace/protenix_input"

_PROTENIX_TEMPLATE_CONTAINER_DIR = f"{_PROTENIX_INPUT_CONTAINER_DIR}/template_mmcif"
# 用户模板缺少 PDB 注册元数据（release_dates、chem_comp.type），由后端按
# 任务补齐：合成 ID 形如 t001（4 字符字母数字，满足 hit 名约束），日期取
# 泄漏截止期（2021-09-30）之前的固定值。

_PROTENIX_CCD_CIF = "components.cif"

_PROTENIX_CCD_PKL = "components.cif.rdkit_mol.pkl"

_PROTENIX_MERGED_CCD_FILES = (_PROTENIX_CCD_CIF, _PROTENIX_CCD_PKL)



_PROTENIX_SUMMARY_CONF_RE = re.compile(r"_summary_confidence_sample_(\d+)\.json$", re.IGNORECASE)



_BOLTZ_RESULT_CONF_RE = re.compile(r"^confidence_(.+)_model_(\d+)\.json$", re.IGNORECASE)

def build_chain_sequence_map(yaml_data: dict) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in yaml_data.get("sequences", []):
        if not isinstance(item, dict):
            continue
        # Support both "protein" (Boltz format) and "proteinChain" (V-Bio format)
        protein = item.get("protein") or item.get("proteinChain")
        if protein is None or not isinstance(protein, dict):
            continue
        seq = protein.get("sequence", "")
        ids = protein.get("id") or protein.get("chain_id")
        if isinstance(ids, list):
            chain_ids = ids
        else:
            chain_ids = [ids] if ids is not None else []
        if not chain_ids:
            continue
        for chain_id in chain_ids:
            mapping[chain_id] = seq
    return mapping


def build_alignment_indices(query_seq: str, template_seq: str) -> Tuple[List[int], List[int]]:
    if not query_seq or not template_seq:
        return [], []

    aligner = Align.PairwiseAligner()
    aligner.mode = "global"
    alignment = aligner.align(query_seq, template_seq)[0]
    query_indices: List[int] = []
    template_indices: List[int] = []

    for query_block, template_block in zip(alignment.aligned[0], alignment.aligned[1]):
        q_start, q_end = query_block
        t_start, t_end = template_block
        length = min(q_end - q_start, t_end - t_start)
        for offset in range(length):
            query_indices.append(int(q_start + offset))
            template_indices.append(int(t_start + offset))

    return query_indices, template_indices


from backend.runtime.peptide_design_backend import run_peptide_design_backend
from backend.runtime.rfd4_strategy import (
    run_bicyclic_strategy,
    run_rfd4_backend,
    run_rfd4_design_strategy,
)
from backend.runtime.backends import structure_backend
from backend.runtime.backends.base import BackendRunContext
from backend.runtime.backends.request import PredictionRequest

from backend.runtime.affinity_support import (  # noqa: F401
    _infer_affinity_chain_plan,
    _legacy_parse_ligand_from_text,
    _run_boltz2score_affinity_postprocess,
    _sanitize_atom_name_for_affinity,
    _stage_structure_for_affinity_container,
    _is_protein_like_residue_name,
    _extract_ligand_smiles_list,
    extract_affinity_config_from_yaml,
    find_ligand_resname_in_cif,
    prepare_structure_for_affinity,
)
from backend.runtime.msa_pipeline import (
    _inject_local_msa_paths_into_yaml,
    _require_complete_external_msa,
    cache_msa_files_from_temp_dir,
    generate_msa_for_sequences,
    get_sequence_hash,
    parse_a3m_content,
    request_msa_from_server,
    save_msa_result_to_file,
)

from backend.runtime.archiving import (
    assert_boltz_preprocessing_succeeded,
    create_af3_archive,
    create_archive_with_a3m,
    create_protenix_archive,
    get_cached_a3m_files,
)
AF3_DEFAULT_MODEL_SEED_COUNT = 5
def build_af3_model_seeds(seed: Optional[int], count: int = AF3_DEFAULT_MODEL_SEED_COUNT) -> Optional[List[int]]:
    if seed is None:
        return None
    try:
        base_seed = int(seed)
    except (TypeError, ValueError):
        return None
    if count <= 1:
        return [base_seed]
    return [base_seed + offset for offset in range(count)]




def _ensure_af3_required_fields(cif_text: str) -> str:
    """
    Ensure mmCIF contains all fields required by AlphaFold3 for template parsing.

    AF3 requires _atom_site.pdbx_PDB_model_num field which may be missing
    when converting from PDB or generating mmCIF with gemmi.
    """
    try:
        doc = gemmi.cif.read_string(cif_text)
        if len(doc) == 0:
            return cif_text
        block = doc.sole_block()

        # Check if _atom_site table exists
        atom_site_loop = block.find_loop("_atom_site")
        if not atom_site_loop:
            return cif_text

        tags = atom_site_loop.tags
        has_model_num = "_atom_site.pdbx_PDB_model_num" in tags

        if not has_model_num:
            lines = cif_text.splitlines()
            result_lines = []
            in_atom_site_loop = False
            atom_site_tags_found = False
            model_num_idx = -1

            for i, line in enumerate(lines):
                stripped = line.strip()

                if stripped.startswith("loop_"):
                    j = i + 1
                    atom_site_tags = []
                    while j < len(lines) and lines[j].strip().startswith("_"):
                        atom_site_tags.append(lines[j].strip())
                        j += 1

                    if atom_site_tags and any(t.startswith("_atom_site.") for t in atom_site_tags):
                        in_atom_site_loop = True
                        atom_site_tags_found = True

                        # pdbx_PDB_model_num belongs after group_PDB and before id.
                        insert_idx = -1
                        for idx, tag in enumerate(atom_site_tags):
                            if tag == "_atom_site.group_PDB":
                                insert_idx = idx + 1
                                break
                            elif tag == "_atom_site.id" and insert_idx == -1:
                                insert_idx = idx
                                break

                        result_lines.append(line)
                        for k, tag in enumerate(atom_site_tags):
                            if k == insert_idx:
                                result_lines.append("_atom_site.pdbx_PDB_model_num")
                                model_num_idx = insert_idx
                            result_lines.append(lines[i + 1 + k])

                        # If we didn't find a place to insert, add at the end
                        if insert_idx == -1:
                            result_lines.append("_atom_site.pdbx_PDB_model_num")
                            model_num_idx = len(atom_site_tags)

                        continue

                if in_atom_site_loop and atom_site_tags_found:
                    if not stripped.startswith("_") and stripped and not stripped.startswith("loop_") and not stripped.startswith("data_"):
                        parts = stripped.split()
                        if model_num_idx >= 0 and model_num_idx < len(parts) + 1:
                            parts.insert(model_num_idx, "1")
                            result_lines.append(" ".join(parts))
                        else:
                            result_lines.append(line)
                        continue
                    elif stripped.startswith("_") or stripped.startswith("loop_") or stripped.startswith("data_"):
                        in_atom_site_loop = False
                        atom_site_tags_found = False
                        model_num_idx = -1

                result_lines.append(line)

            return "\n".join(result_lines) + ("\n" if result_lines else "")

        return cif_text
    except Exception:
        return cif_text




def prepare_template_payloads(
    yaml_content: str,
    template_inputs: Optional[List[dict]],
    temp_dir: str,
) -> Tuple[str, List[dict]]:
    if not template_inputs:
        return yaml_content, []

    yaml_data = yaml.safe_load(yaml_content) or {}
    had_templates = bool(yaml_data.get("templates"))
    chain_seq_map = build_chain_sequence_map(yaml_data)
    boltz_templates = list(yaml_data.get("templates", []) or [])
    if boltz_templates:
        normalized = []
        for entry in boltz_templates:
            if not isinstance(entry, dict):
                continue
            path_ref = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
            if not path_ref:
                continue
            path = Path(str(path_ref))
            resolved: Optional[Path] = None
            if path.is_absolute():
                if path.exists():
                    resolved = path
            else:
                if path.exists():
                    resolved = path.resolve()
                else:
                    candidate = Path(temp_dir) / path
                    if candidate.exists():
                        resolved = candidate
            if not resolved:
                continue
            updated = dict(entry)
            if "pdb" in updated:
                updated["pdb"] = str(resolved)
            else:
                updated.pop("pdb", None)
                if "cif" in updated:
                    updated["cif"] = str(resolved)
                    updated.pop("mmcif", None)
                elif "mmcif" in updated:
                    updated["mmcif"] = str(resolved)
            normalized.append(updated)
        boltz_templates = normalized
    af3_templates: List[dict] = []

    templates_dir = Path(temp_dir) / "templates"
    for idx, template in enumerate(template_inputs):
        content_b64 = template.get("content_base64")
        if not content_b64:
            print("[WARN] 模板内容为空，跳过。", file=sys.stderr)
            continue
        try:
            raw_bytes = base64.b64decode(content_b64)
        except Exception:
            print("[WARN] 模板内容解码失败，跳过。", file=sys.stderr)
            continue
        text = raw_bytes.decode("utf-8", errors="replace")
        fmt = (template.get("format") or "pdb").lower()
        file_name = template.get("file_name") or template.get("filename") or f"template_{idx}.{fmt}"
        template_chain_id = template.get("template_chain_id")

        if fmt == "pdb":
            chain_sequences, first_chain = _extract_chain_sequences_from_pdb_text(text)
        else:
            chain_sequences = extract_chain_sequences_from_structure(text, fmt)
            first_chain = next(iter(chain_sequences.keys()), None)
        if not chain_sequences:
            print("[WARN] 模板未解析出蛋白质链，跳过。", file=sys.stderr)
            continue
        if template_chain_id not in chain_sequences:
            template_chain_id = first_chain or next(iter(chain_sequences.keys()))
        template_seq = chain_sequences.get(template_chain_id, "")

        templates_dir.mkdir(parents=True, exist_ok=True)
        raw_path = templates_dir / file_name
        try:
            raw_path.write_bytes(raw_bytes)
        except Exception as exc:
            print(f"[WARN] 保存模板文件失败 {raw_path}: {exc}", file=sys.stderr)
            continue
        # The single-chain conversion writes into cif_path, which for cif
        # uploads equals raw_path and would clobber it; keep a copy with the
        # original author numbering for pocket-residue translation.
        author_path = templates_dir / f"{Path(file_name).stem}_author{Path(file_name).suffix or '.cif'}"
        try:
            author_path.write_bytes(raw_bytes)
        except Exception as exc:
            print(f"[WARN] 保存原始模板失败 {author_path}: {exc}", file=sys.stderr)

        if fmt == "pdb":
            filtered_path = templates_dir / f"{Path(file_name).stem}_chain{template_chain_id}.pdb"
            try:
                _write_filtered_pdb_by_chain(text, str(template_chain_id or ""), filtered_path)
                raw_path = filtered_path
            except Exception as exc:
                print(f"[WARN] 过滤 PDB 模板失败 {raw_path}: {exc}", file=sys.stderr)

        cif_stem = Path(file_name).stem or f"template_{idx}"
        cif_path = templates_dir / f"{cif_stem}.cif"
        try:
            cif_path, cif_text, resolved_chain_id, cif_template_seq = convert_structure_to_single_chain_mmcif(
                raw_path, str(template_chain_id or ""), cif_path
            )
        except Exception as exc:
            print(f"[WARN] 模板转换失败，已跳过 {file_name}: {exc}", file=sys.stderr)
            continue
        if cif_template_seq:
            template_seq = cif_template_seq
            template_chain_id = resolved_chain_id

        target_chain_ids = template.get("target_chain_ids") or []
        if target_chain_ids and template_seq:
            for item in yaml_data.get("sequences", []):
                if "protein" not in item:
                    continue
                protein = item.get("protein", {})
                ids = protein.get("id")
                if isinstance(ids, list):
                    ids_list = ids
                else:
                    ids_list = [ids] if ids is not None else []
                if not set(ids_list).intersection(target_chain_ids):
                    continue
                if not protein.get("sequence"):
                    protein["sequence"] = template_seq
            chain_seq_map = build_chain_sequence_map(yaml_data)
        if target_chain_ids:
            query_seq = chain_seq_map.get(target_chain_ids[0], "")
        else:
            query_seq = ""

        query_indices, template_indices = build_alignment_indices(query_seq, template_seq)

        # Boltz template entry
        boltz_entry: Dict[str, Any] = {"cif": str(cif_path)}
        # pocket-residue translation needs the author numbering of the
        # original upload; the converted mmcif renumbers residues 1..N
        boltz_entry["author_pdb"] = str(author_path)
        if target_chain_ids:
            boltz_entry["chain_id"] = target_chain_ids if len(target_chain_ids) > 1 else target_chain_ids[0]
        boltz_templates.append(boltz_entry)

        # AF3 template payload
        if query_indices and template_indices:
            af3_source_text = cif_text
            af3_mmcif = _sanitize_release_date_text_with_gemmi(
                af3_source_text,
                release_date=None,
                include_loops=True,
            )
            af3_mmcif = _force_af3_release_date_text(af3_mmcif, None)
            af3_templates.append({
                "target_chain_ids": target_chain_ids,
                "mmcif": af3_mmcif,
                "queryIndices": query_indices,
                "templateIndices": template_indices,
            })

    if boltz_templates or had_templates:
        yaml_data["templates"] = boltz_templates
    elif "templates" in yaml_data:
        yaml_data.pop("templates", None)
    yaml_content = yaml.safe_dump(
        yaml_data,
        sort_keys=False,
        default_flow_style=False,
    )

    return yaml_content, af3_templates






def prepare_yaml_template_payloads(yaml_content: str, temp_dir: str) -> List[dict]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    template_entries = yaml_data.get("templates") or []
    if not isinstance(template_entries, list) or not template_entries:
        return []

    chain_seq_map = build_chain_sequence_map(yaml_data)
    if not chain_seq_map:
        return []

    af3_templates: List[dict] = []
    templates_dir = Path(temp_dir) / "templates_from_yaml"

    for idx, entry in enumerate(template_entries):
        if not isinstance(entry, dict):
            continue
        cif_ref = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
        if not cif_ref:
            continue
        cif_path = Path(str(cif_ref))
        if not cif_path.is_absolute():
            candidate = Path(temp_dir) / cif_path
            if candidate.exists():
                cif_path = candidate
        if not cif_path.exists():
            print(f"[WARN] 模板 CIF 文件不存在，跳过: {cif_path}", file=sys.stderr)
            continue
        suffix = cif_path.suffix.lower()
        fmt = "cif" if suffix in (".cif", ".mmcif") else "pdb"
        try:
            text = cif_path.read_text()
        except Exception as exc:
            print(f"[WARN] 读取模板文件失败 {cif_path}: {exc}", file=sys.stderr)
            continue

        template_chain_id = entry.get("template_id") or entry.get("template_chain_id")
        if isinstance(template_chain_id, (list, tuple)):
            template_chain_id = template_chain_id[0] if template_chain_id else None
        target_chain_ids = _normalize_chain_id_list(
            entry.get("chain_id") or entry.get("target_chain_ids") or entry.get("chain_ids")
        )
        if not target_chain_ids and chain_seq_map:
            target_chain_ids = [next(iter(chain_seq_map.keys()))]

        if fmt == "pdb":
            chain_sequences, first_chain = _extract_chain_sequences_from_pdb_text(text)
        else:
            chain_sequences = extract_chain_sequences_from_structure(text, fmt)
            first_chain = next(iter(chain_sequences.keys()), None)
        if not chain_sequences:
            continue
        if template_chain_id not in chain_sequences:
            template_chain_id = first_chain or next(iter(chain_sequences.keys()))
        template_seq = chain_sequences.get(template_chain_id, "")

        cif_text: Optional[str] = None
        cif_out = templates_dir / f"template_yaml_{idx}.cif"
        try:
            if fmt == "pdb":
                filtered_path = templates_dir / f"template_yaml_{idx}_chain{template_chain_id}.pdb"
                _write_filtered_pdb_by_chain(text, str(template_chain_id or ""), filtered_path)
                source_path = filtered_path
            else:
                source_path = cif_path
            _, cif_text, resolved_chain_id, cif_template_seq = convert_structure_to_single_chain_mmcif(
                source_path, str(template_chain_id or ""), cif_out
            )
            if cif_template_seq:
                template_seq = cif_template_seq
                template_chain_id = resolved_chain_id
        except Exception as exc:
            if fmt in ("cif", "mmcif") and text:
                print(f"[WARN] 转换模板失败，改用原始 mmCIF: {cif_path} ({exc})", file=sys.stderr)
                cif_text = text
            else:
                print(f"[WARN] 转换模板为单链 mmCIF 失败 {cif_path}: {exc}", file=sys.stderr)
                continue

        query_seq = chain_seq_map.get(target_chain_ids[0], "") if target_chain_ids else ""
        query_indices, template_indices = build_alignment_indices(query_seq, template_seq)
        if not query_indices or not template_indices:
            continue

        af3_mmcif = _sanitize_release_date_text_with_gemmi(
            cif_text or "",
            release_date=None,
            include_loops=True,
        )
        af3_mmcif = _force_af3_release_date_text(af3_mmcif, None)
        af3_templates.append({
            "target_chain_ids": target_chain_ids,
            "mmcif": af3_mmcif,
            "queryIndices": query_indices,
            "templateIndices": template_indices,
        })

    return af3_templates


_PROTENIX_INPUT_CONTAINER_DIR = "/workspace/protenix_input"
_PROTENIX_TEMPLATE_CONTAINER_DIR = f"{_PROTENIX_INPUT_CONTAINER_DIR}/template_mmcif"
# 用户模板缺少 PDB 注册元数据（release_dates、chem_comp.type），由后端按
# 任务补齐：合成 ID 形如 t001（4 字符字母数字，满足 hit 名约束），日期取
# 泄漏截止期（2021-09-30）之前的固定值。
_PROTENIX_TEMPLATE_RELEASE_DATE = "2019-01-01"
# 标准氨基酸的 PDB 注册 _chem_comp.type：Protenix 解析模板时以该字段判定
# 蛋白链（须含 "peptide"）。
_PROTENIX_STD_AA_CHEM_COMP_TYPES = {
    **{aa: "L-peptide linking" for aa in
       ("ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "HIS", "ILE",
        "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL")},
    "GLY": "peptide linking",
    "MSE": "L-peptide derivative",
}




def validate_af3_database_files(database_dir: str) -> None:
    """
    Perform lightweight sanity checks on key AF3 database FASTA files to fail fast
    when files are missing or corrupted (common cause of jackhmmer 'Parse failed').
    """
    required_files = [
        "uniref90_2022_05.fa",
        "uniprot_all_2021_04.fa",
        "mgy_clusters_2022_05.fa",
        "bfd-first_non_consensus_sequences.fasta",
    ]

    # Allow common amino-acid symbols plus gap/stop placeholders; digits are not valid.
    allowed_seq_pattern = re.compile(r"^[A-Za-z\-\.*?]+$")
    max_lines_scan = AF3_VALIDATE_MAX_LINES  # scan early part; set to 0 to scan full file

    for filename in required_files:
        path = Path(database_dir) / filename
        if not path.exists() or not path.is_file():
            raise RuntimeError(
                f"AlphaFold3 数据库缺少必需文件: {path}. 请重新下载/解压 AF3 数据库。"
            )
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
                if b"\x00" in head:
                    raise RuntimeError(
                        f"检测到文件包含非法空字节，可能已损坏: {path}. 请重新下载/解压该文件。"
                    )
                # 第一条非空行应为 FASTA 标题
                first_line = head.splitlines()[0] if head else b""
                if not first_line.startswith(b">"):
                    raise RuntimeError(
                        f"文件不是有效的 FASTA 格式（首行未以 '>' 开头）: {path}. "
                        "请重新下载/解压 AF3 数据库。"
                    )

            # Streaming scan of the early portion of the file to catch corruption quickly.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                header_seen = False
                for lineno, line in enumerate(f, start=1):
                    if max_lines_scan > 0 and lineno > max_lines_scan:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith(">"):  # header line
                        header_seen = True
                        continue
                    if not header_seen:
                        raise RuntimeError(
                            f"文件开头缺少 FASTA 标题: {path} (行 {lineno})。"
                        )
                    if not allowed_seq_pattern.match(stripped):
                        preview = stripped[:80]
                        raise RuntimeError(
                            f"检测到无效的 FASTA 序列字符 (行 {lineno}): '{preview}'. "
                            f"请重新下载/解压 {path.name}，当前文件可能已损坏。"
                        )
        except OSError as e:
            raise RuntimeError(f"无法读取 AF3 数据库文件 {path}: {e}") from e




from backend.runtime.ipsae_postprocess import (
    _run_af3_ipsae_postprocess,
    _run_boltz2score_ipsae_postprocess_in_docker,
    _run_boltz_ipsae_postprocess,
    _run_protenix_ipsae_postprocess,
    _run_standalone_ipsae_postprocess,
)
from backend.runtime.constraints_pipeline import (
    normalize_ligand_chain_collisions as _normalize_ligand_chain_collisions,
    print_constraint_residue_summary as _print_constraint_residue_summary,
    remap_constraints_by_template_alignment as _remap_constraints_by_template_alignment,
    sanitize_constraints_for_chain_lengths as _sanitize_constraints_for_chain_lengths,
    validate_unique_sequence_chain_ids as _validate_unique_sequence_chain_ids,
)
from backend.runtime import bicyclic_linkers, rfd4_adapter
from backend.runtime.nesso_backend import run_nesso_backend
from backend.runtime import rfd4_adapter
from backend.runtime.af3_adapter import (
    AF3Preparation,
    build_af3_fasta,
    build_af3_json,
    collect_chain_msa_paths,
    load_unpaired_msa,
    parse_yaml_for_af3,
    safe_filename,
    serialize_af3_json,
)
from backend.runtime.protenix_adapter import (
    ProtenixPreparation,
    apply_protein_msa_paths,
    parse_yaml_for_protenix,
    serialize_protenix_json,
)
from Bio import Align
import gemmi
from rdkit import Chem
from backend.runtime.custom_ccd_builder import (
    _append_custom_residues_ccd,
    _append_custom_residues_ccd_from_molecules,
    _boltz_custom_ccd_aliases,
    _build_custom_ccd_bundle,
    _build_custom_ccd_mol,
    _normalize_custom_ccd_molecules,
)
from backend.runtime.ccd_contract import validate_ccd_additions

from backend.runtime.msa_pipeline import MSA_CACHE_CONFIG

IPSAE_PAE_CUTOFF = 12.0
IPSAE_DIST_CUTOFF = 5.0
_BOLTZ_RESULT_CONF_RE = re.compile(r"^confidence_(.+)_model_(\d+)\.json$", re.IGNORECASE)
_PROTENIX_SUMMARY_CONF_RE = re.compile(r"_summary_confidence_sample_(\d+)\.json$", re.IGNORECASE)


def _normalized_msa_server_url() -> str:
    return str(MSA_SERVER_URL or "").strip()


def _assert_msa_server_configured(backend: str) -> str:
    msa_server_url = _normalized_msa_server_url()
    if not msa_server_url:
        raise ValueError(
            f"Backend '{backend}' requires MSA_SERVER_URL in .env."
        )
    return msa_server_url

# How many lines of each AF3 FASTA to scan for corruption. Set env ALPHAFOLD3_VALIDATE_MAX_LINES=0
# to scan the whole file (may take time but catches deep corruption).
AF3_VALIDATE_MAX_LINES = os.environ.get("ALPHAFOLD3_VALIDATE_MAX_LINES")
AF3_VALIDATE_MAX_LINES = int(AF3_VALIDATE_MAX_LINES) if AF3_VALIDATE_MAX_LINES else 200000
AMINO_ACID_MAPPING = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLU": "E", "GLN": "Q", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
ONE_TO_THREE_AMINO_ACID = {one: three for three, one in AMINO_ACID_MAPPING.items()}
DEFAULT_TEMPLATE_RELEASE_DATE = "1987-11-16"
_RELEASE_DATE_PAIR_TAGS = (
    "_pdbx_database_status.recvd_initial_deposition_date",
    "_pdbx_database_status.date_of_initial_deposition",
    "_pdbx_database_status.date_of_release",
)
_REVISION_DATE_TAG = "_pdbx_audit_revision_history.revision_date"
_CIF_RELEASE_DATE_TAGS = _RELEASE_DATE_PAIR_TAGS + (
    "_pdbx_audit_revision_history.revision_date",
    "_database_PDB_rev.date_original",
    "_database_PDB_rev.date",
)
_RELEASE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_release_date(value: Optional[str]) -> bool:
    if not value:
        return False
    return _RELEASE_DATE_RE.match(str(value).strip()) is not None


def _sanitize_date_tags(block: gemmi.cif.Block, date_value: str) -> None:
    date_value = date_value if _is_valid_release_date(date_value) else DEFAULT_TEMPLATE_RELEASE_DATE
    for item in block:
        try:
            tag, val = item.pair
        except Exception:
            tag = None
            val = None
        if tag and "date" in str(tag).lower():
            if not _is_valid_release_date(val):
                block.set_pair(tag, date_value)
        loop = getattr(item, "loop", None)
        if not loop:
            continue
        date_cols = [idx for idx, tag_name in enumerate(loop.tags) if "date" in tag_name.lower()]
        if not date_cols:
            continue
        for row_idx in range(loop.length()):
            for col_idx in date_cols:
                current = loop[row_idx, col_idx]
                if not _is_valid_release_date(current):
                    loop[row_idx, col_idx] = date_value


def _remove_loops_with_prefix(block: gemmi.cif.Block, prefixes: Tuple[str, ...]) -> None:
    items = list(block)
    for item in items:
        loop = getattr(item, "loop", None)
        if not loop:
            continue
        tags = [tag.lower() for tag in loop.tags]
        for prefix in prefixes:
            if any(tag.startswith(prefix.lower()) for tag in tags):
                try:
                    item.erase()
                except Exception:
                    pass
                break


def _extract_release_date_from_cif(path: Path) -> Optional[str]:
    try:
        doc = gemmi.cif.read(str(path))
    except Exception:
        return None
    if len(doc) == 0:
        return None
    block = doc.sole_block()
    for tag in _CIF_RELEASE_DATE_TAGS:
        try:
            value = block.find_value(tag)
        except Exception:
            value = ""
        if value and value not in (".", "?"):
            text = str(value).strip()
            if _is_valid_release_date(text):
                return text
    return None


def _ensure_release_date(
    block: gemmi.cif.Block,
    release_date: Optional[str],
    include_loops: bool = False,
) -> None:
    date_value = release_date if _is_valid_release_date(release_date) else DEFAULT_TEMPLATE_RELEASE_DATE

    # Always set pair tags to a valid ISO date to avoid "0"/"?" placeholders.
    for tag in _RELEASE_DATE_PAIR_TAGS:
        block.set_pair(tag, date_value)

    if include_loops:
        _remove_loops_with_prefix(block, ("_pdbx_audit_revision_history.", "_database_PDB_rev."))
        audit_loop = block.init_loop(
            "_pdbx_audit_revision_history.",
            [
                "revision_ordinal",
                "data_content_type",
                "major_revision",
                "minor_revision",
                "revision_date",
            ],
        )
        audit_loop.add_row(["1", "Structure model", "1", "0", date_value])

        db_loop = block.init_loop(
            "_database_PDB_rev.",
            [
                "num",
                "date",
                "date_original",
            ],
        )
        db_loop.add_row(["1", date_value, date_value])
    else:
        # Drop audit/history loops to avoid malformed loop rows; rely on pair tags instead.
        _remove_loops_with_prefix(block, ("_pdbx_audit_revision_history.", "_database_PDB_rev."))
        block.set_pair(_REVISION_DATE_TAG, date_value)


def _inject_release_date_text(
    cif_text: str,
    release_date: Optional[str],
    include_loops: bool = False,
) -> str:
    date_value = release_date if _is_valid_release_date(release_date) else DEFAULT_TEMPLATE_RELEASE_DATE
    lines = cif_text.splitlines()
    updated_lines: List[str] = []
    saw_valid_pair = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            updated_lines.append(line)
            continue
        replaced = False
        for tag in _RELEASE_DATE_PAIR_TAGS:
            if stripped.startswith(tag):
                parts = stripped.split()
                if len(parts) >= 2 and _is_valid_release_date(parts[1]):
                    saw_valid_pair = True
                else:
                    line = f"{tag} {date_value}"
                    replaced = True
                break
        updated_lines.append(line)
    if saw_valid_pair:
        return "\n".join(updated_lines) + ("\n" if updated_lines else "")

    out_lines = updated_lines
    insert_at = 1 if out_lines and out_lines[0].lower().startswith("data_") else 0
    injection = [
        f"_pdbx_database_status.recvd_initial_deposition_date {date_value}",
        f"_pdbx_database_status.date_of_initial_deposition {date_value}",
        f"_pdbx_database_status.date_of_release {date_value}",
        f"{_REVISION_DATE_TAG} {date_value}",
        "",
    ]
    if include_loops:
        injection = [
            f"_pdbx_database_status.recvd_initial_deposition_date {date_value}",
            f"_pdbx_database_status.date_of_initial_deposition {date_value}",
            f"_pdbx_database_status.date_of_release {date_value}",
            "loop_",
            "_pdbx_audit_revision_history.revision_ordinal",
            "_pdbx_audit_revision_history.data_content_type",
            "_pdbx_audit_revision_history.major_revision",
            "_pdbx_audit_revision_history.minor_revision",
            "_pdbx_audit_revision_history.revision_date",
            f"1 'Structure model' 1 0 {date_value}",
            "loop_",
            "_database_PDB_rev.num",
            "_database_PDB_rev.date",
            "_database_PDB_rev.date_original",
            f"1 {date_value} {date_value}",
            "",
        ]
    merged = out_lines[:insert_at] + injection + out_lines[insert_at:]
    return "\n".join(merged) + ("\n" if merged and not merged[-1].endswith("\n") else "")


def _force_af3_release_date_text(cif_text: str, release_date: Optional[str] = None) -> str:
    """Ensure AF3 can read a release date by injecting a proper audit loop + db status pairs."""
    date_value = release_date if _is_valid_release_date(release_date) else DEFAULT_TEMPLATE_RELEASE_DATE
    stripped = _strip_problem_loops_text(
        cif_text,
        ("_pdbx_audit_revision_history.", "_database_PDB_rev."),
    )
    lines = stripped.splitlines()
    cleaned: List[str] = []
    for line in lines:
        stripped_line = line.strip()
        if stripped_line.startswith(_REVISION_DATE_TAG):
            continue
        cleaned.append(line)

    # Replace or insert database_status pairs
    for tag in _RELEASE_DATE_PAIR_TAGS:
        replaced = False
        for idx, line in enumerate(cleaned):
            if line.strip().startswith(tag):
                cleaned[idx] = f"{tag} {date_value}"
                replaced = True
                break
        if not replaced:
            insert_at = 1 if cleaned and cleaned[0].lower().startswith("data_") else 0
            cleaned.insert(insert_at, f"{tag} {date_value}")

    insert_at = 1 if cleaned and cleaned[0].lower().startswith("data_") else 0
    audit_loop = [
        "loop_",
        "_pdbx_audit_revision_history.revision_ordinal",
        "_pdbx_audit_revision_history.data_content_type",
        "_pdbx_audit_revision_history.major_revision",
        "_pdbx_audit_revision_history.minor_revision",
        "_pdbx_audit_revision_history.revision_date",
        f"1 'Structure model' 1 0 {date_value}",
        "",
    ]
    merged = cleaned[:insert_at] + audit_loop + cleaned[insert_at:]
    return "\n".join(merged) + ("\n" if merged else "")


def _strip_problem_loops_text(cif_text: str, prefixes: Tuple[str, ...]) -> str:
    lines = cif_text.splitlines()
    out_lines: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.lower() == "loop_":
            tag_lines = []
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("_"):
                tag_lines.append(lines[j].strip())
                j += 1
            if tag_lines and any(
                any(tag.lower().startswith(prefix.lower()) for tag in tag_lines)
                for prefix in prefixes
            ):
                # Skip loop data rows until next item/loop/data block
                k = j
                while k < len(lines):
                    row_stripped = lines[k].strip()
                    if not row_stripped:
                        k += 1
                        continue
                    if row_stripped.startswith("_") or row_stripped.lower() == "loop_" or row_stripped.lower().startswith("data_"):
                        break
                    k += 1
                i = k
                continue
            out_lines.append(line)
            i += 1
            continue
        out_lines.append(line)
        i += 1
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _sanitize_release_date_text_with_gemmi(
    cif_text: str,
    release_date: Optional[str],
    include_loops: bool = False,
) -> str:
    try:
        doc = gemmi.cif.read_string(cif_text)
        if len(doc) == 0:
            return _inject_release_date_text(cif_text, release_date, include_loops=include_loops)
        block = doc.sole_block()
        _ensure_release_date(block, release_date, include_loops=include_loops)
        date_value = release_date if _is_valid_release_date(release_date) else DEFAULT_TEMPLATE_RELEASE_DATE
        _sanitize_date_tags(block, date_value)
        return doc.as_string()
    except Exception:
        stripped = _strip_problem_loops_text(
            cif_text,
            ("_pdbx_audit_revision_history.", "_database_PDB_rev."),
        )
        return _inject_release_date_text(stripped, release_date, include_loops=include_loops)


def extract_chain_sequences_from_structure(content: str, fmt: str) -> Dict[str, str]:
    fmt = (fmt or "").lower()
    if fmt == "pdb":
        structure = gemmi.read_pdb_string(content)
    elif fmt in {"cif", "mmcif"}:
        document = gemmi.cif.read_string(content)
        if len(document) == 0:
            return {}
        structure = gemmi.make_structure_from_block(document.sole_block())
    else:
        raise ValueError(f"Unsupported structure format: {fmt}")

    sequences: Dict[str, str] = {}
    if len(structure) == 0:
        return sequences

    for chain in structure[0]:
        seq_chars: List[str] = []
        for residue in chain:
            aa = _pdb_resname_to_one_letter(residue.name)
            if aa is None:
                continue
            seq_chars.append(aa)
        if seq_chars:
            sequences[chain.name] = "".join(seq_chars)
    return sequences


def _pdb_resname_to_one_letter(resname: str) -> Optional[str]:
    resname = resname.strip().upper()
    if not resname:
        return None
    if resname in AMINO_ACID_MAPPING:
        return AMINO_ACID_MAPPING[resname]
    info = gemmi.find_tabulated_residue(resname)
    if getattr(info, "found", False) and getattr(info, "is_amino_acid", False):
        code = getattr(info, "one_letter_code", "") or getattr(info, "fasta_code", "")
        if not code or code == "?" or len(code) != 1:
            code = "X"
        return code
    return None


def _extract_chain_sequences_from_pdb_text(pdb_text: str) -> Tuple[Dict[str, str], Optional[str]]:
    sequences: Dict[str, List[str]] = {}
    last_res_id: Dict[str, Tuple[str, str]] = {}
    first_chain: Optional[str] = None
    for line in pdb_text.splitlines():
        if not (line.startswith("ATOM  ") or line.startswith("HETATM")):
            continue
        if len(line) < 26:
            continue
        chain_id = line[21].strip() or "_"
        if first_chain is None:
            first_chain = chain_id
        resname = line[17:20].strip().upper()
        resseq = line[22:26].strip()
        icode = line[26].strip() if len(line) > 26 else ""
        res_id = (resseq, icode)
        if last_res_id.get(chain_id) == res_id:
            continue
        last_res_id[chain_id] = res_id
        aa = _pdb_resname_to_one_letter(resname)
        if aa is None:
            continue
        sequences.setdefault(chain_id, []).append(aa)
    seq_map = {cid: "".join(seq) for cid, seq in sequences.items() if seq}
    return seq_map, first_chain


def _write_filtered_pdb_by_chain(pdb_text: str, chain_id: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected = chain_id or "_"
    out_lines: List[str] = []
    in_model = False
    saw_model = False
    for line in pdb_text.splitlines():
        if line.startswith("MODEL"):
            if saw_model:
                break
            in_model = True
            saw_model = True
            out_lines.append(line)
            continue
        if line.startswith("ENDMDL"):
            if in_model:
                out_lines.append(line)
            break
        if line.startswith(("ATOM  ", "HETATM", "TER")):
            if len(line) < 22:
                continue
            line_chain = line[21].strip() or "_"
            if line_chain != selected:
                continue
            if line.startswith("HETATM"):
                resname = line[17:20].strip().upper() if len(line) >= 20 else ""
                if _pdb_resname_to_one_letter(resname) is None:
                    continue
            out_lines.append(line)
            continue
        if not saw_model and line.startswith((
            "HEADER", "TITLE ", "COMPND", "SOURCE", "KEYWDS", "EXPDTA",
            "AUTHOR", "REVDAT", "JRNL  ", "REMARK", "DBREF ", "SEQRES",
        )):
            out_lines.append(line)
    if not out_lines:
        out_lines = pdb_text.splitlines()
    output_path.write_text("\n".join(out_lines) + "\n")


def _canonicalize_template_residue_name(resname: str) -> Optional[str]:
    name = (resname or "").strip().upper()
    if not name:
        return None
    if name in AMINO_ACID_MAPPING:
        return name
    if name == "MSE":
        return "MET"
    info = gemmi.find_tabulated_residue(name)
    if getattr(info, "found", False) and getattr(info, "is_amino_acid", False):
        code = (getattr(info, "one_letter_code", "") or getattr(info, "fasta_code", "") or "").strip().upper()
        if len(code) == 1 and code in ONE_TO_THREE_AMINO_ACID:
            return ONE_TO_THREE_AMINO_ACID[code]
        return None
    return None


def _sanitize_template_chain_residues(chain: gemmi.Chain) -> Tuple[int, int]:
    removed = 0
    renamed = 0
    for idx in range(len(chain) - 1, -1, -1):
        residue = chain[idx]
        normalized_name = _canonicalize_template_residue_name(residue.name)
        if normalized_name is None:
            del chain[idx]
            removed += 1
            continue
        if residue.name != normalized_name:
            chain[idx].name = normalized_name
            renamed += 1
    return removed, renamed


def _build_single_chain_structure(
    source_path: Path,
    chain_id: str,
) -> Tuple[gemmi.Structure, str]:
    structure = gemmi.read_structure(str(source_path))
    if len(structure) == 0:
        raise ValueError("No model found in template structure.")

    # keep only first model
    while len(structure) > 1:
        del structure[1]

    model = structure[0]
    chain_ids = [c.name for c in model]
    selected_chain = chain_id if chain_id in chain_ids else (chain_ids[0] if chain_ids else None)
    if not selected_chain:
        raise ValueError("No chain found in template structure.")

    for chain in list(model):
        if chain.name != selected_chain:
            model.remove_chain(chain.name)

    structure.remove_waters()
    structure.remove_hydrogens()
    structure.remove_alternative_conformations()
    structure.remove_empty_chains()

    chain = model[selected_chain]
    removed_count, renamed_count = _sanitize_template_chain_residues(chain)
    if removed_count or renamed_count:
        print(
            f"[WARN] 模板链 {selected_chain} 已清理残基：移除 {removed_count} 个，标准化 {renamed_count} 个。",
            file=sys.stderr,
        )
    if len(chain) == 0:
        raise ValueError(
            f"Template chain '{selected_chain}' has no supported amino-acid residues after cleanup."
        )

    # Some model/mmCIF providers preserve source PDB residue numbers (for
    # example 241..625) while their ``_entity_poly_seq`` table starts at 1.
    # AF3 joins atoms to the polymer scheme using these numbers, so normalize
    # the sanitized single chain before rebuilding its entity sequence tables.
    for residue_index, residue in enumerate(chain, start=1):
        residue.seqid = gemmi.SeqId(residue_index, ' ')
        residue.label_seq = residue_index

    # Drop any pre-existing sequence tables that may not match the selected
    # chain, then rebuild the entity sequence from the residues we retained.
    structure.clear_sequences()
    structure.setup_entities()
    residue_names = [gemmi.Entity.first_mon(res.name) for res in chain]
    subchains = {res.subchain for res in chain}
    for entity in structure.entities:
        if any(sc in entity.subchains for sc in subchains):
            entity.full_sequence = list(residue_names)

    # Ensure label_seq_id and related tables are consistent with the sequence.
    try:
        structure.assign_label_seq_id()
    except Exception:
        # If label assignment fails, AF3 parsing will likely fail too; keep original
        pass

    return structure, selected_chain


def _extract_sequence_from_mmcif_text(cif_text: str, chain_id: Optional[str]) -> str:
    try:
        sequences = extract_chain_sequences_from_structure(cif_text, "cif")
    except Exception:
        return ""
    if not sequences:
        return ""
    if chain_id and chain_id in sequences:
        return sequences[chain_id]
    return next(iter(sequences.values()))


def convert_structure_to_single_chain_mmcif(
    source_path: Path,
    chain_id: str,
    output_path: Path,
) -> Tuple[Path, str, str, str]:
    structure, selected_chain = _build_single_chain_structure(source_path, chain_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    release_date: Optional[str] = None
    if source_path.suffix.lower() in {".cif", ".mmcif"}:
        release_date = _extract_release_date_from_cif(source_path)
    doc = structure.make_mmcif_document()
    try:
        block = doc.sole_block()
        _ensure_release_date(block, release_date)
    except Exception:
        pass
    cif_text = doc.as_string()
    # Ensure AF3-required fields are present
    cif_text = _ensure_af3_required_fields(cif_text)
    cif_text = _sanitize_release_date_text_with_gemmi(
        cif_text,
        release_date,
        include_loops=False,
    )
    output_path.write_text(cif_text)
    template_seq = _extract_sequence_from_mmcif_text(cif_text, selected_chain)
    return output_path, cif_text, selected_chain, template_seq






def _complete_template_chem_comp_types(cif_text: str) -> str:
    """补全模板 cif 中缺失的标准氨基酸 _chem_comp.type 元数据。

    id/type 两列由转换管线保证存在且等长，缺失即上游产物违约，抛错。
    """
    doc = gemmi.cif.read_string(cif_text)
    block = doc.sole_block()
    id_col = block.find_loop("_chem_comp.id")
    type_col = block.find_loop("_chem_comp.type")
    if id_col is None or type_col is None:
        raise ValueError(
            "模板 mmCIF 缺少 _chem_comp 注册元数据（id/type 列），"
            "无法通过 Protenix 模板解析。")
    if len(id_col) != len(type_col):
        raise ValueError(
            "模板 mmCIF 的 _chem_comp id/type 列长度不一致，文件已损坏。")
    changed = False
    for i in range(len(id_col)):
        comp = str(id_col[i]).strip("'\"").upper()
        if str(type_col[i]) in (".", "?"):
            canonical = _PROTENIX_STD_AA_CHEM_COMP_TYPES.get(comp)
            if canonical:
                # 含空格的值必须走 cif 引号序列化，否则 loop 行值数错乱。
                type_col[i] = gemmi.cif.quote(canonical)
                changed = True
    return doc.as_string() if changed else cif_text


def _stage_protenix_user_templates(
    template_payloads: List[dict],
    protenix_json: Any,
    protenix_input_dir: str,
    prep: Any,
) -> bool:
    """把用户模板注册进 Protenix 推理模板体系。

    Protenix 按 hit ID 查 release_dates 并从 mmcif 目录取模板结构。用户
    模板缺注册元数据，由本函数补齐：分配合成 ID、写出对应 cif、聚合
    hit a3m、生成按任务 release_dates 覆盖文件；过滤/取结构/对齐全部
    原样复用。实体定位与 MSA 挂载同源（entity_seq_positions 映射，
    实体下标与 sequences 位置不保证一致）。

    契约：空内容、无蛋白链、target_chain_ids 不匹配任一蛋白实体时抛
    ValueError；payloads 非空时全部注册成功返回 True，空列表返回 False。
    """
    if not template_payloads:
        return False
    template_dir = os.path.join(protenix_input_dir, "template_mmcif")
    os.makedirs(template_dir, exist_ok=True)
    container_dir = _PROTENIX_TEMPLATE_CONTAINER_DIR

    entity_chain_ids = prep.entity_chain_ids
    entity_kinds = prep.entity_kinds
    entity_seq_positions = prep.entity_seq_positions
    root_item = protenix_json[0] if isinstance(protenix_json, list) else protenix_json
    sequences = root_item.get("sequences") or []

    available_chains = sorted({
        str(c).upper()
        for eidx, chains in entity_chain_ids.items()
        if entity_kinds.get(eidx) == "protein"
        for c in chains
    })

    # 先全部校验并构建，后落盘——失败时不留半成品文件。
    release_dates_overlay: Dict[str, Dict[str, str]] = {}
    hits_by_seq_pos: Dict[int, List[str]] = {}
    for idx, payload in enumerate(template_payloads, start=1):
        cif_text = str(payload.get("mmcif") or payload.get("cif") or "")
        if not cif_text:
            raise ValueError(
                f"模板 #{idx} 内容为空：模板转换产物缺少 mmCIF 字段。")
        synthetic_id = f"t{idx:03d}"
        completed_cif = _complete_template_chem_comp_types(cif_text)
        chain_sequences = extract_chain_sequences_from_structure(completed_cif, "cif")
        template_chain = next(iter(chain_sequences), None)
        template_seq = chain_sequences.get(template_chain, "") if template_chain else ""
        if not template_seq:
            raise ValueError(
                f"模板 #{idx} ({synthetic_id}.cif) 未解析出蛋白链序列——"
                "模板必须是含蛋白链的结构。")
        target_chains = [
            str(c).strip().upper()
            for c in (payload.get("target_chain_ids") or [])
        ]
        linked_seq_positions = [
            entity_seq_positions[eidx]
            for eidx, chains in entity_chain_ids.items()
            if entity_kinds.get(eidx) == "protein"
            and entity_seq_positions.get(eidx) is not None
            and any(str(c).strip().upper() in target_chains for c in chains)
        ]
        if not linked_seq_positions:
            raise ValueError(
                f"模板 #{idx} 的 target_chain_ids {target_chains} "
                f"未匹配任何输入蛋白实体；可用蛋白链: {available_chains}。"
                "请核对 template_meta 的 target_chain_ids。")
        seq_len = len(template_seq)
        hit_block = (
            f">{synthetic_id}_{template_chain}/1-{seq_len} "
            f"protein length:{seq_len} mol:protein\n"
            f"{template_seq.upper()}\n"
        )
        release_dates_overlay[synthetic_id] = {
            "release_date": _PROTENIX_TEMPLATE_RELEASE_DATE
        }
        for seq_pos in linked_seq_positions:
            hits_by_seq_pos.setdefault(seq_pos, []).append(hit_block)

        with open(os.path.join(template_dir, f"{synthetic_id}.cif"),
                  "w", encoding="utf-8") as f:
            f.write(completed_cif)

    # 每个拿到命中的蛋白实体写聚合 a3m 并挂 templatesPath
    # （templatesPath 每条蛋白链只能指向一个文件，多模板合并多 hit）。
    for seq_pos, hit_blocks in hits_by_seq_pos.items():
        a3m_name = f"entity_{seq_pos:02d}_templates.a3m"
        with open(os.path.join(template_dir, a3m_name),
                  "w", encoding="ascii") as f:
            f.write("".join(hit_blocks))
        entry = sequences[seq_pos]
        for kind, body in entry.items():
            if "protein" in kind and isinstance(body, dict):
                body["templatesPath"] = f"{container_dir}/{a3m_name}"

    with open(os.path.join(protenix_input_dir, "template_release_dates.json"),
              "w", encoding="utf-8") as f:
        json.dump(release_dates_overlay, f, indent=1)
    return True


def validate_template_paths(yaml_content: str) -> None:
    yaml_data = yaml.safe_load(yaml_content) or {}
    template_entries = yaml_data.get("templates") or []
    if not isinstance(template_entries, list) or not template_entries:
        return

    missing: List[str] = []
    for entry in template_entries:
        if not isinstance(entry, dict):
            continue
        path_ref = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
        if not path_ref:
            continue
        path = Path(str(path_ref))
        if not path.exists():
            missing.append(str(path))

    if missing:
        missing_list = "\n".join(f"- {p}" for p in missing)
        raise FileNotFoundError(
            "模板文件不存在，已中止任务。请重新上传模板文件或移除 YAML 中的 templates 条目。\n"
            f"缺失文件列表:\n{missing_list}"
        )


def resolve_low_vram(predict_args: Optional[Dict[str, Any]]) -> bool:
    return coerce_bool((predict_args or {}).get("low_vram"))


from backend.worker.docker_cmd import (
    collect_gpu_device_group_ids,
    docker_args_has_flag,
    make_scoped_container_name,
    sanitize_docker_extra_args,
)


from backend.runtime.docker_exec import remove_container, run_docker_streaming


def _docker_warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)






def _structure_candidate_priority(name: str, base_priority: int, jobname: str) -> int:
    priority = base_priority
    suffix = Path(name).suffix.lower()
    if suffix == ".cif":
        priority -= 10
    elif suffix == ".pdb":
        priority -= 5

    lowered = name.lower()
    job_lower = jobname.lower()
    if job_lower and job_lower in lowered:
        priority -= 4
    if "ranked_0" in lowered:
        priority -= 2
    if "predicted" in lowered:
        priority -= 1
    if "model" in lowered:
        priority -= 1
    return priority


def locate_af3_structure_file(af3_output_dir: Path, jobname: str) -> Optional[Path]:
    """Locate the primary AlphaFold3 structure file (.cif or .pdb) for affinity post-processing."""
    base_dir = Path(af3_output_dir)
    if not base_dir.exists():
        return None

    candidates: List[Tuple[int, Path]] = []

    def register_candidate(path: Path, base_priority: int) -> None:
        if not path.is_file():
            return
        priority = _structure_candidate_priority(path.name, base_priority, jobname)
        candidates.append((priority, path))

    job_dir = base_dir / jobname
    search_roots: List[Tuple[int, Path]] = []
    if job_dir.exists():
        search_roots.append((0, job_dir))
    search_roots.append((10, base_dir))

    for base_priority, root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.cif"):
            register_candidate(path, base_priority)
        for path in root.rglob("*.pdb"):
            register_candidate(path, base_priority + 2)

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], len(str(item[1]))))
    return candidates[0][1]


def extract_af3_structure_from_archives(
    af3_output_dir: Path,
    scratch_dir: Path,
    jobname: str,
) -> Optional[Path]:
    archive_candidates: List[Tuple[int, Path, str, str]] = []

    job_dir = af3_output_dir / jobname
    archive_patterns = ["*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.xz", "*.tar.bz2"]

    for pattern in archive_patterns:
        for archive_path in af3_output_dir.rglob(pattern):
            base_priority = 60
            try:
                if job_dir.exists() and archive_path.is_relative_to(job_dir):  # type: ignore[attr-defined]
                    base_priority = 40
            except AttributeError:
                try:
                    archive_path.relative_to(job_dir)
                    base_priority = 40
                except ValueError:
                    base_priority = 60

            suffix = archive_path.suffix.lower()
            if archive_path.name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
                archive_type = "tar"
            elif suffix in {".tar"}:
                archive_type = "tar"
            else:
                archive_type = "zip"

            if archive_type == "zip":
                try:
                    with zipfile.ZipFile(archive_path) as zf:
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            entry_suffix = Path(info.filename).suffix.lower()
                            if entry_suffix not in {".cif", ".pdb"}:
                                continue
                            priority = _structure_candidate_priority(info.filename, base_priority + 10, jobname)
                            archive_candidates.append((priority, archive_path, info.filename, archive_type))
                except (zipfile.BadZipFile, OSError):
                    continue
            else:
                try:
                    with tarfile.open(archive_path, "r:*") as tf:
                        for member in tf.getmembers():
                            if not member.isreg():
                                continue
                            entry_suffix = Path(member.name).suffix.lower()
                            if entry_suffix not in {".cif", ".pdb"}:
                                continue
                            priority = _structure_candidate_priority(member.name, base_priority + 10, jobname)
                            archive_candidates.append((priority, archive_path, member.name, archive_type))
                except (tarfile.TarError, OSError):
                    continue

    if not archive_candidates:
        return None

    archive_candidates.sort(key=lambda item: (item[0], len(item[2])))
    _, selected_archive, selected_member, selected_type = archive_candidates[0]

    scratch_dir.mkdir(parents=True, exist_ok=True)
    member_path = Path(selected_member)
    stem = safe_filename(member_path.stem) or "structure"
    dest_name = stem + member_path.suffix.lower()
    dest_path = scratch_dir / dest_name

    counter = 1
    while dest_path.exists():
        dest_path = scratch_dir / f"{stem}_{counter}{member_path.suffix.lower()}"
        counter += 1

    try:
        if selected_type == "zip":
            with zipfile.ZipFile(selected_archive) as zf:
                with zf.open(selected_member) as source, open(dest_path, "wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(selected_archive, "r:*") as tf:
                member = tf.getmember(selected_member)
                extracted = tf.extractfile(member)
                if extracted is None:
                    return None
                with extracted, open(dest_path, "wb") as target:
                    shutil.copyfileobj(extracted, target)
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None

    print(
        f"从归档文件提取 AlphaFold3 结构: {selected_archive} -> {dest_path}",
        file=sys.stderr,
    )
    return dest_path


def run_af3_affinity_pipeline(
    temp_dir: str,
    yaml_data: Dict[str, Any],
    prep: AF3Preparation,
    af3_output_dir: str,
    results_root: Optional[Path] = None,
) -> List[Tuple[Path, str]]:
    """
    若 YAML 配置请求亲和力预测，则在 AlphaFold3 结果上运行 Boltz-2 亲和力流程。
    返回需要附加到归档中的额外文件列表 (Path, arcname)。
    """
    affinity_config = extract_affinity_config_from_yaml(yaml_data)
    if not affinity_config:
        return []

    binder_chain = affinity_config.get("binder")
    if not binder_chain:
        print("亲和力配置未提供有效的 binder，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = str(binder_chain).strip()
    if not binder_chain:
        print("亲和力配置 binder 为空，跳过亲和力预测。", file=sys.stderr)
        return []

    ligand_entries = [
        entry for entry in yaml_data.get("sequences", [])
        if isinstance(entry, dict) and "ligand" in entry
    ]
    if not ligand_entries:
        print("未检测到配体条目，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = prep.chain_id_label_map.get(binder_chain, safe_filename(binder_chain))

    af3_output_path = Path(af3_output_dir)
    model_path = locate_af3_structure_file(af3_output_path, prep.jobname)

    if not model_path or not model_path.exists():
        extracted_root = (results_root / "extracted_structures") if results_root else (Path(temp_dir) / "af3_extracted_structures")
        extracted_path = extract_af3_structure_from_archives(
            af3_output_path,
            extracted_root,
            prep.jobname,
        )
        model_path = extracted_path

    if not model_path or not model_path.exists():
        print(
            "[WARN] 未找到 AlphaFold3 预测的结构文件，无法进行亲和力预测。",
            file=sys.stderr,
        )
        return []

    print(
        f"使用 AlphaFold3 结构进行亲和力评估: {model_path}",
        file=sys.stderr,
    )

    ligand_resname = find_ligand_resname_in_cif(model_path, binder_chain)
    if not ligand_resname:
        print(
            f"[WARN] 未能在结构中找到链 {binder_chain} 的配体残基，跳过亲和力预测。",
            file=sys.stderr,
        )
        return []

    affinity_base = (results_root / "affinity") if results_root else (Path(temp_dir) / "af3_affinity")
    try:
        return _run_boltz2score_affinity_postprocess(
            affinity_base=affinity_base,
            model_path=model_path,
            requested_ligand_chain=binder_chain,
            ligand_resname=ligand_resname,
            source="alphafold3",
            archive_prefix="af3",
            ligand_smiles_list=_extract_ligand_smiles_list(yaml_data),
        )
    except Exception as err:
        print(f"[WARN] 运行 Boltz2Score 亲和力后处理失败: {err}", file=sys.stderr)
        return []


def _read_protenix_error_report(protenix_output_dir: Path, max_chars: int = 6000) -> str:
    base_dir = Path(protenix_output_dir)
    if not base_dir.exists():
        return ""
    error_files = sorted(base_dir.rglob("ERR/*.txt"), key=lambda item: (len(str(item)), str(item)))
    if not error_files:
        error_files = sorted(base_dir.rglob("*.err"), key=lambda item: (len(str(item)), str(item)))
    chunks: List[str] = []
    for path in error_files[:3]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not content:
            continue
        try:
            rel = path.relative_to(base_dir)
        except ValueError:
            rel = path
        chunks.append(f"{rel}:\n{content[-max_chars:]}")
    return "\n\n".join(chunks)[:max_chars]


def _raise_if_protenix_reported_error_without_structure(protenix_output_dir: Path, input_name: str) -> None:
    if locate_protenix_structure_file(protenix_output_dir, input_name) is not None:
        return
    report = _read_protenix_error_report(protenix_output_dir)
    if not report:
        return
    raise RuntimeError(f"Protenix finished without a renderable structure. Error report:\n{report}")


def locate_protenix_structure_file(protenix_output_dir: Path, input_name: str) -> Optional[Path]:
    """Locate the primary Protenix structure file (.cif or .pdb) for affinity post-processing."""
    base_dir = Path(protenix_output_dir)
    if not base_dir.exists():
        return None

    candidates: List[Tuple[int, Path]] = []

    def register_candidate(path: Path, base_priority: int) -> None:
        if not path.is_file():
            return
        try:
            rel_name = str(path.relative_to(base_dir))
        except ValueError:
            rel_name = path.name
        priority = _structure_candidate_priority(rel_name, base_priority, input_name)
        lowered = rel_name.lower()
        if f"{os.sep}msa{os.sep}" in lowered or lowered.startswith("msa/"):
            priority += 20
        candidates.append((priority, path))

    for path in base_dir.rglob("*.cif"):
        register_candidate(path, 0)
    for path in base_dir.rglob("*.pdb"):
        register_candidate(path, 2)

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], len(str(item[1]))))
    return candidates[0][1]


def run_protenix_affinity_pipeline(
    temp_dir: str,
    yaml_data: Dict[str, Any],
    prep: ProtenixPreparation,
    protenix_output_dir: str,
    results_root: Optional[Path] = None,
) -> List[Tuple[Path, str]]:
    """
    若 YAML 配置请求亲和力预测，则在 Protenix 结果上运行 Boltz-2 亲和力流程。
    返回需要附加到归档中的额外文件列表 (Path, arcname)。
    """
    affinity_config = extract_affinity_config_from_yaml(yaml_data)
    if not affinity_config:
        return []

    binder_chain_raw = affinity_config.get("binder")
    if not binder_chain_raw:
        print("亲和力配置未提供有效的 binder，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain_raw = str(binder_chain_raw).strip()
    if not binder_chain_raw:
        print("亲和力配置 binder 为空，跳过亲和力预测。", file=sys.stderr)
        return []

    ligand_entries = [
        entry for entry in yaml_data.get("sequences", [])
        if isinstance(entry, dict) and "ligand" in entry
    ]
    if not ligand_entries:
        print("未检测到配体条目，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = (
        prep.chain_alias_map.get(binder_chain_raw)
        or prep.chain_alias_map.get(binder_chain_raw.upper())
        or prep.chain_alias_map.get(binder_chain_raw.lower())
        or binder_chain_raw
    )

    model_path = locate_protenix_structure_file(Path(protenix_output_dir), prep.input_name)
    if not model_path or not model_path.exists():
        print("[WARN] 未找到 Protenix 预测的结构文件，无法进行亲和力预测。", file=sys.stderr)
        return []

    print(f"使用 Protenix 结构进行亲和力评估: {model_path}", file=sys.stderr)

    ligand_resname = find_ligand_resname_in_cif(model_path, binder_chain)
    if not ligand_resname:
        inferred = _find_ligand_chain_and_resname_in_structure(model_path)
        if inferred:
            inferred_chain, inferred_resname = inferred
            print(
                f"未在链 {binder_chain} 找到配体，自动回退到链 {inferred_chain} ({inferred_resname})。",
                file=sys.stderr,
            )
            binder_chain = inferred_chain
            ligand_resname = inferred_resname

    if not ligand_resname:
        print(
            f"[WARN] 未能在结构中找到链 {binder_chain} 的配体残基，跳过亲和力预测。",
            file=sys.stderr,
        )
        return []

    affinity_base = (results_root / "affinity") if results_root else (Path(temp_dir) / "protenix_affinity")
    try:
        return _run_boltz2score_affinity_postprocess(
            affinity_base=affinity_base,
            model_path=model_path,
            requested_ligand_chain=binder_chain,
            ligand_resname=ligand_resname,
            source="protenix",
            archive_prefix="protenix",
            ligand_smiles_list=_extract_ligand_smiles_list(yaml_data),
        )
    except Exception as err:
        print(f"[WARN] 运行 Boltz2Score 亲和力后处理失败: {err}", file=sys.stderr)
        return []






def _resolve_backend_results_root(backend: str, task_id: Optional[str], temp_dir: str) -> Path:
    from backend.runtime.backends.support import resolve_backend_results_root
    return resolve_backend_results_root(backend, task_id, temp_dir)


def _resolve_backend_work_root(results_root: Path) -> Path:
    from backend.runtime.backends.support import resolve_backend_work_root
    return resolve_backend_work_root(results_root)


def _extract_sequence_chain_types_from_yaml(yaml_data: Dict[str, Any]) -> Dict[str, str]:
    chain_types: Dict[str, str] = {}
    if not isinstance(yaml_data, dict):
        return chain_types
    for entity in yaml_data.get("sequences", []) or []:
        if not isinstance(entity, dict):
            continue
        entity_type = ""
        entity_payload: Dict[str, Any] = {}
        for key in ("protein", "ligand", "rna", "dna"):
            payload = entity.get(key)
            if isinstance(payload, dict):
                entity_type = key
                entity_payload = payload
                break
        if not entity_type:
            continue
        ids = entity_payload.get("id")
        if isinstance(ids, list):
            chain_ids = [str(item or "").strip() for item in ids]
        else:
            chain_ids = [str(ids or "").strip()]
        for chain_id in chain_ids:
            if not chain_id:
                continue
            existing_type = chain_types.get(chain_id)
            if existing_type and existing_type != entity_type:
                raise ValueError(
                    f"Duplicate chain id '{chain_id}' used by multiple sequence types: {existing_type}, {entity_type}."
                )
            if existing_type == entity_type:
                raise ValueError(
                    f"Duplicate chain id '{chain_id}' appears multiple times in sequences."
                )
            chain_types[chain_id] = entity_type
    return chain_types


def _normalize_protenix_output_permissions(
    temp_dir: str,
    image: str,
    paths: List[str],
) -> None:
    existing_paths = [path for path in paths if path and os.path.exists(path)]
    if not existing_paths:
        return

    host_uid = os.getuid()
    host_gid = os.getgid()
    target_paths = " ".join(shlex.quote(path) for path in existing_paths)
    fix_script = (
        f"chown -R {host_uid}:{host_gid} {target_paths} >/dev/null 2>&1 || true; "
        f"chmod -R u+rwX {target_paths} >/dev/null 2>&1 || true"
    )
    volume_dirs = sorted({
        str((Path(path).resolve().parent if Path(path).is_file() else Path(path).resolve()))
        for path in existing_paths
    })
    cmd = [
        "docker",
        "run",
        "--rm",
        "--user",
        "root",
    ]
    for volume_dir in volume_dirs:
        cmd.extend(["--volume", f"{volume_dir}:{volume_dir}"])
    cmd.extend([
        image,
        "sh",
        "-lc",
        fix_script,
    ])
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as perm_err:
        print(f"[WARN] 无法自动修复 Protenix 输出目录权限: {perm_err}", file=sys.stderr)


def _safe_runtime_token(raw: Any) -> str:
    from backend.runtime.backends.support import safe_runtime_token
    return safe_runtime_token(raw)












# Legacy shape derived from the unified linker registry (bicyclic_linkers.py)
BICYCLIC_LINKER_ATOM_MAP: Dict[str, List[str]] = bicyclic_linkers.atom_map()
















def build_alignment_indices(query_seq: str, template_seq: str) -> Tuple[List[int], List[int]]:
    if not query_seq or not template_seq:
        return [], []

    aligner = Align.PairwiseAligner()
    aligner.mode = "global"
    alignment = aligner.align(query_seq, template_seq)[0]
    query_indices: List[int] = []
    template_indices: List[int] = []

    for query_block, template_block in zip(alignment.aligned[0], alignment.aligned[1]):
        q_start, q_end = query_block
        t_start, t_end = template_block
        length = min(q_end - q_start, t_end - t_start)
        for offset in range(length):
            query_indices.append(int(q_start + offset))
            template_indices.append(int(t_start + offset))

    return query_indices, template_indices




def build_chain_sequence_map(yaml_data: dict) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in yaml_data.get("sequences", []):
        if not isinstance(item, dict):
            continue
        # Support both "protein" (Boltz format) and "proteinChain" (V-Bio format)
        protein = item.get("protein") or item.get("proteinChain")
        if protein is None or not isinstance(protein, dict):
            continue
        seq = protein.get("sequence", "")
        ids = protein.get("id") or protein.get("chain_id")
        if isinstance(ids, list):
            chain_ids = ids
        else:
            chain_ids = [ids] if ids is not None else []
        if not chain_ids:
            continue
        for chain_id in chain_ids:
            mapping[chain_id] = seq
    return mapping




def build_af3_model_seeds(seed: Optional[int], count: int = AF3_DEFAULT_MODEL_SEED_COUNT) -> Optional[List[int]]:
    if seed is None:
        return None
    try:
        base_seed = int(seed)
    except (TypeError, ValueError):
        return None
    if count <= 1:
        return [base_seed]
    return [base_seed + offset for offset in range(count)]




def _ensure_af3_required_fields(cif_text: str) -> str:
    """
    Ensure mmCIF contains all fields required by AlphaFold3 for template parsing.

    AF3 requires _atom_site.pdbx_PDB_model_num field which may be missing
    when converting from PDB or generating mmCIF with gemmi.
    """
    try:
        doc = gemmi.cif.read_string(cif_text)
        if len(doc) == 0:
            return cif_text
        block = doc.sole_block()

        # Check if _atom_site table exists
        atom_site_loop = block.find_loop("_atom_site")
        if not atom_site_loop:
            return cif_text

        tags = atom_site_loop.tags
        has_model_num = "_atom_site.pdbx_PDB_model_num" in tags

        if not has_model_num:
            lines = cif_text.splitlines()
            result_lines = []
            in_atom_site_loop = False
            atom_site_tags_found = False
            model_num_idx = -1

            for i, line in enumerate(lines):
                stripped = line.strip()

                if stripped.startswith("loop_"):
                    j = i + 1
                    atom_site_tags = []
                    while j < len(lines) and lines[j].strip().startswith("_"):
                        atom_site_tags.append(lines[j].strip())
                        j += 1

                    if atom_site_tags and any(t.startswith("_atom_site.") for t in atom_site_tags):
                        in_atom_site_loop = True
                        atom_site_tags_found = True

                        # pdbx_PDB_model_num belongs after group_PDB and before id.
                        insert_idx = -1
                        for idx, tag in enumerate(atom_site_tags):
                            if tag == "_atom_site.group_PDB":
                                insert_idx = idx + 1
                                break
                            elif tag == "_atom_site.id" and insert_idx == -1:
                                insert_idx = idx
                                break

                        result_lines.append(line)
                        for k, tag in enumerate(atom_site_tags):
                            if k == insert_idx:
                                result_lines.append("_atom_site.pdbx_PDB_model_num")
                                model_num_idx = insert_idx
                            result_lines.append(lines[i + 1 + k])

                        # If we didn't find a place to insert, add at the end
                        if insert_idx == -1:
                            result_lines.append("_atom_site.pdbx_PDB_model_num")
                            model_num_idx = len(atom_site_tags)

                        continue

                if in_atom_site_loop and atom_site_tags_found:
                    if not stripped.startswith("_") and stripped and not stripped.startswith("loop_") and not stripped.startswith("data_"):
                        parts = stripped.split()
                        if model_num_idx >= 0 and model_num_idx < len(parts) + 1:
                            parts.insert(model_num_idx, "1")
                            result_lines.append(" ".join(parts))
                        else:
                            result_lines.append(line)
                        continue
                    elif stripped.startswith("_") or stripped.startswith("loop_") or stripped.startswith("data_"):
                        in_atom_site_loop = False
                        atom_site_tags_found = False
                        model_num_idx = -1

                result_lines.append(line)

            return "\n".join(result_lines) + ("\n" if result_lines else "")

        return cif_text
    except Exception:
        return cif_text




def prepare_template_payloads(
    yaml_content: str,
    template_inputs: Optional[List[dict]],
    temp_dir: str,
) -> Tuple[str, List[dict]]:
    if not template_inputs:
        return yaml_content, []

    yaml_data = yaml.safe_load(yaml_content) or {}
    had_templates = bool(yaml_data.get("templates"))
    chain_seq_map = build_chain_sequence_map(yaml_data)
    boltz_templates = list(yaml_data.get("templates", []) or [])
    if boltz_templates:
        normalized = []
        for entry in boltz_templates:
            if not isinstance(entry, dict):
                continue
            path_ref = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
            if not path_ref:
                continue
            path = Path(str(path_ref))
            resolved: Optional[Path] = None
            if path.is_absolute():
                if path.exists():
                    resolved = path
            else:
                if path.exists():
                    resolved = path.resolve()
                else:
                    candidate = Path(temp_dir) / path
                    if candidate.exists():
                        resolved = candidate
            if not resolved:
                continue
            updated = dict(entry)
            if "pdb" in updated:
                updated["pdb"] = str(resolved)
            else:
                updated.pop("pdb", None)
                if "cif" in updated:
                    updated["cif"] = str(resolved)
                    updated.pop("mmcif", None)
                elif "mmcif" in updated:
                    updated["mmcif"] = str(resolved)
            normalized.append(updated)
        boltz_templates = normalized
    af3_templates: List[dict] = []

    templates_dir = Path(temp_dir) / "templates"
    for idx, template in enumerate(template_inputs):
        content_b64 = template.get("content_base64")
        if not content_b64:
            print("[WARN] 模板内容为空，跳过。", file=sys.stderr)
            continue
        try:
            raw_bytes = base64.b64decode(content_b64)
        except Exception:
            print("[WARN] 模板内容解码失败，跳过。", file=sys.stderr)
            continue
        text = raw_bytes.decode("utf-8", errors="replace")
        fmt = (template.get("format") or "pdb").lower()
        file_name = template.get("file_name") or template.get("filename") or f"template_{idx}.{fmt}"
        template_chain_id = template.get("template_chain_id")

        if fmt == "pdb":
            chain_sequences, first_chain = _extract_chain_sequences_from_pdb_text(text)
        else:
            chain_sequences = extract_chain_sequences_from_structure(text, fmt)
            first_chain = next(iter(chain_sequences.keys()), None)
        if not chain_sequences:
            print("[WARN] 模板未解析出蛋白质链，跳过。", file=sys.stderr)
            continue
        if template_chain_id not in chain_sequences:
            template_chain_id = first_chain or next(iter(chain_sequences.keys()))
        template_seq = chain_sequences.get(template_chain_id, "")

        templates_dir.mkdir(parents=True, exist_ok=True)
        raw_path = templates_dir / file_name
        try:
            raw_path.write_bytes(raw_bytes)
        except Exception as exc:
            print(f"[WARN] 保存模板文件失败 {raw_path}: {exc}", file=sys.stderr)
            continue
        # The single-chain conversion writes into cif_path, which for cif
        # uploads equals raw_path and would clobber it; keep a copy with the
        # original author numbering for pocket-residue translation.
        author_path = templates_dir / f"{Path(file_name).stem}_author{Path(file_name).suffix or '.cif'}"
        try:
            author_path.write_bytes(raw_bytes)
        except Exception as exc:
            print(f"[WARN] 保存原始模板失败 {author_path}: {exc}", file=sys.stderr)

        if fmt == "pdb":
            filtered_path = templates_dir / f"{Path(file_name).stem}_chain{template_chain_id}.pdb"
            try:
                _write_filtered_pdb_by_chain(text, str(template_chain_id or ""), filtered_path)
                raw_path = filtered_path
            except Exception as exc:
                print(f"[WARN] 过滤 PDB 模板失败 {raw_path}: {exc}", file=sys.stderr)

        cif_stem = Path(file_name).stem or f"template_{idx}"
        cif_path = templates_dir / f"{cif_stem}.cif"
        try:
            cif_path, cif_text, resolved_chain_id, cif_template_seq = convert_structure_to_single_chain_mmcif(
                raw_path, str(template_chain_id or ""), cif_path
            )
        except Exception as exc:
            print(f"[WARN] 模板转换失败，已跳过 {file_name}: {exc}", file=sys.stderr)
            continue
        if cif_template_seq:
            template_seq = cif_template_seq
            template_chain_id = resolved_chain_id

        target_chain_ids = template.get("target_chain_ids") or []
        if target_chain_ids and template_seq:
            for item in yaml_data.get("sequences", []):
                if "protein" not in item:
                    continue
                protein = item.get("protein", {})
                ids = protein.get("id")
                if isinstance(ids, list):
                    ids_list = ids
                else:
                    ids_list = [ids] if ids is not None else []
                if not set(ids_list).intersection(target_chain_ids):
                    continue
                if not protein.get("sequence"):
                    protein["sequence"] = template_seq
            chain_seq_map = build_chain_sequence_map(yaml_data)
        if target_chain_ids:
            query_seq = chain_seq_map.get(target_chain_ids[0], "")
        else:
            query_seq = ""

        query_indices, template_indices = build_alignment_indices(query_seq, template_seq)

        # Boltz template entry
        boltz_entry: Dict[str, Any] = {"cif": str(cif_path)}
        # pocket-residue translation needs the author numbering of the
        # original upload; the converted mmcif renumbers residues 1..N
        boltz_entry["author_pdb"] = str(author_path)
        if target_chain_ids:
            boltz_entry["chain_id"] = target_chain_ids if len(target_chain_ids) > 1 else target_chain_ids[0]
        boltz_templates.append(boltz_entry)

        # AF3 template payload
        if query_indices and template_indices:
            af3_source_text = cif_text
            af3_mmcif = _sanitize_release_date_text_with_gemmi(
                af3_source_text,
                release_date=None,
                include_loops=True,
            )
            af3_mmcif = _force_af3_release_date_text(af3_mmcif, None)
            af3_templates.append({
                "target_chain_ids": target_chain_ids,
                "mmcif": af3_mmcif,
                "queryIndices": query_indices,
                "templateIndices": template_indices,
            })

    if boltz_templates or had_templates:
        yaml_data["templates"] = boltz_templates
    elif "templates" in yaml_data:
        yaml_data.pop("templates", None)
    yaml_content = yaml.safe_dump(
        yaml_data,
        sort_keys=False,
        default_flow_style=False,
    )

    return yaml_content, af3_templates




def prepare_yaml_template_payloads(yaml_content: str, temp_dir: str) -> List[dict]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    template_entries = yaml_data.get("templates") or []
    if not isinstance(template_entries, list) or not template_entries:
        return []

    chain_seq_map = build_chain_sequence_map(yaml_data)
    if not chain_seq_map:
        return []

    af3_templates: List[dict] = []
    templates_dir = Path(temp_dir) / "templates_from_yaml"

    for idx, entry in enumerate(template_entries):
        if not isinstance(entry, dict):
            continue
        cif_ref = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
        if not cif_ref:
            continue
        cif_path = Path(str(cif_ref))
        if not cif_path.is_absolute():
            candidate = Path(temp_dir) / cif_path
            if candidate.exists():
                cif_path = candidate
        if not cif_path.exists():
            print(f"[WARN] 模板 CIF 文件不存在，跳过: {cif_path}", file=sys.stderr)
            continue
        suffix = cif_path.suffix.lower()
        fmt = "cif" if suffix in (".cif", ".mmcif") else "pdb"
        try:
            text = cif_path.read_text()
        except Exception as exc:
            print(f"[WARN] 读取模板文件失败 {cif_path}: {exc}", file=sys.stderr)
            continue

        template_chain_id = entry.get("template_id") or entry.get("template_chain_id")
        if isinstance(template_chain_id, (list, tuple)):
            template_chain_id = template_chain_id[0] if template_chain_id else None
        target_chain_ids = _normalize_chain_id_list(
            entry.get("chain_id") or entry.get("target_chain_ids") or entry.get("chain_ids")
        )
        if not target_chain_ids and chain_seq_map:
            target_chain_ids = [next(iter(chain_seq_map.keys()))]

        if fmt == "pdb":
            chain_sequences, first_chain = _extract_chain_sequences_from_pdb_text(text)
        else:
            chain_sequences = extract_chain_sequences_from_structure(text, fmt)
            first_chain = next(iter(chain_sequences.keys()), None)
        if not chain_sequences:
            continue
        if template_chain_id not in chain_sequences:
            template_chain_id = first_chain or next(iter(chain_sequences.keys()))
        template_seq = chain_sequences.get(template_chain_id, "")

        cif_text: Optional[str] = None
        cif_out = templates_dir / f"template_yaml_{idx}.cif"
        try:
            if fmt == "pdb":
                filtered_path = templates_dir / f"template_yaml_{idx}_chain{template_chain_id}.pdb"
                _write_filtered_pdb_by_chain(text, str(template_chain_id or ""), filtered_path)
                source_path = filtered_path
            else:
                source_path = cif_path
            _, cif_text, resolved_chain_id, cif_template_seq = convert_structure_to_single_chain_mmcif(
                source_path, str(template_chain_id or ""), cif_out
            )
            if cif_template_seq:
                template_seq = cif_template_seq
                template_chain_id = resolved_chain_id
        except Exception as exc:
            if fmt in ("cif", "mmcif") and text:
                print(f"[WARN] 转换模板失败，改用原始 mmCIF: {cif_path} ({exc})", file=sys.stderr)
                cif_text = text
            else:
                print(f"[WARN] 转换模板为单链 mmCIF 失败 {cif_path}: {exc}", file=sys.stderr)
                continue

        query_seq = chain_seq_map.get(target_chain_ids[0], "") if target_chain_ids else ""
        query_indices, template_indices = build_alignment_indices(query_seq, template_seq)
        if not query_indices or not template_indices:
            continue

        af3_mmcif = _sanitize_release_date_text_with_gemmi(
            cif_text or "",
            release_date=None,
            include_loops=True,
        )
        af3_mmcif = _force_af3_release_date_text(af3_mmcif, None)
        af3_templates.append({
            "target_chain_ids": target_chain_ids,
            "mmcif": af3_mmcif,
            "queryIndices": query_indices,
            "templateIndices": template_indices,
        })

    return af3_templates


_PROTENIX_INPUT_CONTAINER_DIR = "/workspace/protenix_input"
_PROTENIX_TEMPLATE_CONTAINER_DIR = f"{_PROTENIX_INPUT_CONTAINER_DIR}/template_mmcif"
# 用户模板缺少 PDB 注册元数据（release_dates、chem_comp.type），由后端按
# 任务补齐：合成 ID 形如 t001（4 字符字母数字，满足 hit 名约束），日期取
# 泄漏截止期（2021-09-30）之前的固定值。
_PROTENIX_TEMPLATE_RELEASE_DATE = "2019-01-01"
# 标准氨基酸的 PDB 注册 _chem_comp.type：Protenix 解析模板时以该字段判定
# 蛋白链（须含 "peptide"）。
_PROTENIX_STD_AA_CHEM_COMP_TYPES = {
    **{aa: "L-peptide linking" for aa in
       ("ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "HIS", "ILE",
        "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL")},
    "GLY": "peptide linking",
    "MSE": "L-peptide derivative",
}




def validate_af3_database_files(database_dir: str) -> None:
    """
    Perform lightweight sanity checks on key AF3 database FASTA files to fail fast
    when files are missing or corrupted (common cause of jackhmmer 'Parse failed').
    """
    required_files = [
        "uniref90_2022_05.fa",
        "uniprot_all_2021_04.fa",
        "mgy_clusters_2022_05.fa",
        "bfd-first_non_consensus_sequences.fasta",
    ]

    # Allow common amino-acid symbols plus gap/stop placeholders; digits are not valid.
    allowed_seq_pattern = re.compile(r"^[A-Za-z\-\.*?]+$")
    max_lines_scan = AF3_VALIDATE_MAX_LINES  # scan early part; set to 0 to scan full file

    for filename in required_files:
        path = Path(database_dir) / filename
        if not path.exists() or not path.is_file():
            raise RuntimeError(
                f"AlphaFold3 数据库缺少必需文件: {path}. 请重新下载/解压 AF3 数据库。"
            )
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
                if b"\x00" in head:
                    raise RuntimeError(
                        f"检测到文件包含非法空字节，可能已损坏: {path}. 请重新下载/解压该文件。"
                    )
                # 第一条非空行应为 FASTA 标题
                first_line = head.splitlines()[0] if head else b""
                if not first_line.startswith(b">"):
                    raise RuntimeError(
                        f"文件不是有效的 FASTA 格式（首行未以 '>' 开头）: {path}. "
                        "请重新下载/解压 AF3 数据库。"
                    )

            # Streaming scan of the early portion of the file to catch corruption quickly.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                header_seen = False
                for lineno, line in enumerate(f, start=1):
                    if max_lines_scan > 0 and lineno > max_lines_scan:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith(">"):  # header line
                        header_seen = True
                        continue
                    if not header_seen:
                        raise RuntimeError(
                            f"文件开头缺少 FASTA 标题: {path} (行 {lineno})。"
                        )
                    if not allowed_seq_pattern.match(stripped):
                        preview = stripped[:80]
                        raise RuntimeError(
                            f"检测到无效的 FASTA 序列字符 (行 {lineno}): '{preview}'. "
                            f"请重新下载/解压 {path.name}，当前文件可能已损坏。"
                        )
        except OSError as e:
            raise RuntimeError(f"无法读取 AF3 数据库文件 {path}: {e}") from e



def _detect_bicyclic_linker_codes(yaml_content: str) -> List[str]:
    data = yaml.safe_load(yaml_content) or {}
    found: List[str] = []
    for block in data.get("sequences", []) or []:
        ligand = block.get("ligand") if isinstance(block, dict) else None
        if isinstance(ligand, dict):
            code = str(ligand.get("ccd") or "").strip().upper()
            if code in BICYCLIC_LINKER_ATOM_MAP and code not in found:
                found.append(code)
    return found


def _linker_ccd_mmcif_bundle(codes: Iterable[str]) -> str:
    linker_dir = Path(__file__).resolve().parent / "linker_ccd"
    parts: List[str] = []
    for code in codes:
        path = linker_dir / f"{code}.cif"
        if not path.is_file():
            raise FileNotFoundError(f"Bicyclic linker CCD mmcif is missing: {path}")
        parts.append(path.read_text())
    return "\n".join(parts)
























PEPTIDE_NATURAL_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
PEPTIDE_PRESET_BASE_RESIDUE = {
    "AIB": "A", "NLE": "L", "NVA": "V", "ORN": "K", "CIT": "R",
    "HSE": "S", "HCY": "C", "MSE": "M", "SEC": "C", "HYP": "P",
    "PCA": "E", "SEP": "S", "TPO": "T", "PTR": "Y", "CSO": "C",
    "MLY": "K", "DAL": "A", "BALA": "A", "MANS": "S", "MANT": "T",
    "MANN": "N", "NAGS": "S", "NAGT": "T", "NAGN": "N", "GALS": "S",
    "GALT": "T", "FUCS": "S", "GLCS": "S", "XYLS": "S",
}

PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES = {
    "AIB": {"smiles": "NC(C)(C)C(=O)O", "label": "alpha-aminoisobutyric acid"},
    "NLE": {"smiles": "N[C@@H](CCCCC)C(=O)O", "label": "norleucine"},
    "NVA": {"smiles": "N[C@@H](CCC)C(=O)O", "label": "norvaline"},
    "ORN": {"smiles": "N[C@@H](CCCN)C(=O)O", "label": "ornithine"},
    "CIT": {"smiles": "N[C@@H](CCCNC(N)=O)C(=O)O", "label": "citrulline"},
    "HSE": {"smiles": "N[C@@H](CCO)C(=O)O", "label": "homoserine"},
    "HCY": {"smiles": "N[C@@H](CCS)C(=O)O", "label": "homocysteine"},
    "MSE": {"smiles": "N[C@@H](CC[Se]C)C(=O)O", "label": "selenomethionine"},
    "SEC": {"smiles": "N[C@@H](C[SeH])C(=O)O", "label": "selenocysteine"},
    "HYP": {"smiles": "O=C(O)[C@@H]1CC(O)CN1", "label": "hydroxyproline"},
    "PCA": {"smiles": "O=C(O)[C@@H]1CCC(=O)N1", "label": "pyroglutamic acid"},
    "SEP": {"smiles": "N[C@@H](COP(=O)(O)O)C(=O)O", "label": "phosphoserine"},
    "TPO": {"smiles": "N[C@@H]([C@H](C)OP(=O)(O)O)C(=O)O", "label": "phosphothreonine"},
    "PTR": {"smiles": "N[C@@H](Cc1ccc(OP(=O)(O)O)cc1)C(=O)O", "label": "phosphotyrosine"},
    "CSO": {"smiles": "N[C@@H](CSO)C(=O)O", "label": "S-hydroxycysteine"},
    "MLY": {"smiles": "N[C@@H](CCCCNC)C(=O)O", "label": "N6-methyllysine"},
    "DAL": {"smiles": "N[C@H](C)C(=O)O", "label": "D-alanine"},
    "BALA": {"smiles": "NCCC(=O)O", "label": "beta-alanine"},
    "MANS": {"smiles": "N[C@@H](CO[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O", "label": "O-Man-Ser"},
    "MANT": {"smiles": "N[C@@H]([C@H](C)O[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O", "label": "O-Man-Thr"},
    "MANN": {"smiles": "N[C@@H](CC(=O)N[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O", "label": "N-Man-Asn"},
    "NAGS": {"smiles": "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O", "label": "O-GlcNAc-Ser"},
    "NAGT": {"smiles": "N[C@@H]([C@H](C)O[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O", "label": "O-GlcNAc-Thr"},
    "NAGN": {"smiles": "N[C@@H](CC(=O)N[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O", "label": "N-GlcNAc-Asn"},
    "GALS": {"smiles": "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C(=O)O", "label": "O-Gal-Ser"},
    "GALT": {"smiles": "N[C@@H]([C@H](C)O[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C(=O)O", "label": "O-Gal-Thr"},
    "FUCS": {"smiles": "N[C@@H](CO[C@H]1O[C@@H](C)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O", "label": "O-Fuc-Ser"},
    "GLCS": {"smiles": "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C(=O)O", "label": "O-Glc-Ser"},
    "XYLS": {"smiles": "N[C@@H](CO[C@H]1O[C@@H](O)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O", "label": "O-Xyl-Ser"},
}

PEPTIDE_PRESET_PLACEMENT_RULES = {
    "PCA": "n_term",
}





def _collect_preset_modification_ccds_from_yaml(yaml_content: str) -> List[str]:
    try:
        data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    codes: List[str] = []
    seen: set[str] = set()
    for entry in data.get("sequences") or []:
        if not isinstance(entry, dict):
            continue
        protein = entry.get("protein")
        if not isinstance(protein, dict):
            continue
        for mod in protein.get("modifications") or []:
            if not isinstance(mod, dict):
                continue
            raw = mod.get("ccd") or mod.get("ptmType") or mod.get("modification")
            code = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or "")).upper()[:12]
            if not code or code in seen or code not in PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES:
                continue
            seen.add(code)
            codes.append(code)
    return codes


def _merge_referenced_preset_modification_molecules(
    custom_molecules: List[Dict[str, str]],
    yaml_content: str,
) -> List[Dict[str, str]]:
    return _merge_peptide_preset_molecules_by_code(
        custom_molecules,
        _collect_preset_modification_ccds_from_yaml(yaml_content),
    )


def _extract_user_ccd_one_letter_overrides(user_ccd_text: Optional[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    current_id = ""
    current_parent = ""
    current_one = ""

    def _clean_token(value: str) -> str:
        return str(value or "").strip().strip(chr(39) + chr(34)).upper()

    def _flush() -> None:
        nonlocal current_id, current_parent, current_one
        if current_id:
            one = _clean_token(current_one)
            parent = _clean_token(current_parent)
            resolved = one if len(one) == 1 and one != "?" else parent
            if len(resolved) == 1 and resolved in "ARNDCQEGHILKMFPSTWYV":
                overrides[current_id] = resolved
        current_id = ""
        current_parent = ""
        current_one = ""

    for raw_line in str(user_ccd_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("data_"):
            _flush()
            current_id = _clean_token(line[5:])
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0], parts[1]
        if key == "_chem_comp.id":
            current_id = _clean_token(value)
        elif key == "_chem_comp.one_letter_code":
            current_one = value
        elif key == "_chem_comp.mon_nstd_parent_comp_id":
            current_parent = value
    _flush()
    return overrides




def _validate_amidated_terminal_constraints(yaml_text: str, custom_molecules: List[Dict[str, Any]]) -> None:
    """C-terminal amidated custom residues carry no leaving atom on the backbone carbon, so they
    can only occupy the last position of a LINEAR protein chain. Reject them on cyclic chains or at
    non-terminal positions. The frontend locks this; this function is the backend guard."""
    amidated_codes = {
        str(m.get("ccd") or "").upper()
        for m in custom_molecules or []
        if isinstance(m, dict) and m.get("cTerminalAmidated")
    }
    if not amidated_codes:
        return
    data = yaml.safe_load(yaml_text) or {}
    for entry in data.get("sequences") or []:
        if not isinstance(entry, dict):
            continue
        protein = entry.get("protein")
        if not isinstance(protein, dict):
            continue
        sequence = re.sub(r"\s+", "", str(protein.get("sequence") or ""))
        seq_len = len(sequence)
        cyclic = bool(protein.get("cyclic") or False)
        for mod in protein.get("modifications") or []:
            if not isinstance(mod, dict):
                continue
            ccd = str(mod.get("ccd") or "").upper()
            if ccd not in amidated_codes:
                continue
            if cyclic:
                raise ValueError(
                    f"酰胺化残基 {ccd} 不能用于环肽（环肽没有 C 端）。请取消该残基的「C 端酰胺化」，或改用线性链。"
                )
            position = int(mod.get("position") or 0)
            if seq_len > 0 and position != seq_len:
                raise ValueError(
                    f"酰胺化残基 {ccd} 只能放在蛋白链的 C 端最后一位（第 {seq_len} 位），"
                    f"当前位于第 {position} 位。请将该修饰移至 C 端。"
                )












































def _clear_peptide_subtask_registry(parent_task_id: str) -> None:
    parent_token = str(parent_task_id or "").strip()
    if not parent_token:
        return
    client = _get_redis_client_optional()
    if client is None:
        return
    try:
        client.delete(_peptide_subtask_registry_key(parent_token))
    except Exception:
        pass














def _binder_msa_assignment(binder_sequence: str) -> str:
    """MSA for one designed binder, cache-first, HARD failure on miss.

    Policy ("MSA everywhere" + no-fallback): the env-database MSA
    service returns usable alignments for designed peptides too, and the MSA
    measurably improves results — a candidate without its MSA is not
    equivalent and must not silently run degraded. A transport/search failure
    raises; the generation prefetch propagates it and the design task fails
    loudly instead of burning GPU hours on no-MSA conformers.
    Returns the shared-cache a3m path.
    """
    sequence = str(binder_sequence or "").strip().upper()
    if not sequence:
        raise RuntimeError("binder MSA 预取收到空序列")
    sequence_hash = get_sequence_hash(sequence)
    # auto 分层与 input_prep.resolve_msa 一致: 短肽 uniref(秒级), 长链 env。
    # 分层缓存文件名同样对齐(msa_<h>_<tier>.a3m), 两端共享同一份缓存。
    normalized = "".join(
        aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
        for aa in sequence)
    binder_tier = "env" if len(normalized) >= 50 else "uniref"
    cache_dir = MSA_CACHE_CONFIG["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    cache_name = (f"msa_{sequence_hash}.a3m" if binder_tier == "env"
                  else f"msa_{sequence_hash}_{binder_tier}.a3m")
    cached_msa_path = os.path.join(cache_dir, cache_name)
    if MSA_CACHE_CONFIG["enable_cache"] and os.path.exists(cached_msa_path):
        sanitize_a3m_file(cached_msa_path, context="binder 缓存原文件")
        if _ensure_nonempty_a3m_file(cached_msa_path, sequence, context="binder 缓存校验", header="binder"):
            return cached_msa_path
    msa_timeout = MSA_SERVER_TIMEOUT_SECONDS if MSA_SERVER_TIMEOUT_SECONDS > 0 else 600
    # Measured boundary: the env-db CPU stages (result2msa) on a
    # short designed peptide can run tens of minutes — the GPU prefilter is
    # NOT the bottleneck. The auto tier (uniref for short binders) keeps the
    # per-generation prefetch at seconds; env stays available for long chains.
    binder_timeout = min(int(msa_timeout), 1800)
    msa_result = request_msa_from_server(sequence, timeout=binder_timeout, msa_mode=binder_tier)
    if not msa_result or not save_msa_result_to_file(msa_result, cached_msa_path):
        raise RuntimeError(
            f"binder MSA 搜索失败（{sequence[:12]}…, tier={binder_tier}）— "
            "基础设施故障不降级;请检查 MSA 服务健康状态")
    if not _ensure_nonempty_a3m_file(cached_msa_path, sequence, context="binder 下载写入", header="binder"):
        raise RuntimeError(
            f"binder MSA 写入校验失败（{sequence[:12]}…）— 硬失败,不降级")
    print(f"binder MSA 就绪: {os.path.basename(cached_msa_path)} (tier={binder_tier})", file=sys.stderr)
    return cached_msa_path
































class DpeptideStaleWorkerError(TransientError):
    """A refine dispatch flag was silently dropped by the executing worker.

    The worker task contract echoes the flags it honored into the dpeptide
    contract; a missing/mismatched echo means the celery process predates the
    flag (long-running worker, code updated on disk after its start) — the
    dropped ``blind_peptide`` flag causes a silent
    downgraded to a clashing staged-local refine and every candidate was
    chirality-rejected. Raised task-level (not per-candidate) so the run fails
    fast with the actionable fix: restart the GPU worker container.
    """


















# Interface engagement floor for sample selection: receptor-peptide heavy-atom
# pairs within 4.5 A. A docked 12-20-mer buries dozens of pairs; below this
# the sample never docked and must not ship regardless of clash-freedom.
ENGAGE_MIN_CONTACTS = 8
























POCKET_CONTACT_MAX_A = 5.0






















# The two CCD artifacts are rewritten (merged) per overlay; every other entry in
# the shared cache is static and must be linked, never duplicated. Keep the names
# in one place: the skip set in the materialization loop is load-bearing — an
# unlisted CCD artifact would be hardlinked over the freshly merged overlay copy.
_PROTENIX_CCD_CIF = "components.cif"
_PROTENIX_CCD_PKL = "components.cif.rdkit_mol.pkl"
_PROTENIX_MERGED_CCD_FILES = (_PROTENIX_CCD_CIF, _PROTENIX_CCD_PKL)


def _merge_custom_ccd_with_existing_cache(
    source_common_dir: Path,
    overlay_root: Path,
    custom_molecules: List[Dict[str, str]],
    extra_cif_text: str = "",
    extra_mols: Optional[Dict[str, Chem.Mol]] = None,
) -> Optional[Path]:
    extra_mols = extra_mols or {}
    if not custom_molecules and not extra_cif_text.strip() and not extra_mols:
        return None
    if not source_common_dir.is_dir():
        raise RuntimeError(f"Protenix common cache directory is missing: {source_common_dir}")

    source_components = source_common_dir / _PROTENIX_CCD_CIF
    source_rdkit = source_common_dir / _PROTENIX_CCD_PKL
    if not source_components.is_file():
        raise FileNotFoundError(f"Missing Protenix CCD components file: {source_components}")
    if not source_rdkit.is_file():
        raise FileNotFoundError(f"Missing Protenix CCD RDKit cache file: {source_rdkit}")

    overlay_common = overlay_root / "common"
    overlay_common.mkdir(parents=True, exist_ok=True)

    custom_cif_text, custom_mols = _build_custom_ccd_bundle(custom_molecules) if custom_molecules else ("", {})
    additions = [text for text in (custom_cif_text.strip(), extra_cif_text.strip()) if text]
    # Contract-check appended blocks before the overlay is written: a header-only loop or
    # undefined bond atom must fail here with the CCD named, never during GPU featurization.
    validate_ccd_additions(*additions)
    merged_cif_text = source_components.read_text(encoding="utf-8", errors="replace").rstrip()
    if additions:
        merged_cif_text = merged_cif_text + "\n" + "\n".join(additions) + "\n"
    (overlay_common / _PROTENIX_CCD_CIF).write_text(merged_cif_text, encoding="utf-8")

    merged_rdkit_mols: Dict[str, Chem.Mol] = {}
    try:
        with source_rdkit.open("rb") as handle:
            loaded = pickle.load(handle)
        if isinstance(loaded, dict):
            merged_rdkit_mols.update(loaded)
    except Exception as exc:
        raise RuntimeError(f"Failed to load Protenix CCD RDKit cache: {source_rdkit}") from exc

    merged_rdkit_mols.update(custom_mols)
    merged_rdkit_mols.update(extra_mols)
    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
    with (overlay_common / _PROTENIX_CCD_PKL).open("wb") as handle:
        pickle.dump(merged_rdkit_mols, handle)

    for source_item in source_common_dir.iterdir():
        if source_item.name in _PROTENIX_MERGED_CCD_FILES:
            continue
        target_item = overlay_common / source_item.name
        if target_item.is_symlink():
            target_item.unlink()
        try:
            if source_item.is_dir():
                if target_item.exists() and not target_item.is_dir():
                    target_item.unlink()
                try:
                    # hf_hub is a multi-GB static tree whose snapshot entries are symlinks
                    # into blobs/. A default copytree dereferences those symlinks and
                    # duplicates every blob into each task's overlay, so hardlink the
                    # files and keep the symlinks as symlinks instead.
                    shutil.copytree(
                        source_item,
                        target_item,
                        symlinks=True,
                        copy_function=os.link,
                        dirs_exist_ok=True,
                    )
                except OSError as link_err:
                    # A partial first pass leaves hardlinks behind, and copyfile's
                    # samefile guard then fails a plain re-copy on every linked
                    # entry; start the fallback from a clean target instead.
                    print(
                        f"[WARN] Protenix common cache hardlink failed for "
                        f"{source_item.name} ({link_err}); falling back to full copy",
                        file=sys.stderr,
                    )
                    shutil.rmtree(target_item, ignore_errors=True)
                    shutil.copytree(
                        source_item, target_item, symlinks=True, dirs_exist_ok=True
                    )
            elif source_item.is_file():
                if target_item.exists():
                    if not target_item.is_file():
                        raise RuntimeError(f"Overlay cache target is not a file: {target_item}")
                    target_item.unlink()
                try:
                    os.link(source_item, target_item)
                except OSError:
                    shutil.copy2(source_item, target_item)
            else:
                continue
        except Exception as exc:
            raise RuntimeError(
                f"Failed to materialize Protenix common cache entry in overlay: "
                f"{source_item} -> {target_item}"
            ) from exc

    return overlay_root

def _prepare_task_boltz_custom_mols_dir(
    temp_dir: str,
    molecules: List[Dict[str, str]],
    source_mols_dir: Path,
    container_base_mols_dir: str,
) -> Optional[Path]:
    custom_molecules = _normalize_custom_ccd_molecules(molecules)
    if not custom_molecules:
        return None
    if not source_mols_dir.is_dir():
        raise RuntimeError(f"Boltz2 molecule cache directory is missing: {source_mols_dir}")

    task_mols = Path(temp_dir) / "boltz_custom_mols"
    task_mols.mkdir(parents=True, exist_ok=True)
    for source in source_mols_dir.glob("*.pkl"):
        link_path = task_mols / source.name
        if link_path.exists() or link_path.is_symlink():
            continue
        link_path.symlink_to(f"{container_base_mols_dir.rstrip('/')}/{source.name}")

    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
    for item in custom_molecules:
        amidated = bool(item.get("cTerminalAmidated") or False)
        custom_mol = _build_custom_ccd_mol(item["smiles"], kind=item.get("kind") or "residue", backbone=item.get("backbone"), amidated=amidated)
        aliases = _boltz_custom_ccd_aliases(item["ccd"])
        for alias in aliases:
            mol_path = task_mols / f"{alias}.pkl"
            if mol_path.exists() or mol_path.is_symlink():
                mol_path.unlink()
            with mol_path.open("wb") as handle:
                pickle.dump(custom_mol, handle)
        alias_note = f" aliases={','.join(aliases)}" if len(aliases) > 1 else ""
        print(
            f"Registered custom residue CCD {item['ccd']} from drawn SMILES{alias_note}"
            + (f" at base residue {item['base_residue']}" if item.get("base_residue") else ""),
            file=sys.stderr,
        )
    return task_mols


def _normalize_peptide_gpu_ids(raw_gpu_ids: Any) -> List[int]:
    if isinstance(raw_gpu_ids, int):
        return [raw_gpu_ids]
    if not isinstance(raw_gpu_ids, (list, tuple)):
        return []
    normalized: List[int] = []
    seen = set()
    for item in raw_gpu_ids:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return normalized



def main():
    """
    Main function to run a single prediction based on arguments provided in a JSON file.
    The JSON file should contain the necessary parameters for the prediction, including:
    - output_archive_path: Path where the output archive will be saved.
    - yaml_content: YAML content as a string that will be written to a temporary file.
    - Other parameters that will be passed to the predict function as command-line arguments.
    """
    if len(sys.argv) != 2:
        print("Usage: python -m backend.runtime.run_single_prediction <args_file_path>")
        sys.exit(1)

    args_file_path = sys.argv[1]

    try:
        with open(args_file_path, 'r') as f:
            predict_args = json.load(f)

        if bool(predict_args.pop("__peptide_candidate_worker__", False)):
            worker_temp_dir = str(predict_args.pop("temp_dir"))
            worker_yaml_content = str(predict_args.pop("yaml_content"))
            worker_output_archive_path = str(predict_args.pop("output_archive_path"))
            worker_predict_args = predict_args.pop("predict_args", {})
            worker_model_name = predict_args.pop("model_name", None)
            worker_backend = _normalize_peptide_backend(predict_args.pop("backend", "boltz"))
            worker_acquire_gpu = bool(predict_args.pop("__peptide_worker_acquire_gpu__", True))
            worker_low_vram = resolve_low_vram(worker_predict_args)
            worker_task_id = str(predict_args.pop("__peptide_worker_task_id__", "")).strip() or "peptide-candidate-worker"
            if not isinstance(worker_predict_args, dict):
                worker_predict_args = {}
            os.makedirs(worker_temp_dir, exist_ok=True)
            worker_gpu_id = -1
            worker_release_gpu = None
            try:
                if worker_acquire_gpu:
                    from gpu_manager import (
                        acquire_gpu_for_peptide_worker as worker_acquire_gpu_fn,
                        release_gpu as worker_release_gpu_fn,
                    )
                    worker_release_gpu = worker_release_gpu_fn
                    def _raise_if_cancelled() -> None:
                        try:
                            client = _get_redis_client_optional()
                            if client is None:
                                return
                            own = os.environ.get("VBIO_PEPTIDE_WORKER_TASK_ID", "")
                            parent = os.environ.get("VBIO_PARENT_TASK_ID", "")
                            for marker_task_id in (own, parent):
                                if marker_task_id and client.exists(f"task_cancelled:{marker_task_id}"):
                                    raise RuntimeError(
                                        f"任务已取消 (task_cancelled:{marker_task_id[:8]}), 中止 GPU 等待"
                                    )
                        except RuntimeError:
                            raise
                        except Exception:
                            return  # 取消检查 best-effort:redis 故障不打断等卡

                    worker_gpu_id = worker_acquire_gpu_fn(
                        task_id=worker_task_id,
                        timeout=PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS,
                        cancel_check=_raise_if_cancelled,
                    )
                    os.environ["CUDA_VISIBLE_DEVICES"] = str(worker_gpu_id)

                worker_seed = worker_predict_args.pop("seed", None)
                worker_custom_ccd_molecules = worker_predict_args.pop("custom_ccd_molecules", [])
                worker_ipsae_ligand_chain = str(
                    worker_predict_args.pop("ipsaeLigandChainId", None)
                    or worker_predict_args.pop("ipsae_ligand_chain_id", None)
                    or ""
                ).strip() or None
                if worker_seed is not None:
                    worker_predict_args["seed"] = worker_seed
                worker_options = {
                    **worker_predict_args,
                    "predict_args": worker_predict_args,
                    "low_vram": worker_low_vram,
                    "custom_ccd_molecules": (
                        worker_custom_ccd_molecules
                        if isinstance(worker_custom_ccd_molecules, list) else []),
                    "ipsae_ligand_chain_id": worker_ipsae_ligand_chain,
                    "model_name": worker_model_name,
                    "pocket_sequence_contacts": predict_args.pop(
                        "mol1_pocket_sequence_contacts", None),
                }
                structure_backend(worker_backend).execute(
                    PredictionRequest.from_yaml(worker_yaml_content),
                    BackendRunContext(
                        temp_dir=worker_temp_dir,
                        yaml_content=worker_yaml_content,
                        output_archive_path=worker_output_archive_path,
                        seed=worker_seed or 101,
                        task_id=worker_task_id,
                        options=worker_options,
                    ),
                )
            finally:
                if worker_gpu_id != -1 and callable(worker_release_gpu):
                    worker_release_gpu(gpu_id=worker_gpu_id, task_id=worker_task_id)
            if not os.path.exists(worker_output_archive_path):
                raise FileNotFoundError(
                    f"Peptide candidate worker did not produce archive: {worker_output_archive_path}"
                )
            return

        output_archive_path = predict_args.pop("output_archive_path")
        runtime_task_id = str(predict_args.pop("task_id", "")).strip() or None
        yaml_content = predict_args.pop("yaml_content")
        backend = str(predict_args.pop("backend", "boltz")).strip().lower()
        if backend in {"nesso1", "nesso-1"}:
            backend = "nesso"
        if (str(predict_args.get("workflow") or "").strip().lower()
                in {"peptide_design", "peptide", "peptide_designer", "designer"}
                and backend == "protenix"
                and str((predict_args.get("peptide_design_options") or {}).get(
                    "peptideChirality",
                    (predict_args.get("peptide_design_options") or {}).get(
                        "peptide_chirality", "l"))).strip().lower() == "d"):
            raise ValueError(
                "D-peptide design runs the mirror/refine workflow and requires "
                "the protenix2dock backend; use protenix only for L designs.")
        if backend == "protenix2dock":
            backend = "protenix"
        elif backend == "boltz2dock":
            backend = "boltz"
        if backend not in ("boltz", "alphafold3", "protenix", "nesso", "mol1",
                           "rfd4", "esmfold2"):
            raise ValueError(f"Unsupported backend '{backend}'.")
        low_vram = resolve_low_vram(predict_args)
        workflow = str(predict_args.pop("workflow", "prediction")).strip().lower()
        if workflow in {"peptide", "peptide_designer", "designer"}:
            workflow = "peptide_design"
        elif workflow in {"virtual screening", "virtual-screening", "screening", "vs"}:
            workflow = "virtual_screening"
        if workflow not in {"prediction", "peptide_design", "virtual_screening"}:
            raise ValueError(f"Unsupported workflow '{workflow}'.")
        if backend == "nesso" and workflow != "virtual_screening":
            raise ValueError(
                "Nesso is an independent virtual-screening backend; use workflow=virtual_screening."
            )
        if workflow == "virtual_screening" and backend != "nesso":
            raise ValueError("The virtual_screening workflow requires backend=nesso.")
        peptide_design_options = predict_args.pop("peptide_design_options", {})
        if not isinstance(peptide_design_options, dict):
            peptide_design_options = {}
        peptide_design_target_chain = str(predict_args.pop("peptide_design_target_chain", "")).strip() or None
        peptide_progress_path = str(predict_args.pop("peptide_progress_path", "")).strip() or None
        peptide_gpu_ids = _normalize_peptide_gpu_ids(predict_args.pop("peptide_gpu_ids", []))
        peptide_subtask_queue = str(predict_args.pop("peptide_subtask_queue", "")).strip() or None
        predict_args.pop("peptide_parallel_gpus", None)
        predict_args.pop("peptideParallelGpus", None)

        model_name = predict_args.pop("model_name", None)
        seed = predict_args.pop("seed", None)
        template_inputs = predict_args.pop("template_inputs", None)
        custom_ccd_molecules = predict_args.pop("custom_ccd_molecules", [])
        strict_ligand_confidence_contract = _read_bool_option(
            predict_args,
            "strict_ligand_confidence_contract",
            False,
        )
        predict_args.pop("strict_ligand_confidence_contract", None)

        use_msa_raw = predict_args.get("use_msa_server", True)
        if isinstance(use_msa_raw, bool):
            use_msa_server = use_msa_raw
        elif isinstance(use_msa_raw, (int, float)):
            use_msa_server = bool(use_msa_raw)
        else:
            use_msa_server = str(use_msa_raw).strip().lower() in {"1", "true", "yes", "y"}
        if backend in {"boltz", "alphafold3", "protenix", "mol1"}:
            use_msa_server = infer_use_msa_server_from_yaml_text(yaml_content)
            predict_args["use_msa_server"] = use_msa_server
            if use_msa_server:
                _assert_msa_server_configured(backend)

        runtime_temp_parent = str(Path(output_archive_path).resolve().parent)
        os.makedirs(runtime_temp_parent, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=runtime_temp_parent) as temp_dir:
            processed_yaml = yaml_content
            af3_template_payloads: List[dict] = []
            if template_inputs and (backend in ("boltz", "alphafold3", "protenix") or workflow == "peptide_design"):
                template_backend = "boltz" if workflow == "peptide_design" else backend
                template_work_root = _resolve_backend_work_root(
                    _resolve_backend_results_root(template_backend, runtime_task_id, temp_dir)
                )
                processed_yaml, af3_template_payloads = prepare_template_payloads(
                    yaml_content,
                    template_inputs,
                    str(template_work_root),
                )
            if workflow == "peptide_design":
                peptide_design_mode = _normalize_peptide_design_mode(
                    peptide_design_options.get("peptideDesignMode") or peptide_design_options.get("peptide_design_mode")
                )
                validate_template_paths(processed_yaml)
                peptide_parent_task_id = str(runtime_task_id or os.environ.get("VBIO_TASK_ID") or "").strip()
                proposer_backend_sel = str(
                    peptide_design_options.get("peptideProposerBackend")
                    or peptide_design_options.get("peptide_proposer_backend") or "esm3").strip().lower()
                if proposer_backend_sel == "rfd4":
                    chirality_raw = str(
                        peptide_design_options.get("peptideChirality")
                        or peptide_design_options.get("peptide_chirality") or "l").strip().lower()
                    from backend.core.engine_capabilities import engine_supports_chirality
                    if not engine_supports_chirality("rfd4", chirality_raw):
                        raise ValueError(
                            f"RFD4-Proteina 不支持 {chirality_raw.upper()} 肽设计；"
                            "D 肽请使用 ESM3 + RL 策略。")
                    # 一次性生成后自动派发验证子任务，不进入世代循环
                    score_backend_sel = str(
                        peptide_design_options.get("peptideScorerBackend")
                        or peptide_design_options.get("peptide_scorer_backend") or "protenix").strip().lower()
                    _assert_bond_capable_scorer(peptide_design_mode, score_backend_sel)
                    run_rfd4_design_strategy(
                        temp_dir=temp_dir,
                        yaml_content=processed_yaml,
                        output_archive_path=output_archive_path,
                        score_backend=score_backend_sel,
                        project_id=str(predict_args.get("project_id") or ""),
                        predict_args=predict_args,
                        seed=seed,
                        target_chain_id=peptide_design_target_chain,
                        template_inputs=template_inputs if isinstance(template_inputs, list) else None,
                        options=peptide_design_options,
                    )
                else:
                    try:
                        _assert_bond_capable_scorer(peptide_design_mode, backend)
                        run_peptide_design_backend(
                            temp_dir=temp_dir,
                            yaml_content=processed_yaml,
                            output_archive_path=output_archive_path,
                            backend=backend,
                            predict_args=predict_args,
                            model_name=model_name,
                            seed=seed,
                            options=peptide_design_options,
                            target_chain_id=peptide_design_target_chain,
                            progress_path=peptide_progress_path,
                            gpu_ids=peptide_gpu_ids,
                            subtask_queue=peptide_subtask_queue,
                            custom_ccd_molecules=custom_ccd_molecules if isinstance(custom_ccd_molecules, list) else [],
                            template_inputs=template_inputs if isinstance(template_inputs, list) else None,
                        )
                    finally:
                        if peptide_parent_task_id:
                            _clear_peptide_subtask_registry(peptide_parent_task_id)
            elif backend in ("alphafold3", "protenix"):
                if not af3_template_payloads:
                    af3_template_payloads = prepare_yaml_template_payloads(
                        processed_yaml,
                        str(_resolve_backend_work_root(_resolve_backend_results_root(backend, runtime_task_id, temp_dir))),
                    )
                structure_backend(backend).execute(
                    PredictionRequest.from_yaml(processed_yaml),
                    BackendRunContext(
                        temp_dir=temp_dir,
                        yaml_content=processed_yaml,
                        output_archive_path=output_archive_path,
                        seed=seed or 101,
                        task_id=runtime_task_id,
                        options={
                            "low_vram": low_vram,
                            "custom_ccd_molecules": (
                                custom_ccd_molecules
                                if isinstance(custom_ccd_molecules, list) else []),
                            "template_payloads": af3_template_payloads,
                            "msa_mode": str(predict_args.get("msa_mode") or "auto"),
                        },
                    ),
                )
            elif backend == "nesso":
                if template_inputs:
                    raise ValueError("Nesso does not support template files.")
                if custom_ccd_molecules:
                    raise ValueError("Nesso does not support custom CCD residue uploads.")
                run_nesso_backend(
                    temp_dir=temp_dir,
                    yaml_content=processed_yaml,
                    output_archive_path=output_archive_path,
                    seed=seed,
                    task_id=runtime_task_id,
                    low_vram=low_vram,
                )
            elif backend == "mol1":
                structure_backend("mol1").execute(
                    PredictionRequest.from_yaml(processed_yaml),
                    BackendRunContext(
                        temp_dir=temp_dir,
                        yaml_content=processed_yaml,
                        output_archive_path=output_archive_path,
                        seed=seed or 101,
                        task_id=runtime_task_id,
                        options={
                            "use_msa_server": use_msa_server,
                            "custom_ccd_molecules": custom_ccd_molecules
                            if isinstance(custom_ccd_molecules, list) else [],
                        },
                    ),
                )
            elif backend == "esmfold2":
                if template_inputs:
                    raise ValueError("ESMFold2 does not support template files.")
                structure_backend("esmfold2").execute(
                    PredictionRequest.from_yaml(processed_yaml),
                    BackendRunContext(
                        temp_dir=temp_dir,
                        yaml_content=processed_yaml,
                        output_archive_path=output_archive_path,
                        seed=seed or 101,
                        task_id=runtime_task_id,
                        options={
                            "custom_ccd_molecules": custom_ccd_molecules
                            if isinstance(custom_ccd_molecules, list) else [],
                        },
                    ),
                )
            elif backend == "rfd4":
                run_rfd4_backend(
                    temp_dir=temp_dir,
                    yaml_content=processed_yaml,
                    output_archive_path=output_archive_path,
                    template_inputs=template_inputs if isinstance(template_inputs, list) else None,
                    rfd4_spec=predict_args.get("rfd4_spec"),
                    seed=seed,
                    task_id=runtime_task_id,
                )
            else:
                if seed is not None:
                    predict_args["seed"] = seed
                validate_template_paths(processed_yaml)
                structure_backend("boltz").execute(
                    PredictionRequest.from_yaml(processed_yaml),
                    BackendRunContext(
                        temp_dir=temp_dir,
                        yaml_content=processed_yaml,
                        output_archive_path=output_archive_path,
                        seed=seed or 101,
                        task_id=runtime_task_id,
                        options={
                            "predict_args": predict_args,
                            "model_name": model_name,
                            "strict_ligand_confidence_contract": strict_ligand_confidence_contract,
                            "low_vram": low_vram,
                            "custom_ccd_molecules": (
                                custom_ccd_molecules
                                if isinstance(custom_ccd_molecules, list) else []),
                        },
                    ),
                )

            if not os.path.exists(output_archive_path):
                raise FileNotFoundError(
                    f"CRITICAL ERROR: Archive not found at {output_archive_path} immediately after creation."
                )

    except Exception as e:
        print(f"Error during prediction subprocess: {e}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()


def __getattr__(name: str):
    """迁移转发：肽设计簇符号实际定义于 peptide_design_backend。"""
    from backend.runtime import peptide_design_backend as _pdb
    if name not in _pdb.__dict__:
        raise AttributeError(name)
    value = getattr(_pdb, name)
    globals()[name] = value
    return value
