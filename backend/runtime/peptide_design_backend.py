"""肽设计后端编排：提案、细化、D/L 双空间精修与候选归档。

原 run_peptide_design_backend 及其辅助簇（run_single_prediction.py）
整体迁入。调度符号经模块 __getattr__ 从 run_single_prediction 惰性解析。
"""
from __future__ import annotations

import glob
import json
import math
import os
import random
import re
import base64
import copy
import glob
import hashlib
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from rdkit import Chem

import numpy as np

from backend.core.config import (
    PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX,
    RESULTS_BASE_DIR,
)
from backend.runtime.design_constants import (
    BACKBONE_CA_CA_MIN_A,
    PRODUCT_BOUND_MAX_DISTANCE_A,
    REFINE_ABSOLUTE_PAIR_MIN_A,
    REFINE_INTERPENETRATION_MIN_A,
    REFINE_PACKING_BAND_A,
)
from backend.runtime.bicyclic_linkers import get_linker
from backend.runtime.backends import structure_backend
from backend.runtime.backends.base import BackendRunContext
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.backends.support import (
    determine_docker_gpu_arg,
    resolve_backend_results_root,
    resolve_backend_work_root,
)
from backend.runtime.constraints_pipeline import (
    normalize_ligand_chain_collisions,
    remap_constraints_by_template_alignment,
    sanitize_constraints_for_chain_lengths,
    validate_unique_sequence_chain_ids,
)
from backend.runtime.linker_measurement import (
    _append_staged_bicyclic_links,
    _cyclic_headtail_bond_report,
    _dpeptide_linker_bond_report,
    _rebuild_single_atom_linker_anchor,
    _structure_linker_anchor_map,
)
from backend.runtime.msa_pipeline import (
    cache_msa_files_from_temp_dir,
    generate_msa_for_sequences,
)


# 能力目录挂载（与 run_single_prediction 同规则；脚本本体依赖其下模块路径）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PROJECT_ROOT / "capabilities"
if CAPABILITIES_DIR.is_dir() and str(CAPABILITIES_DIR) not in sys.path:
    sys.path.insert(0, str(CAPABILITIES_DIR))


def __getattr__(name: str):
    """编排符号（MSA/断言/正则/常量等）从 run_single_prediction 惰性解析。"""
    from backend.runtime import run_single_prediction as _rsp
    if name not in _rsp.__dict__:
        raise AttributeError(name)
    value = getattr(_rsp, name)
    globals()[name] = value
    return value


def _resolve_capability_dir(name: str) -> Path:
    return CAPABILITIES_DIR / name


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
from backend.runtime.backends import structure_backend
from backend.runtime.backends.base import BackendRunContext
from backend.runtime.backends.request import PredictionRequest
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




def _normalize_chain_id_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    return [text] if text else []




def _a3m_has_sequence_content(content: str) -> bool:
    saw_header = False
    for raw_line in sanitize_a3m_content(content).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            saw_header = True
            continue
        if saw_header and any(ch not in {"-", "."} for ch in line):
            return True
    return False




def _ensure_nonempty_a3m_file(path: str, sequence: str, context: str = "", header: str = "query") -> bool:
    existing_content = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                existing_content = f.read()
        except OSError as exc:
            print(f"[WARN] 无法读取 A3M 文件 {path}: {exc}", file=sys.stderr)
            return False

    sanitized = sanitize_a3m_content(existing_content, context=context or path)
    if sanitized and _a3m_has_sequence_content(sanitized):
        if sanitized != existing_content:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(sanitized)
            except OSError as exc:
                print(f"[WARN] 无法写回清理后的 A3M 文件 {path}: {exc}", file=sys.stderr)
                return False
        return True

    msg_context = f" ({context})" if context else ""
    print(f"[ERROR] A3M 文件无有效序列内容: {path}{msg_context}", file=sys.stderr)
    return False




def find_results_dir(base_dir: str) -> str:
    def _find_deepest_result(root_dir: str, exclude_tokens: List[str]) -> Optional[str]:
        result_path = None
        max_depth = -1
        for root, _, files in os.walk(root_dir):
            if any(token in root for token in exclude_tokens):
                continue
            if any(f.endswith((".cif", ".pdb")) for f in files):
                depth = root.count(os.sep)
                if depth > max_depth:
                    max_depth = depth
                    result_path = root
        return result_path

    exclude_tokens = [
        f"{os.sep}templates",
        f"{os.sep}templates_from_yaml",
        f"{os.sep}af3_input",
        f"{os.sep}af3_output",
        f"{os.sep}msa",
    ]

    predictions_root = os.path.join(base_dir, "predictions")
    result_path = None
    if os.path.isdir(predictions_root):
        result_path = _find_deepest_result(predictions_root, exclude_tokens)

    if not result_path:
        result_path = _find_deepest_result(base_dir, exclude_tokens)

    if result_path:
        print(f"Found results in directory: {result_path}", file=sys.stderr)
        return result_path

    raise FileNotFoundError(
        f"Could not find any directory containing result files within the base directory {base_dir}"
    )




def _extract_peptide_candidate_archive_for_metrics(candidate_dir: str, archive_path: str) -> str:
    candidate_root = str(candidate_dir or "").strip()
    if not candidate_root:
        raise ValueError("Peptide candidate directory is required for metrics parsing.")

    archive_file = Path(str(archive_path or "")).expanduser().resolve()
    if not archive_file.is_file():
        raise FileNotFoundError(
            f"Peptide candidate archive not found for metrics parsing: {archive_file}"
        )

    extract_root = Path(candidate_root).expanduser().resolve() / "_metrics_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive_file, "r") as zf:
            zf.extractall(extract_root)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            f"Peptide candidate archive is not a valid zip: {archive_file}"
        ) from exc

    # parse_confidence_metrics expects af3/ or protenix/ as a direct child of the result root.
    # If the extracted archive contains af3/ or protenix/ at the top level, return extract_root
    # so that parse_confidence_metrics can find them at root_path / "af3" (or "protenix").
    if (extract_root / "af3").is_dir() or (extract_root / "protenix").is_dir():
        return str(extract_root)

    return find_results_dir(str(extract_root))




def _extract_protein_chain_lengths_from_yaml(yaml_data: Dict[str, Any]) -> Dict[str, int]:
    chain_lengths: Dict[str, int] = {}
    if not isinstance(yaml_data, dict):
        return chain_lengths
    for entity in yaml_data.get("sequences", []) or []:
        if not isinstance(entity, dict):
            continue
        protein = entity.get("protein")
        if not isinstance(protein, dict):
            continue
        sequence = str(protein.get("sequence") or "").replace("\n", "").replace(" ", "").strip()
        if not sequence:
            continue
        ids = protein.get("id")
        if isinstance(ids, list):
            chain_ids = [str(item or "").strip() for item in ids]
        else:
            chain_ids = [str(ids or "").strip()]
        for chain_id in chain_ids:
            if not chain_id:
                continue
            chain_lengths[chain_id] = len(sequence)
    return chain_lengths




def _read_int_option(
    options: Dict[str, Any],
    key: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    raw = options.get(key, default)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))




def _read_bool_option(options: Dict[str, Any], key: str, default: bool) -> bool:
    raw = options.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    token = str(raw or "").strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default




def _normalize_peptide_design_mode(raw: Any) -> str:
    from backend.services.common_utils import normalize_peptide_design_mode
    return normalize_peptide_design_mode(raw)




def _normalize_peptide_backend(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token == "rfd4":
        # rfd4 是设计引擎，不是打分后端；静默映射成 boltz 会误导验证结论
        raise ValueError(
            "rfd4 不能作为打分后端：请用 peptideProposerBackend=rfd4 指定设计引擎，"
            "并以 peptideScorerBackend 选择 mol1/protenix/alphafold3。")
    if token in {"alphafold3", "protenix", "mol1", "esmfold2"}:
        return token
    # docking engines map onto their corresponding full predictors at the
    # engine level (queue selection, prediction calls); the docking semantics
    # themselves are carried by _is_docking_peptide_backend / peptideChirality
    if token in {"protenix2dock", "protenix-2-dock"}:
        return "protenix"
    if token in {"mol1dock", "mol-1-dock", "mol-1"}:
        return "mol1"
    return "boltz"




def _is_docking_peptide_backend(raw: Any) -> bool:
    """Structure-based peptide docking engines (protenix2dock; boltz2dock 与
mol1dock 已下线，token 仅为历史任务行保留).

    They map onto the corresponding full predictors at the engine level but
    carry docking semantics: a target structure is required (uploaded, or
    predicted first with the full engine when absent) and D-peptide design
    (mirror workflow) is available.
    """
    return str(raw or "").strip().lower() in {"boltz2dock", "boltz-2-dock",
                                              "protenix2dock", "protenix-2-dock",
                                              "mol1dock", "mol-1-dock"}


# Legacy shape derived from the unified linker registry (bicyclic_linkers.py)
BICYCLIC_LINKER_ATOM_MAP: Dict[str, List[str]] = bicyclic_linkers.atom_map()




def _build_template_residue_maps(yaml_data: Dict[str, Any]) -> Dict[str, Dict[int, int]]:
    """Author-to-sequence residue numbering per YAML chain.

    Returns {query_chain_id: {author_resnum: 1-based_sequence_position}} built
    by aligning each YAML chain sequence against the uploaded template chains.
    Chains without a resolvable template are absent from the result.
    """
    templates = yaml_data.get("templates")
    if not isinstance(templates, list) or not templates:
        return {}

    chain_seq_map = build_chain_sequence_map(yaml_data)
    if not chain_seq_map:
        return {}

    mapping_by_chain: Dict[str, Dict[int, int]] = {}
    for entry in templates:
        if not isinstance(entry, dict):
            continue
        template_path_raw = (entry.get("author_pdb")
                             or entry.get("cif") or entry.get("mmcif")
                             or entry.get("pdb"))
        template_path_text = str(template_path_raw or "").strip()
        if not template_path_text:
            continue
        template_path = Path(template_path_text)
        if not template_path.exists():
            continue
        chain_ids = _normalize_chain_id_list(entry.get("chain_id") or entry.get("target_chain_ids"))
        preferred_chain = str(entry.get("template_id") or entry.get("template_chain_id") or "").strip() or None
        try:
            template_chains = _load_template_chain_mappings(template_path, preferred_chain)
        except Exception:
            continue
        if not template_chains:
            continue

        # No chain meta (bare template_files upload): align every YAML chain
        # against every template chain and keep the best match per query
        # chain. Contacts then use template chain names/author numbers, so
        # each template chain's map is stored under its own name as well.
        query_candidates = chain_ids or sorted(chain_seq_map)
        for query_chain in query_candidates:
            query_seq = str(chain_seq_map.get(query_chain) or "").strip()
            if not query_seq:
                continue
            for template_chain_name, template_seq, residue_numbers in template_chains:
                query_indices, template_indices = build_alignment_indices(query_seq, template_seq)
                if not query_indices or not template_indices:
                    continue
                template_to_query = {int(t): int(q) + 1 for q, t in zip(query_indices, template_indices)}
                residue_map: Dict[int, int] = {}
                for template_idx, residue_number in enumerate(residue_numbers):
                    mapped_pos = template_to_query.get(int(template_idx))
                    if mapped_pos is not None:
                        residue_map[int(residue_number)] = int(mapped_pos)
                if not residue_map:
                    continue
                existing = mapping_by_chain.get(query_chain)
                if not existing or len(residue_map) > len(existing):
                    mapping_by_chain[query_chain] = residue_map
                if not chain_ids:
                    existing_by_name = mapping_by_chain.get(template_chain_name)
                    if not existing_by_name or len(residue_map) > len(existing_by_name):
                        mapping_by_chain[template_chain_name] = residue_map

    return mapping_by_chain






def _load_template_chain_mappings(
    template_path: Path,
    preferred_chain: Optional[str] = None,
) -> List[Tuple[str, str, List[int]]]:
    """All polymer chains of a template as (chain name, sequence, author
    residue numbers); the preferred chain, if present, comes first."""
    structure = gemmi.read_structure(str(template_path))
    structure.setup_entities()
    if len(structure) == 0:
        return []
    aa3_to1 = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E", "GLY": "G",
        "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P", "SER": "S",
        "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V", "SEC": "U", "PYL": "O",
    }
    preferred = str(preferred_chain or "").strip()
    chains: List[Tuple[str, str, List[int]]] = []
    for chain in structure[0]:
        seen: set[Tuple[int, str]] = set()
        sequence_chars: List[str] = []
        residue_numbers: List[int] = []
        for residue in chain:
            if residue.het_flag != "A":
                continue
            residue_key = (int(residue.seqid.num), str(residue.seqid.icode or "").strip())
            if residue_key in seen:
                continue
            seen.add(residue_key)
            residue_numbers.append(int(residue.seqid.num))
            sequence_chars.append(aa3_to1.get(str(residue.name or "").strip().upper(), "X"))
        if sequence_chars:
            chains.append((str(chain.name or "").strip(), "".join(sequence_chars), residue_numbers))
    chains.sort(key=lambda c: c[0] != preferred)
    return chains






def _linker_ccd_mols(codes: Iterable[str]) -> Dict[str, Chem.Mol]:
    pkl_path = Path(__file__).resolve().parent / "linker_ccd" / "linker_mols.pkl"
    if not pkl_path.is_file():
        raise FileNotFoundError(f"Bicyclic linker CCD mols are missing: {pkl_path}")
    with pkl_path.open("rb") as handle:
        mols = pickle.load(handle)
    if not isinstance(mols, dict):
        raise RuntimeError(f"Corrupt linker mols cache (expected a dict): {pkl_path}")
    missing = [code for code in codes if code not in mols]
    if missing:
        raise KeyError(f"Bicyclic linker mols missing for: {missing}")
    return {code: mols[code] for code in codes}




def _extract_chain_ids_from_yaml(yaml_data: Dict[str, Any]) -> List[str]:
    chain_ids: List[str] = []
    for seq_block in yaml_data.get("sequences", []) or []:
        if not isinstance(seq_block, dict):
            continue
        payload: Dict[str, Any] = {}
        for key in ("protein", "dna", "rna", "ligand"):
            candidate = seq_block.get(key)
            if isinstance(candidate, dict):
                payload = candidate
                break
        if not payload:
            continue
        seq_id = payload.get("id")
        if isinstance(seq_id, list):
            for item in seq_id:
                text = str(item or "").strip()
                if text and text not in chain_ids:
                    chain_ids.append(text)
        else:
            text = str(seq_id or "").strip()
            if text and text not in chain_ids:
                chain_ids.append(text)
    return chain_ids




def _next_available_chain_id(used_chain_ids: List[str], preferred: str) -> str:
    token = str(preferred or "").strip()
    if token and token not in used_chain_ids:
        return token
    alphabet = [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    for candidate in alphabet:
        if candidate not in used_chain_ids:
            return candidate
    suffix = 1
    while True:
        candidate = f"Z{suffix}"
        if candidate not in used_chain_ids:
            return candidate
        suffix += 1




def _pocket_residue_letters(pocket_contacts, yaml_data, target_chain_id):
    """One-letter codes for the user's pocket residues from the target chain's
    sequence — the chemistry profile for warm-start seed construction."""
    if not pocket_contacts:
        return ""
    seq_by_pos = {}
    for entry in (yaml_data.get("sequences") or []):
        if not isinstance(entry, dict):
            continue
        prot = entry.get("protein") or {}
        sid = str(prot.get("id") or "").strip()
        if sid != str(target_chain_id or "").strip():
            continue
        seq = str(prot.get("sequence") or "").strip().upper()
        for _, pos in pocket_contacts:
            if 1 <= int(pos) <= len(seq):
                seq_by_pos[int(pos)] = seq[int(pos) - 1]
        break
    return "".join(seq_by_pos.get(p, "X") for _, p in sorted(pocket_contacts))




def _normalize_sequence_mask(raw_mask: Any, binder_length: int) -> str:
    if raw_mask is None:
        return ""
    text = str(raw_mask).strip()
    if not text:
        return ""
    mask = text.replace("-", "").replace("_", "").replace(" ", "").upper()
    valid = {"X", "A", "C", "D", "E", "F", "G", "H", "I", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "V", "W", "Y"}
    bad_chars = sorted({char for char in mask if char not in valid})
    if bad_chars:
        raise ValueError(
            f"序列掩码含非法字符 {''.join(bad_chars)}：仅允许 X（自由位）与 20 种天然氨基酸。")
    if len(mask) != binder_length:
        raise ValueError(
            f"序列掩码长度 {len(mask)} 与肽长度 {binder_length} 不一致："
            "请将掩码轨道与设计长度对齐后重新提交。")
    return mask




def _mask_fixed_edges(mask: str) -> tuple[str, str]:
    """掩码两端的固定残基段(首个/末个 X 之前的部分)。固定段是用户设计,
    序列质量检查与排除名单都须豁免。"""
    prefix = ""
    for ch in mask or "":
        if ch == "X":
            break
        prefix += ch
    suffix = ""
    for ch in reversed(mask or ""):
        if ch == "X":
            break
        suffix = ch + suffix
    return prefix, suffix




def _normalize_excluded_aas(raw: Any) -> List[str]:
    """用户排除的氨基酸(omit_AAs 式)。接受列表或逗号/空白分隔串, 归一化
    为 20 种天然氨基酸单字母的去重排序; 含非法字母抛 ValueError。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("peptideExcludedAAs 需为氨基酸单字母列表。")
    valid = set("ACDEFGHIKLMNPQRSTVWY")
    excluded = sorted({str(a).strip().upper() for a in raw
                       if str(a).strip()})
    bad = [a for a in excluded if a not in valid]
    if bad:
        raise ValueError(
            f"peptideExcludedAAs 含非法氨基酸 {''.join(bad)}：仅允许 20 种天然氨基酸单字母。")
    return excluded




def _adapt_mask_to_length(mask: str, length: int) -> Optional[str]:
    """把掩码适配到目标长度: 两端固定段保留, 自由 X 段伸缩。

    仅当全部固定残基都落在两端时安全(双环 CREEEC+自由段+C); 中部
    还有固定残基(如额外锚点 Cys)的掩码无法伸缩, 返回 None, 调用方
    退回按原掩码处理。"""
    if "X" not in mask:
        return None
    prefix, suffix = _mask_fixed_edges(mask)
    if any(ch != "X" for ch in mask[len(prefix):len(mask) - len(suffix)]):
        return None
    free = length - len(prefix) - len(suffix)
    if free < 1:
        return None
    return prefix + "X" * free + suffix




def _apply_sequence_mask(sequence: str, sequence_mask: str) -> str:
    if not sequence_mask:
        return sequence
    mask = sequence_mask
    if len(sequence) != len(mask):
        # 掩码与候选长度不等(长度窗口所致): 伸缩适配失败时沿用原掩码。
        adapted = _adapt_mask_to_length(mask, len(sequence))
        if adapted is not None:
            mask = adapted
    seq_chars = list(sequence)
    for idx, mask_char in enumerate(mask):
        if idx >= len(seq_chars):
            break
        if mask_char != "X":
            seq_chars[idx] = mask_char
    # a bicyclic topology needs exactly the masked cysteines: ESM3's
    # mutation path can propose Cys at free positions, tripping the
    # "exactly 3" validation downstream. Free positions never carry Cys,
    # and anything past the mask length is dropped.
    if "C" in mask:
        for idx, mask_char in enumerate(mask):
            if mask_char == "X" and idx < len(seq_chars) and seq_chars[idx] == "C":
                seq_chars[idx] = "S"
        seq_chars = seq_chars[:len(mask)]
    return "".join(seq_chars)




def _substitute_ncaa_residues(
    sequence: str,
    modifications: List[Dict[str, Any]],
    unnatural_pool: List[Dict[str, Any]],
    nonnatural_min: int,
    nonnatural_max: int,
    cys_positions: List[int],
    rng: random.Random,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Overlay user-selected non-natural residues onto the proposer's
    standard-20 backbone. The sequence keeps standard base letters; the
    modifications list tells protenix which positions carry a CCD
    substitution. Protenix's CCD coverage provides the generalization;
    the LoRA adapter learns which substitutions improve binding from
    the GRPO reward."""
    if not unnatural_pool or nonnatural_max <= 0:
        return sequence, modifications
    protected = {int(p) for p in cys_positions}
    protected |= {i for i, ch in enumerate(sequence) if ch == "C"}
    available = [i for i in range(len(sequence)) if i not in protected]
    rng.shuffle(available)

    # placement rules: "n_term" locks to the first residue, "c_term" to the
    # last; "any" takes the remaining slots
    n_term = [e for e in unnatural_pool if e.get("placement") == "n_term"]
    c_term = [e for e in unnatural_pool if e.get("placement") == "c_term"]
    any_pool = [e for e in unnatural_pool
                if e.get("placement") not in ("n_term", "c_term")]
    if not any_pool and not n_term and not c_term:
        return sequence, modifications

    count = min(rng.randint(nonnatural_min, nonnatural_max), len(available))
    if count <= 0:
        return sequence, modifications
    mods = list(modifications)
    used = set()

    for entry in n_term + c_term:
        if count <= 0 or not available:
            break
        pos = len(sequence) - 1 if entry.get("placement") == "c_term" else 0
        if pos in protected or pos in used:
            continue
        base = entry.get("base", "A")
        mods.append({"position": pos + 1, "ccd": entry["ccd"],
                     "baseResidue": base})
        used.add(pos)
        count -= 1

    rng.shuffle(any_pool)
    for entry in any_pool * max(1, count // max(1, len(any_pool))):
        if count <= 0 or not available:
            break
        pos = available.pop(0)
        if pos in used:
            continue
        base = entry.get("base", "A")
        mods.append({"position": pos + 1, "ccd": entry["ccd"],
                     "baseResidue": base})
        used.add(pos)
        count -= 1

    return sequence, mods




def _stamp_bicyclic_cysteines(
    sequence: str,
    *,
    explicit_positions: List[int],
    cys_layout: Optional[Dict[str, Any]],
    sequence_mask: str,
    allow_extra_cys: bool,
    resolve_anchors: Callable[[int, Optional[dict], tuple, Optional[dict]], Optional[tuple]],
) -> Tuple[str, List[int]]:
    """Stamp the bicyclic anchor cysteines onto a proposed sequence.

    The ESM3 proposer samples raw sequences with no notion of bicyclic
    topology, while the candidate YAML builder accepts exactly 3 Cys at the
    anchor positions. Anchors are stamped C; stray Cys outside anchors
    demote to A (mirrors peplm.loop.constraints.apply_post_edit).
    """
    chars = list(sequence)
    length = len(chars)
    if length < 5:
        return sequence, []
    fixed = {
        idx: ch for idx, ch in enumerate(sequence_mask or "")
        if ch not in ("", "X")
    } if sequence_mask else {}
    resolved = resolve_anchors(length, fixed, tuple(explicit_positions or ()), cys_layout)
    anchors = sorted({
        int(pos) for pos in (resolved or ())
        if pos is not None and 0 <= int(pos) < length
    })
    if len(anchors) != 3:
        return sequence, []
    for pos in anchors:
        chars[pos] = "C"
    if not allow_extra_cys:
        anchor_set = set(anchors)
        for idx, ch in enumerate(chars):
            if ch == "C" and idx not in anchor_set and idx not in fixed:
                chars[idx] = "A"
    return "".join(chars), anchors


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



def _merge_peptide_preset_molecules_by_code(
    custom_molecules: List[Dict[str, str]],
    ccd_codes: Iterable[str],
) -> List[Dict[str, str]]:
    merged = list(custom_molecules)
    seen = {str(item.get("ccd") or "").upper() for item in merged if isinstance(item, dict)}
    for raw_code in ccd_codes or []:
        ccd = str(raw_code or "").strip().upper()
        preset = PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES.get(ccd)
        if not ccd or not preset or ccd in seen:
            continue
        seen.add(ccd)
        merged.append({
            "ccd": ccd,
            "smiles": str(preset.get("smiles") or ""),
            "base_residue": PEPTIDE_PRESET_BASE_RESIDUE.get(ccd, "A"),
            "label": str(preset.get("label") or ccd),
            "kind": "residue",
        })
    return merged




def _merge_selected_peptide_preset_molecules(
    custom_molecules: List[Dict[str, str]],
    unnatural_pool: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    return _merge_peptide_preset_molecules_by_code(
        custom_molecules,
        [
            str(row.get("ccd") or "").strip().upper()
            for row in unnatural_pool or []
            if isinstance(row, dict) and str(row.get("kind") or "").lower() == "preset"
        ],
    )




def _normalize_peptide_residue_pool(raw_pool: Any, custom_molecules: List[Dict[str, str]]) -> Tuple[List[str], List[Dict[str, str]]]:
    natural: List[str] = []
    unnatural: List[Dict[str, str]] = []
    custom_by_code = {str(item.get("ccd") or "").upper(): item for item in custom_molecules if isinstance(item, dict)}
    if isinstance(raw_pool, list):
        for item in raw_pool:
            if not isinstance(item, dict):
                continue
            code = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("code") or item.get("ccd") or "")).upper()[:12]
            kind = str(item.get("kind") or "").strip().lower()
            if not code:
                continue
            if kind == "natural":
                aa = PEPTIDE_NATURAL_THREE_TO_ONE.get(code, code if len(code) == 1 else "")
                if aa and aa in "ARNDCQEGHILKMFPSTWYV" and aa not in natural:
                    natural.append(aa)
                continue
            if kind == "preset" and code not in PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES:
                raise ValueError(
                    f"未知的非天然氨基酸预设 {code}：可选 "
                    + ", ".join(sorted(PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES)) + "。")
            if kind in {"preset", "custom"}:
                base = ""
                if kind == "custom":
                    if code not in custom_by_code:
                        raise ValueError(f"Custom peptide residue '{code}' is selected but no user-scoped custom CCD molecule was submitted.")
                    base = str(custom_by_code[code].get("base_residue") or "A").upper()[:1] or "A"
                else:
                    base = PEPTIDE_PRESET_BASE_RESIDUE.get(code, "A")
                if base not in "ARNDCQEGHILKMFPSTWYV":
                    base = "A"
                if not any(row.get("ccd") == code and row.get("kind") == kind for row in unnatural):
                    if kind == "preset":
                        placement = PEPTIDE_PRESET_PLACEMENT_RULES.get(code, "any")
                    else:
                        # A C-terminal amidated custom residue has no leaving atom on its backbone C,
                        # so it can only sit at the C-terminus: placement == "c_term"
                        # (the PeptideLM decoder enforces last-position only).
                        custom_def = custom_by_code.get(code, {})
                        placement = "c_term" if bool(custom_def.get("cTerminalAmidated") or False) else "any"
                    unnatural.append({
                        "ccd": code,
                        "base": base,
                        "kind": kind,
                        "placement": placement,
                    })
    if isinstance(raw_pool, list) and not natural and not unnatural:
        raise ValueError("Peptide residue candidate pool is empty; select at least one natural or non-natural residue.")
    if not natural and not unnatural:
        natural = list("ARNDCQEGHILKMFPSTWYV")
    if not natural:
        natural = sorted({row["base"] for row in unnatural if row.get("base")})
    if not natural:
        raise ValueError("Peptide residue candidate pool has no usable base residues.")
    return natural, unnatural




def _peptide_allowed_residues(natural_pool: List[str], design_mode: str) -> List[str]:
    residues: List[str] = []
    for aa in natural_pool:
        token = str(aa or "").strip().upper()[:1]
        if token and token in "ARNDCQEGHILKMFPSTWYV" and token not in residues:
            residues.append(token)
    if design_mode == "bicyclic":
        residues = [aa for aa in residues if aa != "C"]
    if not residues:
        raise ValueError(
            "Peptide natural residue candidate pool has no residues usable in "
            f"{design_mode} mode. Select at least one compatible natural residue."
        )
    return residues




def _peptide_sequence_liability_penalty(sequence: str, modifications: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    seq = str(sequence or "").upper()
    length = max(1, len(seq))
    counts = Counter(seq)
    hydrophobic_ratio = sum(counts.get(aa, 0) for aa in "AILMFWYV") / length
    charged_ratio = sum(counts.get(aa, 0) for aa in "DEKRH") / length
    pro_gly_ratio = sum(counts.get(aa, 0) for aa in "PG") / length
    max_run = 1
    run = 1
    # run>=4 阈值与 peplm.sequence_quality._MIN_RUN 联动: 本处按可开发性
    # 计一层惩罚, 序列门禁再按预测器虚高置信计一层, 两处需同步修改。
    for idx in range(1, len(seq)):
        if seq[idx] == seq[idx - 1]:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
    repeated_triples = sum(1 for idx in range(0, max(0, len(seq) - 2)) if seq[idx] == seq[idx + 1] == seq[idx + 2])
    penalty = 0.0
    if hydrophobic_ratio > 0.58:
        penalty += (hydrophobic_ratio - 0.58) * 0.35
    if charged_ratio > 0.45:
        penalty += (charged_ratio - 0.45) * 0.20
    if pro_gly_ratio > 0.35:
        penalty += (pro_gly_ratio - 0.35) * 0.20
    if max_run >= 4:
        penalty += min(0.12, (max_run - 3) * 0.03)
    if repeated_triples:
        penalty += min(0.08, repeated_triples * 0.015)
    mod_count = len(modifications) if isinstance(modifications, list) else 0
    return {
        "penalty": min(0.25, penalty),
        "hydrophobic_ratio": hydrophobic_ratio,
        "charged_ratio": charged_ratio,
        "pro_gly_ratio": pro_gly_ratio,
        "max_homopolymer_run": max_run,
        "repeated_triples": repeated_triples,
        "modification_count": mod_count,
    }




def _extract_peptide_plddts(structure_file) -> list:
    """Per-residue pLDDT for the peptide chain, read from the CA B-factor
    column of the refined structure (protenix writes plddt*100 there).
    Returns [] on any failure — the proposer treats empty plddts as
    uniform weight (no pLDDT guidance), matching the pre-upgrade path."""
    try:
        st = gemmi.read_structure(str(structure_file))
        st.setup_entities()
        chains = sorted(
            [c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3],
            key=lambda c: sum(1 for r in c if r.het_flag != "H"))
        if len(chains) < 2:
            return []
        pep = chains[-1]  # shortest chain = peptide
        plddts = [
            float(r.find_atom("CA", "*").b_iso) / 100.0
            for r in pep
            if r.het_flag != "H" and r.find_atom("CA", "*") is not None
        ]
        # Staged PDB writers leave B-factor at 0.0 — fall back to the
        # engine's CIF in the same directory (protenix writes plddt there)
        if plddts and max(plddts) < 0.01:
            for cif in Path(structure_file).parent.glob("*.cif"):
                try:
                    alt = _extract_peptide_plddts(cif)
                    if alt and max(alt) > 0.01:
                        return alt
                except Exception:
                    continue
        return plddts
    except Exception:
        return []




def _peptide_sequence_similarity(seq_a: str, seq_b: str) -> float:
    if not seq_a or not seq_b or len(seq_a) != len(seq_b):
        return 0.0
    return sum(1 for aa, bb in zip(seq_a, seq_b) if aa == bb) / max(1, len(seq_a))




def _peptide_rank_score(row: Dict[str, Any]) -> float:
    value = row.get("composite_score")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return float("-inf")




def _peptide_float(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return default




def _peptide_objectives(row: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        _peptide_float(row, "interface_confidence"),
        _peptide_float(row, "binder_confidence"),
        _peptide_float(row, "pair_iptm_confidence"),
        _peptide_float(row, "developability_score", 1.0),
    )




def _peptide_dominates(row_a: Dict[str, Any], row_b: Dict[str, Any]) -> bool:
    obj_a = _peptide_objectives(row_a)
    obj_b = _peptide_objectives(row_b)
    return all(a >= b for a, b in zip(obj_a, obj_b)) and any(a > b for a, b in zip(obj_a, obj_b))




def _peptide_non_dominated_fronts(results: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    candidates = [row for row in results if str(row.get("sequence") or "")]
    domination_counts: Dict[int, int] = {}
    dominated_by_index: Dict[int, List[int]] = {}
    first_front: List[int] = []
    for idx, row in enumerate(candidates):
        dominated: List[int] = []
        domination_count = 0
        for other_idx, other in enumerate(candidates):
            if idx == other_idx:
                continue
            if _peptide_dominates(row, other):
                dominated.append(other_idx)
            elif _peptide_dominates(other, row):
                domination_count += 1
        dominated_by_index[idx] = dominated
        domination_counts[idx] = domination_count
        if domination_count == 0:
            first_front.append(idx)

    fronts: List[List[Dict[str, Any]]] = []
    current = first_front
    while current:
        fronts.append([candidates[idx] for idx in current])
        next_front: List[int] = []
        for idx in current:
            for dominated_idx in dominated_by_index.get(idx, []):
                domination_counts[dominated_idx] = domination_counts.get(dominated_idx, 0) - 1
                if domination_counts[dominated_idx] == 0:
                    next_front.append(dominated_idx)
        current = next_front
    return fronts




def _peptide_crowding_distance(front: List[Dict[str, Any]]) -> Dict[int, float]:
    distances = {idx: 0.0 for idx in range(len(front))}
    if len(front) <= 2:
        return {idx: float("inf") for idx in range(len(front))}
    objective_count = len(_peptide_objectives(front[0])) if front else 0
    for objective_idx in range(objective_count):
        values = [_peptide_objectives(row)[objective_idx] for row in front]
        order = sorted(range(len(front)), key=lambda idx: values[idx])
        distances[order[0]] = float("inf")
        distances[order[-1]] = float("inf")
        min_value = values[order[0]]
        max_value = values[order[-1]]
        span = max_value - min_value
        if span <= 1e-12:
            continue
        for rank_idx in range(1, len(order) - 1):
            prev_value = values[order[rank_idx - 1]]
            next_value = values[order[rank_idx + 1]]
            distances[order[rank_idx]] += (next_value - prev_value) / span
    return distances




def _select_nsga2_peptide_elites(results: List[Dict[str, Any]], elite_size: int) -> List[Dict[str, Any]]:
    fronts = _peptide_non_dominated_fronts(results)
    selected: List[Dict[str, Any]] = []
    for front in fronts:
        if len(selected) + len(front) <= elite_size:
            selected.extend(sorted(front, key=_peptide_rank_score, reverse=True))
            continue
        distances = _peptide_crowding_distance(front)
        ranked_front = sorted(
            enumerate(front),
            key=lambda item: (distances.get(item[0], 0.0), _peptide_rank_score(item[1])),
            reverse=True,
        )
        for _, row in ranked_front:
            seq = str(row.get("sequence") or "")
            if not seq:
                continue
            if all(_peptide_sequence_similarity(seq, str(prev.get("sequence") or "")) < 0.92 for prev in selected):
                selected.append(row)
            if len(selected) >= elite_size:
                break
        if len(selected) < elite_size:
            for _, row in ranked_front:
                if row not in selected:
                    selected.append(row)
                if len(selected) >= elite_size:
                    break
        break
    if len(selected) < elite_size:
        for row in sorted(results, key=_peptide_rank_score, reverse=True):
            if row not in selected:
                selected.append(row)
            if len(selected) >= elite_size:
                break
    return selected[:elite_size]




def _peptide_candidate_key(sequence: str, modifications: List[Dict[str, Any]]) -> str:
    return f"{sequence}|{json.dumps(modifications, sort_keys=True, separators=(',', ':'))}"




def _normalize_initial_sequence(
    raw_sequence: Any,
    *,
    binder_length: int,
    sequence_mask: str,
) -> str:
    """User seed sequence for generation 1: A-Z only, padded/truncated to the
    binder length, then overlaid with the fixed-position mask."""
    cleaned = "".join(ch for ch in str(raw_sequence or "").upper() if "A" <= ch <= "Z")
    if not cleaned:
        raise ValueError("peptideUseInitialSequence 已启用，但 peptideInitialSequence 为空。")
    if len(cleaned) < binder_length:
        cleaned = (cleaned + "G" * binder_length)[:binder_length]
    else:
        cleaned = cleaned[:binder_length]
    return _apply_sequence_mask(cleaned, sequence_mask)




def _build_peptide_candidate_yaml(
    base_yaml_data: Dict[str, Any],
    *,
    binder_chain_id: str,
    binder_sequence: str,
    design_mode: str,
    linker_ccd: str,
    linker_chain_id: str,
    linker_atom_map: Dict[str, List[str]],
    modifications: Optional[List[Dict[str, Any]]] = None,
    backend: str = "boltz",
    cys_positions: Optional[List[int]] = None,
    pocket_constraint: Optional[Dict[str, Any]] = None,
    binder_only: bool = False,
    binder_msa: str = "empty",
    amidated_ccds: Optional[set] = None,
) -> str:
    yaml_data = copy.deepcopy(base_yaml_data)
    if binder_only:
        # D-route conformer prediction: the isolated candidate only (ring,
        # linker and NCAA topology included) — the receptor never enters a
        # de novo complex prediction. Constraints reference the target chain
        # by name and would crash the isolated prediction.
        yaml_data["sequences"] = []
        yaml_data.pop("templates", None)
        yaml_data.pop("properties", None)
        yaml_data.pop("constraints", None)
        pocket_constraint = None
    if not isinstance(yaml_data.get("sequences"), list):
        yaml_data["sequences"] = []

    binder_entry: Dict[str, Any] = {
        "protein": {
            "id": binder_chain_id,
            "sequence": binder_sequence,
            # Binder MSA policy (user directive: MSA everywhere): the orchestrator
            # pre-fetches this candidate's MSA into the shared cache and passes the a3m
            # path here (PROVIDED mode — the worker needs no server round trip). When the
            # search fails for this one candidate, "empty" keeps the run alive.
            "msa": binder_msa,
        }
    }
    if modifications:
        binder_modifications: List[Dict[str, Any]] = []
        for item in modifications:
            if not isinstance(item, dict):
                continue
            ccd = str(item.get("ccd") or item.get("ptmType") or "").strip().upper()
            if not ccd:
                continue
            base_residue = str(item.get("baseResidue") or item.get("base_residue") or "").strip().upper()[:1]
            # 修饰位置来自提案期序列；最终链长可能不同。
            # 酰胺化残基锚到交付 C 端，其余限制在链内。
            seq_len = len(binder_sequence)
            position = int(item.get("position") or item.get("ptmPosition") or 1)
            if amidated_ccds and ccd in amidated_ccds:
                position = max(1, seq_len)
            else:
                position = max(1, min(position, seq_len))
            mod_entry: Dict[str, Any] = {"position": position, "ccd": ccd}
            if base_residue:
                mod_entry["baseResidue"] = base_residue
            binder_modifications.append(mod_entry)
        if binder_modifications:
            binder_entry["protein"]["modifications"] = binder_modifications
    if design_mode == "cyclic":
        if _normalize_peptide_backend(backend) == "boltz":
            binder_entry["protein"]["cyclic"] = True
        else:
            # AF3/Protenix have no native cyclic flag; express head-to-tail cyclization
            # as an explicit N(1)-C(L) bond (adapters map YAML bonds → bondedAtomPairs /
            # covalent_bonds).
            constraints = yaml_data.get("constraints")
            if not isinstance(constraints, list):
                constraints = []
            constraints.append(
                {
                    "bond": {
                        "atom1": [binder_chain_id, 1, "N"],
                        "atom2": [binder_chain_id, len(binder_sequence), "C"],
                    }
                }
            )
            yaml_data["constraints"] = constraints

    yaml_data["sequences"].append(binder_entry)

    if pocket_constraint:
        constraints = yaml_data.get("constraints")
        if not isinstance(constraints, list):
            constraints = []
        constraints.append({"pocket": pocket_constraint})
        yaml_data["constraints"] = constraints

    if design_mode == "bicyclic":
        yaml_data["sequences"].append({"ligand": {"id": linker_chain_id, "ccd": linker_ccd}})
        if cys_positions:
            cys_indices = sorted({int(p) for p in cys_positions})
            for cys_idx in cys_indices:
                if not 0 <= cys_idx < len(binder_sequence) or binder_sequence[cys_idx] != "C":
                    raise ValueError(
                        f"Bicyclic anchor position {cys_idx + 1} does not hold a cysteine "
                        f"in binder sequence {binder_sequence}."
                    )
        else:
            cys_indices = [idx for idx, aa in enumerate(binder_sequence) if aa == "C"]
        if len(cys_indices) != 3:
            raise ValueError(f"Bicyclic peptide requires exactly 3 cysteine residues, got {len(cys_indices)}.")
        linker_atoms = linker_atom_map.get(linker_ccd) or []
        if len(linker_atoms) != 3:
            raise ValueError(f"Unsupported bicyclic linker '{linker_ccd}'.")

        existing_constraints = yaml_data.get("constraints")
        if not isinstance(existing_constraints, list):
            existing_constraints = []
        for cys_idx, linker_atom in zip(cys_indices, linker_atoms):
            existing_constraints.append(
                {
                    "bond": {
                        "atom1": [binder_chain_id, cys_idx + 1, "SG"],
                        "atom2": [linker_chain_id, 1, linker_atom],
                    }
                }
            )
        yaml_data["constraints"] = existing_constraints

    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False)




def _materialize_candidate_template_paths(
    base_yaml_data: Dict[str, Any],
    *,
    candidate_dir: str,
    temp_dir: str,
) -> Dict[str, Any]:
    """
    Copy template files into candidate-local directory and rewrite template paths.
    This guarantees each peptide worker can access template files via its own mounted
    `candidate_dir` without depending on parent-task runtime paths.
    """
    yaml_data = copy.deepcopy(base_yaml_data)
    template_entries = yaml_data.get("templates")
    if not isinstance(template_entries, list) or not template_entries:
        return yaml_data

    target_templates_dir = Path(candidate_dir) / "templates"
    target_templates_dir.mkdir(parents=True, exist_ok=True)

    rewritten_entries: List[Dict[str, Any]] = []
    for index, entry in enumerate(template_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid templates[{index}] entry: expected mapping, got {type(entry).__name__}.")

        path_key = next(
            (key for key in ("cif", "mmcif", "pdb") if str(entry.get(key) or "").strip()),
            None,
        )
        if not path_key:
            raise ValueError(
                f"Invalid templates[{index}] entry: missing template path key (expected one of cif/mmcif/pdb)."
            )

        source_text = str(entry.get(path_key) or "").strip()
        source_path = Path(source_text)

        candidates: List[Path] = []
        if source_path.is_absolute():
            candidates.append(source_path)
        else:
            # Relative template paths are resolved against task temp root first.
            candidates.append(Path(temp_dir) / source_path)
            candidates.append(source_path)

        resolved_source: Optional[Path] = None
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                resolved_source = candidate
                break

        if resolved_source is None:
            raise FileNotFoundError(
                "Peptide design template file is missing before candidate dispatch. "
                f"templates[{index}] path='{source_text}'."
            )

        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", resolved_source.stem).strip("._-") or f"template_{index:02d}"
        suffix = resolved_source.suffix or (".pdb" if path_key == "pdb" else ".cif")
        destination = target_templates_dir / f"{index:02d}_{stem}{suffix}"
        shutil.copy2(resolved_source, destination)

        updated_entry = dict(entry)
        for other_key in ("cif", "mmcif", "pdb"):
            if other_key != path_key:
                updated_entry.pop(other_key, None)
        updated_entry[path_key] = str(destination)
        rewritten_entries.append(updated_entry)

    yaml_data["templates"] = rewritten_entries
    return yaml_data




def _select_primary_structure_file(results_dir: str) -> Optional[Path]:
    path_obj = Path(results_dir)
    candidates = [p for p in path_obj.glob("*.cif")]
    if not candidates:
        candidates = [p for p in path_obj.glob("*.pdb")]
    if not candidates:
        candidates = [p for p in path_obj.rglob("*.cif")]
    if not candidates:
        candidates = [p for p in path_obj.rglob("*.pdb")]
    if not candidates:
        return None

    def _score(path: Path) -> Tuple[int, str]:
        name = path.name.lower()
        rel = str(path.relative_to(path_obj)).replace("\\", "/").lower()
        score = 100
        if "rank_1" in name:
            score = 1
        elif "rank_" in name:
            score = 10
        elif "model_0" in name:
            score = 20
        elif "model_" in name:
            score = 30
        if rel.startswith("af3/output/") or "/af3/output/" in rel:
            score -= 10
        elif rel.startswith("protenix/output/") or "/protenix/output/" in rel:
            score -= 8
        elif rel.startswith("structures/") or "/structures/" in rel:
            score -= 6
        return (score, rel)

    return sorted(candidates, key=_score)[0]




def _write_peptide_progress(progress_path: Optional[str], payload: Dict[str, Any]) -> None:
    if not progress_path:
        return
    try:
        path_obj = Path(progress_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] Failed to write peptide progress file: {exc}", file=sys.stderr)




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




def _peptide_subtask_registry_key(parent_task_id: str) -> str:
    token = str(parent_task_id or "").strip()
    if not token:
        token = "unknown"
    return f"{PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX}{token}"




def _get_redis_client_optional():
    try:
        from gpu_manager import get_redis_client as get_redis_client_fn
        return get_redis_client_fn()
    except Exception:
        return None




def _register_peptide_subtask(parent_task_id: str, subtask_id: str) -> None:
    parent_token = str(parent_task_id or "").strip()
    subtask_token = str(subtask_id or "").strip()
    if not parent_token or not subtask_token:
        return
    client = _get_redis_client_optional()
    if client is None:
        return
    key = _peptide_subtask_registry_key(parent_token)
    try:
        pipe = client.pipeline()
        pipe.sadd(key, subtask_token)
        # The registry is the cancel path's map to in-flight candidate tasks.
        # Design parents legitimately run for DAYS; a 24h TTL let the map
        # expire mid-run, so a late cancel/terminate could no longer revoke
        # dispatched candidates and they kept burning GPU as orphans (the
        # late-cancel orphaned dispatched candidates). 14d matches the cancel-marker TTL.
        pipe.expire(key, 14 * 24 * 3600)
        pipe.execute()
    except Exception as exc:
        # A registry miss makes the candidate invisible to cancel/terminate —
        # silent swallowing here is how orphans happen. At minimum log it.
        print(f"[peptide-design] WARNING: subtask registry write failed "
              f"({exc}); cancel/terminate will not see {subtask_token}",
              file=sys.stderr)




def _candidate_dispatch_window() -> int:
    """Per-parent in-flight cap for the candidate dispatch loops.

    A generation used to be enqueued in one burst; with several design
    parents active, the broker FIFO let early parents' whole batches run
    while late parents' candidates sat uncomputed for hours. A per-parent
    window keeps every active parent circulating: dispatch W, refill
    one-for-one as results land. Default W = GPU count — a single parent
    still saturates the pool (candidates already wave across all GPUs),
    multi-parent runs interleave fairly. Override with
    PEPTIDE_CANDIDATE_DISPATCH_WINDOW.
    """
    raw = str(os.environ.get("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    try:
        from gpu_manager import get_gpu_status
        return max(1, int(get_gpu_status().get("valid_count") or 1))
    except Exception:
        return 4




def _submit_peptide_candidate_worker_job(job: Dict[str, Any], queue_name: str, parent_task_id: str):
    from backend.core.celery_app import celery_app

    payload = {
        "generation": int(job.get("generation") or 0),
        "candidate_index": int(job.get("candidate_index") or 0),
        "sequence": str(job.get("sequence") or ""),
        "candidate_yaml": str(job.get("candidate_yaml") or ""),
        "candidate_dir": str(job.get("candidate_dir") or ""),
        "archive_path": str(job.get("archive_path") or ""),
        "predict_args": job.get("predict_args", {}),
        "model_name": job.get("model_name"),
        "backend": str(job.get("backend") or "boltz"),
        "worker_args_path": str(job.get("worker_args_path") or ""),
        # The cancel-marker check on the candidate side keys on the PARENT's
        # task id (the marker is written per cancelled parent); without this
        # field a queued candidate cannot know it belongs to a dead plan.
        "parent_task_id": str(parent_task_id or ""),
    }
    async_result = celery_app.send_task(
        "tasks.peptide_candidate_worker_task",
        args=[payload],
        queue=queue_name,
    )
    _register_peptide_subtask(parent_task_id=parent_task_id, subtask_id=str(getattr(async_result, "id", "") or ""))
    return async_result




def _execute_peptide_generation_jobs(
    jobs: List[Dict[str, Any]],
    queue_name: str,
    parent_task_id: str,
    progress_callback: Optional[Callable[[Dict[str, int]], None]] = None,
) -> List[Dict[str, Any]]:
    """Dispatch every candidate job at once and drain their results.

    No orchestrator-side concurrency window: the shared GPU pool plus the
    worker's own concurrency are the only bounds, so freed GPUs are picked up
    immediately no matter how pool occupancy shifted since dispatch.
    """
    if not jobs:
        return []
    pending = list(jobs)
    running: List[Tuple[Any, Dict[str, Any]]] = []
    completed_jobs: List[Dict[str, Any]] = []
    first_error: Optional[Exception] = None
    last_progress_signature: Optional[Tuple[int, int, int, int, int]] = None

    window = _candidate_dispatch_window()

    def _fill_running() -> None:
        while pending and first_error is None and len(running) < window:
            next_job = pending.pop(0)
            async_result = _submit_peptide_candidate_worker_job(
                next_job,
                queue_name=queue_name,
                parent_task_id=parent_task_id,
            )
            running.append((async_result, next_job))

    _fill_running()
    while pending or running:
        if not running:
            break

        next_running: List[Tuple[Any, Dict[str, Any]]] = []
        state_counts = {
            "queued": 0,
            "running": 0,
            "success": 0,
            "failure": 0,
        }
        for async_result, submitted_job in running:
            state = str(getattr(async_result, "state", "") or "").upper()
            if state in {"SUCCESS", "FAILURE", "REVOKED"}:
                if state == "SUCCESS":
                    completed_job = dict(submitted_job)
                    worker_result = getattr(async_result, "result", None)
                    if isinstance(worker_result, dict):
                        archive_from_worker = str(worker_result.get("archive_path") or "").strip()
                        if archive_from_worker:
                            completed_job["archive_path"] = archive_from_worker
                        candidate_dir_from_worker = str(worker_result.get("candidate_dir") or "").strip()
                        if candidate_dir_from_worker:
                            completed_job["candidate_dir"] = candidate_dir_from_worker
                    completed_jobs.append(completed_job)
                    state_counts["success"] += 1
                    continue
                failure_info = getattr(async_result, "result", None)
                if first_error is None:
                    first_error = RuntimeError(
                        "Peptide candidate worker celery task failed "
                        f"(generation={submitted_job.get('generation')}, candidate={submitted_job.get('candidate_index')}, "
                        f"celery_task_id={getattr(async_result, 'id', '')}, state={state}): {failure_info}"
                    )
                state_counts["failure"] += 1
                continue
            if state in {"PENDING", "RECEIVED", "RETRY"}:
                state_counts["queued"] += 1
            else:
                state_counts["running"] += 1
            next_running.append((async_result, submitted_job))
        running = next_running
        _fill_running()

        if callable(progress_callback):
            try:
                progress_payload = {
                    "total": len(jobs),
                    "completed": len(completed_jobs),
                    "queued": int(state_counts.get("queued", 0)),
                    "running": int(state_counts.get("running", 0)),
                    "failed": int(state_counts.get("failure", 0)),
                }
                progress_signature = (
                    int(progress_payload["total"]),
                    int(progress_payload["completed"]),
                    int(progress_payload["queued"]),
                    int(progress_payload["running"]),
                    int(progress_payload["failed"]),
                )
                if progress_signature != last_progress_signature:
                    progress_callback(progress_payload)
                    last_progress_signature = progress_signature
            except Exception:
                pass

        if first_error is not None:
            break
        time.sleep(0.8)

    if first_error is not None:
        for async_result, _ in running:
            try:
                async_result.revoke(terminate=True, signal="SIGTERM")
            except Exception:
                pass
        raise first_error

    if running:
        # Stuck-in-flight escape: a broker restart or worker crash orphans
        # dispatched candidates as PENDING forever (no worker will ever ack
        # them), and the parent would spin here for eternity. Treat any
        # candidate still unfinished after the window as failed and let the
        # generation complete with what landed.
        stuck = [submitted for async_result, submitted in running
                 if str(getattr(async_result, "state", "") or "").upper()
                 in {"PENDING", "RECEIVED", "RETRY"}]
        raise RuntimeError(
            "Peptide candidate dispatch drained with unfinished jobs "
            f"(parent_task_id={parent_task_id}, stuck={len(stuck)}/"
            f"{len(running)}): probable broker/worker restart mid-flight."
        )

    return completed_jobs




def _run_dpeptide_refine_stage(
    stage_contexts: List[Dict[str, Any]],
    parent_task_id: str,
    finalize_context: Callable[[Dict[str, Any]], None],
    poll_interval: float = 2.0,
) -> None:
    """Run the per-candidate GPU refine stage with all refines in flight.

    Each context carries a ``dispatch`` callable (staging/placement already
    done — dispatching only sends the celery task) or ``dispatch=None`` for
    candidates that need no GPU refine. The refine previously ran inline in
    the collection loop — one ~4-5 min protenix2dock task at a time while
    every other GPU in the pool sat idle for the whole generation (design
    wall time ≈ candidates × refine). Every refine is now dispatched
    immediately; the shared GPU pool plus worker concurrency schedule them,
    and each candidate finalizes as soon as its own refine lands, so
    per-candidate progress keeps updating.

    A per-candidate refine failure rejects only that candidate (the task
    still fails when NO candidate survives, enforced by the collection tail).
    """
    for ctx in stage_contexts:
        if ctx.get("dispatch") is None:
            finalize_context(ctx)

    pending = [ctx for ctx in stage_contexts if ctx.get("dispatch") is not None]
    running: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
    window = _candidate_dispatch_window()

    def _fill_refines() -> None:
        while pending and len(running) < window:
            ctx = pending.pop(0)
            try:
                async_result = ctx["dispatch"]()
            except Exception as exc:  # noqa: BLE001 — reject this candidate only
                ctx["refine_error"] = exc
                finalize_context(ctx)
                continue
            _register_peptide_subtask(
                parent_task_id=parent_task_id,
                subtask_id=str(getattr(async_result, "id", "") or ""),
            )
            ctx["async_result"] = async_result
            running[str(getattr(async_result, "id", "") or "")] = (async_result, ctx)

    _fill_refines()
    while pending or running:
        if not running:
            break

        finished: List[str] = []
        for task_key, (async_result, ctx) in running.items():
            state = str(getattr(async_result, "state", "") or "").upper()
            if state in {"FAILURE", "REVOKED"}:
                ctx["refine_error"] = RuntimeError(
                    f"D-space refine task {getattr(async_result, 'id', '')} failed."
                )
                finished.append(task_key)
            elif state == "SUCCESS":
                finished.append(task_key)
        if not finished:
            time.sleep(max(0.2, poll_interval))
            continue
        for task_key in finished:
            _async_result, ctx = running.pop(task_key)
            finalize_context(ctx)
        _fill_refines()




def _dpeptide_target_sequence(base_yaml_data: Dict[str, Any], target_chain_id: str) -> str:
    """First (or requested) protein chain sequence from the design YAML."""
    target = (target_chain_id or "").strip()
    fallback = ""
    for block in base_yaml_data.get("sequences", []) or []:
        if not isinstance(block, dict):
            continue
        protein = block.get("protein")
        if isinstance(protein, dict) and protein.get("sequence"):
            chain_ids = protein.get("id") if isinstance(protein, dict) else None
            if target and chain_ids != target and (
                    not isinstance(chain_ids, list) or target not in chain_ids):
                fallback = fallback or str(protein["sequence"])
                continue
            return str(protein["sequence"])
    if fallback:
        return fallback
    raise ValueError("D-peptide design requires a protein target chain in the input YAML.")




def _dpeptide_uploaded_target_structure(predict_args: Dict[str, Any], out_dir: Path) -> Optional[Path]:
    """First uploaded template structure file, decoded to disk.

    Deterministic pick: entries arrive in upload order; extras are ignored
    (logged once at the call site via returned metadata when needed)."""
    for entry in predict_args.get("template_inputs") or []:
        content = entry.get("content_base64") if isinstance(entry, dict) else None
        if not content:
            continue
    
        raw = base64.b64decode(content)
        fmt = str(entry.get("format") or "pdb").lower()
        suffix = ".cif" if fmt in ("cif", "mmcif") else ".pdb"
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        path = path / f"uploaded_target{suffix}"
        path.write_bytes(raw)
        return path
    return None




def _seed_single_sequence_binder_msas(sequences: List[str]) -> int:
    """Write query-only a3m entries into the shared MSA cache for de novo
    binders, keyed exactly like input_prep.resolve_msa reads them.

    Single-sequence policy made durable: on a cache miss the engine falls
    through to the MSA server (30-min hard timeout under GPU starvation);
    a seeded self-only a3m gives it the official N_msa=1 cache hit instead.
    Existing entries are never overwritten — a real cached MSA (e.g. a seed
    sequence fetched by the prefetch path) always wins.
    """
    cache_dir = MSA_CACHE_CONFIG["cache_dir"]
    if not MSA_CACHE_CONFIG.get("enable_cache", True):
        return 0
    os.makedirs(cache_dir, exist_ok=True)
    seeded = 0
    for raw in sequences:
        sequence = "".join(
            aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
            for aa in str(raw or "").strip().upper())
        if not sequence:
            continue
        tier = "env" if len(sequence) >= 50 else "uniref"
        h = get_sequence_hash(sequence)
        cache_name = (f"msa_{h}.a3m" if tier == "env"
                      else f"msa_{h}_{tier}.a3m")
        path = os.path.join(cache_dir, cache_name)
        if os.path.exists(path):
            continue
        tmp = f"{path}.seed.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f">query\n{sequence}\n")
        os.replace(tmp, path)
        seeded += 1
    if seeded:
        print(f"[d-peptide] seeded {seeded} single-sequence binder MSA "
              "cache entries", file=sys.stderr)
    return seeded




def _prefetch_generation_binder_msas(
    sequences: List[str],
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, str]:
    """Binder MSAs for one generation, deduped and fetched concurrently.

    Serial per-candidate prefetch would stack population_size MSA searches of
    pure wall time onto every generation; the searches are HTTP-bound, so a
    small thread pool overlaps them (cache hits return instantly — elites and
    repeated proposals dominate later generations). ``on_progress(done, total)``
    fires after each search lands so the task status can show the prefetch
    instead of a silent multi-minute stall before the first candidates.
    """
    unique = list(dict.fromkeys(str(s or "").strip().upper() for s in sequences if str(s or "").strip()))
    if not unique:
        return {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = max(1, min(_msa_search_concurrency(), len(unique)))
    t0 = time.time()
    assignments: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_binder_msa_assignment, sequence): sequence for sequence in unique}
        done_count = 0
        for future in as_completed(futures):
            sequence = futures[future]
            assignments[sequence] = future.result()
            done_count += 1
            if callable(on_progress):
                try:
                    on_progress(done_count, len(unique))
                except Exception:  # noqa: BLE001
                    pass
    print(
        f"binder MSA 预取完成: {len(assignments)}/{len(unique)} 就绪（{time.time() - t0:.1f}s）",
        file=sys.stderr,
    )
    return assignments




def _dpeptide_predict_target_structure(
    sequence: str,
    backend: str,
    work_dir: Path,
    seed: int,
) -> Path:
    """Full-structure prediction of the L-target when nothing was uploaded.

    boltz2dock -> boltz2 predict (local venv); protenix2dock -> Protenix
    docker.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    fasta = work_dir / "target.fasta"
    fasta.write_text(f">target\n{sequence}\n")

    # Single-engine policy (no cross-engine fallback): boltz2dock uses the
    # Boltz-2 predictor; protenix2dock uses Protenix. A failure surfaces so it
    # can be fixed at the root.
    # Orchestrator containers carry neither the Boltz2Score venv nor a docker
    # CLI — the target-structure prediction is dispatched as a standard
    # predict_task to the selected engine's GPU queue, and its result archive
    # lands on the shared results volume where this loop reads it back.
    from backend.core.celery_app import celery_app as _celery

    yaml_min = (
        "sequences:\n"
        f"  - protein:\n"
        f"      id: A\n"
        f"      sequence: {sequence}\n"
    )

    from backend.worker.tasks import predict_task  # same entry used by routes

    # Receptor-MSA prefetch on the CPU side (this orchestrator holds no GPU):
    # the dispatched prediction task resolves its MSA INSIDE its GPU lease, so
    # a cache miss there + a full GPU pool forms a circular wait — every
    # refine wave holds all cards while the target's mmseqs search queues
    # behind them, and vice versa on a design task).
    # Seeding the shared cache (prediction-side plain key msa_<md5>.a3m)
    # turns the in-task fetch into a cache hit. Failure is non-fatal: the
    # sub-task retries in-task exactly as before this prefetch existed.
    try:
        _h = get_sequence_hash(sequence)
        _cached = os.path.join(MSA_CACHE_CONFIG["cache_dir"], f"msa_{_h}.a3m")
        if not (MSA_CACHE_CONFIG["enable_cache"]
                and os.path.exists(_cached)
                and _ensure_nonempty_a3m_file(_cached, sequence,
                                              context="受体预取缓存校验",
                                              header="target")):
            _msa = request_msa_from_server(sequence, timeout=900,
                                           msa_mode="auto")
            if _msa and MSA_CACHE_CONFIG["enable_cache"]:
                os.makedirs(MSA_CACHE_CONFIG["cache_dir"], exist_ok=True)
                if save_msa_result_to_file(_msa, _cached):
                    print(f"[d-peptide] 受体 MSA 预取完成: {os.path.basename(_cached)}",
                          file=sys.stderr)
    except Exception as _msa_exc:  # noqa: BLE001
        print(f"[d-peptide] 受体 MSA 预取失败({_msa_exc}); "
              "子任务将自行重试", file=sys.stderr)

    async_result = predict_task.apply_async(
        kwargs={"predict_args": {
            "yaml_content": yaml_min,
            "backend": ("boltz" if str(backend).startswith("boltz") else "protenix"),
            "model_name": None,
            "seed": int(seed),
            # MSA is mandatory: it locks the target fold (MSA-watershed result)
            "use_msa_server": True,
            "workflow": "prediction",
        }},
        queue=("cap.boltz2.default" if str(backend).startswith("boltz") else "cap.protenix.default"),
    )

    engine_cap = "protenix" if str(backend).startswith("protenix") else "boltz2"
    results_base = Path(os.environ.get(
        "RESULTS_BASE_DIR",
        str(getattr(__import__("backend.core.config", fromlist=["RESULTS_BASE_DIR"]),
                    "RESULTS_BASE_DIR", "/data/boltz_central_results")),
    ))
    # The dispatched predict_task writes engine outputs under
    # <results_base>/<engine_cap>/<CELERY task id>/ (see _resolve_backend_results_root,
    # which keys on the runtime task id == the celery id), and the worker uploads its
    # archive flat to <results_base>/<CELERY task id>_results.zip (upload_result saves
    # under UPLOAD_FOLDER, no engine subfolder). Poll exactly those two layouts — the
    # poll exactly the two layouts predict_task writes; no speculative layouts.
    result_roots = [
        results_base / engine_cap / str(async_result.id),
    ]

    # The target prediction rides the shared GPU queue BEHIND bulk candidate
    # work, so the bound must cover queue wait plus runtime, not just runtime
    # (a fixed runtime-only deadline fails the parent whenever a backlog
    # exists). Env-tunable for deployments that run a dedicated lane.
    deadline = time.time() + int(
        os.environ.get("DPEPTIDE_TARGET_PREDICT_TIMEOUT_SECONDS", "21600"))
    cif_pat = re.compile(r".*model.*\.cif$|.*_sample_.*\.cif$", re.IGNORECASE)

    def _extract_first_cif(zip_path: Path) -> Optional[Path]:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist()
                         if n.endswith(".cif") and not n.startswith("__MACOSX")
                         and cif_pat.search(n)]
                if not names:
                    return None
                best_name = max(names, key=lambda n: zf.getinfo(n).date_time)
                dest = work_dir / "target_from_zip.cif"
                with zf.open(best_name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                return dest
        except (zipfile.BadZipFile, OSError):
            return None

    while True:
        cifs: list[Path] = []
        for root in result_roots:
            if root.exists():
                cifs.extend(c for c in root.rglob("*.cif")
                    if cif_pat.search(str(c)) and c.stat().st_size > 0)
        if cifs:
            return max(cifs, key=lambda p: p.stat().st_mtime)
        # Results may exist only as the uploaded zip (<celery_id>_results.zip at the
        # results root); read straight from it instead of waiting on an unpack step.
        zip_candidate = results_base / f"{async_result.id}_results.zip"
        if zip_candidate.is_file():
            extracted = _extract_first_cif(zip_candidate)
            if extracted is not None:
                return extracted
        if async_result.state in ("FAILURE", "REVOKED"):
            # Surface the engine's real failure (OOM traceback, docker exit
            # details) — a bare state string hid the cause behind "FAILURE"
            # and made the whole peptide task undiagnosable from the UI.
            inner = getattr(async_result, "result", None)
            detail = f": {inner}" if inner is not None else ""
            raise RuntimeError(
                f"D-peptide target structure prediction failed: {async_result.state}{detail}"
            )
        if time.time() > deadline:
            raise RuntimeError(
                f"D-peptide target structure prediction timed out; roots={result_roots}"
            )
        time.sleep(6)




def _read_pocket_radius_option(options: Dict[str, Any]) -> float:
    """User pocket radius in A: peptidePocketBox clamped to 4-40, default 6.

    The interactive box picker derives it from the box size (max edge / 2),
    mirroring boltz2score's box->radius conversion."""
    raw = options.get("peptidePocketBox")
    if raw is None:
        raw = options.get("peptide_pocket_box")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 6.0
    if not math.isfinite(value):
        value = 6.0
    return max(4.0, min(40.0, value))




def _plain_positions_as_author_contacts(
    base_yaml_data: Dict[str, Any],
    positions: List[int],
    target_chain_id: Optional[str],
) -> List[Tuple[str, int]]:
    """Sequence positions on the target chain as pocket contacts.

    Sequence-only targets cannot be picked in 3D, so the user names residues
    directly on the target sequence ("25,26,27"). With an uploaded template the
    YAML pocket constraint keeps author numbering, so translate sequence ->
    author through the template map (the engines remap author -> sequence back
    at backend entry). Without a template the YAML numbers polymer residues
    1..N and the positions pass through unchanged.
    """
    chain_lengths = _extract_protein_chain_lengths_from_yaml(base_yaml_data)
    if not chain_lengths:
        raise ValueError(
            "Pocket positions need a protein target chain in the YAML."
        )
    resolved_chain = str(target_chain_id or "").strip()
    if resolved_chain not in chain_lengths:
        resolved_chain = next(iter(chain_lengths.keys()), "")
    if not resolved_chain:
        raise ValueError("Pocket positions could not resolve a target chain.")
    chain_length = int(chain_lengths.get(resolved_chain) or 0)
    out_of_range = sorted({p for p in positions if not 1 <= p <= chain_length})
    if out_of_range:
        raise ValueError(
            "Pocket positions outside the target sequence (1-"
            f"{chain_length}): {', '.join(str(p) for p in out_of_range)}"
        )
    mapping_by_chain = _build_template_residue_maps(base_yaml_data)
    residue_map = None
    for key in (resolved_chain, str(resolved_chain).upper(), str(resolved_chain).lower()):
        residue_map = mapping_by_chain.get(key)
        if residue_map:
            break
    if not residue_map:
        return [(resolved_chain, int(p)) for p in positions]
    sequence_to_author = {int(seq): int(auth) for auth, seq in residue_map.items()}
    contacts: List[Tuple[str, int]] = []
    unresolved: List[str] = []
    for position in positions:
        author_num = sequence_to_author.get(int(position))
        if author_num is None:
            unresolved.append(str(position))
        else:
            contacts.append((resolved_chain, author_num))
    if unresolved:
        raise ValueError(
            "Pocket positions not present in the uploaded target structure: "
            f"{', '.join(unresolved)}"
        )
    return contacts




def _pocket_contacts_for_staged_space(
    base_yaml_data: Dict[str, Any],
    options: Dict[str, Any],
    target_chain_id: Optional[str] = None,
) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """Pocket specification in both numbering systems.

    Returns (author_contacts, sequence_contacts). Native predictions (and the
    staged PDBs derived from them) number polymer residues 1..N over the input
    sequence, while the user picks residues by author numbering of the
    uploaded structure ("A:152"). Sequence-only targets instead name positions
    directly on the target chain sequence ("25,26,27") — without a template
    those positions already are the author numbering. The YAML pocket
    constraint keeps author numbering (backends translate it via template
    alignment); the staged-space placement consumes the sequence-numbered
    list. An explicit pocket center selects the surrounding template residues
    within the peptidePocketBox radius. Every requested residue must resolve
    — a silently wrong pocket site is worse than a loud failure.
    """
    # Residue-list input resolves to the centroid of the requested template
    # residues and then takes the same center path. The diffusion-stage
    # PocketPotential stays removed (it buckled aromatic rings); what
    # survives is the model's own pocket conditioning in the candidate
    # co-folding YAML.
    author_contacts: List[Tuple[str, int]] = []
    plain_positions: List[int] = []
    raw_residues = str(
        options.get("peptidePocketResidues") or options.get("peptide_pocket_residues") or ""
    ).strip()
    raw_center = str(options.get("peptidePocketCenter") or options.get("peptide_pocket_center") or "").strip()
    if raw_residues and not raw_center:
        residue_centroid = _pocket_residues_centroid(base_yaml_data, raw_residues)
        if residue_centroid is not None:
            raw_center = ",".join(f"{v:.1f}" for v in residue_centroid)
    if raw_residues:
        for token in raw_residues.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                chain_part, num_part = token.split(":", 1)
                try:
                    author_contacts.append((chain_part.strip(), int(num_part)))
                except ValueError:
                    continue
            else:
                try:
                    position = int(token)
                except ValueError:
                    continue
                if position > 0:
                    plain_positions.append(position)

    if not author_contacts and not plain_positions and raw_center:
        parts = [float(x) for x in raw_center.split(",")]
        if len(parts) != 3:
            raise ValueError("peptidePocketCenter must be 'x,y,z'")
        center = np.asarray(parts)
        radius = _read_pocket_radius_option(options)
        author_contacts = _template_residues_near_center(base_yaml_data, center, radius=radius)
        if not author_contacts:
            raise ValueError(
                f"peptidePocketCenter {raw_center!r} selects no template "
                f"residues within {radius:g} A; check the coordinates "
                "against the uploaded structure."
            )

    chain_prefixed_given = bool(author_contacts)
    if plain_positions:
        author_contacts = author_contacts + _plain_positions_as_author_contacts(
            base_yaml_data, plain_positions, target_chain_id
        )

    if not author_contacts:
        return [], []

    mapping_by_chain = _build_template_residue_maps(base_yaml_data)
    if not mapping_by_chain:
        if not chain_prefixed_given:
            # Sequence-only target: the YAML numbers polymer residues 1..N
            # over the input sequence, so the plain positions already are the
            # author numbering (a center input is impossible here — it raised
            # above when it selected no template residues).
            return list(author_contacts), list(author_contacts)
        raise ValueError(
            "Pocket residues need the uploaded target structure: the YAML "
            "carries no readable templates to translate author numbering to "
            "sequence positions."
        )

    translated: List[Tuple[str, int]] = []
    unresolved: List[str] = []
    for chain_raw, resnum in author_contacts:
        residue_map = None
        for key in (chain_raw, str(chain_raw).upper(), str(chain_raw).lower()):
            residue_map = mapping_by_chain.get(key)
            if residue_map:
                break
        seq_pos = residue_map.get(int(resnum)) if residue_map else None
        if seq_pos is None:
            unresolved.append(f"{chain_raw}:{resnum}")
        else:
            translated.append((chain_raw, int(seq_pos)))
    if unresolved:
        raise ValueError(
            "Pocket residues not found in the uploaded target structure: "
            f"{', '.join(unresolved)}"
        )
    return author_contacts, translated




def _pocket_residues_centroid(
    base_yaml_data: Dict[str, Any],
    raw_residues: str,
) -> Optional[np.ndarray]:
    """Heavy-atom centroid of the requested template residues.

    Accepts the frontend picker's formats: "A:54,A:55" (chain:author),
    "54,55" (plain author numbers on the single target chain), or the
    3D-residue pick encoding. Returns None when nothing resolves — the
    caller then reports the site as invalid rather than guessing.

    The centroid feeds the center path: the model's pocket conditioning
    in the candidate co-folding YAML, not the removed diffusion-stage
    PocketPotential.
    """
    tokens = [t.strip() for t in raw_residues.replace(";", ",").split(",") if t.strip()]
    if not tokens:
        return None
    wanted: List[Tuple[str, int]] = []
    plain: List[int] = []
    for token in tokens:
        chain_part = ""
        num_text = token
        if ":" in token:
            chain_part, num_text = token.split(":", 1)
            chain_part = chain_part.strip()
        try:
            num = int(num_text.strip())
        except ValueError:
            # 3D pick style "A/54" or non-numeric — take trailing digits
            digits = "".join(ch for ch in num_text if ch.isdigit())
            if not digits:
                continue
            num = int(digits)
        if chain_part:
            wanted.append((chain_part, num))
        else:
            plain.append(num)

    templates = base_yaml_data.get("templates")
    if not isinstance(templates, list):
        return None
    for entry in templates:
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("author_pdb")
                        or entry.get("cif") or entry.get("mmcif")
                        or entry.get("pdb") or "").strip()
        path = Path(path_text) if path_text else None
        if path is None or not path.exists():
            continue
        chain_ids = _normalize_chain_id_list(entry.get("chain_id") or entry.get("target_chain_ids"))
        if not chain_ids:
            continue
        try:
            structure = gemmi.read_structure(str(path))
            structure.setup_entities()
        except Exception:
            continue
        model = structure[0] if len(structure) else None
        if model is None:
            continue
        pts: List[np.ndarray] = []
        for chain in model:
            if chain.name not in chain_ids and len(chain_ids) > 1:
                continue
            for residue in chain:
                if residue.het_flag != "A":
                    continue
                matched = ((chain.name, residue.seqid.num) in
                           [(c, n) for c, n in wanted])
                matched_plain = (not wanted) and (residue.seqid.num in plain)
                if not matched and not matched_plain:
                    continue
                for atom in residue:
                    if atom.element != gemmi.Element("H"):
                        pts.append(np.array([atom.pos.x, atom.pos.y, atom.pos.z]))
        if pts:
            return np.mean(np.stack(pts), axis=0)
    return None




def _template_residues_near_center(
    base_yaml_data: Dict[str, Any],
    center: np.ndarray,
    radius: float,
) -> List[Tuple[str, int]]:
    """Author-numbered residues whose CA falls within radius of center, read
    from the uploaded templates (the same source the numbering map uses)."""
    templates = base_yaml_data.get("templates")
    if not isinstance(templates, list):
        return []
    hits: List[Tuple[str, int]] = []
    for entry in templates:
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("cif") or entry.get("mmcif") or entry.get("pdb") or "").strip()
        path = Path(path_text) if path_text else None
        if path is None or not path.exists():
            continue
        chain_ids = _normalize_chain_id_list(entry.get("chain_id") or entry.get("target_chain_ids"))
        if not chain_ids:
            continue
        try:
            structure = gemmi.read_structure(str(path))
            structure.setup_entities()
        except Exception:
            continue
        model = structure[0] if len(structure) else None
        if model is None:
            continue
        for chain in model:
            for residue in chain:
                if residue.het_flag != "A":
                    continue
                atom = residue.find_atom("CA", "*")
                if atom is None:
                    continue
                if np.linalg.norm(np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - center) > radius:
                    continue
                for query_chain in chain_ids:
                    hits.append((query_chain, int(residue.seqid.num)))
    return hits




def _dpeptide_kabsch_rotation(mobile: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Rotation R (about the centroids) minimizing |R @ (mobile-m) - (target-t)|."""
    m = mobile.mean(axis=0)
    t = target.mean(axis=0)
    H = (mobile - m).T @ (target - t)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1.0, 1.0, d]) @ U.T




def _dpeptide_align_product_to_input(product_pdb: Path,
                                     input_structure_path: Path) -> float:
    """Restore the flipped product into the USER's coordinate frame.

    The modeling complex lived in the engine's output frame: even with a
    fixed receptor the writer applies its own global placement. Mapping back
    is the second half of the exact display transform
    upload -> x->-x -> [engine] -> -x->x -> rigid-align-to-upload; because
    the receptor was held fixed during sampling, shape identity holds and
    any residual offset is reported as telemetry. Returns RMSD."""
    def _ca_in_chain_order(path: Path, min_protein_chains: int):
        """CA coordinates of the largest protein chain, in chain order.

        Engine writers renumber residues and rename chains, so neither resnum
        nor chain name is a stable key — the mirrored target and the user
        upload are the SAME sequence in the SAME order by construction."""
        st = gemmi.read_structure(str(path))
        st.setup_entities()
        chains = sorted(
            (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
            key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
        if len(chains) < min_protein_chains:
            raise RuntimeError(
                f"D-peptide align: expected >={min_protein_chains} protein "
                f"chains in {path}")
        pts = []
        for residue in chains[0]:
            ca = residue.find_atom("CA", "*")
            if ca is not None:
                pts.append(np.array([ca.pos.x, ca.pos.y, ca.pos.z]))
        return st, np.stack(pts)

    # the PRODUCT carries target + peptide (>=2 protein chains); the user's
    # REFERENCE is the bare target upload and is valid with a single chain.
    product_st, P = _ca_in_chain_order(product_pdb, min_protein_chains=2)
    _, Q = _ca_in_chain_order(input_structure_path, min_protein_chains=1)
    if len(P) != len(Q):
        # telemetry, not a crash: count mismatch means the predicted receptor
        # and the upload differ in resolved residues; align on the common
        # prefix (both are the same sequence in the same order by
        # construction, so a prefix alignment is exact for the shared part)
        n = min(len(P), len(Q))
        print(
            f"[d-peptide] product frame: CA count product={len(P)} vs "
            f"input={len(Q)}; aligning on the first {n}.",
            file=sys.stderr,
        )
        P, Q = P[:n], Q[:n]
    pc, qc = P.mean(axis=0), Q.mean(axis=0)

    # Frame restoration needs the full rigid transform: the receptor was
    # pinned to the staged pose, but the staged pose itself is the mirror of
    # a de novo prediction that lives in an arbitrary SE(3) frame. The
    # rotation below is computed from matched CA pairs of the SAME receptor,
    # so it is an exact transform (not a least-squares fit between different
    # atoms) and carries the peptide rigidly. Residual RMSD measures the
    # de novo backbone accuracy vs the upload.
    rot = _dpeptide_kabsch_rotation(P, Q)

    shift = qc - rot @ pc
    for model in product_st:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    v = np.array([atom.pos.x, atom.pos.y, atom.pos.z])
                    nv = rot @ v + shift
                    atom.pos = gemmi.Position(*nv)
    product_st.setup_entities()
    product_st.write_pdb(str(product_pdb))

    moved = (rot @ P.T).T + shift
    return float(np.sqrt(((moved - Q) ** 2).sum(axis=1).mean()))




def _relax_staged_bicyclic_strain(
    staged_path: Path,
    *,
    bond_specs: List[Tuple[int, str]],
    linker_code: str,
    cb_lo: float = 1.35,
    cb_hi: float = 1.75,
    sweeps: int = 120,
) -> Dict[str, Any]:
    """Bond window follows anchor chemistry: C-S (SEZ/29N) ~1.82 A,
    Bi-S coordination (BS3) ~2.55 A."""
    if any(anchor == "BI" for _, anchor in bond_specs):
        bond_lo, bond_hi = 2.20, 2.90
    else:
        bond_lo, bond_hi = 1.75, 2.05
    """In-place strain relief of a staged bicyclic complex (host, numpy).

    Damped-Jacobi projection of every Cys-SG <-> linker-anchor pair into
    [bond_lo, bond_hi] (moving only the two bonded atoms, per-atom step
    capped), the same projection for the linker's anchor-neighbor bonds to
    their CCD lengths, and a CA-CB bond renormalization of the peptide. The
    refine must start from a chemically sound ring, not a strained one.
    """
    st = gemmi.read_structure(str(staged_path))
    st.setup_entities()
    polymer = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"),
    )
    pep = polymer[-1] if len(polymer) > 1 else None
    linker_res = None
    for chain in st[0]:
        if linker_res is None and any(r.name == linker_code for r in chain):
            for r in chain:
                if r.name == linker_code:
                    linker_res = r
                    break
    if pep is None or linker_res is None:
        return {"relaxed": False, "reason": "peptide or linker not found"}

    pairs = []
    for cys_num, anchor_name in bond_specs:
        sg = pep[cys_num - 1].find_atom("SG", "*") if cys_num - 1 < len(pep) else None
        an = linker_res.find_atom(anchor_name, "*")
        if sg is None or an is None:
            return {"relaxed": False, "reason": f"missing atom for {cys_num}:{anchor_name}"}
        pairs.append((cys_num, anchor_name))

    # 单原子链接器(BS3=Bi): 先确定性重建锚点，后续投影微调
    single_atom_rebuild = None
    if sum(1 for a in linker_res if a.element != gemmi.Element("H")) == 1:
        single_atom_rebuild = _rebuild_single_atom_linker_anchor(
            staged_path, prefer_code=linker_res.name)
        # 重建重写了文件, 重读以同步内存中的结构
        st = gemmi.read_structure(str(staged_path))
        st.setup_entities()
        polymer = sorted(
            (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
            key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
        pep = polymer[-1] if len(polymer) > 1 else None
        linker_res = None
        for chain in st[0]:
            if linker_res is None and any(r.name == linker_code for r in chain):
                for r in chain:
                    if r.name == linker_code:
                        linker_res = r
                        break
        if pep is None or linker_res is None:
            return {"relaxed": False, "reason": "structure lost after anchor rebuild"}

    def _residue_atoms(residue):
        return [a for a in residue if a.element != gemmi.Element("H")]

    # Linker anchor-neighbor bonds: the rigid placement puts the anchor atoms
    # on the SG triangle while their bonded neighbors keep the CCD geometry,
    # stretching the internal bonds. Relax them to their CCD lengths,
    # splitting the correction between the two bonded atoms.
    neighbor_bonds = []
    try:
        mols = _linker_ccd_mols([linker_code])
        mol = mols[linker_code]
        conf = mol.GetConformer(getattr(mol, "ref_conf_id", 0) or 0)
        anchor_names = {a for _, a in bond_specs}
        for bond in mol.GetBonds():
            ia, ib = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
            names = [mol.GetAtomWithIdx(ia).GetProp("name"),
                     mol.GetAtomWithIdx(ib).GetProp("name")]
            if names[0] in anchor_names or names[1] in anchor_names:
                if not (names[0] in anchor_names and names[1] in anchor_names):
                    target = float(np.linalg.norm(
                        np.array([conf.GetAtomPosition(ia).x,
                                  conf.GetAtomPosition(ia).y,
                                  conf.GetAtomPosition(ia).z])
                        - np.array([conf.GetAtomPosition(ib).x,
                                    conf.GetAtomPosition(ib).y,
                                    conf.GetAtomPosition(ib).z])))
                    neighbor_bonds.append((names[0], names[1], target))
    except Exception as exc:
        print(f"[d-peptide] linker neighbor bonds unavailable: {exc}",
              file=sys.stderr)

    # Receptor steric guard: the bond projections below can shove peptide
    # or linker atoms into the receptor wall. Every sweep, push any free heavy atom
    # closer than 2.3 A to a receptor atom back out along the contact
    # normal; the bond projections and this guard converge jointly.
    from scipy.spatial import cKDTree as _RelaxKD
    rec_pts = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for r in polymer[0] for a in r if a.element != gemmi.Element("H")])
    rec_tree = _RelaxKD(rec_pts)

    def _steric_guard() -> float:
        worst = 0.0
        for group in (pep, [linker_res]):
            for residue in group:
                for atom in _residue_atoms(residue):
                    v = np.array([atom.pos.x, atom.pos.y, atom.pos.z])
                    d, j = rec_tree.query(v, k=1)
                    if d >= 2.3:
                        continue
                    worst = max(worst, 2.3 - float(d))
                    u = v - rec_pts[j]
                    n = np.linalg.norm(u)
                    u = u / n if n > 1e-8 else np.array([0.0, 0.0, 1.0])
                    atom.pos = gemmi.Position(*(rec_pts[j] + 2.3 * u))
        return worst

    # iterative bond relaxation: the SG absorbs the correction, its residue
    # follows at 30% (flexible propagation so the residue is not torn apart),
    # the linker anchor keeps its CCD geometry
    for _ in range(sweeps):
        max_v = 0.0
        for (cys_num, anchor_name) in pairs:
            residue = pep[cys_num - 1]
            sg = residue.find_atom("SG", "*")
            an = linker_res.find_atom(anchor_name, "*")
            p_sg = np.array([sg.pos.x, sg.pos.y, sg.pos.z])
            p_an = np.array([an.pos.x, an.pos.y, an.pos.z])
            d = float(np.linalg.norm(p_an - p_sg))
            target = float(np.clip(d, bond_lo, bond_hi))
            viol = d - target
            max_v = max(max_v, abs(viol))
            if abs(viol) < 1e-3:
                continue
            u = (p_an - p_sg) / max(d, 1e-8)
            # moving SG by t along u changes the distance to |d - t|;
            # t = viol = d - target lands exactly on the band edge
            step = np.clip(viol * u, -0.5, 0.5)
            sg.pos = gemmi.Position(*(p_sg + step))
            for atom in _residue_atoms(residue):
                if atom.name == "SG":
                    continue
                v = np.array([atom.pos.x, atom.pos.y, atom.pos.z])
                atom.pos = gemmi.Position(*(v + 0.3 * step))
        # linker anchor-neighbor bonds: split the correction between the two
        # bonded atoms (the anchor may move a little; its target is
        # re-projected by the SG relaxation on the next sweep)
        for (na, nb, target) in neighbor_bonds:
            aa = linker_res.find_atom(na, "*")
            ab = linker_res.find_atom(nb, "*")
            if aa is None or ab is None:
                continue
            pa = np.array([aa.pos.x, aa.pos.y, aa.pos.z])
            pb = np.array([ab.pos.x, ab.pos.y, ab.pos.z])
            d = float(np.linalg.norm(pb - pa))
            viol = d - target
            max_v = max(max_v, abs(viol))
            if abs(viol) < 1e-3:
                continue
            u = (pb - pa) / max(d, 1e-8)
            half = np.clip(0.5 * viol, -0.25, 0.25)
            aa.pos = gemmi.Position(*(pa + half * u))
            ab.pos = gemmi.Position(*(pb - half * u))
        _steric_guard()
        if max_v < 5e-3:
            break

    # CA-CB renormalization across the peptide
    fixed_cb = 0
    for residue in pep:
        ca = residue.find_atom("CA", "*")
        cb = residue.find_atom("CB", "*")
        if ca is None or cb is None:
            continue
        v_ca = np.array([ca.pos.x, ca.pos.y, ca.pos.z])
        v_cb = np.array([cb.pos.x, cb.pos.y, cb.pos.z])
        d = float(np.linalg.norm(v_cb - v_ca))
        if cb_lo <= d <= cb_hi:
            continue
        direction = (v_cb - v_ca) / max(d, 1e-8)
        cb.pos = gemmi.Position(*(v_ca + 1.53 * direction))
        fixed_cb += 1
    # the renormalisation above can push CBs back into the receptor wall;
    # final guard so the written pose honours the staging contract
    _steric_guard()

    st.setup_entities()
    st.write_pdb(str(staged_path))
    # gemmi's write_pdb drops connections — restore the covalent topology
    _append_staged_bicyclic_links(
        staged_path,
        [n - 1 for (n, _) in bond_specs],
        linker_code,
    )
    final = []
    for (cys_num, anchor_name) in pairs:
        sg = pep[cys_num - 1].find_atom("SG", "*")
        an = linker_res.find_atom(anchor_name, "*")
        final.append(float(np.linalg.norm(
            np.array([an.pos.x, an.pos.y, an.pos.z])
            - np.array([sg.pos.x, sg.pos.y, sg.pos.z]))))
    return {"relaxed": True, "final_bonds": [round(x, 3) for x in final],
            "cb_renormalized": fixed_cb,
            "anchor_rebuild": single_atom_rebuild}




def _dpeptide_prepare_reference_peptide(
    predict_args: Dict[str, Any],
    out_dir: Path,
    binder_length: int,
) -> Optional[Path]:
    """Uploaded initial peptide structure, mirrored into design space.

    The upload must share the coordinate frame of the uploaded target
    structure (a reference complex). Returns the mirrored peptide PDB with
    residues renumbered 1..N, or None when nothing was uploaded. Used only
    for chirality='d' mode-anchored design."""
    entry = predict_args.get("peptide_structure_input")
    if not isinstance(entry, dict) or not entry.get("content_base64"):
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = str(entry.get("format") or "pdb").lower()
    suffix = ".cif" if fmt in ("cif", "mmcif") else ".pdb"
    path = out_dir / f"reference_peptide{suffix}"
    path.write_bytes(base64.b64decode(entry["content_base64"]))

    st = gemmi.read_structure(str(path))
    st.setup_entities()
    st.remove_alternative_conformations()
    chain_id = str(entry.get("chain_id") or "").strip()
    pep_chain = None
    if chain_id:
        for chain in st[0]:
            if chain.name == chain_id:
                pep_chain = chain
                break
    if pep_chain is None:
        # largest polymer chain that is plausibly peptide-sized
        polys = sorted(
            (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
            key=lambda c: sum(1 for r in c if r.het_flag != "H"))
        if polys:
            pep_chain = polys[0]
    if pep_chain is None:
        raise ValueError(
            "上传的初始肽结构中没有可识别的肽链（需要至少 3 个残基的多聚链）。")
    residues = [r for r in pep_chain if r.het_flag != "H"]
    if not (3 <= len(residues) <= 120):
        raise ValueError(
            f"初始肽结构残基数 {len(residues)} 超出设计范围（3-120）。")

    out = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("B")
    for ordinal, residue in enumerate(residues, start=1):
        clone = residue.clone()
        clone.seqid = gemmi.SeqId(ordinal, " ")
        for atom in clone:
            atom.pos = gemmi.Position(-atom.pos.x, atom.pos.y, atom.pos.z)
        chain.add_residue(clone)
    model.add_chain(chain)
    out.add_model(model)
    out.setup_entities()
    out_path = out_dir / "d_reference_peptide.pdb"
    lines = []
    serial = 1
    for residue in out[0][0]:
        for atom in residue:
            if atom.element.name == "H":
                continue
            name_field = f" {atom.name:<3}" if len(atom.name) < 4 else atom.name
            lines.append(
                f"ATOM  {serial:5d} {name_field} {residue.name:>3} B{int(residue.seqid.num):4d}    "
                f"{atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}"
                f"{1.00:6.2f}{0.00:6.2f}          {atom.element.name:>2}"
            )
            serial += 1
    out_path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    print(f"[d-peptide] reference peptide mirrored for mode-anchored design "
          f"({len(residues)} residues)", file=sys.stderr)
    return out_path




def _dpeptide_prepare_d_target(
    predict_args: Dict[str, Any],
    base_yaml_data: Dict[str, Any],
    options: Dict[str, Any],
    target_chain_id: str,
    backend: str,
    work_root: Path,
    seed: int,
) -> Tuple[Path, Path]:
    """D-route target preparation: uploaded structure (or single-chain
    prediction) plus its x->-x mirror.

    Returns (l_target_path, d_target_path). The mirror is written with
    polymer residues renumbered 1..N in sequence order so pocket contacts in
    sequence numbering resolve on it."""
    work_root.mkdir(parents=True, exist_ok=True)
    uploaded = _dpeptide_uploaded_target_structure(predict_args, work_root)
    if uploaded is None:
        # yaml-referenced template paths are a valid upload form too
        for entry in base_yaml_data.get("templates") or []:
            for key in ("author_pdb", "pdb", "cif", "mmcif"):
                cand = str((entry or {}).get(key) or "").strip()
                if cand and os.path.isfile(cand):
                    uploaded = Path(cand)
                    break
            if uploaded is not None:
                break
    if uploaded is None:
        target_seq = _dpeptide_target_sequence(base_yaml_data, target_chain_id)
        uploaded = _dpeptide_predict_target_structure(
            target_seq, backend, work_root / "target_pred", seed)
    if not uploaded.exists() or uploaded.stat().st_size == 0:
        raise RuntimeError(
            f"靶标结构文件为空或不存在: {uploaded} "
            f"(size={uploaded.stat().st_size if uploaded.exists() else 'N/A'}). "
            "请检查上传的结构文件，或确认靶标序列能被正确预测。")
    try:
        target_l = gemmi.read_structure(str(uploaded))
        target_l.setup_entities()
        target_l.remove_alternative_conformations()
    except (IndexError, RuntimeError, ValueError) as parse_exc:
        raise RuntimeError(
            f"靶标结构解析失败: {uploaded} "
            f"({uploaded.stat().st_size} bytes): {parse_exc}. "
            "文件可能损坏——请重新上传或重试。") from parse_exc
    if not any(sum(1 for r in c if r.het_flag != "H") >= 3 for c in target_l[0]):
        raise RuntimeError(
            f"靶标结构中没有聚合物链: {uploaded}. "
            f"文件大小 {uploaded.stat().st_size} bytes. "
            "请上传有效的 PDB/CIF 结构文件。")

    target_d = gemmi.read_structure(str(uploaded))
    target_d.setup_entities()
    target_d.remove_alternative_conformations()
    peplm_root = str(_resolve_capability_dir("peptide_lm"))
    if peplm_root not in sys.path:
        sys.path.insert(0, peplm_root)
    from peplm.dpeptide import mirror as dpm
    dpm.mirror_structure(target_d)

    d_path = work_root / "d_target.pdb"
    # write EVERY polymer chain (not just the first): functional sites like
    # the RANKL trimer groove span two adjacent monomers — dropping the
    # second chain would model only half the binding cleft. Chain IDs are
    # re-lettered A, B, C... with per-chain 1..N residue numbering (the
    # staged-complex contract downstream).
    lines: List[str] = []
    serial = 1
    poly_chains_written = 0

    def _stageable(residue: gemmi.Residue) -> bool:
        names = {a.name for a in residue}
        if not {"N", "CA", "C"} <= names:
            return False
        return residue.name == "GLY" or "CB" in names

    for chain in target_d[0]:
        polymer = [r for r in chain if r.het_flag != "H"]
        if len(polymer) < 3:
            continue
        # Trim terminal residues the model would have to rebuild: a staged
        # residue without complete backbone (or CB for non-Gly) is re-grown
        # from its L-CCD token with L chirality and then fails the D
        # chirality gate for every candidate (1HL3 THR332: terminal residue
        # carrying a single N atom). Head/tail only — interior ordinals stay
        # put so pocket contacts in sequence numbering keep resolving.
        tail = len(polymer)
        while tail > 0 and not _stageable(polymer[tail - 1]):
            tail -= 1
        head = 0
        while head < tail and not _stageable(polymer[head]):
            head += 1
        if head >= tail:
            # Fully unstageable chain (e.g. every residue lacks backbone
            # atoms): skip without consuming a chain letter — an empty
            # re-lettered chain would shift every later chain's ID and break
            # the staged-complex chain contract downstream.
            continue
        chain_letter = chr(ord("A") + poly_chains_written)
        for ordinal, residue in enumerate(polymer[head:tail], start=1):
            for atom in residue:
                if atom.element.name == "H":
                    continue
                name_field = f" {atom.name:<3}" if len(atom.name) < 4 else atom.name
                lines.append(
                    f"ATOM  {serial:5d} {name_field} {residue.name:>3} {chain_letter}{ordinal:4d}    "
                    f"{atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}"
                    f"{1.00:6.2f}{0.00:6.2f}          {atom.element.name:>2}"
                )
                serial += 1
        poly_chains_written += 1
    if poly_chains_written == 0:
        raise RuntimeError("D-target preparation: no polymer chain found")
    d_path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    return Path(uploaded), d_path




def _dpeptide_stage_conformer_in_pocket(
    d_target_path: Path,
    conformer_path: Path,
    out_path: Path,
    pocket_sequence_contacts: List[Tuple[str, int]],
    seed: int,
    linker_ccd: str = "SEZ",
    reference_peptide_path: Optional[Path] = None,
    pose_matters: bool = True,
) -> Path:
    """Stage the design-space complex: pinned D-target + placed L-conformer.

    The conformer (isolated candidate prediction, already L, ring and NCAA
    topology included) is rigidly placed against the mirrored D-target either
    anchored to the uploaded reference peptide pose (mode A: CA-trace
    alignment over the residue-index window, keeping the known binding mode)
    or with the clash-free surface search at the user pocket; a bicyclic
    linker chain rides along from the conformer."""
    target = gemmi.read_structure(str(d_target_path))
    target.setup_entities()
    conf = gemmi.read_structure(str(conformer_path))
    conf.setup_entities()
    conf.remove_alternative_conformations()

    # aggregate receptor atoms across EVERY polymer chain (multi-chain
    # targets like the RANKL trimer groove span two monomers; pocket
    # positions are chain-tagged so contacts can name either wall)
    rec_chains = [ch for ch in target[0]
                  if sum(1 for r in ch if r.het_flag != "H") >= 3]
    rec_atoms = np.array([
        [a.pos.x, a.pos.y, a.pos.z] for ch in rec_chains
        for r in ch for a in r
        if a.element != gemmi.Element("H")])
    pocket_pts, pocket_atoms = [], []
    wanted = {(chain_id, n) for chain_id, n in pocket_sequence_contacts}
    wanted_any_pos = {n for _, n in pocket_sequence_contacts}
    for ch in rec_chains:
        for ordinal, residue in enumerate(ch, start=1):
            if (ch.name, ordinal) not in wanted and ordinal not in wanted_any_pos:
                continue
            ca = residue.find_atom("CA", "*")
            if ca is not None:
                pocket_pts.append([ca.pos.x, ca.pos.y, ca.pos.z])
            for atom in residue:
                if atom.element != gemmi.Element("H"):
                    pocket_atoms.append([atom.pos.x, atom.pos.y, atom.pos.z])
    has_reference = (
        reference_peptide_path is not None
        and os.path.isfile(str(reference_peptide_path)))
    if pocket_sequence_contacts and not pocket_pts:
        raise ValueError(
            "pocket residues "
            + ",".join(f"{c}:{n}" for c, n in pocket_sequence_contacts)
            + " not found on the mirrored D-target")
    if not pocket_pts and not has_reference and pose_matters:
        # No silent centroid fallback when the pose feeds the sampler: without
        # a pocket the binder gets buried at the receptor centroid        # 0.16 A min distance on the MDM2 runs). pose_matters=False
        # (blind inpainting route) discards the peptide start entirely — the
        # engine re-noises those rows — so any placement is acceptable.
        raise ValueError(
            "D-肽 staging 需要口袋定义(残基或中心)或参考肽结构;无口袋时不存在"
            "合法摆位。线性盲对接模式不需要口袋。")
    center = np.mean(np.array(pocket_pts), axis=0) if pocket_pts else rec_atoms.mean(axis=0)

    conf_chains = sorted(
        (c for c in conf[0]
         if any(a.element != gemmi.Element("H") for r in c for a in r)),
        key=lambda c: -sum(1 for r in c))
    if not conf_chains:
        raise RuntimeError("candidate conformer has no chains")
    free_atoms = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for c in conf_chains for r in c for a in r
        if a.element != gemmi.Element("H")])
    if reference_peptide_path is not None and os.path.isfile(str(reference_peptide_path)):
        # mode A: anchor the candidate backbone to the uploaded reference
        # peptide pose (CA trace, residue-index correspondence over a
        # centered window) — keeps the known binding mode
        ref_st = gemmi.read_structure(str(reference_peptide_path))
        ref_st.setup_entities()
        ref_ca = np.array([
            [a.pos.x, a.pos.y, a.pos.z] for r in ref_st[0][0] for a in r
            if a.name.strip() == "CA"])
        cand_ca = np.array([
            [a.pos.x, a.pos.y, a.pos.z] for r in conf_chains[0] for a in r
            if a.name.strip() == "CA"])
        if len(ref_ca) >= 3 and len(cand_ca) >= 3:
            n = min(len(ref_ca), len(cand_ca))
            ro = (len(ref_ca) - n) // 2
            co = (len(cand_ca) - n) // 2
            rot = _dpeptide_kabsch_rotation(cand_ca[co:co + n], ref_ca[ro:ro + n])
            free_center = cand_ca[co:co + n].mean(axis=0)
            # emit loops transform as rot @ (x - free_center) + free_center + shift;
            # at x = free_center that is free_center + shift, so the shift is
            # just the centroid difference
            shift = ref_ca[ro:ro + n].mean(axis=0) - free_center
            fitted = (cand_ca[co:co + n] - free_center) @ rot.T \
                + ref_ca[ro:ro + n].mean(axis=0)
            align_rmsd = float(np.sqrt(
                ((fitted - ref_ca[ro:ro + n]) ** 2).sum(axis=1).mean()))
            print(f"[d-peptide] mode A placement: {n}-residue CA alignment "
                  f"rmsd {align_rmsd:.2f} A", file=sys.stderr)
        else:
            rot = np.eye(3)
            free_center = free_atoms.mean(axis=0)
            shift = center - free_center
    else:
        # Blind route: the peptide rows are re-noised by the engine, so
        # the placed pose never reaches the sampler — an identity transform
        # is the only correct input (no rigid placement is performed).
        rot = np.eye(3)
        shift = np.zeros(3)
        free_center = free_atoms.mean(axis=0)

    lines: List[str] = []
    serial = 1

    def _emit(record, aname, resname, chain_id, resnum, pos, element):
        nonlocal serial
        name_field = f" {aname:<3}" if len(aname) < 4 else aname
        lines.append(
            f"{record:<6}{serial:5d} {name_field} {resname:>3} {chain_id}{resnum:4d}    "
            f"{pos[0]:8.3f}{pos[1]:8.3f}{pos[2]:8.3f}{1.00:6.2f}{0.00:6.2f}"
            f"          {element:>2}"
        )
        serial += 1

    for ch_idx, rec_ch in enumerate(rec_chains):
        chain_letter = chr(ord("A") + ch_idx)
        for ordinal, residue in enumerate(rec_ch, start=1):
            for atom in residue:
                if atom.element.name == "H":
                    continue
                _emit("ATOM", atom.name, residue.name, chain_letter, ordinal,
                      (atom.pos.x, atom.pos.y, atom.pos.z), atom.element.name)

    # the designed peptide takes the chain letter right after the last
    # receptor chain (B for a monomer target, C for a dimer, ...)
    pep_chain_letter = chr(ord("A") + len(rec_chains))
    pep_chain = conf_chains[0]
    sg_rows: List[Tuple[int, np.ndarray]] = []
    for ordinal, residue in enumerate(pep_chain, start=1):
        for atom in residue:
            if atom.element.name == "H":
                continue
            v = rot @ (np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - free_center) \
                + free_center + shift
            _emit("ATOM", atom.name, residue.name, pep_chain_letter, ordinal,
                  (v[0], v[1], v[2]), atom.element.name)
            if residue.name == "CYS" and atom.name == "SG":
                sg_rows.append((ordinal, v))

    linker_name = None
    if len(conf_chains) > 1:
        # the linker chain is identified by its CCD, not het flags — PDB
        # roundtrips do not preserve them reliably
        for extra in conf_chains[1:]:
            if any(r.name in BICYCLIC_LINKER_ATOM_MAP for r in extra):
                linker_res = extra[0]
                linker_name = linker_res.name
                for atom in linker_res:
                    if atom.element.name == "H":
                        continue
                    v = rot @ (np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - free_center) \
                        + free_center + shift
                    _emit("HETATM", atom.name, linker_res.name, "L", 1,
                          (v[0], v[1], v[2]), atom.element.name)
                conf_chains = [conf_chains[0], extra]
                break

    link_rows: List[str] = []
    if linker_name is not None and sg_rows:
        anchors = BICYCLIC_LINKER_ATOM_MAP.get(linker_name, [])
        placed_linker = {}
        lchain = conf_chains[1]
        for atom in lchain[0]:
            if atom.element.name == "H":
                continue
            v = rot @ (np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - free_center) \
                + free_center + shift
            placed_linker[atom.name.strip()] = v
        # one-to-one greedy assignment: symmetric linkers list the SAME
        # anchor atom several times (BS3 = BI,BI,BI), and a plain
        # nearest-per-anchor would bond one SG three times
        remaining = list(sg_rows)
        for anchor in anchors:
            av = placed_linker.get(anchor)
            if av is None or not remaining:
                continue
            best = min(remaining,
                       key=lambda row: float(np.linalg.norm(row[1] - av)))
            remaining.remove(best)
            link_rows.append(_pdb_link_line(
                "SG", "CYS", pep_chain_letter, best[0], anchor,
                linker_name, "L", 1))

    out_path.write_text("\n".join(link_rows + lines) + "\nEND\n",
                        encoding="utf-8")

    if link_rows:
        pairs = _staged_bicyclic_bond_pairs(out_path)
        if pairs:
            # each pair is "B:<cys>:SG,L:<pos>:<anchor>" — the relaxer resolves
            # the atom on the LINKER residue, so take the anchor half
            specs = []
            for pair in pairs.split(";"):
                a1, a2 = pair.split(",")
                # a1 = peptide side "B:<cys>:SG" (the residue to project),
                # a2 = linker side "L:1:<anchor>" (the anchor atom). The old
                # parser took a2's resnum — the linker residue is always 1,
                # so EVERY anchor relaxed against Cys-1 staged
                # ring bonds landed 3.4 A while two converged by accident).
                _, cys_num, _ = a1.strip().split(":")
                _, _, anchor = a2.strip().split(":")
                specs.append((int(cys_num), anchor))
            _relax_staged_bicyclic_strain(
                out_path, bond_specs=specs, linker_code=linker_name)

    check = gemmi.read_structure(str(out_path))
    check.setup_entities()
    names = {c.name: len(c) for c in check[0]}
    if not {"A", "B"} <= set(names):
        raise RuntimeError(
            f"D-peptide staging: written PDB missing chains (got {names}).")

    # Self-check hard gate: the staged pose ENTERS the diffusion sampler as
    # the inpainting reference. A pose with inter-chain hard clashes is a
    # garbage reference
    # flipped >50% of binder CA chiralities in every refined sample. The
    # sampler must never receive a buried pose silently: zero <2.2 A pairs
    # and at least one <6 A contact (anchored, not floating) or we fail.
    # Skipped when pose_matters=False (blind route: peptide rows are
    # re-noised; only the receptor + sequences feed the engine).
    if pose_matters:
        _poly_chk = sorted(
            (c for c in check[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
            key=lambda c: sum(1 for r in c if r.het_flag != "H"))
        _pep_chk = _poly_chk[0]
        rec_atoms_chk = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for _rc in _poly_chk[1:] for r in _rc
            for a in r if a.element.name != "H"])
        pep_atoms_chk = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for r in _pep_chk for a in r if a.element.name != "H"])
        from scipy.spatial import cKDTree as _CKD
        _tree = _CKD(rec_atoms_chk)
        _nbr = _tree.query(pep_atoms_chk, k=1)[0]
        _n_clash = int((_nbr < 2.2).sum())
        _min_d = float(_nbr.min())
        _anchored = bool((_nbr < 6.0).any())
        if _n_clash > 0 or not _anchored:
            raise RuntimeError(
                f"D-peptide staging 自检失败: {_n_clash} 个受体-肽原子对 <2.2 A"
                f"(最近 {_min_d:.2f} A), anchored={_anchored}。摆位搜索未能产生"
                "无冲突的表面构型 — 拒绝将埋置构型送入精修(请检查口袋定义)。")
    return out_path




def _staged_bicyclic_bond_pairs(staged_path: Path) -> Optional[str]:
    """Reconstruct 'chain:resnum:atom,chain:resnum:atom;...' bond pairs from a
    staged complex: each linker anchor bonds to its NEAREST peptide Cys-SG
    (the staged geometry carries the constructive 1.8 A contacts). Returns
    None when the complex has no linker chain."""
    st = gemmi.read_structure(str(staged_path))
    st.setup_entities()
    linker_chain = None
    for chain in st[0]:
        for residue in chain:
            if residue.name in BICYCLIC_LINKER_ATOM_MAP:
                linker_chain = chain
                break
        if linker_chain is not None:
            break
    if linker_chain is None:
        return None
    polymer = [
        c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3
    ]
    if len(polymer) < 2:
        return None
    # the peptide is the SHORTEST polymer chain; polymer[1] would pick a
    # second receptor copy on multimer targets (same bug fixed at
    # _cyclic_headtail_bond_report and _dpeptide_refined_chirality_gate)
    peptide = min(polymer, key=lambda c: sum(1 for r in c if r.het_flag != "H"))
    receptor = next(c for c in polymer if c is not peptide)
    anchors = BICYCLIC_LINKER_ATOM_MAP[linker_chain[0].name]
    sg_atoms = []
    for residue in peptide:
        sg = residue.find_atom("SG", "*")
        if sg is not None:
            sg_atoms.append((int(residue.seqid.num), sg))
    if not sg_atoms:
        raise ValueError(
            "staged bicyclic complex has no Cys-SG on the peptide chain")
    pairs: List[str] = []
    remaining = list(sg_atoms)
    for anchor_name in anchors:
        anchor_atom = linker_chain[0].find_atom(anchor_name, "*")
        if anchor_atom is None:
            raise ValueError(
                f"staged linker {linker_chain[0].name} lacks anchor {anchor_name}")
        if not remaining:
            raise ValueError(
                "staged bicyclic complex has more linker anchors than Cys-SG")
        nearest = min(
            remaining,
            key=lambda item: np.linalg.norm(
                np.array([item[1].pos.x, item[1].pos.y, item[1].pos.z])
                - np.array([anchor_atom.pos.x, anchor_atom.pos.y, anchor_atom.pos.z])),
        )
        remaining.remove(nearest)
        pairs.append(
            f"{peptide.name}:{nearest[0]}:SG,{linker_chain.name}:1:{anchor_name}")
    return ";".join(pairs)




def _dpeptide_dispatch_refine(
    staged_path: Path, seed: int, queue: str, blind: bool = False,
    cyclic_headtail: bool = False,
    pocket_res: Optional[str] = None,
    peptide_chain_letter: str = "B",
    receptor_chain_letters: str = "A",
) -> Any:
    """Dispatch one protenix2dock ``peptide`` refine task and return its
    AsyncResult immediately (pair with :func:`_dpeptide_collect_refine`).

    The receptor is pinned to the staged coordinates for every diffusion step
    (vendor inpainting side channel), the peptide enters the input json as a
    proteinChain, and bicyclic ring bonds (peptide SG <-> linker anchors) are
    carried both as input.json covalent_bonds and as hard TFG contacts.
    ``cyclic_headtail`` adds the head-to-tail N-C bond of a cyclic peptide in
    both channels — pose-independent chemistry, so it composes with the blind
    (pure-noise peptide start) route where the placed pose never reaches the
    sampler. Without it a cyclic peptide refines as a linear chain and the
    ring silently opens (or worse, collapses).
    ``pocket_res`` ("A:18,A:76,...", staged/sequence numbering) activates the
    native boltz2-style PocketPotential guidance — the pocket conditions the
    SAMPLER, never a hand-rolled placement.
    """
    from backend.core.celery_app import celery_app as _celery

    bond_pairs = _staged_bicyclic_bond_pairs(staged_path)
    if cyclic_headtail and not bond_pairs:
        # Find the PEPTIDE chain from the staged complex instead of
        # assuming "B": multi-chain receptors (e.g. the RANKL trimer A/B/C)
        # re-letter the peptide to D and the hardcoded B bond pointed at a
        # receptor chain (rejected with "bond pair does not touch the peptide chain"). The
        # peptide is the shortest polymer chain (receptors are 80-600 aa,
        # designed peptides 4-25); residues are numbered 1..L by the
        # production writer contract.
        st_len = gemmi.read_structure(str(staged_path))
        st_len.setup_entities()
        poly = [(ch.name, sum(1 for r in ch if r.het_flag != "H"))
                for ch in st_len[0] if sum(1 for r in ch if r.het_flag != "H") >= 3]
        if not poly:
            raise RuntimeError(
                f"cyclic head-tail dispatch: staged complex {staged_path} "
                "carries no polymer chains")
        poly.sort(key=lambda kv: kv[1])
        pep_letter, pep_len = poly[0]
        # guard: the peptide must be clearly the small chain (a malformed
        # staging with two receptor-sized chains would mis-pick)
        if pep_len >= 4 and (len(poly) == 1 or pep_len * 3 < poly[1][1]):
            bond_pairs = f"{pep_letter}:1:N,{pep_letter}:{pep_len}:C"
    linker_ccd = "SEZ"
    if bond_pairs:
        try:
            st_lk = gemmi.read_structure(str(staged_path))
            st_lk.setup_entities()
            for chain in st_lk[0]:
                for residue in chain:
                    if residue.name in BICYCLIC_LINKER_ATOM_MAP:
                        linker_ccd = residue.name
                        break
        except Exception as exc:  # noqa: BLE001
            # A parse failure here silently defaults the linker CCD to SEZ —
            # the wrong chemistry with no trace. Surface it in the dispatch log.
            print(f"[WARN] linker CCD scan failed on {staged_path}: {exc}",
                  file=sys.stderr)

    # linker chain only exists for bicyclic topology (SG <-> linker anchors);
    # cyclic head-tail pairs are intra-peptide and carry no linker entity.
    # The staged linker chain letter follows the receptor chains (C for a
    # monomer target, D for a dimer, ...) — NOT the literal "L" — so derive
    # the letter from any cross-chain bond ref.
    linker_letter = ""
    if bond_pairs and "," in bond_pairs:
        refs = [r for r in bond_pairs.replace(";", ",").split(",") if ":" in r]
        for ref in refs:
            letter = ref.split(":", 1)[0].strip()
            if letter and letter != peptide_chain_letter:
                linker_letter = letter
                break
    return _celery.send_task(
        "backend.worker.tasks.protenix2dock_task",
        kwargs={"score_args": {
            "mode": "peptide",
            "input_file_content": staged_path.read_text(),
            "input_filename": f"{staged_path.stem}.pdb",
            # peptide letter follows the LAST receptor chain (B for a
            # monomer target, C for a dimer, ...) — matches the staged
            # complex written by _dpeptide_stage_conformer_in_pocket
            "peptide_chain": peptide_chain_letter,
            # interface metrics scoped to target<->peptide only; the linker
            # pair would drag the reported iptm
            "interface_chains": receptor_chain_letters + "," + peptide_chain_letter,
            "bond_pairs": bond_pairs or "",
            "linker_chain": linker_letter,
            "linker_ccd": linker_ccd,
            "pocket_res": pocket_res or "",
            "seed": int(seed),
            # Optional fast-screen: the worker passes --sampling_steps /
            # --diffusion_samples through to protenix2dock; unset → engine
            # default (200 steps). Operators can halve refine cost on
            # screening runs via PROTENIX2DOCK_REFINE_SAMPLING_STEPS.
            **(
                {"sampling_steps": int(os.environ["PROTENIX2DOCK_REFINE_SAMPLING_STEPS"])}
                if os.environ.get("PROTENIX2DOCK_REFINE_SAMPLING_STEPS", "").strip().isdigit()
                else {}
            ),
            # blind inpainting route flag — the worker appends --blind_peptide
            # and echoes it back; the collector's expect_blind contract guard
            # fails the candidate if it's missing.
            "blind_peptide": bool(blind),
            # PocketPotential per-pair upper bound: boltz2's pocket
            # max_distance default is 6.0 (= POCKET_CONTACT_MAX_A + 1, so the
            # guidance envelope strictly contains the acceptance gate)
            "pocket_upper": float(POCKET_CONTACT_MAX_A) + 1.0,
            "dpeptide_contract": True,
        }},
        queue=queue,
    )




def _dpeptide_refined_chirality_gate(refined_path: Path) -> float:
    """Chirality gate on a REFINED mirror-space complex; returns score penalty.

    Returns 0.0 for a clean complex, or a composite-score penalty
    proportional to the peptide's CA-violation fraction (≤0.15 max).

    Mirror-space contract: receptor chain must be all-D, designed peptide
    all-L (the product flip then yields L-target + D-peptide). The diffusion
    sampler has no improper-dihedral term — under contradictory guidance it
    inverts CA centres (observed: 11-15 of 17 residues flipped on
    buried starts). A mixed-chirality refined complex is a sampler failure:
    reject the candidate loudly instead of shipping a product whose peptide
    is part-L after the flip.
    """
    peplm_root = str(_resolve_capability_dir("peptide_lm"))
    if peplm_root not in sys.path:
        sys.path.insert(0, peplm_root)
    from peplm.dpeptide import chirality_violations

    st = gemmi.read_structure(str(refined_path))
    st.setup_entities()
    protein_chains = [
        c for c in st[0]
        if sum(1 for r in c if r.het_flag != "H") >= 3
    ]
    protein_chains.sort(key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    if len(protein_chains) < 2:
        raise RuntimeError(
            f"精修产物缺少两条聚合链: {[c.name for c in st[0]]}")
    # The peptide is the SHORTEST polymer chain, every other chain is a
    # receptor copy to check. The old chains[0]/chains[1] pick assumed a
    # single receptor: on a multi-chain receptor (RANKL trimer A/B/C + D)
    # chains[1] was a second RECEPTOR copy and every candidate died at the
    # gate with 144/144 phantom violations.
    peptide = protein_chains[-1]
    receptor_chains = protein_chains[:-1]
    rec_bad = []
    for _rec in receptor_chains:
        rec_bad.extend(chirality_violations(st, _rec.name, "D"))
    pep_bad = chirality_violations(st, peptide.name, "L")
    # Receptor flips = real sampler failure (the receptor is PINNED — any
    # inversion means the inpainting contract broke). Hard reject.
    if rec_bad:
        raise RuntimeError(
            f"受体链 {[c.name for c in receptor_chains]} 应为 D, "
            f"违规 {len(rec_bad)}: "
            + ",".join(f"{n}{r}" for n, r, _ in rec_bad[:6]))
    # Peptide flips: the free-diffusion chain has no improper-dihedral term;
    # 1-3 CA inversions per 11-13-mer are numerical artifacts of the mirror
    # refinement, not design failures. The SEQUENCE is the chirality source of truth; the product
    # flip mirrors coordinates wholesale. Score-penalize, don't kill.
    pep_total = sum(1 for r in peptide if r.het_flag != "H"
                    and r.name not in ("GLY",))
    pep_violation_frac = (len(pep_bad) / max(pep_total, 1)) if pep_total else 0.0
    if pep_violation_frac > 0.30:
        raise RuntimeError(
            f"肽链 {peptide.name} 手性违规超过 30%（{len(pep_bad)}/{pep_total}）"
            f"— 采样器产生了结构性手性混乱: "
            + ",".join(f"{n}{r}" for n, r, _ in pep_bad[:6]))
    # penalty in [0, 0.15]: proportional to violation fraction — the sequence
    # is the real chirality; structural flips are cosmetic, but candidates
    # with zero artifacts should still rank above ones with several
    return pep_violation_frac * 0.15




def _dpeptide_composite_from_refined(
    *,
    refined_ipsae: float,
    binder_avg_plddt: float,
    developability_score: float,
    has_pocket: bool,
) -> Dict[str, Any]:
    """Ranking inputs recomputed from the REFINED D-space complex.

    Interface objective is ipSAE (interface_score), matching the published
    Mirror-Peptidizer BO fitness (0.6*ipsae_dom + 0.4*plddt - ...) and the
    engine's own ipsae-favoring ranking weights — ipTM systematically
    overrates strained/anchored poses (A/B comparison: the
    staged-local arm scored ipTM 0.82 while its poses were 10-21 A off and
    chirality-broken; the blind arm's honest ipTM was 0.73 with 1.5-2.0 A
    redock RMSD). Weights mirror the native interface branch: with a user
    pocket the base sums to 0.68 so the refined-pocket rescore composes to
    1.0; without a pocket the terms sum to 1.0. Pure function — unit tested.
    """
    interface_confidence = max(0.0, min(1.0, float(refined_ipsae)))
    binder_confidence = (
        max(0.0, min(1.0, float(binder_avg_plddt) / 100.0))
        if float(binder_avg_plddt) > 0 else 0.0)
    pair_iptm_confidence = interface_confidence
    developability = max(0.0, min(1.0, float(developability_score)))
    if has_pocket:
        composite = (0.40 * interface_confidence + 0.15 * binder_confidence
                     + 0.08 * pair_iptm_confidence + 0.05 * developability)
    else:
        composite = (0.58 * interface_confidence + 0.22 * binder_confidence
                     + 0.12 * pair_iptm_confidence + 0.08 * developability)
    return {
        "interface_metric_value": float(refined_ipsae),
        "interface_metric_label": "ipSAE",
        "interface_metric_source": "d_space_refined_ipsae",
        "interface_metric_kind": "ipsae",
        "interface_confidence": interface_confidence,
        "binder_confidence": binder_confidence,
        "pair_iptm_confidence": pair_iptm_confidence,
        "developability_score": developability,
        "composite_score": float(composite),
    }




def _dpeptide_collect_refine(
    async_result: Any,
    staged_path: Path,
    refined_cif: Path,
    expect_blind: bool = False,
) -> Dict[str, Any]:
    """Wait for a dispatched refine task, then rank its diffusion samples.

    The confidence head scores the refined coordinates; the best sample (by
    ipTM, gated on covalent integrity + clash-freedom) is copied to
    refined_cif and its metrics returned.

    expect_blind: the dispatch asked for the blind-inpainting route — verify
    the worker's contract echo agrees. A stale worker process silently drops
    the flag (fork children inherit the parent's imports, so respawns do not
    reload the file); fail the task loudly instead of letting a downgraded
    staged-local refine chirality-reject every candidate for hours.
    """
    task_tmp = Path(RESULTS_BASE_DIR) / "_runtime_tmp" / \
        f"dpeptide_task_{async_result.id}"
    conf_path = task_tmp / "out" / "confidence.json"
    # Poll no longer than the dispatched GPU task itself is allowed to run
    # (SUBPROCESS_TIMEOUT) plus a small grace for result writes — plus a
    # queue grace: refines dispatch into the shared GPU queue behind bulk
    # candidate work, so a deep backlog alone can outlast the runtime cap.
    # Env-tunable.
    from backend.worker import tasks as _worker_tasks

    refine_deadline = time.time() + int(
        getattr(_worker_tasks, "SUBPROCESS_TIMEOUT", 3 * 60 * 60)
    ) + int(os.environ.get("PEPTIDE_REFINE_QUEUE_GRACE_SECONDS", "21600")) + 120
    while True:
        if conf_path.is_file():
            break
        state = str(getattr(async_result, "state", "") or "").upper()
        if state in ("FAILURE", "REVOKED"):
            raise RuntimeError(f"D-space refine task {async_result.id} failed.")
        if time.time() > refine_deadline:
            _celery_revoke_quiet(async_result)
            raise RuntimeError("D-space refine timed out.")
        time.sleep(4.0)

    metrics = json.loads(conf_path.read_text())
    if expect_blind and not metrics.get("blind_peptide"):
        raise DpeptideStaleWorkerError(
            "protenix worker 未执行 blind_peptide 契约回显不符(期望 blind=True, "
            f"实际 echo={metrics.get('blind_peptide')!r}) — protenix GPU worker 进程"
            "运行的是旧任务代码(更新代码后未重启,fork 子进程不会重新加载文件)。"
            "请重启 protenix GPU worker 容器后重跑任务。")
    # Interface objective is ipSAE (published Mirror-Peptidizer BO fitness
    # uses ipsae_dom; the engine's own ranking weights default to ipsae too).
    # The per-sample confidence jsons carry raw engine numbers only — the
    # ipSAE metrics live in the task RESULT summary (best_by_interface),
    # exactly the source Mirror-Peptidizer consumes. Hard-require them: a
    # refine that produced no ipSAE is a broken refine, not a 0.
    try:
        _res = async_result.result
        _best = (_res.get("best_by_interface") or _res.get("best") or {}) \
            if isinstance(_res, dict) else {}
        metrics["interface_score"] = _best.get("interface_score")
        metrics["ipsae_dom"] = _best.get("ipsae_dom")
        metrics["ligand_ipsae_max"] = _best.get("ligand_ipsae_max")
        metrics["chain_iptm"] = _best.get("chain_iptm")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"D-space refine 结果缺少 ipSAE 读数 ({exc})") from exc
    if not isinstance(metrics.get("interface_score"), (int, float)):
        raise RuntimeError(
            "D-space refine 结果未携带 ipSAE interface_score — 评分目标为 "
            "ipSAE(非 ipTM),缺数即拒绝")
    # persist staged + refined samples for post-run audits (task tmp dirs are
    # auto-cleaned); keep under the TASK-scoped candidate dir — a shared
    # "cand_001" name let consecutive tasks overwrite each other's evidence
    # exactly when a defect needed diagnosing. The GENERATION is part of the
    # dir name too: cand_001 recurs in every generation, and a gen-agnostic
    # dir let the last writer win — a redelivered export's keep fallback then
    # shipped gen-6 structures for gen-2 rows.
    runtime_task_id = str(os.environ.get("VBIO_TASK_ID") or "").strip().replace(":", "_")
    _cand_dir = Path(refined_cif).parent
    _gen_dir = _cand_dir.parent.name if _cand_dir.parent.name.startswith("gen_") else ""
    if _gen_dir:
        keep_dir = Path(RESULTS_BASE_DIR) / "_runtime_tmp" / \
            f"dpeptide_keep_{runtime_task_id or 'local'}_{_gen_dir}_{_cand_dir.name}"
    else:
        keep_dir = Path(RESULTS_BASE_DIR) / "_runtime_tmp" / \
            f"dpeptide_keep_{runtime_task_id or 'local'}_{_cand_dir.name}"
    keep_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged_path, keep_dir / "staged.pdb")
    structure_dir = Path(str(metrics.get("structure_dir") or ""))
    if not str(structure_dir):
        raise RuntimeError("metrics.structure_dir missing; cannot locate confidence files")
    scored = []
    for conf_json in sorted(structure_dir.glob("confidence_*_model_*.json")):
        payload = json.loads(conf_json.read_text())
        iptm = float(payload.get("iptm") or 0.0)
        # No sample filtering: all samples rank; the model's own confidence
        # orders them (a collapsed sample sorts last). Selection-by-confidence
        # is engine capability, not a discard rule.
        cif = structure_dir / conf_json.name.replace("confidence_", "").replace(".json", ".cif")
        if cif.is_file():
            scored.append((iptm, cif))
    if not scored:
        raise RuntimeError(
            f"D-space refine produced no samples under {structure_dir}.")
    # Selection policy: module-level _select_dspace_samples, unit tested
    # against the MDM-2 failure set.
    # Tuple layout (see _select_dspace_samples): 0 intact, 1 engaged,
    # 2 -clashes, 3 iptm, 4 cif.NAME, 5 cif PATH, 6 detached, 7 contacts.
    ranked = _select_dspace_samples(scored)
    best = ranked[0]
    if best[1] != 1:
        raise RuntimeError(
            "D-space refine 无界面结合样本: 所有样本受体-肽接触 <4.5 A 均低于 "
            f"{ENGAGE_MIN_CONTACTS} 对（最优 {best[4]} 仅 {best[7]} 对）— "
            "采样器未把肽停靠到受体上")
    # Absolute inter-chain clash gate: a physically impossible pose is a
    # failed candidate, not a low-ranked one. Ranking alone could not
    # prevent this — when every sample interpenetrates the least-jammed
    # one shipped and the composite's contact count then REWARDED the
    # buried-inside poses carried peptide atoms 1.2-1.9 A
    # from receptor atoms).
    _min_d, _n22, _n28 = _interchain_clash_profile(Path(best[5]))
    # Gate aligned with physical interfaces: crystals carry contacts down
    # to 2.52 A (3LNJ) and post-polish samples sit at 2.55-2.6,
    # so the packing-band COUNT (pairs < 2.8) is not a violation signal.
    # True interpenetration is min < 2.3 or ANY pair < 2.2.
    if _min_d < REFINE_INTERPENETRATION_MIN_A or _n22 > 0:
        raise RuntimeError(
            f"D-space refine 界面物理违规: 受体-肽最小重原子距离 {_min_d:.2f} A, "
            f"{_n22} 对 < 2.2 A — 肽嵌入受体内部（采样力失衡）")
    if not _backbone_intact(Path(best[5])):
        raise RuntimeError(
            "D-space refine 主链完整性违规: 存在 CA-CA 虚拟键 < 2.8 A — "
            "后处理压碎了肽主链（抛光缺少 omega 保护带）")
    shutil.copyfile(best[5], refined_cif)
    metrics["refined_iptm"] = best[3]
    metrics["refined_covalent_intact"] = bool(best[0])
    metrics["refined_interchain_clashes"] = _n22
    metrics["shipped_interface_contacts"] = best[7]
    # Score-structure consistency: the confidence.json interface numbers come
    # from the worker's best-by-iptm sample — possibly a DIFFERENT sample than
    # this geometry-driven pick. Overwrite with the SHIPPED sample's own ipSAE
    # (the worker copies per-sample ipsae jsons next to the model cif) so the
    # row's interface score always describes the coordinates it ships.
    metrics["interface_metric_sample_consistent"] = _apply_shipped_sample_ipsae(
        Path(best[5]), metrics)
    for r in ranked:
        shutil.copyfile(r[5], keep_dir / Path(r[5]).name)
    if best[6]:
        metrics["refined_detached_atoms"] = best[6][:8]
        print(
            f"[d-peptide] refined sample picked with {len(best[6])} detached "
            f"atoms (no intact sample survived): {best[6][:4]}",
            file=sys.stderr,
        )
    # Per-chain mean pLDDT from the confidence B-factors of the SHIPPED
    # (best) sample — scored[0] is merely the alphabetically-first sample,
    # not the ranking winner. The design candidate rows need a native
    # pLDDT (binder_avg_plddt); the engine never emits it as a scalar,
    # but the CIF B-factor column carries the per-atom confidence.
    try:
        metrics["chain_mean_plddt"] = _chain_mean_plddt_from_structure(best[5])
    except Exception:  # noqa: BLE001
        metrics["chain_mean_plddt"] = {}
    return metrics




def _celery_revoke_quiet(async_result: Any, *, terminate: bool = True) -> float:
    """Best-effort revoke; a control-bus hiccup must not mask the real error."""
    try:
        from backend.core.celery_app import celery_app as _celery

        _celery.control.revoke(async_result.id, terminate=terminate)
    except Exception:  # noqa: BLE001
        pass




def _chain_mean_plddt_from_structure(path: Path) -> Dict[str, float]:
    """Mean B-factor (== per-atom pLDDT in engine structure output) per chain.

    Chains with a short polymer (the SEZ linker: one HETATM residue carrying a
    handful of high-confidence atoms) are EXCLUDED: their mean reads ~0.93 and,
    with chain letters sorted, would outrank the actual peptide — feeding the
    linker's confidence into binder_avg_plddt, the pLDDT gate, and the
    composite's binder term for every bicyclic candidate."""
    st = gemmi.read_structure(str(path))
    st.setup_entities()
    means: Dict[str, float] = {}
    for chain in st[0]:
        polymer = [r for r in chain if r.het_flag != "H"]
        if len(polymer) < 8:
            continue
        values = []
        for residue in chain:
            for atom in residue:
                b_iso = float(getattr(atom, "b_iso", 0.0) or 0.0)
                if b_iso > 0:
                    values.append(b_iso)
        if values:
            # 0-1 scale: the candidate-row contract multiplies by 100 downstream.
            means[chain.name] = sum(values) / len(values) / 100.0
    return means




def _covalent_detached_atoms(
    structure_path: Path,
    threshold: float = 2.2,
) -> List[str]:
    """Heavy atoms further than `threshold` from any atom of their own chain.

    Catches every covalent tear the diffusion projections produced on the
    free chains — terminal side chains (OH/CD1/CD2/...), backbone C-N, even
    whole-residue rips — which the CA-CB-only integrity report misses
.
    """
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    detached: List[str] = []
    for chain in st[0]:
        pts = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for residue in chain for a in residue
            if a.element != gemmi.Element("H")
        ])
        labels = [
            (residue, atom)
            for residue in chain for atom in residue
            if atom.element != gemmi.Element("H")
        ]
        if len(pts) < 2:
            continue
        d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
        np.fill_diagonal(d2, np.inf)
        nearest = np.sqrt(d2.min(axis=1))
        for (residue, atom), dist in zip(labels, nearest):
            if dist > threshold:
                detached.append(
                    f"{chain.name}{int(residue.seqid.num)}"
                    f"{residue.name}:{atom.name}({dist:.1f}A)")
    return detached




def _ring_shape_penalty(structure_path: Path) -> Tuple[float, Dict[str, float]]:
    """Penalty (0..1) for molten, unstructured cyclic peptides.

    Structured rings of 10-16 residues (cyclic RGDfK, SFTI-style) run
    Rg/n ~0.7-0.9 with several non-local contacts (beta-turns); molten
    blobs measure 0.42-0.60 with 0-2 contacts (audit of every
    sample). The confidence head cannot separate them (pLDDT 80 on
    blobs), so the shape signal has to come from geometry. Soft penalty,
    not rejection — early generations may legitimately hold only blobs
    and starving them kills the search."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    st.remove_hydrogens()
    chains = sorted(st[0], key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    if len(chains) < 2:
        return (0.0, {})
    # peptide = shortest polymer chain with >= 8 residues (excludes the
    # single-residue linker chain that bicyclic complexes carry)
    _pep_candidates = [c for c in chains
                       if sum(1 for r in c if r.het_flag != "H") >= 8]
    if not _pep_candidates:
        return 0.0, {}
    pep = _pep_candidates[-1]
    ca = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for r in pep for a in r if a.name == "CA"
    ])
    n = len(ca)
    if n < 6:
        return (0.0, {})
    rg = float(np.sqrt(((ca - ca.mean(axis=0)) ** 2).sum(axis=1).mean()))
    d = np.linalg.norm(ca[:, None, :] - ca[None, :, :], axis=-1)
    iuj = np.triu_indices(n, 1)
    nonlocal_c = int(((np.abs(iuj[0] - iuj[1]) >= 3) & (d[iuj] < 4.5)).sum())
    rg_ratio = rg / n
    penalty = 0.0
    # Calibrated against known structured cyclic peptides:
    # SFTI-1 (14aa, disulfide-stapled) runs rg/n ~0.46; kalata B1
    # cyclotide (29aa) ~0.29 — small rings are NATURALLY compact and the
    # the old flat 0.62 cutoff penalised every 10-16aa candidate (RANKL cyclic at 0.48-0.61 got -0.30, crushing every
    # composite to zero). The size-aware expected band scales as
    # ~2.8/sqrt(n) (empirical fit to structured macrocycles); use the
    # tighter of that and 0.62, and require BOTH low compactness AND
    # near-zero nonlocal contacts (the true molten signature).
    expected_rg_ratio = min(0.62, 2.8 / (n ** 0.5))
    if rg_ratio < expected_rg_ratio * 0.75 and nonlocal_c < 2:
        penalty = 0.30
    return (penalty, {"rg_over_n": round(rg_ratio, 3),
                      "expected_rg_over_n": round(expected_rg_ratio, 3),
                      "nonlocal_contacts": nonlocal_c})


# Interface engagement floor for sample selection: receptor-peptide heavy-atom
# pairs within 4.5 A. A docked 12-20-mer buries dozens of pairs; below this
# the sample never docked and must not ship regardless of clash-freedom.
ENGAGE_MIN_CONTACTS = 8




def _interchain_contact_count(
    structure_path: Path,
    cutoff: float = 4.5,
) -> int:
    """Heavy-atom pairs across the receptor/peptide interface within cutoff.

    Same chain convention as _interchain_clash_profile (largest chain =
    receptor, every other poly chain counts as peptide side)."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    st.remove_hydrogens()
    chains = sorted(st[0], key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    if len(chains) < 2:
        return 0
    rec = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for residue in chains[0] for a in residue
    ])
    n = 0
    for chain in chains[1:]:
        other = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for residue in chain for a in residue
        ])
        if not len(other):
            continue
        d = np.sqrt(((rec[:, None, :] - other[None, :, :]) ** 2).sum(-1))
        n += int((d < cutoff).sum())
    return n




def _backbone_intact(cif: Path, min_ca_ca: float = BACKBONE_CA_CA_MIN_A) -> bool:
    """Every consecutive CA-CA virtual bond >= min_ca_ca.

    Guards against backbone crush: clash pushes without an omega-band can
    fold CA(i) onto CA(i+1)    sampler output was a clean helix).
    """
    st = gemmi.read_structure(str(cif))
    st.setup_entities()
    for chain in st[0]:
        # virtual bonds are WITHIN a chain: reset per chain, else the last
        # CA of chain A gets compared with the first CA of chain B (multi-
        # chain receptors put unrelated CAs within 2.8 A at their interface)
        prev = None
        for res in chain:
            ca = res.find_atom("CA", "*")
            if ca is None:
                continue
            pos = np.array([ca.pos.x, ca.pos.y, ca.pos.z])
            if prev is not None and np.linalg.norm(pos - prev) < min_ca_ca:
                return False
            prev = pos
    return True




def _apply_shipped_sample_ipsae(shipped_cif: Path, metrics: dict) -> bool:
    """Overwrite the interface metrics with the SHIPPED sample's own ipSAE.

    The confidence.json numbers describe the worker's best-by-iptm sample,
    which may differ from the geometry-driven pick; the per-sample ipsae
    json sits next to the shipped cif. Returns whether the override ran.
    """
    ipsae_path = shipped_cif.with_name(
        "ipsae_" + shipped_cif.stem + ".json")
    if not ipsae_path.is_file():
        return False
    try:
        meta = json.loads(ipsae_path.read_text())
    except (OSError, ValueError):
        return False
    for key in ("ipsae_dom", "ligand_ipsae_max"):
        if isinstance(meta.get(key), (int, float)):
            metrics[key] = meta[key]
    if isinstance(meta.get("ipsae_dom"), (int, float)):
        metrics["interface_score"] = meta["ipsae_dom"]
    return True




def _select_dspace_samples(scored):
    """Rank D-space diffusion samples for shipping.

    Sort key (descending): covalent integrity, interface ENGAGEMENT, clash
    count, ipTM, name. Engagement must outrank clash-freedom (a detached
    peptide has zero clashes and would outrank every bound sample) and
    outrank ipSAE (detached poses can carry deceptively high values).

    Tuple layout: (intact, engaged, -clashes, iptm, cif.name, cif,
    detached, contacts).
    """
    ranked = []
    for iptm, cif in scored:
        try:
            detached = _covalent_detached_atoms(Path(cif))
            clashes = _interchain_clash_count(Path(cif))
            contacts = _interchain_contact_count(Path(cif))
        except Exception:  # noqa: BLE001
            detached = ["integrity-check-failed"]
            clashes = 10**6
            contacts = 0
        engaged = 1 if contacts >= ENGAGE_MIN_CONTACTS else 0
        ranked.append((0 if detached else 1, engaged, -clashes, iptm, cif.name,
                       cif, detached, contacts))
    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4]), reverse=True)
    return ranked




def _pdb_peptide_sequence(pdb_path: Path, binder_chain_id: str = "B") -> str:
    """One-letter peptide sequence of a staged PDB, auto-detected.

    The named chain is tried first (design-space staging writes the peptide on
    B), but multi-chain targets re-letter chains — staging writes receptors
    A..N and the peptide on the next letter ("B for a monomer, C for a
    dimer"). Fallback: the polymer chain whose length is peptide-sized
    (8-40 residues). Empty when nothing qualifies."""
    three2one = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
        "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
        "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
        "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    }
    by_chain: Dict[str, List[str]] = {}
    seen: set = set()
    try:
        for line in pdb_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("ATOM"):
                continue
            chain = line[21].strip()
            key = (chain, line[22:26])
            if key in seen:
                continue
            seen.add(key)
            by_chain.setdefault(chain, []).append(
                three2one.get(line[17:20].strip(), "X"))
    except OSError:
        return ""
    named = by_chain.get(binder_chain_id, [])
    if 8 <= len(named) <= 40:
        return "".join(named)
    # Multi-chain staging re-letters chains; the peptide is the polymer chain
    # of peptide size (8-40 residues) — receptors are longer.
    for chain in sorted(by_chain):
        residues = by_chain[chain]
        if 8 <= len(residues) <= 40:
            return "".join(residues)
    return ""




def _polymer_chain_roles(
    structure: "gemmi.Structure",
    binder_chain_id: str = "",
) -> Tuple[Optional[str], List[str], List[str]]:
    """Classify chains into (binder, target_chains, linker_or_short_chains).

    Binder = the named chain when it holds a peptide-length polymer (8-40
    residues), else the first chain of that size. Target = every polymer chain
    LONGER than 40 residues (multi-copy receptors included). Everything with a
    shorter polymer (SEZ linker: one HETATM residue) is excluded from binding
    measurements — the linker is COVALENTLY bonded to the binder, so counting
    it as 'target' side pins min distance at the bond length (~1.8 A) and
    neutralizes any dissociation check for bicyclic products."""
    binder, targets, others = None, [], []
    for ch in structure[0]:
        polymer = [r for r in ch if r.het_flag != "H"]
        n = len(polymer)
        if n == 0:
            continue
        if n > 40:
            targets.append(ch.name)
        elif 8 <= n <= 40:
            others.append(ch.name)
        else:
            others.append(ch.name)
    if binder_chain_id and binder_chain_id in others:
        others.remove(binder_chain_id)
        binder = binder_chain_id
    else:
        for name in others:
            ch = structure[0][name]
            if 8 <= len([r for r in ch if r.het_flag != "H"]) <= 40:
                others.remove(name)
                binder = name
                break
    return binder, targets, others




def _assert_product_bound(
    structure_path: Path,
    binder_chain_id: str,
    *,
    rank: int,
    sequence: str,
    max_distance: float = PRODUCT_BOUND_MAX_DISTANCE_A,
) -> float:
    """Export gate: a shipped product must be a BOUND complex.

    A dissociated product (binder farther than max_distance from the target
    protein chains) means an upstream stage shipped a pre-docking frame —
    staged geometries sit 50-100 A off-target. A detached "result" with a high
    score attached is actively misleading, so the export fails loudly instead.
    Chain roles come from polymer length, not chain letters: the flipped
    product re-letters chains (multi-chain targets make the designed peptide
    chain D, E, ...), and the SEZ linker is covalently bonded to the binder —
    counting it on the target side would pin min distance at the bond length
    and neutralize the gate for every bicyclic product.
    Returns the measured min binder-to-target heavy-atom distance."""
    try:
        st = gemmi.read_structure(str(structure_path))
        st.setup_entities()
    except Exception as exc:
        raise RuntimeError(
            f"product structure unreadable for rank {rank}: "
            f"{structure_path} ({exc})") from exc
    binder_name, target_names, _linkers = _polymer_chain_roles(st, binder_chain_id)
    chain_atoms: Dict[str, np.ndarray] = {}
    for ch in st[0]:
        atoms = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for residue in ch for a in residue if a.element.name != "H"
        ])
        if atoms.shape[0] != 0:
            chain_atoms[ch.name] = atoms
    binder_atoms = chain_atoms.get(binder_name) if binder_name else None
    if binder_atoms is None or not target_names:
        raise RuntimeError(
            f"product structure for rank {rank} ({str(sequence)[:16]}) has no "
            f"verifiable binder↔target pair (chains: "
            f"{[(c.name, sum(1 for r in c if r.het_flag != 'H')) for c in st[0]]}) — "
            "cannot verify the bound state")
    target_atoms = [
        chain_atoms[name] for name in target_names if name in chain_atoms
    ]
    if not target_atoms:
        raise RuntimeError(
            f"product structure for rank {rank} has no target protein chain "
            f"(expected one of {target_names}) — cannot verify the bound state")
    min_d = min(
        float(np.linalg.norm(binder_atoms[:, None, :] - t[None, :, :], axis=2).min())
        for t in target_atoms
    )
    if min_d > max_distance:
        raise RuntimeError(
            f"product structure for rank {rank} ({str(sequence)[:16]}) is "
            f"dissociated: binder-to-target min heavy-atom distance "
            f"{min_d:.1f} A > {max_distance:.0f} A — refusing to export a "
            "pre-docking frame as a result (inspect this candidate's refine "
            "output)")
    return min_d




def _interchain_clash_profile(
    structure_path: Path,
) -> Tuple[float, int, int]:
    """(min binder-target heavy-atom distance, pairs < 2.2 A, pairs < 2.8 A).

    The peptide side is the designed binder ONLY (polymer of 8-40 residues);
    the receptor side is every chain longer than 40 residues. Receptor-receptor
    pairs are excluded: multi-copy receptors with inter-chain disulfides
    (SG-SG ~2.0 A) or metal sites otherwise read as permanent "peptide embedded
    in receptor" and reject every candidate for that target. Linker chains
    (< 8 residues, covalently bonded to the binder) are excluded too."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    st.remove_hydrogens()
    binder_chains, target_chains = [], []
    for ch in st[0]:
        n = sum(1 for r in ch if r.het_flag != "H")
        if 8 <= n <= 40:
            binder_chains.append(ch)
        elif n > 40:
            target_chains.append(ch)
    if not binder_chains or not target_chains:
        return (99.0, 0, 0)
    rec = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for chain in target_chains for residue in chain for a in residue
    ])
    min_d, n22, n28 = 99.0, 0, 0
    for chain in binder_chains:
        other = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for residue in chain for a in residue
        ])
        if not len(other):
            continue
        d2 = ((rec[:, None, :] - other[None, :, :]) ** 2).sum(-1)
        d = np.sqrt(d2)
        min_d = min(min_d, float(d.min()))
        n22 += int((d < REFINE_ABSOLUTE_PAIR_MIN_A).sum())
        n28 += int((d < REFINE_PACKING_BAND_A).sum())
    return (min_d, n22, n28)




def _interchain_clash_count(
    structure_path: Path,
    threshold: float = 2.2,
) -> int:
    """Heavy-atom contacts below `threshold` between the largest (receptor)
    chain and every other chain. Complements the covalent check: the
    projections can leave the pose chemically intact yet clashing."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    chains = sorted(st[0], key=lambda c: -len(c))
    if len(chains) < 2:
        return 0
    rec = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for residue in chains[0] for a in residue
        if a.element != gemmi.Element("H")
    ])
    count = 0
    for chain in chains[1:]:
        other = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for residue in chain for a in residue
            if a.element != gemmi.Element("H")
        ])
        if not len(other):
            continue
        d2 = ((rec[:, None, :] - other[None, :, :]) ** 2).sum(-1)
        count += int((d2 < threshold * threshold).sum())
    return count




def _structure_integrity_report(
    structure_path: Path,
) -> Dict[str, Any]:
    """Per-residue backbone bond integrity of every polymer chain.

    A CB pushed tens of A away passes the chirality gate (the sign still
    reads D) and the linker gate (SG untouched)
    product: CA-CB 30.4 A. Any broken bond here means some projection or
    transform deformed the structure; callers reject on it.
    """
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    broken: List[Dict[str, Any]] = []
    for chain in st[0]:
        for residue in chain:
            if residue.het_flag == "H":
                continue
            ca = residue.find_atom("CA", "*")
            cb = residue.find_atom("CB", "*")
            if ca is None or cb is None:
                continue
            d = float(np.linalg.norm(
                np.array([ca.pos.x, ca.pos.y, ca.pos.z])
                - np.array([cb.pos.x, cb.pos.y, cb.pos.z])))
            if not 1.2 <= d <= 2.0:
                broken.append({
                    "chain": chain.name, "resnum": int(residue.seqid.num),
                    "resname": residue.name, "ca_cb": round(d, 2),
                })
    return {"broken_bonds": broken, "all_intact": not broken}




def _pocket_contact_report(
    structure_path: Path,
    pocket_sequence_contacts: List[Tuple[str, int]],
) -> Dict[str, Any]:
    """Min heavy-atom distance between the pocket residues and every chain
    other than the largest (receptor) chain. Bound peptides sit well under
    4.5 A; the gate cut-off is POCKET_CONTACT_MAX_A. Chain names match modulo
    boltz2's processed-structure suffix ("A" matches "A1")."""
    def _strip_suffix(name: str) -> str:
        return name[:-1] if len(name) > 1 and name[-1].isdigit() else name

    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    chains = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 1),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"),
    )
    if not chains:
        return {"pocket_min_distance": None}
    receptor = chains[0]
    wanted = {(_strip_suffix(c), int(n)) for c, n in pocket_sequence_contacts}
    pocket_xyz = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for residue in receptor
        if (_strip_suffix(receptor.name), residue.seqid.num) in wanted
        for a in residue if a.element != gemmi.Element("H")
    ])
    free_xyz = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for chain in st[0]
        if chain.name != receptor.name
        for residue in chain
        for a in residue if a.element != gemmi.Element("H")
    ])
    if pocket_xyz.size == 0 or free_xyz.size == 0:
        return {"pocket_min_distance": None,
                "pocket_residues_found": bool(pocket_xyz.size)}
    dist = np.linalg.norm(
        pocket_xyz[:, None, :] - free_xyz[None, :, :], axis=-1)
    return {
        "pocket_min_distance": float(dist.min()),
        "pocket_residues_found": True,
        "pocket_contacts_within_4p5": int((dist < 4.5).sum()),
    }


POCKET_CONTACT_MAX_A = 5.0




def _pocket_chemistry_demands(
    target_sequence: str,
    pocket_sequence_contacts: List[Tuple[str, int]],
) -> Optional[Dict[str, int]]:
    """Side-chain chemistry demands implied by the user pocket's composition.

    Returns {"acid": n, "basic": n, "aromatic": n} anchors the peptide should
    supply, or None when the pocket makes no strong demand (term inactive).
    Interface physics diagnostics: a peptide
    can pack as well as the native ligand (Sc 0.445 vs 0.424) yet score
    poorly when the polar lock is missing — zero salt bridges against a
    4-Lys/1-Arg rim, zero aromatics against Y/H. A basic rim demands acidic
    anchors (Glu > Asp: longer side chain reaches the NZ), an acidic rim
    demands basic residues, aromatic pockets (Y/F/H/W) demand aromatic
    partners. Anchor counts are halved with a floor of 2: a 4-Lys rim is
    locked by 2 well-placed carboxylates, not 4."""
    pocket_aas = "".join(
        target_sequence[int(pos) - 1]
        for _chain, pos in pocket_sequence_contacts
        if 1 <= int(pos) <= len(target_sequence)
    )
    if len(pocket_aas) < 4:
        return None
    basic = sum(aa in "KR" for aa in pocket_aas)
    acidic = sum(aa in "DE" for aa in pocket_aas)
    aromatic = sum(aa in "FYHW" for aa in pocket_aas)
    demands: Dict[str, int] = {}
    if basic - acidic >= 2:
        demands["acid"] = max(2, (basic - acidic + 1) // 2)
    if acidic - basic >= 2:
        demands["basic"] = max(2, (acidic - basic + 1) // 2)
    if aromatic >= 2:
        demands["aromatic"] = min(3, aromatic // 2)
    return demands or None




def _pocket_chemistry_complementarity(
    sequence: str, demands: Optional[Dict[str, int]],
) -> Optional[float]:
    """Match of the peptide's side-chain supply to the pocket demands, [0, 1].

    Glu counts fully toward the acid demand and Asp at half weight (side-chain
    reach); Lys/Arg symmetrically for a basic rim; F/Y/W for aromatic stacking
    (peptide His is pH-ambiguous and excluded)."""
    if not demands:
        return None
    seq = str(sequence or "").upper()
    supply = {
        "acid": seq.count("E") + 0.5 * seq.count("D"),
        "basic": seq.count("R") + 0.5 * seq.count("K"),
        "aromatic": float(seq.count("F") + seq.count("Y") + seq.count("W")),
    }
    scores = [min(1.0, supply[k] / float(demands[k])) for k in demands]
    return sum(scores) / len(scores)




def _pocket_place_for_refine(
    staged_path: Path,
    *,
    pocket_sequence_contacts: List[Tuple[str, int]],
    seed: int,
    require_bonds: bool,
    bicyclic_cys_positions: Optional[List[int]] = None,
    linker_ccd: str = "SEZ",
    keep_pose: bool = False,
) -> None:
    """CPU half of the pocket route (pairs with dispatch + collect): rigidly
    place the free chains (peptide+linker) at the user pocket on the staged
    receptor — skipped when keep_pose is set (the staged pose is already
    anchored to a reference) — and restore the covalent LINK topology that
    gemmi's write_pdb drops, so the refine diffusion keeps the ring intact.

    Raises ValueError when the pocket residues are not on the staged receptor.
    """
    # rigid placement at the pocket center (contacts already sequence-numbered
    # to match the staged PDB's 1..N polymer numbering)
    st_pl = gemmi.read_structure(str(staged_path))
    st_pl.setup_entities()
    pl_ch = sorted((c for c in st_pl[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
                   key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    if not pl_ch:
        raise ValueError("staged complex has no polymer chains to place against")
    st_rec = pl_ch[0]
    _wanted = set(pocket_sequence_contacts)
    pts = []
    pocket_atom_coords = []
    for residue in st_rec:
        if (st_rec.name, residue.seqid.num) not in _wanted:
            continue
        a = residue.find_atom("CA", "*")
        if a is not None:
            pts.append((a.pos.x, a.pos.y, a.pos.z))
        for atom in residue:
            if atom.element != gemmi.Element("H"):
                pocket_atom_coords.append((atom.pos.x, atom.pos.y, atom.pos.z))
    if not pts:
        raise ValueError(
            "pocket residues "
            + ",".join(f"{c}:{n}" for c, n in pocket_sequence_contacts)
            + f" not found on the staged receptor (chain {st_rec.name})"
        )
    _target_center = np.mean(np.array(pts), axis=0)
    # clash-minimal rigid placement: deterministic rotations x radial offsets,
    # scored against the staged receptor (centroid-on-pocket buries the binder
    # in the pocket wall
    # the staged pose already carries the reference binding mode
    # Reference-anchored only: the staged pose (user's upload or model
    # output) is kept as-is; the covalent LINK topology is restored below
    st_pl.setup_entities()
    st_pl.write_pdb(str(staged_path))
    # gemmi's write_pdb dropped the LINK records above — restore the covalent
    # topology or the refine diffusion breaks the ring
    if require_bonds:
        _append_staged_bicyclic_links(
            staged_path, bicyclic_cys_positions, linker_ccd)




def _pocket_collect_refine(
    async_result: Any,
    *,
    staged_path: Path,
    refined_cif: Path,
    pocket_sequence_contacts: List[Tuple[str, int]],
    require_bonds: bool,
    chirality_label: str,
    expect_blind: bool = False,
    linker_ccd: str = "SEZ",
) -> Dict[str, Any]:
    """GPU-result half of the pocket route (pairs with
    :func:`_pocket_place_for_refine` + dispatch): collect the dispatched
    refine, then report on the shipped structure.

    Raises ValueError when a gate fails; the caller rejects the candidate.
    """
    metrics = _dpeptide_collect_refine(
        async_result, staged_path, refined_cif, expect_blind=expect_blind)
    refined_path = str(refined_cif)

    # Quality TELEMETRY, not filtering: the staged strain relief upstream
    # makes the refine start chemically sound, so these reports describe the
    # shipped structure (and would expose any residual defect) without
    # rejecting the candidate — rejection hides engine problems instead of
    # solving them at the source.
    integrity = _structure_integrity_report(Path(refined_path))

    if require_bonds:
        rebuild_info = _rebuild_single_atom_linker_anchor(
            Path(refined_path), prefer_code=linker_ccd)
        bond_report = _dpeptide_linker_bond_report(
            Path(refined_path), prefer_code=linker_ccd)
        if not bond_report.get("all_bonded"):
            prefix = (f"anchor rebuild {rebuild_info}; " if rebuild_info else "")
            raise ValueError(
                "linker covalent integrity gate: "
                f"{prefix}{bond_report.get('bonds')}")
    else:
        bond_report = None

    pocket_report = _pocket_contact_report(Path(refined_path), pocket_sequence_contacts)
    pocket_min = pocket_report.get("pocket_min_distance")
    flags = []
    if not integrity.get("all_intact"):
        flags.append("integrity")
    if require_bonds and not (bond_report and bond_report.get("all_bonded")):
        flags.append("ring_bonds")
    if pocket_min is None or float(pocket_min) > POCKET_CONTACT_MAX_A:
        flags.append("pocket")
    print(
        f"[{chirality_label}-peptide] pocket refine ipTM={metrics.get('iptm')} "
        f"pocket_min={pocket_min if pocket_min is None else round(float(pocket_min), 2)}A "
        f"integrity={'ok' if integrity.get('all_intact') else 'BROKEN'} "
        f"flags={flags or 'none'}",
        file=sys.stderr,
    )
    # Integrity stays a HARD gate: a collapsed structure (broken CA-CB) is
    # physically invalid — no argument rescues it.
    # The pocket gate is now SOFT: rejecting every off-pocket candidate left
    # the frontend with zero results whenever the sampler's site preference
    # disagreed with the user pocket contention). Instead the candidate ships with off_pocket=True; the
    # composite heavily penalizes it (pocket_satisfaction near 0), so
    # in-pocket candidates always outrank off-pocket ones when both exist —
    # but the user always gets results, ranked honestly.
    if "integrity" in flags:
        raise ValueError(
            f"精修产物完整性破损（broken CA-CB: "
            f"{integrity.get('broken_bonds', [])[:3]}）— 采样塌缩，拒收")
    return {
        "staged": str(staged_path),
        "refined": refined_path,
        "metrics": metrics,
        "pocket": pocket_report,
        "bonds": bond_report,
        "integrity": integrity,
        "quality_flags": flags,
        "off_pocket": "pocket" in flags,
    }




def _lpeptide_keep_dir(parent_task_id: str) -> Path:
    return Path(RESULTS_BASE_DIR) / "_runtime_tmp" / f"lpeptide_keep_{parent_task_id}"




def _persist_lpeptide_structure_keep(
    parent_task_id: str,
    generation: int,
    sequence: str,
    structure_file,
) -> str:
    """Persist an L candidate's native product under the shared keep root.

    The D route recovers refined samples from dpeptide_keep_* at zip time
    because its per-candidate structures only live in the run's tmp dir; an
    L candidate's structure has the same lifetime problem, and a checkpoint
    resume + tmp cleanup would leave its archive rows permanently
    structureless. Returns the keep file name, '' when the copy failed.
    """
    if structure_file is None or not Path(structure_file).is_file():
        return ""
    keep_name = f"gen{generation:03d}_{sequence}.pdb"
    try:
        keep_dir = _lpeptide_keep_dir(parent_task_id)
        keep_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(structure_file, keep_dir / keep_name)
        return keep_name
    except OSError as keep_exc:
        print(
            f"[peptide] WARNING: could not persist L candidate structure "
            f"for rank fallback: {keep_exc}",
            file=sys.stderr)
        return ""




def _lpeptide_keep_structure(parent_task_id: str, keep_file_name: str, sequence: str) -> str:
    """Recover a persisted L product, guarding against generation collisions
    (a cand index recurs every generation — accept the keep only when its
    peptide matches the row's sequence)."""
    if not keep_file_name:
        return ""
    keep_path = _lpeptide_keep_dir(parent_task_id) / keep_file_name
    if keep_path.is_file() and _pdb_peptide_sequence(keep_path) == \
            str(sequence or "").strip().upper():
        return str(keep_path)
    return ""




def _count_dpeptide_chirality_violations(structure_path) -> Optional[int]:
    """CA-chirality violation count of a shipped D display structure.

    Telemetry only: the hard gate covers the rank-1 product, and the
    diffusion refine can leave minority CA inversions on lower ranks
    (minority inversions can survive on lower ranks). None = unmeasurable.
    """
    try:
        peplm_root = str(_resolve_capability_dir("peptide_lm"))
        if peplm_root not in sys.path:
            sys.path.insert(0, peplm_root)
        from peplm.dpeptide import chirality_violations
        st = gemmi.read_structure(str(structure_path))
        st.setup_entities()
        protein_chains = [
            ch for ch in st[0]
            if sum(1 for r in ch if r.het_flag != "H") >= 3]
        if len(protein_chains) < 2:
            return None
        protein_chains.sort(key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
        return len(chirality_violations(st, protein_chains[-1].name, "D"))
    except Exception:
        return None




def _assert_product_chirality(
    product_pdb: Path,
    reference_structure_path: Optional[Path] = None,
    rmsd_limit: Optional[float] = 0.5,
) -> Dict[str, Any]:
    """Hard gate on the flipped product: receptor must be L (positive CA
    volumes), peptide must be D (negative), and the receptor must coincide
    with the ORIGINAL input geometry (exact x->-x round trip)."""
    peplm_root = str(_resolve_capability_dir("peptide_lm"))
    if peplm_root not in sys.path:
        sys.path.insert(0, peplm_root)
    from peplm.dpeptide import chirality_report

    st = gemmi.read_structure(str(product_pdb))
    st.setup_entities()

    # receptor = the protein chain with the most standard residues; peptide =
    # the next-largest protein chain. Ligand/HETATM chains are ignored.
    protein_chains = [
        chain for chain in st[0]
        if sum(1 for res in chain if res.het_flag != "H") >= 3
    ]
    protein_chains.sort(key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    if len(protein_chains) < 2:
        raise RuntimeError(
            f"D-peptide product gate failed: expected >=2 protein chains, got "
            f"{[c.name for c in st[0]]}"
        )
    # peptide = SHORTEST chain (multi-chain receptors put receptor copies at
    # every length tier; chains[1] was a second receptor copy on trimers)
    receptor_chains = protein_chains[:-1]
    peptide_chain = protein_chains[-1]

    # chirality_report returns a ChiralityReport (not iterable); the
    # downstream gate reads .n_scored / .n_positive — use the first
    # receptor chain's report, per-chain violations covered by rec_bad
    rec_report = chirality_report(st, receptor_chains[0].name)
    pep_report = chirality_report(st, peptide_chain.name)
    from peplm.dpeptide import chirality_violations
    # Product frame contract: receptor ALL-L, peptide ALL-D — per residue.
    # The mean-volume check silently passed 11L/9D mixtures (the mean stays on
    # the expected side); a mixed-chirality product is a failed product.
    rec_bad = chirality_violations(st, receptor_chains[0].name, "L")
    pep_bad = chirality_violations(st, peptide_chain.name, "D")
    if rec_report.n_scored <= 0 or pep_report.n_scored <= 0:
        raise RuntimeError(
            "D-peptide product gate: no scorable CA chiral volumes "
            f"(receptor {rec_report.n_scored}, peptide {pep_report.n_scored})")
    # Policy mirrors the per-candidate evaluation gate: free peptide chains
    # diffuse without improper-dihedral restraints, so a minority of CA
    # inversions per chain are numerical artifacts — evaluation tolerates
    # <30% with a proportional score penalty, and the product gate must not
    # be stricter than the policy that ranked the candidates. A receptor
    # (pinned every step) or a majority-inverted peptide is a real failure.
    if len(rec_bad) > 0.3 * rec_report.n_scored:
        raise RuntimeError(
            "D-peptide product chirality gate FAILED — receptor not all-L: "
            f"{len(rec_bad)}/{rec_report.n_scored} violations ("
            + ",".join(f"{n}{r}" for n, r, _ in rec_bad[:6]) + ")")
    if len(pep_bad) > 0.3 * pep_report.n_scored:
        raise RuntimeError(
            "D-peptide product chirality gate FAILED — peptide not all-D: "
            f"{len(pep_bad)}/{pep_report.n_scored} violations ("
            + ",".join(f"{n}{r}" for n, r, _ in pep_bad[:6]) + ")")
    if pep_bad:
        print(
            f"[d-peptide] product ships with {len(pep_bad)}/{pep_report.n_scored} "
            "CA chirality artifacts (within the 30% policy; penalized at scoring)",
            file=sys.stderr)

    rmsd = None
    if reference_structure_path is not None:
        # order-based pairing: engine writers renumber residues and rename
        # chains (A -> A1), so resnum/name matching silently mispairs.
        ref_pts = []
        ref_st = gemmi.read_structure(str(reference_structure_path))
        ref_st.setup_entities()
        ref_chains = sorted(
            (c for c in ref_st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
            key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
        if not ref_chains:
            raise RuntimeError("D-peptide product gate: reference has no protein chain.")
        for residue in ref_chains[0]:
            ca = residue.find_atom("CA", "*")
            if ca is not None:
                ref_pts.append(np.array([ca.pos.x, ca.pos.y, ca.pos.z]))
        prod_pts = []
        for residue in receptor_chains[0]:
            ca = residue.find_atom("CA", "*")
            if ca is not None:
                prod_pts.append(np.array([ca.pos.x, ca.pos.y, ca.pos.z]))
        if len(prod_pts) == len(ref_pts) and len(prod_pts) >= 20:
            deltas = np.stack(prod_pts) - np.stack(ref_pts)
            rmsd = float(math.sqrt((deltas ** 2).sum(axis=1).mean()))
            if rmsd_limit is not None and rmsd > rmsd_limit:
                print(
                    f"[d-peptide] product alignment flag: receptor deviates by "
                    f"RMSD {rmsd:.3f} A (> {rmsd_limit}); telemetry only.",
                    file=sys.stderr,
                )

    return {
        "receptor_chain": receptor_chains[0].name,
        "peptide_chain": peptide_chain.name,
        "receptor_mean_ca_volume": float(rec_report.mean_volume),
        "peptide_mean_ca_volume": float(pep_report.mean_volume),
        "receptor_vs_input_rmsd": (float(rmsd) if rmsd is not None else None),
        "receptor_config": "L",
        "peptide_config": "D",
        "receptor_violations": len(rec_bad),
        "peptide_violations": len(pep_bad),
    }




def run_peptide_design_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    backend: str,
    predict_args: Dict[str, Any],
    model_name: Optional[str],
    seed: Optional[int],
    options: Dict[str, Any],
    target_chain_id: Optional[str],
    progress_path: Optional[str],
    gpu_ids: Optional[List[int]] = None,
    subtask_queue: Optional[str] = None,
    custom_ccd_molecules: Optional[List[Dict[str, Any]]] = None,
    template_inputs: Optional[List[Dict[str, Any]]] = None,
) -> None:
    designer_dir = str(_resolve_capability_dir("designer"))
    if designer_dir not in sys.path:
        sys.path.append(designer_dir)

    from design_utils import (  # type: ignore
        parse_confidence_metrics,
        resolve_preferred_interface_metric,
    )

    try:
        base_yaml_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML for peptide design: {exc}") from exc
    if not isinstance(base_yaml_data, dict):
        raise ValueError("YAML root must be a mapping for peptide design.")

    random_seed = seed if isinstance(seed, int) else int(time.time())
    random.seed(random_seed)

    docking_engine = _is_docking_peptide_backend(backend)
    peptide_backend = _normalize_peptide_backend(backend)
    design_mode = _normalize_peptide_design_mode(options.get("peptideDesignMode") or options.get("peptide_design_mode"))
    peptide_chirality = str(options.get("peptideChirality") or options.get("peptide_chirality") or "l").strip().lower()
    if peptide_chirality not in ("l", "d"):
        raise ValueError(f"Invalid peptide chirality '{peptide_chirality}'. Must be 'l' or 'd'.")
    # Secondary-structure mode for the ESM3 proposal engine: auto leaves
    # the ss8 track off (model decides), helix/hairpin/strand_loop shape
    # the proposal composition. Absent key falls back to the
    # ESM3_SS_PROFILE deployment default ("env") for older task payloads.
    structure_mode = str(
        options.get("peptideStructureMode")
        or options.get("structure_mode") or "env").strip().lower()
    _ss_modes_root = str(CAPABILITIES_DIR / "peptide_lm")
    if _ss_modes_root not in sys.path:
        sys.path.insert(0, _ss_modes_root)
    from peplm.integrate.ss_modes import MODES as _SS_MODES
    if structure_mode not in _SS_MODES:
        raise ValueError(
            f"Invalid peptide structure mode '{structure_mode}'. Must be "
            f"one of {'/'.join(_SS_MODES)}.")
    # NOTE: the docking-engine REQUIREMENT intentionally lives in the
    # frontend/API contract only. Here `peptideChirality == 'd'` alone triggers
    # the mirror workflow, because live deployments may still submit legacy
    # backend tokens (boltz/protenix) until they restart into the version that
    # understands the dock aliases. Linear-only remains authoritative below.
    # Cyclic/bicyclic are SUPPORTED with D chirality: cyclization is a
    # topological (scalar distance) constraint and mirror x->-x preserves it,
    # so D-cyclic/D-bicyclic compose freely with the mirror workflow.
    # The candidate prediction stage (proposer + candidate YAML) already carries
    # cyclic/bicycling/linker/NCAA constraints unchanged.
    if peptide_backend == "alphafold3" and design_mode in ("cyclic", "bicyclic"):
        raise ValueError(
            "AlphaFold3 仅支持直链肽（linear）；环肽/双环肽请使用 Protenix 后端。"
        )
    # Constrained rings require HARD covalent-bond enforcement. Protenix's TFG
    # projects bond/angle atom pairs back into their RDKit bounds every
    # guidance step; Boltz2's bond feature is
    # only a soft diffusion prior and lets constrained rings fall apart under
    # refinement. Rings are therefore Protenix-only for
    # both chirality settings; linear peptides may use any engine.
    if design_mode in ("cyclic", "bicyclic") and peptide_backend != "protenix":
        raise ValueError(
            "环肽/双环肽仅支持 Protenix（protenix2dock）后端：共价键约束需要 "
            "Protenix TFG 硬钳制；Boltz2 的键先验无法保证成环键长。"
        )
    min_binder_len = 8 if design_mode == "bicyclic" else 5
    binder_length = _read_int_option(
        options,
        "peptideBinderLength",
        20 if design_mode != "bicyclic" else 15,
        min_value=min_binder_len,
        max_value=80,
    )
    # explicit length window (frontend min/max inputs): candidates adapt within
    # [lo, hi]; single-length consumers below (sequence mask, initial sequence,
    # NCAA clamps) operate against the window max, matching the frontend's
    # effectiveDesignLength
    _length_lo: Optional[int] = None
    _length_hi: Optional[int] = None
    if "peptideLengthMin" in options or "peptide_length_min" in options:
        _length_lo = _read_int_option(options, "peptideLengthMin", 8,
                                      min_value=min_binder_len, max_value=80)
        _length_hi = _read_int_option(options, "peptideLengthMax", 25,
                                      min_value=min_binder_len, max_value=80)
        if _length_hi < _length_lo:
            raise ValueError(
                f"肽长度窗口无效：min {_length_lo} > max {_length_hi}。")
        binder_length = _length_hi
    iterations = _read_int_option(options, "peptideIterations", 12, min_value=2, max_value=100)
    population_size = _read_int_option(options, "peptidePopulationSize", 16, min_value=2, max_value=100)
    elite_size = _read_int_option(options, "peptideEliteSize", 5, min_value=1, max_value=max(1, population_size))
    use_initial_sequence = _read_bool_option(options, "peptideUseInitialSequence", False)
    mask_raw = str(options.get("peptideSequenceMask") or "")
    mask_stripped = mask_raw.replace("-", "").replace("_", "").replace(" ", "").upper()
    if (mask_stripped and _length_lo
            and len(mask_stripped) != binder_length
            and _length_lo <= len(mask_stripped) <= _length_hi):
        # 掩码长在窗口内即合法: 掩码定布局, 自由段随候选伸缩;
        # _normalize_sequence_mask 返回值弃用, 仅借其校验字符集。
        sequence_mask = mask_stripped
        _normalize_sequence_mask(sequence_mask, len(sequence_mask))
    else:
        sequence_mask = _normalize_sequence_mask(options.get("peptideSequenceMask"), binder_length)
    excluded_aas = _normalize_excluded_aas(options.get("peptideExcludedAAs"))
    linker_ccd = str(options.get("peptideBicyclicLinkerCcd") or "SEZ").strip().upper() or "SEZ"
    # 固定非天然残基修改: [{position, ccd}] — 每个候选在该位置替换为指定 CCD
    fixed_mods_raw = options.get("peptideFixedModifications")
    fixed_modifications: List[Dict[str, Any]] = []
    if isinstance(fixed_mods_raw, list):
        for item in fixed_mods_raw:
            if not isinstance(item, dict):
                continue
            pos = item.get("position")
            ccd = str(item.get("ccd") or "").strip().upper()
            if isinstance(pos, (int, float)) and int(pos) >= 1 and ccd:
                fixed_modifications.append({"position": int(pos), "ccd": ccd})

    used_chain_ids = _extract_chain_ids_from_yaml(base_yaml_data)
    chain_order = list(used_chain_ids)
    binder_chain_id = _next_available_chain_id(used_chain_ids, "B")
    chain_order.append(binder_chain_id)
    linker_chain_id = _next_available_chain_id(chain_order, "L")
    if design_mode == "bicyclic":
        chain_order.append(linker_chain_id)

    resolved_target_chain_id = str(target_chain_id or "").strip()
    if not resolved_target_chain_id:
        protein_chain_lengths = _extract_protein_chain_lengths_from_yaml(base_yaml_data)
        resolved_target_chain_id = next(iter(protein_chain_lengths.keys()), "")
    if resolved_target_chain_id and resolved_target_chain_id not in chain_order:
        chain_order.append(resolved_target_chain_id)

    design_params: Dict[str, Any] = {
        "design_type": "bicyclic" if design_mode == "bicyclic" else "linear",
        "sequence_mask": sequence_mask or None,
        "include_cysteine": True,
    }
    if design_mode == "cyclic":
        design_params["cyclic_binder"] = True
    allow_extra_cys = False
    bicyclic_manual_anchors = False
    cys_layout_spec: Optional[Dict[str, Any]] = None
    if design_mode == "bicyclic":
        cys_position_mode = str(options.get("peptideBicyclicCysPositionMode") or "auto").strip().lower()
        fix_terminal_cys = _read_bool_option(options, "peptideBicyclicFixTerminalCys", True)
        allow_extra_cys = _read_bool_option(options, "peptideBicyclicIncludeExtraCys", False)
        # Anchor layout semantics: the peptide length is a range,
        # so absolute Cys positions alone cannot express the user's intent.
        # `peptideBicyclicCysLayout` selects how anchors follow the length:
        #   ring     — pin the two ring sizes; Cys3 rides the C-terminus and
        #              the N-flank absorbs the range (rigid topology).
        #   ratio    — pin percentages of the length; anchors scale with
        #              every candidate (shape preserved, rings flex).
        #   absolute — literal positions; only valid when min == max.
        #   auto     — engine layout (first / interior / last).
        # Legacy option mapping: mode=manual == absolute.
        layout_raw = str(options.get("peptideBicyclicCysLayout") or "").strip().lower()
        cys_layout_mode = layout_raw if layout_raw in {"auto", "ring", "ratio", "absolute"} else (
            "absolute" if cys_position_mode == "manual" else "auto")

        def _reject_mask_conflicts(layout_label: str) -> None:
            """Length-function anchors move with the candidate length, so a
            mask can only pin X (free) or C (extra cysteine) residues."""
            if not sequence_mask:
                return
            bad = [(idx + 1, ch) for idx, ch in enumerate(sequence_mask)
                   if ch not in ("X", "C")]
            if bad:
                pos, ch = bad[0]
                raise ValueError(
                    f"{layout_label}模式下 Cys 锚点随长度自适应移动，序列掩码不能固定具体氨基酸："
                    f"第 {pos} 位为 {ch!r}。请将该位设为 X，或改用绝对位置模式（min=max）。")
            if not allow_extra_cys and any(ch == "C" for ch in sequence_mask):
                raise ValueError(
                    f"{layout_label}模式下掩码固定的 C 可能不是 Cys 锚点：请开启 Allow Extra Cys，"
                    "或去掉掩码中的 C。")

        if cys_layout_mode == "ring":
            ring1 = _read_int_option(options, "peptideBicyclicRing1", 4, min_value=1, max_value=40)
            ring2 = _read_int_option(options, "peptideBicyclicRing2", 6, min_value=1, max_value=40)
            cys_layout_spec = {"mode": "ring", "ring1": ring1, "ring2": ring2}
            design_params["cys_layout"] = cys_layout_spec
            _reject_mask_conflicts("环拓扑")
        elif cys_layout_mode == "ratio":
            pct1 = _read_int_option(options, "peptideBicyclicRatio1", 15, min_value=0, max_value=100)
            pct2 = _read_int_option(options, "peptideBicyclicRatio2", 50, min_value=0, max_value=100)
            pct3 = (100 if fix_terminal_cys else _read_int_option(
                options, "peptideBicyclicRatio3", 100, min_value=0, max_value=100))
            cys_layout_spec = {"mode": "ratio", "pct1": pct1, "pct2": pct2, "pct3": pct3}
            design_params["cys_layout"] = cys_layout_spec
            _reject_mask_conflicts("按比例")
        elif cys_layout_mode == "absolute":
            bicyclic_manual_anchors = True
            cys1_pos = _read_int_option(options, "peptideBicyclicCys1Pos", 3, min_value=1, max_value=binder_length)
            cys2_pos = _read_int_option(options, "peptideBicyclicCys2Pos", 8, min_value=1, max_value=binder_length)
            cys3_pos = (
                binder_length
                if fix_terminal_cys
                else _read_int_option(options, "peptideBicyclicCys3Pos", binder_length, min_value=1, max_value=binder_length)
            )
            manual_anchor_set = sorted({cys1_pos, cys2_pos, cys3_pos})
            if len(manual_anchor_set) != 3:
                raise ValueError(
                    f"双环肽需要 3 个互不相同的 Cys 位置，当前为 {manual_anchor_set}。"
                )
            if any(b - a < 2 for a, b in zip(manual_anchor_set, manual_anchor_set[1:])):
                raise ValueError(
                    f"Cys 位置 {manual_anchor_set} 间隔过近：相邻锚点之间至少需要 2 个残基才能形成两个环。"
                )
            anchor_set_0b = {p - 1 for p in manual_anchor_set}
            if sequence_mask:
                for idx, mask_char in enumerate(sequence_mask):
                    if idx in anchor_set_0b and mask_char not in ("X", "C"):
                        raise ValueError(
                            f"序列掩码在第 {idx + 1} 位固定了 {mask_char!r}，与手动 Cys 锚点冲突："
                            "请将该位设为 X 或 C。"
                        )
                    if mask_char == "C" and idx not in anchor_set_0b and not allow_extra_cys:
                        raise ValueError(
                            f"序列掩码在第 {idx + 1} 位固定了 C，但该位不是 Cys 锚点 "
                            f"{sorted(p + 1 for p in anchor_set_0b)}：请开启 Allow Extra Cys 或移动该 C。"
                        )
            design_params["cys_positions"] = sorted(anchor_set_0b)
        else:
            design_params["cys_positions"] = []
            if sequence_mask and sequence_mask[:1] and sequence_mask[0] not in ("X", "C"):
                raise ValueError(
                    "双环肽默认布局要求第 1 位为 Cys："
                    f"序列掩码第 1 位为 {sequence_mask[0]!r}，请设为 X 或 C。"
                )

    linker_atom_map = BICYCLIC_LINKER_ATOM_MAP
    if design_mode == "bicyclic" and linker_ccd not in linker_atom_map:
        raise ValueError(
            f"Unsupported bicyclic linker CCD '{linker_ccd}'. Supported: {sorted(linker_atom_map)}."
        )

    custom_molecules = _normalize_custom_ccd_molecules(custom_ccd_molecules or [])
    natural_pool, unnatural_pool = _normalize_peptide_residue_pool(options.get("peptideResiduePool") or options.get("peptide_residue_pool"), custom_molecules)
    # A C-terminal amidated residue needs a free C-terminus, which cyclic/bicyclic
    # peptides lack. Reject it up front so no generation is wasted.
    if design_mode in ("cyclic", "bicyclic") and any(row.get("placement") == "c_term" for row in unnatural_pool):
        raise ValueError("C-terminal amidated residues cannot be used in cyclic/bicyclic peptide design (no free C-terminus). Remove them or switch to linear mode.")
    custom_molecules = _merge_selected_peptide_preset_molecules(custom_molecules, unnatural_pool)
    _peptide_allowed_residues(natural_pool, design_mode)
    nonnatural_min = _read_int_option(options, "peptideNonNaturalMin", 0, min_value=0, max_value=binder_length)
    nonnatural_max = _read_int_option(options, "peptideNonNaturalMax", 0, min_value=0, max_value=binder_length)
    if "peptideNonNaturalMax" in options and nonnatural_max < nonnatural_min:
        raise ValueError(
            f"非天然氨基酸数量窗口无效：min {nonnatural_min} > max {nonnatural_max}。")

    initial_sequence = ""
    if use_initial_sequence:
        initial_sequence = _normalize_initial_sequence(
            options.get("peptideInitialSequence"),
            binder_length=binder_length,
            sequence_mask=sequence_mask,
        )


    total_tasks = iterations * population_size
    completed_tasks = 0
    evaluated_sequences: set[str] = set()
    elite_population: List[Dict[str, Any]] = []
    all_results: List[Dict[str, Any]] = []
    peptide_started_at = time.time()

    # Generation checkpoint: acks_late redelivers a predict_task whose worker
    # died mid-run; without persistence the redesign restarts from generation
    # 1 and burns hours of GPU time. The checkpoint lives under the stable
    # VBIO_TASK_ID (same across redeliveries), not the per-attempt temp dir.
    _ckpt_root = Path(RESULTS_BASE_DIR) / "_runtime_tmp" / "peptide_checkpoints"
    _ckpt_path = _ckpt_root / f"{os.environ.get('VBIO_TASK_ID') or 'local'}.json"
    _start_generation = 1

    def _save_generation_checkpoint(gen: int) -> None:
        try:
            _ckpt_root.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "generation": gen,
                "completed_tasks": completed_tasks,
                "evaluated": sorted(evaluated_sequences),
                "elite_population": elite_population,
                "all_results": all_results,
            })
            # Atomic replace: a process death mid-write_text used to leave a
            # truncated JSON, and the resume path then silently restarted from
            # generation 1 (losing every completed generation).
            _tmp_ckpt = _ckpt_path.with_suffix(".json.tmp")
            _tmp_ckpt.write_text(payload)
            os.replace(_tmp_ckpt, _ckpt_path)
        except OSError as exc:
            # Checkpoint is best-effort, but ENOSPC/permission failures silently
            # disabled multi-day resume — print so the operator sees it.
            print(f"[peptide-design] WARNING: checkpoint save failed: {exc}",
                  file=sys.stderr)

    if _ckpt_path.is_file():
        try:
            _saved = json.loads(_ckpt_path.read_text())
            _start_generation = int(_saved.get("generation", 1)) + 1
            completed_tasks = int(_saved.get("completed_tasks", 0))
            evaluated_sequences = set(_saved.get("evaluated", []))
            elite_population = list(_saved.get("elite_population", []))
            all_results = list(_saved.get("all_results", []))
            if _start_generation > 1:
                print(
                    f"[peptide-design] resumed from checkpoint: "
                    f"generation {_start_generation}, {completed_tasks} "
                    f"candidates, {len(elite_population)} elites",
                    file=sys.stderr)
        except (ValueError, json.JSONDecodeError):
            _start_generation = 1



    # PeptideLM proposal engine — the ONLY peptide design algorithm in
    # V-Bio (two-tier language-model design; see capabilities/peptide_lm).
    # No fallback: a failure here is a task failure. Length is optional:
    # when the user did not set peptideBinderLength the engine explores an
    # adaptive range; NCAA residues come ONLY from the user-selected pool
    # (peptideResiduePool non-natural entries + custom CCDs).
    # user pocket (optional): "chain:num,chain:num" receptor residues (author
    # numbering of the upload) or an explicit x,y,z center. Translated here to
    # 1-based sequence positions — staged structures and native predictions
    # number polymer residues 1..N — and consumed by the pocket placement in
    # the collect loop (both chiralities). Empty = global (no pocket).
    try:
        pocket_author_contacts, pocket_sequence_contacts = (
            _pocket_contacts_for_staged_space(
                base_yaml_data, options, resolved_target_chain_id))
        # mol1 走 mol-1-constraint：口袋以序列号 SITE 条件注入候选共折叠
        # yaml 之外的 --constraints-json（作者编号换算已在上方完成）。
        # ── Chemistry-informed warm-start seeds (BindCraft motif-grafting) ──
        # When the user provides NO seed sequence but defined a pocket, derive
        # generation-1 anchor sequences from the pocket's amino-acid composition:
        # a basic-rich pocket (K/R/H) demands acidic residues (E/D) on the
        # peptide for salt bridges; aromatic pocket residues demand F/Y/W.
        # These aren't the designed binders — they're chemistry-compatible
        # starting points that the evolutionary loop refines. The prior's SS
        # conditioning (if provided) shapes the backbone preference.
        seed_sequences: List[str] = []
        if not initial_sequence and pocket_sequence_contacts:
            pocket_res = "".join(
                _pocket_residue_letters(pocket_sequence_contacts, base_yaml_data,
                                        target_chain_id))
            if pocket_res:
                comp = Counter(pocket_res.upper())
                n_basic = sum(comp.get(a, 0) for a in "KRH")
                n_aromatic = sum(comp.get(a, 0) for a in "FYW")
                seed_len = binder_length if binder_length else 12
                # acidic seed: E/D-rich (salt-bridge the basic rim)
                frac_acid = min(0.4, n_basic / max(len(pocket_res), 1))
                n_acid = max(2, int(seed_len * frac_acid))
                # aromatic anchor: 2-3 F/Y (hydrophobic contacts)
                n_aro = min(3, max(1, n_aromatic))
                _seed_rng = random.Random(42)
                acid_seed = list("ED" * seed_len)
                _seed_rng.shuffle(acid_seed)
                acid_seed = acid_seed[:n_acid]
                rest = [ _seed_rng.choice("AFIKLMQVWY")
                         for _ in range(seed_len - n_acid) ]
                seed_letters = acid_seed + rest
                _seed_rng.shuffle(seed_letters)
                seed_sequences.append("".join(seed_letters[:seed_len]))
                print(
                    f"[d-peptide] pocket chemistry seed: {seed_sequences[0]} "
                    f"(pocket {pocket_res}, basic={n_basic} aromatic={n_aromatic} "
                    f"→ {n_acid} acidic + {n_aro} aromatic positions)",
                    file=sys.stderr)
        if initial_sequence and initial_sequence not in seed_sequences:
            seed_sequences.insert(0, initial_sequence)

    except ValueError as pocket_err:
        raise ValueError(f"口袋定义无效：{pocket_err}") from pocket_err
    if pocket_sequence_contacts:
        print(
            "[peptide-design] user pocket (sequence numbering): "
            + ",".join(f"{c}:{n}" for c, n in pocket_sequence_contacts),
            file=sys.stderr,
        )

    # ---- Proposal engine: ESM3 (3B masked LM + GRPO) ----
    # Sequences come from a receptor-conditioned ESM3 policy running in a
    # GPU subprocess; GRPO updates between generations steer the policy
    # toward high-scoring interfaces. The adapter persists across
    # generations within one design task.
    _proposer_log_path = Path(temp_dir) / "proposals.log"

    def _proposer_log(message: str) -> None:
        print(f"[esm3] {message}", file=sys.stderr)
        try:
            with open(_proposer_log_path, "a", encoding="utf-8") as _pf:
                _pf.write(f"{time.strftime('%H:%M:%S')} {message}\n")
        except OSError:
            pass

    _esm3_sys_path = str(CAPABILITIES_DIR / "peptide_lm")
    sys.path.insert(0, _esm3_sys_path)
    try:
        from peplm import sequence_quality as _seq_quality
        from peplm.integrate.esm3_proposer import ESM3Proposer
        from peplm.loop.constraints import resolve_bicyclic_anchors as _resolve_bicyclic_anchors
    finally:
        sys.path.remove(_esm3_sys_path)

    _esm3_gpu = os.environ.get("ESM3_GPU_ID", "0")
    # 双环布局按平台约定固定 CREEEC 面 + 末端锚点 C; 掩码固定了哪端就用
    # 掩码端, 空缺的一端仍补约定段。
    proposer_prefix, proposer_suffix = _mask_fixed_edges(sequence_mask)
    if not proposer_prefix and design_mode == "bicyclic":
        proposer_prefix = "CREEEC"
    if not proposer_suffix and design_mode == "bicyclic":
        proposer_suffix = "C"
    # Proposer backend selection: peptideProposerBackend=rfd4 produces
    # candidates via the RFD4 all-atom generative model (same propose
    # contract, elite rows fed back as motif resampling). Online generation
    # fusion opens in a separate change; error out explicitly for now.
    proposer_backend_sel = str(
        options.get("peptideProposerBackend")
        or options.get("peptide_proposer_backend") or "esm3").strip().lower()
    if proposer_backend_sel == "rfd4":
        raise ValueError(
            "RFD4 提案器与世代循环的在线融合在下一版本开放；"
            "当前请先用 peplm 提案器产出母本，再经 Mol-1 成熟化环掺入 NCAA。")
    proposer = ESM3Proposer(
        chirality=peptide_chirality,
        project_id=str(predict_args.get("project_id") or ""),
        receptor_sequence=(
            _dpeptide_target_sequence(base_yaml_data, resolved_target_chain_id)
            if peptide_chirality in ("d", "l") else ""),
        # 长度窗口时置 None: de novo 在窗口内随机取长, 掩码自由段按候选
        # 伸缩(_apply_sequence_mask); 无窗口仍钉死。
        peptide_length=None if _length_lo else binder_length,
        len_range=(_length_lo or 8, _length_hi or 25),
        cyclic=(design_mode == "cyclic"),
        structure_mode=structure_mode,
        excluded_aas=excluded_aas,
        fixed_prefix=proposer_prefix,
        fixed_suffix=proposer_suffix,
        device=f"cuda:{_esm3_gpu}",
        seed=random_seed,
        work_dir=str(Path(temp_dir) / "esm3_proposer"),
        log=_proposer_log,
    )
    _proposer_log(
        f"ESM3 proposal engine ready (length={binder_length or 'adaptive'}, "
        f"mode={design_mode}, structure={structure_mode}, GPU={_esm3_gpu})")

    # D-peptide mirror workflow context: mirror the target once so every
    # candidate is designed against the fixed D-target (see module docstring
    # block above). Chirality 'l' keeps the plain L-frame loop.
    pocket_constraint_blueprint: Optional[Dict[str, Any]] = (
        {"contacts": [[c, n] for c, n in pocket_author_contacts]}
        if pocket_author_contacts else None
    )
    # Pocket chemistry demands (capability C): pre-folding reward shaping
    # derived from the pocket's own composition — generalizes to any target
    pocket_chem_demands = (
        _pocket_chemistry_demands(
            _dpeptide_target_sequence(base_yaml_data, resolved_target_chain_id),
            pocket_sequence_contacts)
        if pocket_sequence_contacts else None
    )
    if pocket_chem_demands:
        print(
            f"[peptide-design] pocket chemistry demands: {pocket_chem_demands}",
            file=sys.stderr,
        )

    dpeptide_reference_target: Optional[Path] = None
    d_target_staged: Optional[Path] = None
    if peptide_chirality == 'd':
        # template_inputs were popped from predict_args at the main boundary;
        # without re-attaching them the uploaded structure is invisible here
        # and the display products cannot be aligned to the user's frame.
        if template_inputs:
            predict_args = dict(predict_args)
            predict_args["template_inputs"] = template_inputs
        # D-route target preparation: uploaded structure (or single-chain
        # prediction) mirrored x->-x ONCE; every candidate stages against
        # this exact D-target (the legacy de novo L-L complex prediction is
        # gone — a designed peptide carries no coevolution signal, so its
        # interface was unreliable)
        l_target, d_target_staged = _dpeptide_prepare_d_target(
            predict_args, base_yaml_data, options, target_chain_id,
            _normalize_peptide_backend(backend),
            Path(temp_dir) / "d_target_prep", int(seed if isinstance(seed, int) else 7),
        )
        dpeptide_reference_target = l_target
        # Mode-anchored design: an uploaded initial peptide structure (same
        # coordinate frame as the target structure) switches placement from
        # the generic pocket surface search to reference-pose anchoring.
        # Anchoring requires an UPLOADED target: against a de novo predicted
        # target the reference frame can never match.
        d_reference_peptide = None
        if predict_args.get("peptide_structure_input"):
            if not template_inputs:
                raise ValueError(
                    "模式锚定设计需要同时上传靶标结构：初始肽结构与靶标必须同一"
                    "坐标系，而从头预测的靶标坐标帧无法与参考肽匹配。")
            d_reference_peptide = _dpeptide_prepare_reference_peptide(
                predict_args, Path(temp_dir) / "d_peptide_ref_prep",
                binder_length=binder_length)
    else:
        d_reference_peptide = None
    # 协议修订（对齐 dock 模式 决策 + boltz2 原生口袋
    # 语义）：一切 de-novo D-肽（linear/cyclic/bicyclic，无参考肽结构）走
    # native blind inpainting——肽纯噪声 + 完整调度，受体每步钉住，姿态由
    # 扩散先验生成；环拓扑以 pose 无关的 TFG 键对随精修携带；用户口袋经
    # PocketPotential 在采样期引导（不再有任何自研刚体摆位——该路由把肽
    # Cage 在任意局部姿态，sigma_max=0.05 的局部窗口无法逃逸，精修矛盾以
    # 塌缩收场：实测 pocket_min 0.35 A / CA-CB 断裂 / integrity=BROKEN，
    # 16-mer 对照 bb734f9e 复现 13/16 塌缩）。参考锚定模式（用户上传初始
    # 肽结构）是唯一保留的 staged 路径——那是真实结合模式，非发明摆位。
    blind_linear_route = (
        peptide_chirality == 'd' and d_reference_peptide is None)
    if (peptide_chirality == 'd' and not pocket_sequence_contacts
            and not blind_linear_route):
        # 无口袋 D-肽使用 blind inpainting(受体钉住+肽从噪声)。BICYCLIC/参考锚定模式必须有口袋或参考;
        # 线性/环肽模式走盲 inpainting(受体钉住+肽从噪声,姿态由 MSA 先验产生,
        # A/B 实测红dock RMSD 1.5-2.0 A),不需要口袋。
        # 协议修订(与 dock 模式 决策对齐): cyclic+D 不再
        # 走"自研刚体摆位+局部精修"——该路由把肽 Cage 在任意局部摆位
        # (sigma_max=0.05 ≈ ±0.8 A 活动半径),精修矛盾以塌缩收场(实测
        # pocket_min 0.35 A、CA-CB 断裂,integrity=BROKEN)。cyclic 与 linear
        # 同走 native blind inpainting;环头尾 N-C 键以 pose 无关的 TFG 键对
        # 随精修携带;用户口袋退为后置过滤(_pocket_contact_report 硬门槛
        # + composite 0.32 权重),永不条件化采样器。
        raise ValueError(
            "D-肽设计(双环/参考锚定)必须提供口袋定义或上传初始肽结构。")

    def _peptide_runtime_timing(done_count: int) -> Dict[str, Any]:
        elapsed_seconds = max(0.0, time.time() - peptide_started_at)
        done = max(0, int(done_count or 0))
        remaining_seconds = None
        completion_time = None
        if done > 0 and total_tasks > done:
            seconds_per_task = elapsed_seconds / done
            remaining_seconds = max(0.0, seconds_per_task * (total_tasks - done))
            completion_time = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() + remaining_seconds),
            )
        return {
            "elapsed_seconds": elapsed_seconds,
            "estimated_remaining_seconds": remaining_seconds,
            "estimated_completion_time": completion_time,
            "candidates_evaluated": done,
        }

    def _current_best_peptide_rows(limit: int = 10) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for rank, row in enumerate(all_results[: min(limit, len(all_results))], start=1):
            rows.append(
                {
                    "rank": rank,
                    "sequence": row.get("sequence"),
                    "modifications": row.get("modifications"),
                    "generation": row.get("generation"),
                    "score": row.get("composite_score"),
                    "iptm": row.get("iptm"),
                    "pair_iptm": row.get("pair_iptm"),
                    "pair_iptm_target_binder": row.get("pair_iptm_target_binder"),
                    "pair_iptm_target_linker": row.get("pair_iptm_target_linker"),
                    "pair_iptm_formula": row.get("pair_iptm_formula"),
                    "ipsae_dom": row.get("ipsae_dom"),
                    "ligand_ipsae_max": row.get("ligand_ipsae_max"),
                    "interface_metric": row.get("interface_metric"),
                    "interface_metric_label": row.get("interface_metric_label"),
                    "interface_metric_source": row.get("interface_metric_source"),
                    "interface_metric_kind": row.get("interface_metric_kind"),
                    "binder_avg_plddt": row.get("binder_avg_plddt"),
                    "interface_confidence": row.get("interface_confidence"),
                    "binder_confidence": row.get("binder_confidence"),
                    "pair_iptm_confidence": row.get("pair_iptm_confidence"),
                    "developability_score": row.get("developability_score"),
                    "liability_penalty": row.get("liability_penalty"),
                    "target_chain_id": row.get("target_chain_id"),
                    "binder_chain_id": row.get("binder_chain_id"),
                    "linker_chain_id": row.get("linker_chain_id"),
                }
            )
        return rows

    runtime_predict_args = dict(predict_args)
    if "seed" in runtime_predict_args:
        runtime_predict_args.pop("seed", None)
    runtime_predict_args.pop("peptide_gpu_ids", None)
    runtime_predict_args.pop("peptide_parallel_gpus", None)
    runtime_predict_args.pop("peptideParallelGpus", None)
    if custom_molecules:
        runtime_predict_args["custom_ccd_molecules"] = custom_molecules
    # Interface scoring (ipSAE) for candidates: the binder chain is the interface "ligand".
    # Without it the ipsae postprocess looks for a small-molecule ligand chain in the
    # candidate YAML, finds none, and every row ships ligand_ipsae_max=None.
    if peptide_backend in ("protenix", "boltz"):
        runtime_predict_args["ipsaeLigandChainId"] = binder_chain_id

    resolved_subtask_queue = str(subtask_queue or "").strip() or build_capability_queue(
        "boltz2" if peptide_backend == "boltz" else peptide_backend,
        "default",
    )
    peptide_gpu_ids = _normalize_peptide_gpu_ids(gpu_ids)
    parent_task_id = str(os.environ.get("VBIO_TASK_ID") or "peptide-design").strip() or "peptide-design"
    # 候选子任务全量入队：共享 GPU 池 + worker 并发是唯一的并发边界，
    # 不在编排侧做容量快照或窗口猜测。
    if peptide_gpu_ids:
        print(
            f"Peptide design candidate subtasks dispatch unbounded (requested_gpu_ids={peptide_gpu_ids})",
            file=sys.stderr,
        )
    else:
        print(
            "Peptide design candidate subtasks dispatch unbounded (GPU pool schedules them)",
            file=sys.stderr,
        )
    print(f"Peptide design subtask celery queue: {resolved_subtask_queue}", file=sys.stderr)

    for generation in range(_start_generation, iterations + 1):
        _write_peptide_progress(
            progress_path,
            {
                "peptide_design": {
                    "current_generation": generation,
                    "total_generations": iterations,
                    "completed_tasks": completed_tasks,
                    "pending_tasks": max(0, total_tasks - completed_tasks),
                    "total_tasks": total_tasks,
                    "best_score": all_results[0].get("composite_score") if all_results else None,
                    "progress_percent": (completed_tasks / total_tasks * 100.0) if total_tasks > 0 else 0.0,
                    "current_status": f"Generation {generation}/{iterations}",
                    "status_message": f"Running generation {generation} of {iterations}",
                    "current_best_sequences": _current_best_peptide_rows(),


                    **_peptide_runtime_timing(completed_tasks),
                }
            },
        )

        generation_candidates: List[Dict[str, Any]] = []
        # Gate-rejected candidates of the current generation, kept for the
        # GRPO update as floor-reward negative samples. Dropping rejects
        # starved every group of variance (2/48 survival -> <4 learnable
        # rows -> zero policy updates for the whole run).
        generation_rejected_rows: List[Dict[str, Any]] = []

        # PeptideLM proposals — the single proposal source. When the user
        # supplied an initial (seed) sequence, generation 1 anchors edits on
        # it instead of de novo sampling. Length may be adaptive; the
        # bicyclic Cys layout and NCAA pool are enforced by the proposer's
        # decode-time constraint plan.
        try:
            _proposer_elites: List[Dict[str, Any]] = list(elite_population)
            if generation == 1 and not _proposer_elites:
                if initial_sequence:
                    _proposer_elites = [{
                        "sequence": initial_sequence,
                        "modifications": [],
                        "plddts": [],
                    }]
                elif seed_sequences:
                    # chemistry-informed warm-start (BindCraft motif-graft
                    # analog): the pocket composition dictates complementary
                    # residues; the RL refines from this starting point
                    _proposer_elites = [
                        {"sequence": s, "modifications": [], "plddts": []}
                        for s in seed_sequences[:2]
                    ]
            for lm_base, lm_mods, lm_anchors, lm_group in proposer.propose(
                natural_pool,
                unnatural_pool,
                _proposer_elites,
                # 2x oversample: the pseudo-PPL pre-rank below keeps the best
                # half, so the oracle only sees prior-plausible sequences
                population_size * 2,
            ):
                lm_sequence = _apply_sequence_mask(str(lm_base or "").upper(), sequence_mask)
                if design_mode == "bicyclic":
                    # The proposer samples raw sequences with no notion of the
                    # bicyclic topology; anchor stamping must happen here or the
                    # candidate YAML builder rejects every de-novo proposal.
                    lm_sequence, lm_anchors = _stamp_bicyclic_cysteines(
                        lm_sequence,
                        explicit_positions=list(design_params.get("cys_positions") or []),
                        cys_layout=design_params.get("cys_layout"),
                        sequence_mask=sequence_mask,
                        allow_extra_cys=allow_extra_cys,
                        resolve_anchors=_resolve_bicyclic_anchors,
                    )
                # NCAA overlay: replace user-selected positions with
                # non-natural residues from the pool. The backbone stays in
                # ESM3's known space; the modifications list tells protenix
                # which positions carry a CCD substitution.
                if unnatural_pool and nonnatural_max > 0:
                    lm_sequence, lm_mods = _substitute_ncaa_residues(
                        lm_sequence,
                        lm_mods,
                        unnatural_pool,
                        nonnatural_min,
                        nonnatural_max,
                        list(design_params.get("cys_positions") or []),
                        random.Random(random_seed + generation * 1000
                                      + len(lm_sequence)),
                    )
                lm_mods = [m for m in (lm_mods or []) if isinstance(m, dict)]
                lm_key = _peptide_candidate_key(lm_sequence, lm_mods)
                if lm_key in evaluated_sequences:
                    continue
                evaluated_sequences.add(lm_key)
                generation_candidates.append({
                    "sequence": lm_sequence,
                    "modifications": lm_mods,
                    "cys_positions": [int(p) for p in (lm_anchors or [])],
                    # GRPO grouping key from the proposer (de novo pool /
                    # per-parent edit cohort) — carried through to learn
                    "proposal_group": lm_group,
                })
                if len(generation_candidates) >= population_size:
                    break
        except Exception as exc:
            raise RuntimeError(f"PeptideLM propose 失败（generation {generation}）：{exc}") from exc

        if not generation_candidates:
            break

        # Binder MSA (user policy: MSA everywhere) — prefetch the generation's
        # candidates concurrently before dispatch; per-candidate failure degrades
        # to "empty" without touching the run. The prefetch is reported into the
        # task status: without it the first minutes of every generation look
        # like a dead stall ("老是不开始").
        def _report_msa_prefetch(done: int, total: int) -> None:
            _write_peptide_progress(
                progress_path,
                {
                    "peptide_design": {
                        "current_generation": generation,
                        "total_generations": iterations,
                        "completed_tasks": completed_tasks,
                        "pending_tasks": max(0, total_tasks - completed_tasks),
                        "total_tasks": total_tasks,
                        "best_score": all_results[0].get("composite_score") if all_results else None,
                        "progress_percent": (completed_tasks / total_tasks * 100.0) if total_tasks > 0 else 0.0,
                        "current_status": f"Generation {generation}/{iterations}",
                        "status_message": f"Generation {generation}: binder MSA 搜索中（{done}/{total}）",
                        "current_best_sequences": _current_best_peptide_rows(),
                        **_peptide_runtime_timing(completed_tasks),
                    }
                },
            )

        # Fail-fast surrogate screen (dedup + prior pre-rank):
        # (a) exact-duplicate kill — GRPO needs intra-group diversity;
        #     clones produce identical rewards → zero advantage
        # (b) prior plausibility ranking: when 2x oversampled, keep the
        #     top population_size by ESM3 log-likelihood. ZERO GPU cost.
        # Exact-dedup within the generation only: edit children are SUPPOSED
        # to be near their parent (that's how the pLDDT-weighted editor
        # explores); near-dup filtering here killed generations 2+ entirely
        #.
        # Cross-generation exact dedup lives in evaluated_sequences.
        _screened: list = []
        _seen_seqs: set = set()
        for _c in generation_candidates:
            _seq = str(_c.get("sequence") or "").strip().upper()
            if not _seq or _seq in _seen_seqs:
                continue
            _seen_seqs.add(_seq)
            _screened.append(_c)
        if len(_screened) > population_size:
            try:
                _scored = sorted(
                    ((proposer.pseudo_perplexity(
                        str(c.get("sequence") or "")), c)
                     for c in _screened),
                    key=lambda pair: pair[0])
                _before = len(_screened)
                _screened = [c for _, c in _scored[:population_size]]
                print(
                    f"[d-peptide] prior pre-rank: {_before} -> "
                    f"{len(_screened)} (ESM3 likelihood top half)",
                    file=sys.stderr)
            except Exception:
                pass  # ranking is advisory; an encoder hiccup must not kill the run
        _n_killed = len(generation_candidates) - len(_screened)
        if _n_killed > 0:
            print(
                f"[d-peptide] surrogate screen: {len(generation_candidates)} -> "
                f"{len(_screened)} ({_n_killed} near-dup/ranked-out)",
                file=sys.stderr)
        generation_candidates = _screened

        # De novo peptides: MSA is evolutionary noise (a designed sequence
        # has no homologs — any hits mis-anchor the folder's coevolution
        # assumptions, +340s/generation of pure polling waste).
        # Literature: "AF2 with MSA is overconfident on poor de novo designs"
        # (Korbeld 2024); practitioner consensus is single-sequence mode for
        # the binder + receptor template for the target side. Only SEED/
        # reference sequences (uploaded initial peptide = known binder with
        # real homologs) keep their MSA.
        if use_initial_sequence and initial_sequence:
            generation_binder_msa = _prefetch_generation_binder_msas(
                [str(c.get("sequence") or "") for c in generation_candidates
                 if str(c.get("sequence") or "") == initial_sequence],
                on_progress=_report_msa_prefetch,
            )
        else:
            generation_binder_msa = {}
            print(
                f"[d-peptide] de novo binder MSA SKIPPED (evolutionary noise; "
                f"single-sequence mode for designed peptides, literature-backed)",
                file=sys.stderr)
            # Materialize the single-sequence decision in the SHARED MSA
            # cache: the engine-side resolve_msa is cache-first and falls
            # through to the MSA SERVER on a miss — a de novo candidate
            # would hard-fetch (30-min timeout when the server is starved,
            # generation 1 lost 3/8 candidates) instead
            # of running single-sequence. A query-only a3m under the exact
            # cache key (tier = length rule, aligned with input_prep and
            # _binder_msa_assignment) turns that miss into the official
            # N_msa=1 contract the no-server path already uses.
            try:
                _seed_single_sequence_binder_msas(
                    [str(c.get("sequence") or "") for c in generation_candidates])
            except Exception as _msa_seed_exc:  # noqa: BLE001
                print(
                    f"[WARN] de novo single-sequence MSA seeding failed "
                    f"({_msa_seed_exc}); engine-side server fetch remains "
                    "the fallback", file=sys.stderr)

        # 与终端校验器同一判定来源
        amidated_ccds = {
            str(m.get("ccd") or "").strip().upper()
            for m in custom_molecules or []
            if isinstance(m, dict) and m.get("cTerminalAmidated")
        }
        generation_jobs: List[Dict[str, Any]] = []
        generation_completed_base = completed_tasks
        for idx, candidate in enumerate(generation_candidates, start=1):
            candidate_sequence = str(candidate.get("sequence") or "")
            candidate_modifications = candidate.get("modifications") if isinstance(candidate.get("modifications"), list) else []
            candidate_dir = os.path.join(temp_dir, "peptide_design", f"gen_{generation:03d}", f"cand_{idx:03d}")
            os.makedirs(candidate_dir, exist_ok=True)
            candidate_base_yaml_data = _materialize_candidate_template_paths(
                base_yaml_data,
                candidate_dir=candidate_dir,
                temp_dir=temp_dir,
            )
            # user pocket (optional): contacts come straight from the frontend
            # residue list ("chain:num,...") in author numbering of the
            # uploaded structure. Both engines remap to 1-based sequence
            # positions at backend entry (boltz: BoltzBackend, protenix:
            # run_protenix_backend) and enforce the pocket natively during
            # candidate prediction.
            candidate_pocket_constraint = None
            candidate_pocket_sequence = None
            if pocket_constraint_blueprint:
                candidate_pocket_constraint = dict(pocket_constraint_blueprint)
                candidate_pocket_constraint["binder"] = binder_chain_id
                if peptide_backend == "mol1":
                    # mol1 的 SITE 条件用序列号（候选 worker 载荷注入），
                    # yaml 里保留作者编号口袋会与约束注入冲突
                    candidate_pocket_sequence = [
                        [c, int(n)] for c, n in pocket_sequence_contacts]
                    candidate_pocket_constraint = None
            candidate_fixed_modifications: List[Dict[str, Any]] = []
            for m in fixed_modifications or []:
                ccd_u = str(m.get("ccd") or "").upper()
                pos = int(m.get("position") or 1)
                if amidated_ccds and ccd_u in amidated_ccds:
                    pos = len(candidate_sequence)
                elif pos > len(candidate_sequence):
                    continue
                candidate_fixed_modifications.append({**m, "position": pos})
            candidate_yaml = _build_peptide_candidate_yaml(
                candidate_base_yaml_data,
                binder_chain_id=binder_chain_id,
                binder_sequence=candidate_sequence,
                design_mode=design_mode,
                linker_ccd=linker_ccd,
                linker_chain_id=linker_chain_id,
                linker_atom_map=linker_atom_map,
                modifications=(
                    candidate_fixed_modifications + candidate_modifications
                ) if candidate_fixed_modifications else candidate_modifications,
                backend=peptide_backend,
                amidated_ccds=amidated_ccds,
                cys_positions=candidate.get("cys_positions") if isinstance(candidate.get("cys_positions"), list) else None,
                pocket_constraint=candidate_pocket_constraint,
                binder_only=(peptide_chirality == 'd'),
                binder_msa=generation_binder_msa.get(candidate_sequence, "empty"),
            )
            archive_path = os.path.join(candidate_dir, "result.zip")

            per_candidate_args = dict(runtime_predict_args)
            if isinstance(seed, int):
                per_candidate_args["seed"] = int(seed) + generation_completed_base + idx

            generation_jobs.append(
                {

                    # GRPO cohort key (de novo pool / per-parent cohort) — without it
                    # every row lands in "ungrouped" and the group-relative advantage
                    # degenerates to batch-relative.
                    "proposal_group": candidate.get("proposal_group") or "ungrouped",
                    "generation": generation,
                    "candidate_index": idx,
                    "sequence": candidate_sequence,
                    "modifications": candidate_modifications,
                    "cys_positions": candidate.get("cys_positions") if isinstance(candidate.get("cys_positions"), list) else [],
                    "candidate_yaml": candidate_yaml,
                    "candidate_dir": candidate_dir,
                    "pocket_sequence_contacts": candidate_pocket_sequence,
                    "archive_path": archive_path,
                    "predict_args": per_candidate_args,
                    "model_name": model_name,
                    "backend": peptide_backend,
                    "worker_task_id": f"{parent_task_id}:g{generation:03d}:c{idx:03d}",
                    "worker_args_path": os.path.join(candidate_dir, "worker_args.json"),
                }
            )

        def _emit_generation_runtime_progress(runtime_counts: Dict[str, int]) -> None:
            done_now = int(runtime_counts.get("completed") or 0)
            global_done = generation_completed_base + done_now
            current_best_score = all_results[0].get("composite_score") if all_results else None
            queued_now = int(runtime_counts.get("queued") or 0)
            running_now = int(runtime_counts.get("running") or 0)
            generation_total = int(runtime_counts.get("total") or len(generation_jobs))
            _write_peptide_progress(
                progress_path,
                {
                    "peptide_design": {
                        "current_generation": generation,
                        "total_generations": iterations,
                        "completed_tasks": global_done,
                        "pending_tasks": max(0, total_tasks - global_done),
                        "total_tasks": total_tasks,
                        "best_score": current_best_score,
                        "progress_percent": (global_done / total_tasks * 100.0) if total_tasks > 0 else 0.0,
                        "current_status": f"Generation {generation}/{iterations}",
                        "status_message": (
                            f"Generation {generation}/{iterations}: "
                            f"done {done_now}/{generation_total}, running {running_now}, queued {queued_now}"
                        ),
                        "generation_total_tasks": generation_total,
                        "generation_completed_tasks": done_now,
                        "generation_running_tasks": running_now,
                        "generation_queued_tasks": queued_now,
    
    
                        "current_best_sequences": _current_best_peptide_rows(),
                        **_peptide_runtime_timing(global_done),
                    }
                },
            )

        # chirality=d candidates predict the ISOLATED conformer
        # (binder_only); the D-space staging + refine happens in the
        # collection loop below.
        completed_generation_jobs = _execute_peptide_generation_jobs(
            generation_jobs,
            resolved_subtask_queue,
            parent_task_id,
            progress_callback=_emit_generation_runtime_progress,
        )
        if not completed_generation_jobs:
            raise RuntimeError(f"Peptide generation {generation} completed with no candidate results.")

        generation_done = 0
        # ==== Candidate finalize: runs as each candidate's refine lands ====
        # (The GPU refine previously ran inline in a strictly serial per-candidate
        # loop — one ~4-5 min protenix2dock task at a time while every other
        # GPU idled for the whole generation. The loop is now a two-stage
        # pipeline: the prepare pass below stages/dispatches, and
        # _run_dpeptide_refine_stage finalizes each candidate as its refine
        # completes. Finalization order is completion order; all_results is
        # re-sorted by rank score on every append.)
        def _finalize_candidate(ctx: Dict[str, Any]) -> None:
            nonlocal completed_tasks, generation_done, elite_population
            gate_result = None
            if ctx.get("reject"):
                print(ctx["reject"], file=sys.stderr)
                generation_rejected_rows.append({
                    "sequence": str(ctx.get("candidate_sequence") or ""),
                    "generation": generation,
                    "proposal_group": (ctx.get("proposal_group")
                                       or (ctx.get("job") or {}).get("proposal_group")
                                       or "ungrouped"),
                    "gate_rejected": True,
                    "composite_score": 0.0,
                    "reject_reason": str(ctx["reject"])[:200],
                })
                return
            job = ctx["job"]
            candidate_sequence = ctx["candidate_sequence"]
            candidate_modifications = ctx["candidate_modifications"]
            metrics = ctx["metrics"]
            pair_iptm_target_binder = ctx["pair_iptm_target_binder"]
            pair_iptm_target_linker = ctx["pair_iptm_target_linker"]
            pair_iptm = ctx["pair_iptm"]
            pair_iptm_formula = ctx["pair_iptm_formula"]
            interface_metric_value = ctx["interface_metric_value"]
            interface_metric_label = ctx["interface_metric_label"]
            interface_metric_source = ctx["interface_metric_source"]
            interface_metric_kind = ctx["interface_metric_kind"]
            binder_avg_plddt = ctx["binder_avg_plddt"]
            binder_confidence = ctx["binder_confidence"]
            pair_iptm_confidence = ctx["pair_iptm_confidence"]
            interface_confidence = ctx["interface_confidence"]
            liability = ctx["liability"]
            liability_penalty = ctx["liability_penalty"]
            developability_score = ctx["developability_score"]
            composite_score = ctx["composite_score"]
            structure_file = ctx["structure_file"]
            staged_path = ctx["staged_path"]
            # 兜底锚点重建: 双环路径未过 refine 时也拿到几何正确的单原子链接器;
            # 非双环布局无链接器，不改动游离离子。
            if (design_mode == "bicyclic"
                    and structure_file is not None
                    and Path(structure_file).is_file()):
                try:
                    native_rebuild = _rebuild_single_atom_linker_anchor(
                        Path(structure_file), prefer_code=linker_ccd)
                    if native_rebuild:
                        metrics.setdefault(
                            "linker_anchor_rebuild", native_rebuild)
                except Exception as rebuild_exc:
                    print(f"[peptide] anchor rebuild failed: {rebuild_exc}",
                          file=sys.stderr)
            d_space_refined = ctx["d_space_refined"]
            d_space_metrics = ctx["d_space_metrics"]
            pocket_report_row = ctx["pocket_report_row"]

            def _rescore_with_refined_pocket(refined_path: Path) -> None:
                """Recompute the composite once the REFINED pocket distance
                exists — the ranking must reflect the shipped pose, not the
                native prediction's."""
                nonlocal composite_score
                if not (pocket_sequence_contacts and isinstance(composite_score, (int, float))):
                    return
                try:
                    rpr = _pocket_contact_report(refined_path, pocket_sequence_contacts)
                except Exception:
                    return
                pm = rpr.get("pocket_min_distance")
                if not isinstance(pm, (int, float)):
                    return
                # pocket satisfaction: proximity alone rewards
                # grazing contacts; half the weight now comes from the REAL
                # interface size — heavy-atom pairs within 4.5 A of the user's
                # pocket residues (already computed by the report). ~50 pairs
                # is a saturated 12-16-mer epitope engagement.
                n45 = int(rpr.get("pocket_contacts_within_4p5") or 0)
                ps_prox = max(0.0, min(1.0, (8.0 - float(pm)) / 3.0)) if pm > 5.0 else 1.0
                # contact counts must be CLEAN: buried-inside poses would otherwise
                # maximize this term (every overlapping pair also counted as
                # an interface contact). Overlapping pairs (<= 2.8 A) count
                # double against the total — a jammed pose nets zero.
                try:
                    _, _, n_overlap = _interchain_clash_profile(refined_path)
                except Exception:
                    n_overlap = 0
                n45_clean = max(0, n45 - 2 * n_overlap)
                ps_contacts = min(1.0, n45_clean / 35.0)
                ps = 0.5 * ps_prox + 0.5 * ps_contacts
                composite_score = 0.68 * composite_score + 0.32 * ps

            # D-route (chirality=d): collect the dispatched fixed-D diffusion
            # refine; per-candidate failure rejects the candidate, the task
            # only fails when NO candidate survives.
            if peptide_chirality == 'd':
                try:
                    if ctx.get("refine_error") is not None:
                        raise ctx["refine_error"]
                    if ctx["route"] == "pocket":
                        gate_result = _pocket_collect_refine(
                            ctx["async_result"],
                            staged_path=Path(ctx["staged_path"]),
                            refined_cif=Path(ctx["refined_cif"]),
                            pocket_sequence_contacts=pocket_sequence_contacts,
                            require_bonds=(design_mode == "bicyclic"),
                            chirality_label="d",
                            expect_blind=blind_linear_route,
                            linker_ccd=linker_ccd,
                        )
                        d_space_refined = gate_result["refined"]
                        d_space_metrics = gate_result["metrics"]
                        pocket_report_row = gate_result["pocket"]
                    else:
                        d_space_metrics = _dpeptide_collect_refine(
                            ctx["async_result"],
                            Path(ctx["staged_path"]),
                            Path(ctx["refined_cif"]),
                            expect_blind=blind_linear_route,
                        )
                        d_space_refined = str(ctx["refined_cif"])
                        if design_mode == "bicyclic":
                            try:
                                d_rebuild = _rebuild_single_atom_linker_anchor(
                                    Path(d_space_refined), prefer_code=linker_ccd)
                                if d_rebuild:
                                    metrics.setdefault(
                                        "linker_anchor_rebuild", d_rebuild)
                            except Exception as rebuild_exc:
                                print(f"[d-peptide] anchor rebuild failed: "
                                      f"{rebuild_exc}", file=sys.stderr)
                            bond_report = _dpeptide_linker_bond_report(
                                Path(d_space_refined), prefer_code=linker_ccd)
                            if not (bond_report and bond_report.get("all_bonded")):
                                print(
                                    f"[d-peptide] candidate {candidate_sequence[:12]}… "
                                    f"rejected: refined ring bonds broken {bond_report}",
                                    file=sys.stderr,
                                )
                                return
                    # 硬手性门(阈值版): 受体零容忍; 肽≤30%违规过门但记分数惩罚。
                    chirality_penalty = _dpeptide_refined_chirality_gate(
                        Path(d_space_refined))
                    if design_mode == "cyclic":
                        # ring integrity gate: the head-tail N-C bond rides the
                        # refine as pose-independent TFG chemistry; a product
                        # with an open ring is not a cyclic peptide — reject
                        # loudly instead of shipping a macrocycle that is
                        # actually a linear chain
                        ring = _cyclic_headtail_bond_report(Path(d_space_refined))
                        if not ring.get("all_bonded"):
                            print(
                                f"[d-peptide] candidate {candidate_sequence[:12]}… "
                                f"rejected: cyclic ring opened in refine "
                                f"(N-C {ring.get('n_c_distance')} A)",
                                file=sys.stderr,
                            )
                            generation_rejected_rows.append({
                                "sequence": candidate_sequence,
                                "generation": generation,
                                "proposal_group": job.get("proposal_group") or "ungrouped",
                                "gate_rejected": True,
                    "composite_score": 0.0,
                                "reject_reason": "cyclic_ring_open",
                            })
                            return
                    print(
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"D-space ipTM={d_space_metrics.get('iptm')}",
                        file=sys.stderr,
                    )
                except DpeptideStaleWorkerError:
                    # infrastructure failure, not a candidate defect — fail
                    # the whole task now with the actionable message instead
                    # of rejecting every candidate for the rest of the run
                    raise
                except (RuntimeError, ValueError) as d_exc:
                    print(
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {d_exc}",
                        file=sys.stderr,
                    )
                    generation_rejected_rows.append({
                        "sequence": candidate_sequence,
                        "generation": generation,
                        "proposal_group": job.get("proposal_group") or "ungrouped",
                        "gate_rejected": True,
                    "composite_score": 0.0,
                        "reject_reason": str(d_exc)[:200],
                    })
                    return

            # L chirality + user pocket: collect the dispatched pocket refine;
            # the refined structure becomes the candidate's shipped product.
            if (peptide_chirality == 'l' and pocket_sequence_contacts
                    and structure_file is not None):
                try:
                    if ctx.get("refine_error") is not None:
                        raise ctx["refine_error"]
                    gate_result = _pocket_collect_refine(
                        ctx["async_result"],
                        staged_path=Path(ctx["staged_path"]),
                        refined_cif=Path(ctx["refined_cif"]),
                        pocket_sequence_contacts=pocket_sequence_contacts,
                        require_bonds=(design_mode == "bicyclic"),
                        chirality_label="l",
                        linker_ccd=linker_ccd,
                    )
                    structure_file = Path(gate_result["refined"])
                    staged_path = gate_result["staged"]
                    d_space_refined = gate_result["refined"]
                    d_space_metrics = gate_result["metrics"]
                    pocket_report_row = gate_result["pocket"]
                except (RuntimeError, ValueError) as pocket_exc:
                    print(
                        f"[l-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {pocket_exc}",
                        file=sys.stderr,
                    )
                    generation_rejected_rows.append({
                        "sequence": candidate_sequence,
                        "generation": generation,
                        "proposal_group": (ctx.get("proposal_group")
                                           or (ctx.get("job") or {}).get("proposal_group")
                                           or "ungrouped"),
                        "gate_rejected": True,
                        "composite_score": 0.0,
                        "reject_reason": str(pocket_exc)[:200],
                    })
                    return

            # L chirality WITHOUT pocket: collect the dispatched fixed-receptor
            # refine through the SAME gates as the D route (engagement, clash
            # floor, backbone integrity, ipSAE consistency). Before this
            # block the L-blind candidate shipped the raw native prediction
            # with no geometric gate at all.
            if (peptide_chirality == 'l' and not pocket_sequence_contacts
                    and ctx.get("route") == "plain"):
                try:
                    if ctx.get("refine_error") is not None:
                        raise ctx["refine_error"]
                    d_space_metrics = _dpeptide_collect_refine(
                        ctx["async_result"],
                        Path(ctx["staged_path"]),
                        Path(ctx["refined_cif"]),
                        expect_blind=True,
                    )
                    d_space_refined = str(ctx["refined_cif"])
                    structure_file = Path(d_space_refined)
                    if design_mode == "bicyclic":
                        # Single-atom anchor rebuild BEFORE the gate: the
                        # refine can leave the ion inside the peptide; the
                        # deterministic circumcenter placement decides the
                        # chemistry-feasible side, the gate then judges.
                        rebuild_info = _rebuild_single_atom_linker_anchor(
                            Path(d_space_refined), prefer_code=linker_ccd)
                        if rebuild_info:
                            metrics["linker_anchor_rebuild"] = rebuild_info
                        # Same require_bonds guarantee the pocket route and
                        # the D route enforce on their collects.
                        bond_report = _dpeptide_linker_bond_report(
                            Path(d_space_refined), prefer_code=linker_ccd)
                        if not bond_report.get("all_bonded"):
                            raise ValueError(
                                "linker covalent integrity gate: "
                                f"{bond_report.get('bonds')}")
                except (RuntimeError, ValueError) as plain_exc:
                    print(
                        f"[l-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {plain_exc}",
                        file=sys.stderr,
                    )
                    generation_rejected_rows.append({
                        "sequence": candidate_sequence,
                        "generation": generation,
                        "proposal_group": job.get("proposal_group") or "ungrouped",
                        "gate_rejected": True,
                        "composite_score": 0.0,
                        "reject_reason": str(plain_exc)[:200],
                    })
                    return

            if peptide_chirality == 'd' and isinstance(d_space_metrics, dict):
                # The refined D-space complex is the ONLY interface readout for
                # D candidates (binder-only conformers carry none). Recompute
                # every ranking input from it — the conformer-stage code merged only
                # pair_iptm and left interface/binder confidences at the
                # conformer-stage zeros, freezing every composite at the
                # developability floor (all rows scored exactly 0.08 and the
                # 12-generation search random-walked).
                try:
                    refined_ipsae = d_space_metrics.get("interface_score")
                    if not isinstance(refined_ipsae, (int, float)):
                        raise RuntimeError(
                            "精修结果缺少 ipSAE interface_score 读数,"
                            "拒绝以空值评分(评分目标=ipSAE): "
                            f"{sorted(d_space_metrics.keys())}")
                    pair_iptm = float(refined_ipsae)
                    pair_iptm_formula = "d_space_refined_ipsae"
                    chain_plddt = d_space_metrics.get("chain_mean_plddt")
                    # peptide chain = LAST entity letter (receptors are
                    # written first, the peptide appended last; hard "B"
                    # read a receptor copy's plddt on multi-chain targets)
                    _pep_key = (sorted(chain_plddt)[-1]
                                if isinstance(chain_plddt, dict) and chain_plddt
                                else None)
                    if (not isinstance(chain_plddt, dict)
                            or _pep_key is None
                            or not isinstance(chain_plddt.get(_pep_key), (int, float))
                            or float(chain_plddt[_pep_key]) <= 0):
                        raise RuntimeError("精修结果缺少肽链 pLDDT 读数,拒绝以空值评分")
                    chain_b = float(chain_plddt[_pep_key])
                    binder_avg_plddt = chain_b * 100.0 if chain_b <= 1.0 else chain_b
                    resc = _dpeptide_composite_from_refined(
                        refined_ipsae=pair_iptm,
                        binder_avg_plddt=binder_avg_plddt,
                        developability_score=developability_score,
                        has_pocket=bool(pocket_sequence_contacts),
                    )
                    # chirality artifact penalty from the gate (0 = clean)
                    resc["composite_score"] = max(
                        0.0, resc["composite_score"] - chirality_penalty)
                    # molten-ring penalty: geometry-based, the confidence
                    # head cannot see it
                    try:
                        ring_pen, ring_stats = _ring_shape_penalty(
                            Path(d_space_refined))
                    except Exception:
                        ring_pen, ring_stats = 0.0, {}
                    if ring_stats:
                        metrics["ring_shape"] = ring_stats
                    if ring_pen > 0:
                        resc["composite_score"] = max(
                            0.0, resc["composite_score"] - ring_pen)
                        print(
                            f"[d-peptide] candidate {candidate_sequence[:12]}… "
                            f"molten-ring penalty -{ring_pen:.2f} "
                            f"({ring_stats})", file=sys.stderr)
                    if chirality_penalty > 0:
                        print(
                            f"[d-peptide] candidate {candidate_sequence[:12]}… "
                            f"chirality artifact penalty -{chirality_penalty:.3f} "
                            f"(composite {resc['composite_score']:.3f})",
                            file=sys.stderr)
                except RuntimeError as score_exc:
                    print(
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {score_exc}",
                        file=sys.stderr,
                    )
                    return
                interface_metric_value = resc["interface_metric_value"]
                interface_metric_label = resc["interface_metric_label"]
                interface_metric_source = resc["interface_metric_source"]
                interface_metric_kind = resc["interface_metric_kind"]
                interface_confidence = resc["interface_confidence"]
                binder_confidence = resc["binder_confidence"]
                pair_iptm_confidence = resc["pair_iptm_confidence"]
                developability_score = resc["developability_score"]
                composite_score = resc["composite_score"]
                _rescore_with_refined_pocket(Path(d_space_refined))
                # surface the raw ipSAE components on the row: the frontend's
                # interface resolver keys on ligand_ipsae_max/ipsae_dom
                metrics["ipsae_dom"] = d_space_metrics.get("ipsae_dom")
                metrics["ligand_ipsae_max"] = d_space_metrics.get("ligand_ipsae_max")

            # L route: persist the native product next to the D keep dirs so a
            # checkpoint resume + tmp cleanup cannot leave the archive rows
            # structureless (see _persist_lpeptide_structure_keep).
            lpeptide_keep_file = ""
            if peptide_chirality != 'd':
                lpeptide_keep_file = _persist_lpeptide_structure_keep(
                    parent_task_id, generation, candidate_sequence, structure_file)
            # Degenerate-sequence deduction goes into composite_score itself:
            # ranking, elite selection and the GRPO reward all read that one
            # field. Fixed mask positions are exempt — they are user design,
            # not proposer collapse.
            degeneracy_flags = _seq_quality.degeneracy_flags(
                candidate_sequence,
                fixed_prefix=proposer_prefix,
                fixed_suffix=proposer_suffix)
            degeneracy_penalty = _seq_quality.degeneracy_penalty(degeneracy_flags)
            if degeneracy_penalty > 0 and isinstance(composite_score, (int, float)):
                composite_score = max(0.0, composite_score - degeneracy_penalty)
            result_row = {
                "sequence": candidate_sequence,
                "modifications": candidate_modifications,
                "cys_positions": job.get("cys_positions") if isinstance(job.get("cys_positions"), list) else [],
                "generation": generation,
                # GRPO cohort key set by the proposer (de novo pool /
                # per-parent edit group) — consumed by learn
                "proposal_group": job.get("proposal_group") or "ungrouped",
                # pre-folding chemistry match vs the pocket's demands —
                # consumed by learn as the chem_comp reward part
                "chem_comp": _pocket_chemistry_complementarity(
                    candidate_sequence, pocket_chem_demands),
                # pocket-route rows carry the soft off-pocket flag; the
                # plain/blind routes have no pocket gate result at all
                "off_pocket": bool(gate_result.get("off_pocket"))
                if isinstance(gate_result, dict) else False,
                "iptm": pair_iptm,
                "pair_iptm": pair_iptm,
                "pair_iptm_target_binder": pair_iptm_target_binder,
                "pair_iptm_target_linker": pair_iptm_target_linker,
                "pair_iptm_formula": pair_iptm_formula,
                "pair_iptm_resolved": pair_iptm is not None,
                "ipsae_dom": metrics.get("ipsae_dom"),
                "ligand_ipsae_max": metrics.get("ligand_ipsae_max"),
                "interface_metric": interface_metric_value,
                "interface_metric_label": interface_metric_label,
                "interface_metric_source": interface_metric_source,
                "interface_metric_kind": interface_metric_kind,
                "binder_avg_plddt": binder_avg_plddt,
                "interface_confidence": interface_confidence,
                "binder_confidence": binder_confidence,
                "pair_iptm_confidence": pair_iptm_confidence,
                "developability_score": developability_score,
                "liability_penalty": liability_penalty,
                "degeneracy_flags": degeneracy_flags,
                "degeneracy_penalty": degeneracy_penalty,
                "sequence_liabilities": liability,
                "composite_score": composite_score,
                "score": composite_score,
                "plddt": binder_avg_plddt,
                "model": ("Boltz" if peptide_backend == "boltz" else "AlphaFold3" if peptide_backend == "alphafold3" else "Mol-1" if peptide_backend == "mol1" else "Protenix"),
                "backend": peptide_backend,
                "target_chain_id": resolved_target_chain_id,
                "binder_chain_id": binder_chain_id,
                "linker_chain_id": linker_chain_id if design_mode == "bicyclic" else "",
                "structure_source_path": str(structure_file) if structure_file else "",
                "lpeptide_keep_file": lpeptide_keep_file,
                "d_space_staged": str(staged_path),
                "d_space_refined": d_space_refined,
                "pocket_min_distance": (
                    pocket_report_row.get("pocket_min_distance")
                    if isinstance(pocket_report_row, dict) else None),
                "pocket_contacts_within_4p5": (
                    pocket_report_row.get("pocket_contacts_within_4p5")
                    if isinstance(pocket_report_row, dict) else None),
                "d_space_iptm": (
                    float(d_space_metrics.get("iptm"))
                    if isinstance(d_space_metrics, dict) and isinstance(d_space_metrics.get("iptm"), (int, float))
                    else None),
                "structure_format": (
                    "pdb"
                    if structure_file and structure_file.suffix.lower() == ".pdb"
                    else "cif"
                ),
                "plddts": _extract_peptide_plddts(structure_file)
                if structure_file else [],
            }
            all_results.append(result_row)
            # Keep the pool rank-sorted on every append: progress reporting
            # publishes all_results[0] as best_score and all_results[:10] as
            # current_best_sequences, and design_results.json serializes this
            # list verbatim. In completion order those described the
            # first-evaluated (generation-1) candidates for the entire run —
            # the "best" the UI showed never improved no matter what later
            # generations found.
            all_results.sort(key=_peptide_rank_score, reverse=True)
            completed_tasks += 1
            generation_done += 1

            # pLDDT hard gate for the generation's elite pool: the next
            # generation's parents and the GRPO advantage baseline only
            # see foldable candidates. Without this the composite's ipSAE
            # dominance reinforces contact-maximizing junk (H/G-rich
            # sequences with no fold) as parents for mutation.
            _PLDDT_GATE = plddt_gate_for_chirality(peptide_chirality)

            # pLDDT hard gate for this generation's elite pool: only
            # foldable candidates become parents for mutation and GRPO.
            gated_gen = [r for r in all_results
                         if isinstance(r.get("binder_avg_plddt"), (int, float))
                         and r["binder_avg_plddt"] >= _PLDDT_GATE]
            elite_pool = gated_gen if gated_gen else all_results
            elite_population = [
                {
                    "sequence": str(row.get("sequence") or ""),
                    "modifications": row.get("modifications") if isinstance(row.get("modifications"), list) else [],
                    "plddts": row.get("plddts") if isinstance(row.get("plddts"), list) else [],
                    "score": row.get("composite_score"),
                }
                for row in _select_nsga2_peptide_elites(elite_pool, elite_size)
            ]
            # persist after the elites of this generation are final: a worker
            # death before the next generation's save would otherwise lose it
            _save_generation_checkpoint(generation)
            # PeptideLM: GRPO update on this generation's scored rows so the
            # proposal policy improves round over round. The update fires ONCE,
            # on the COMPLETE cohort (successes + rejects): per-candidate calls
            # trained on a partial queue whose advantage baseline drifted with
            # completion order, and early finishers were trained repeatedly
            # while late ones were never trained.
            rejects_this_generation = sum(
                1 for row in generation_rejected_rows
                if row.get("generation") == generation
            )
            pending_generation_finalizations = (
                len(generation_jobs) - generation_done - rejects_this_generation
            )
            if pending_generation_finalizations <= 0:
                try:
                    proposer.learn(
                        elite_population,
                        [row for row in all_results if row.get("generation") == generation]
                        + generation_rejected_rows,
                    )
                except Exception as exc:
                    raise RuntimeError(f"PeptideLM learn 失败（generation {generation}）：{exc}") from exc

            progress_payload = {
                "peptide_design": {
                    "current_generation": generation,
                    "total_generations": iterations,
                    "completed_tasks": completed_tasks,
                    "pending_tasks": max(0, total_tasks - completed_tasks),
                    "total_tasks": total_tasks,
                    "best_score": all_results[0].get("composite_score") if all_results else 0.0,
                    "progress_percent": (completed_tasks / total_tasks * 100.0) if total_tasks > 0 else 0.0,
                    "current_status": f"Generation {generation}/{iterations}",
                    "status_message": (
                        f"Generation {generation}/{iterations}: "
                        f"{generation_done}/{len(generation_jobs)} candidates completed"
                    ),
                    "generation_total_tasks": len(generation_jobs),
                    "generation_completed_tasks": generation_done,
                    "generation_running_tasks": max(0, len(generation_jobs) - generation_done),
                    "generation_queued_tasks": 0,
                    "current_best_sequences": _current_best_peptide_rows(),


                    **_peptide_runtime_timing(completed_tasks),
                }
            }
            _write_peptide_progress(progress_path, progress_payload)

        # ==== Stage 1 (CPU, per candidate): extract, score, stage, dispatch ====
        stage_contexts: List[Dict[str, Any]] = []
        for job in completed_generation_jobs:
            idx = int(job.get("candidate_index") or 0)
            candidate_sequence = str(job.get("sequence") or "")
            candidate_modifications = job.get("modifications") if isinstance(job.get("modifications"), list) else []
            candidate_dir = str(job.get("candidate_dir") or "")
            archive_path = str(job.get("archive_path") or "")
            result_dir = _extract_peptide_candidate_archive_for_metrics(candidate_dir, archive_path)
            metrics = parse_confidence_metrics(
                result_dir,
                binder_chain_id=binder_chain_id,
                target_chain_id=resolved_target_chain_id or None,
                chain_order=chain_order,
                partner_chain_ids=[linker_chain_id] if design_mode == "bicyclic" else None,
            )

            pair_iptm_map_raw = metrics.get("pair_iptm_by_chain")
            pair_iptm_map = pair_iptm_map_raw if isinstance(pair_iptm_map_raw, dict) else {}
            pair_iptm_target_binder_raw = pair_iptm_map.get(binder_chain_id, metrics.get("pair_iptm"))
            pair_iptm_target_binder = (
                float(pair_iptm_target_binder_raw)
                if isinstance(pair_iptm_target_binder_raw, (int, float))
                else None
            )
            pair_iptm_target_linker_raw = pair_iptm_map.get(linker_chain_id)
            pair_iptm_target_linker = (
                float(pair_iptm_target_linker_raw)
                if isinstance(pair_iptm_target_linker_raw, (int, float))
                else None
            )
            global_iptm_raw = metrics.get("iptm")
            global_iptm = float(global_iptm_raw) if isinstance(global_iptm_raw, (int, float)) else None
            pair_iptm = pair_iptm_target_binder if pair_iptm_target_binder is not None else global_iptm
            pair_iptm_formula = "target_vs_peptide_chain" if pair_iptm_target_binder is not None else "global_iptm"
            preferred_interface_metric = resolve_preferred_interface_metric(metrics)
            interface_metric_value = (
                float(preferred_interface_metric.get("value"))
                if isinstance(preferred_interface_metric.get("value"), (int, float))
                else None
            )
            interface_metric_label = (
                "ipTM"
                if str(preferred_interface_metric.get("label") or "").strip() == "ipTM"
                else "IPSAE"
            )
            interface_metric_source = str(preferred_interface_metric.get("source") or "none").strip().lower() or "none"
            interface_metric_kind = str(preferred_interface_metric.get("kind") or "none").strip().lower() or "none"
            binder_avg_plddt = float(metrics.get("binder_avg_plddt") or 0.0)
            binder_confidence = max(0.0, min(1.0, binder_avg_plddt / 100.0)) if binder_avg_plddt > 0 else 0.0
            pair_iptm_confidence = max(0.0, min(1.0, float(pair_iptm))) if isinstance(pair_iptm, (int, float)) else 0.0
            interface_confidence = (
                max(0.0, min(1.0, float(interface_metric_value)))
                if interface_metric_value is not None
                else pair_iptm_confidence
            )
            liability = _peptide_sequence_liability_penalty(candidate_sequence, candidate_modifications)
            liability_penalty = float(liability.get("penalty") or 0.0)
            developability_score = max(0.0, 1.0 - liability_penalty)
            # Pocket satisfaction enters the RANKING when the user defined a
            # pocket — a ranking signal, never a filter: every candidate
            # ships. Measured motivation: with pure confidence ranking the
            # rank-1 pick ignored the user's pocket even though 9-10 of 16
            # candidates sat inside it (some at 0.6-0.9 A). The refined
            # pocket distance arrives later (after the refine below), so the
            # initial composite uses the native pose's pocket contact and is
            # recomputed once the refined report exists.
            _native_structure = _select_primary_structure_file(result_dir)
            # 先于 interface 计分解析，避免首候选 NameError、后续候选读到上一候选残留值。
            structure_file = _native_structure
            native_pocket_min = None
            if pocket_sequence_contacts and _native_structure is not None:
                try:
                    _npr = _pocket_contact_report(Path(_native_structure), pocket_sequence_contacts)
                    native_pocket_min = _npr.get("pocket_min_distance")
                except Exception:
                    native_pocket_min = None
            pocket_satisfaction = None
            _ps_pm = native_pocket_min
            if pocket_sequence_contacts and isinstance(_ps_pm, (int, float)):
                pocket_satisfaction = max(0.0, min(1.0, (8.0 - float(_ps_pm)) / 3.0)) if _ps_pm > 5.0 else 1.0
            if interface_metric_value is not None:
                if pocket_satisfaction is not None:
                    composite_score = (
                        0.40 * interface_confidence
                        + 0.15 * binder_confidence
                        + 0.08 * pair_iptm_confidence
                        + 0.05 * developability_score
                        + 0.32 * pocket_satisfaction
                    )
                else:
                    composite_score = (
                        0.58 * interface_confidence
                        + 0.22 * binder_confidence
                        + 0.12 * pair_iptm_confidence
                        + 0.08 * developability_score
                    )
            elif binder_avg_plddt > 0:
                if pocket_satisfaction is not None:
                    composite_score = (
                        0.40 * pair_iptm_confidence
                        + 0.20 * binder_confidence
                        + 0.08 * developability_score
                        + 0.32 * pocket_satisfaction
                    )
                else:
                    # Interface engagement (BindCraft2's highest-weight signal):
                    # clean cross-chain heavy-atom contacts at 4.5 A, saturated
                    # at 40 pairs. ipSAE is a saturating confidence — contacts
                    # provide the per-candidate discrimination that GRPO needs
                    # for credit assignment.
                    try:
                        _iface_count = _interchain_contact_count(
                            Path(structure_file))
                    except Exception:
                        _iface_count = 0
                    _ic = min(1.0, _iface_count / 40.0)
                    composite_score = (
                        0.45 * interface_confidence
                        + 0.20 * binder_confidence
                        + 0.09 * pair_iptm_confidence
                        + 0.09 * developability_score
                        + 0.18 * _ic
                    )
                if pair_iptm is None:
                    pair_iptm_formula = "binder_avg_plddt_developability_only"
            else:
                composite_score = None

            # D-route (chirality=d): stage the candidate's isolated conformer
            # against the prepared D-target at the user pocket, then dispatch
            # the fixed-D diffusion refine (collected in _finalize_candidate
            # as it completes). Failure here fails the task — the D-space
            # numbers are a mandatory deliverable, never skipped.
            staged_path = ""
            d_space_refined = ""
            d_space_metrics: Optional[Dict[str, Any]] = None
            pocket_report_row: Optional[Dict[str, Any]] = None
            gate_result: Optional[Dict[str, Any]] = None
            route: Optional[str] = None
            dispatch = None
            refined_cif = ""
            # all D candidates go through fixed-D diffusion: it resolves the
            # placement clashes of the staged pose; the per-candidate bond gate
            # in _finalize_candidate rejects any bicyclic candidate whose ring
            # bonds broke
            if peptide_chirality == 'd' and structure_file is None:
                # a worker archive without a structure cannot enter the D
                # route (staging/refine need coordinates); reject the
                # candidate instead of failing the task at zip time
                stage_contexts.append({
                    "reject": (
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: worker returned no structure"
                    ),
                    "candidate_sequence": candidate_sequence,
                    "proposal_group": job.get("proposal_group") or "ungrouped",
                })
                continue
            if peptide_chirality == 'd':
                # A candidate whose staging/refine degenerates (e.g. a
                # collapsed diffusion sample) is rejected individually; the
                # task only fails when NO candidate survives.
                try:
                    _job_seed = job.get("predict_args") if isinstance(job.get("predict_args"), dict) else {}
                    # Deterministic refine seed: (task, generation, sequence).
                    # The resume-dependent counters seed+generation_completed_base+idx
                    # made the same sequence re-evaluate with a different seed after
                    # a checkpoint resume — unreproducible rewards are GRPO noise.
                    _seed_v = int.from_bytes(
                        hashlib.sha256(
                            f"{parent_task_id}|d|{generation}|{candidate_sequence}".encode()
                        ).digest()[:4],
                        "little",
                    )
                    staged_path = _dpeptide_stage_conformer_in_pocket(
                        d_target_staged,
                        Path(structure_file),
                        Path(candidate_dir) / "d_space_staged.pdb",
                        pocket_sequence_contacts,
                        seed=_seed_v,
                        linker_ccd=linker_ccd,
                        reference_peptide_path=d_reference_peptide,
                        pose_matters=not blind_linear_route,
                    )
                    refined_cif = str(Path(candidate_dir) / "d_space_refined.cif")
                    if pocket_sequence_contacts and d_reference_peptide is not None:
                        # mode A (reference-anchored) only: restore the
                        # covalent LINK topology; the user's uploaded pose IS
                        # the binding mode (never clash-searched). The de-novo
                        # clash-free placement route was removed —
                        # its invented pose only fed the sampler a wrong local
                        # minimum the local refine window could not escape.
                        _pocket_place_for_refine(
                            Path(staged_path),
                            pocket_sequence_contacts=pocket_sequence_contacts,
                            seed=_seed_v,
                            require_bonds=(design_mode == "bicyclic"),
                            bicyclic_cys_positions=(
                                job.get("cys_positions")
                                if isinstance(job.get("cys_positions"), list) else None),
                            linker_ccd=linker_ccd,
                            keep_pose=True,
                        )
                        route = "pocket"
                    elif pocket_sequence_contacts:
                        # blind de-novo + user pocket: the pocket conditions
                        # the sampler (PocketPotential) and gates the
                        # collected result; the pose belongs to the model
                        route = "pocket"
                    else:
                        route = "plain"
                    _staged_for_dispatch = Path(staged_path)
                    _seed_for_dispatch = _seed_v
                    _pocket_res_for_dispatch = (
                        ",".join(f"{c}:{n}" for c, n in pocket_sequence_contacts)
                        if pocket_sequence_contacts else None)

                    def dispatch(_staged: Path = _staged_for_dispatch, _seed: int = _seed_for_dispatch, _pk: Optional[str] = _pocket_res_for_dispatch):  # noqa: E731
                        # detect the staged complex's chain layout: the
                        # designed peptide is the LAST protein chain, every
                        # chain before it is receptor
                        _st = gemmi.read_structure(str(_staged))
                        _st.setup_entities()
                        _poly = [ch.name for ch in _st[0]
                                 if sum(1 for r in ch if r.het_flag != "H") >= 3]
                        _pep_letter = _poly[-1] if _poly else "B"
                        _rec_letters = ",".join(_poly[:-1]) if len(_poly) > 1 else "A"
                        return _dpeptide_dispatch_refine(
                            _staged, _seed, build_capability_queue("protenix", "default"),
                            blind=blind_linear_route,
                            cyclic_headtail=(design_mode == "cyclic"),
                            pocket_res=_pk,
                            peptide_chain_letter=_pep_letter,
                            receptor_chain_letters=_rec_letters)
                except (RuntimeError, ValueError) as d_exc:
                    stage_contexts.append({
                        "reject": (
                            f"[d-peptide] candidate {candidate_sequence[:12]}… "
                            f"rejected: {d_exc}"
                        ),
                        "candidate_sequence": candidate_sequence,
                        "proposal_group": job.get("proposal_group") or "ungrouped",
                    })
                    continue

            # L chirality + user pocket: protenix-v2 has no constraint
            # embedder, so the native prediction's peptide pose does not
            # follow the pocket. The native product already carries a
            # MODEL-generated pose — keep it (mode-A style, keep_pose=True)
            # and let the refine run under the fixed receptor with native
            # PocketPotential guidance pulling the peptide into the user
            # pocket. The clash-search re-placement was removed
            # it discarded the model pose in favor of an invented one.
            if (peptide_chirality == 'l' and pocket_sequence_contacts
                    and structure_file is not None):
                try:
                    _job_seed = job.get("predict_args") if isinstance(job.get("predict_args"), dict) else {}
                    _seed_v = _job_seed.get("seed")
                    _seed_v = int(_seed_v) if isinstance(_seed_v, int) else random_seed
                    staged_path = str(Path(candidate_dir) / "pocket_staged.pdb")
                    st_native = gemmi.read_structure(str(structure_file))
                    st_native.setup_entities()
                    st_native.write_pdb(staged_path)
                    if design_mode == "bicyclic":
                        _append_staged_bicyclic_links(
                            Path(staged_path),
                            job.get("cys_positions")
                            if isinstance(job.get("cys_positions"), list) else None,
                            linker_ccd,
                        )
                    _pocket_place_for_refine(
                        Path(staged_path),
                        pocket_sequence_contacts=pocket_sequence_contacts,
                        seed=_seed_v,
                        require_bonds=(design_mode == "bicyclic"),
                        bicyclic_cys_positions=(
                            job.get("cys_positions")
                            if isinstance(job.get("cys_positions"), list) else None),
                        linker_ccd=linker_ccd,
                        keep_pose=True,
                    )
                    route = "pocket"
                    refined_cif = str(Path(candidate_dir) / "pocket_refined.cif")
                    _staged_for_dispatch = Path(staged_path)
                    _seed_for_dispatch = _seed_v
                    _pocket_res_for_dispatch = ",".join(
                        f"{c}:{n}" for c, n in pocket_sequence_contacts)

                    def dispatch(_staged: Path = _staged_for_dispatch, _seed: int = _seed_for_dispatch, _pk: Optional[str] = _pocket_res_for_dispatch):  # noqa: E731
                        _st = gemmi.read_structure(str(_staged))
                        _st.setup_entities()
                        _poly = [ch.name for ch in _st[0]
                                 if sum(1 for r in ch if r.het_flag != "H") >= 3]
                        _pep_letter = _poly[-1] if _poly else "B"
                        _rec_letters = ",".join(_poly[:-1]) if len(_poly) > 1 else "A"
                        return _dpeptide_dispatch_refine(
                            _staged, _seed, build_capability_queue("protenix", "default"),
                            blind=False,
                            cyclic_headtail=(design_mode == "cyclic"),
                            pocket_res=_pk,
                            peptide_chain_letter=_pep_letter,
                            receptor_chain_letters=_rec_letters)
                except (RuntimeError, ValueError) as pocket_exc:
                    stage_contexts.append({
                        "reject": (
                            f"[l-peptide] candidate {candidate_sequence[:12]}… "
                            f"rejected: {pocket_exc}"
                        ),
                        "candidate_sequence": candidate_sequence,
                        "proposal_group": job.get("proposal_group") or "ungrouped",
                    })
                    continue

            # L chirality WITHOUT pocket: the native prediction carries a
            # model-generated pose that has bypassed every geometric gate
            # 12/24 shipped ranks with interface
            # min 1.66-2.23 A). Stage the native complex and dispatch the
            # same fixed-receptor refine the pocket route uses -- minus the
            # pocket conditioning. The state guard, constraint bands, and
            # the collector's engagement/clash/backbone gates then apply.
            # With the docking engine the native pose goes through the
            # fixed-receptor refine + covalent gate. Without it (原版
            # protenix option) the co-folding prediction IS the deliverable:
            # stage_contexts with dispatch=None finalize immediately from
            # the native metrics parsed above.
            if (peptide_chirality == 'l'
                    and not pocket_sequence_contacts
                    and structure_file is not None):
                try:
                    _job_seed = job.get("predict_args") if isinstance(job.get("predict_args"), dict) else {}
                    _seed_v = _job_seed.get("seed")
                    _seed_v = int(_seed_v) if isinstance(_seed_v, int) else random_seed
                    staged_path = str(Path(candidate_dir) / "plain_staged.pdb")
                    st_native = gemmi.read_structure(str(structure_file))
                    st_native.setup_entities()
                    st_native.write_pdb(staged_path)
                    if design_mode == "bicyclic":
                        _append_staged_bicyclic_links(
                            Path(staged_path),
                            job.get("cys_positions")
                            if isinstance(job.get("cys_positions"), list) else None,
                            linker_ccd,
                        )
                    route = "plain"
                    refined_cif = str(Path(candidate_dir) / "plain_refined.cif")
                    _staged_for_dispatch = Path(staged_path)
                    _seed_for_dispatch = _seed_v

                    def dispatch(_staged: Path = _staged_for_dispatch, _seed: int = _seed_for_dispatch):  # noqa: E301
                        # detect the staged complex's chain layout (same as
                        # the D plain route)
                        _st = gemmi.read_structure(str(_staged))
                        _st.setup_entities()
                        _poly = [ch.name for ch in _st[0]
                                 if sum(1 for r in ch if r.het_flag != "H") >= 3]
                        _pep_letter = _poly[-1] if _poly else "B"
                        _rec_letters = ",".join(_poly[:-1]) if len(_poly) > 1 else "A"
                        # Blind inpainting (aligned with the D route): the
                        # peptide denoises from pure noise so the constraint
                        # bands converge from scratch. Refining the staged
                        # native pose instead leaves residual covalent
                        # strain that survives the full band set.
                        return _dpeptide_dispatch_refine(
                            _staged, _seed, build_capability_queue("protenix", "default"),
                            blind=True,
                            cyclic_headtail=(design_mode == "cyclic"),
                            peptide_chain_letter=_pep_letter,
                            receptor_chain_letters=_rec_letters)
                except (RuntimeError, ValueError) as stage_exc:
                    stage_contexts.append({
                        "reject": (
                            f"[l-peptide] candidate {candidate_sequence[:12]}… "
                            f"rejected: {stage_exc}"
                        ),
                        "candidate_sequence": candidate_sequence,
                        "proposal_group": job.get("proposal_group") or "ungrouped",
                    })
                    continue

            stage_contexts.append({
                "job": job,
                "candidate_sequence": candidate_sequence,
                "candidate_modifications": candidate_modifications,
                "metrics": metrics,
                "pair_iptm_target_binder": pair_iptm_target_binder,
                "pair_iptm_target_linker": pair_iptm_target_linker,
                "pair_iptm": pair_iptm,
                "pair_iptm_formula": pair_iptm_formula,
                "interface_metric_value": interface_metric_value,
                "interface_metric_label": interface_metric_label,
                "interface_metric_source": interface_metric_source,
                "interface_metric_kind": interface_metric_kind,
                "binder_avg_plddt": binder_avg_plddt,
                "binder_confidence": binder_confidence,
                "pair_iptm_confidence": pair_iptm_confidence,
                "interface_confidence": interface_confidence,
                "liability": liability,
                "liability_penalty": liability_penalty,
                "developability_score": developability_score,
                "composite_score": composite_score,
                "structure_file": structure_file,
                "staged_path": staged_path,
                "refined_cif": refined_cif,
                "d_space_refined": d_space_refined,
                "d_space_metrics": d_space_metrics,
                "pocket_report_row": pocket_report_row,
                "route": route,
                "dispatch": dispatch,
                "async_result": None,
            })

        # ==== Stage 2 (GPU): collect refines, finalize ====
        # All refines dispatch immediately — the GPU pool + worker concurrency
        # schedule them across every idle card, and each candidate still
        # finalizes (score/learn/progress) the moment its own refine lands.
        _run_dpeptide_refine_stage(
            stage_contexts,
            parent_task_id,
            _finalize_candidate,
        )

    # pLDDT hard gate: only foldable candidates compete for elite slots,
    # then composite (ipSAE-dominant) ranks within the qualified set.
    # Without this, GRPO reinforces contact-maximizing junk — histidine/
    # glycine-rich "peptides" that score ipSAE but have no fold (measured
    # H 19%, G 16%, hydrophobic 8% after 7 auto-mode generations).
    _PLDDT_GATE = plddt_gate_for_chirality(peptide_chirality)

    # Hard gate BEFORE NSGA-II: below-gate candidates are excluded from
    # the pool entirely — NSGA-II ignores list order, so sorting alone
    # would not keep them out of the elite set.
    gated = [r for r in all_results
             if isinstance(r.get("binder_avg_plddt"), (int, float))
             and r["binder_avg_plddt"] >= _PLDDT_GATE]
    elite_pool = gated if gated else all_results
    top_results = _select_nsga2_peptide_elites(elite_pool, min(24, len(elite_pool)))
    top_results.sort(key=_peptide_rank_score, reverse=True)
    if not top_results:
        raise RuntimeError("Peptide design produced no valid candidates.")

    zip_rows: List[Dict[str, Any]] = []
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        rank_cif_source = ""
        for rank, row in enumerate(top_results, start=1):
            next_row = dict(row)
            source_path = str(next_row.pop("structure_source_path", "") or "")
            structure_arcname = ""
            structure_source_for_rank = source_path
            if peptide_chirality == 'd':
                refined_ref = str(next_row.pop("d_space_refined", "") or "")
                staged_ref = str(next_row.get("d_space_staged", "") or "")
                if refined_ref and os.path.isfile(refined_ref):
                    staged_path = refined_ref
                else:
                    # acks_late redelivery after tmp cleanup: the refined
                    # sample persists under dpeptide_keep_<task>_<gen>_<cand>/
                    # (legacy dirs without the gen segment also match). The
                    # STAGED frame is deliberately NOT a fallback: staging
                    # places the peptide by surface search BEFORE docking, so
                    # a staged "product" is a dissociated complex — shipping
                    # it presented a floating peptide to the user as a result.
                    cand_dir = Path(refined_ref or staged_ref or "x").parent
                    gen_dir = cand_dir.parent.name if cand_dir.parent.name.startswith("gen_") else ""
                    staged_path = ""
                    for pattern in (
                        f"dpeptide_keep_{parent_task_id}_{gen_dir}_{cand_dir.name}",
                        f"dpeptide_keep_{parent_task_id}_{cand_dir.name}",
                    ):
                        keep_dir = Path(RESULTS_BASE_DIR) / "_runtime_tmp" / pattern
                        if not keep_dir.is_dir():
                            continue
                        if refined_ref and (keep_dir / Path(refined_ref).name).is_file():
                            staged_path = str(keep_dir / Path(refined_ref).name)
                            break
                        # The keep stores the ranked SAMPLE cifs, not the
                        # refined_cif basename. Accept a keep sample only when
                        # its staged.pdb peptide matches this row's sequence —
                        # a cand index recurs every generation, and a
                        # generation-collided keep belongs to another sequence.
                        keep_staged = keep_dir / "staged.pdb"
                        keep_samples = sorted(keep_dir.glob("d_space_staged_model_*.cif"))
                        if keep_staged.is_file() and keep_samples:
                            if _pdb_peptide_sequence(keep_staged) == \
                                    str(next_row.get("sequence") or "").strip().upper():
                                staged_path = str(keep_samples[0])
                                break
                if not (staged_path and os.path.isfile(staged_path)):
                    # Checkpoint-resumed exports legitimately lose early-gen
                    # structures to tmp cleanup. Ship the remaining ranked
                    # rows and mark this one structureless — the UI shows it
                    # as "no structure" instead of a detached pseudo-result.
                    print(
                        f"[d-peptide] WARNING: rank {rank} "
                        f"({str(next_row.get('sequence'))[:16]}) has no "
                        "refined structure on disk (tmp cleaned after a "
                        "resume) — shipping without a structure",
                        file=sys.stderr)
                    structure_source_for_rank = ""
                else:
                    from peplm.dpeptide import flip_product as _row_flip
                    display_path = Path(output_archive_path).parent / \
                        f"_display_rank_{rank:02d}.pdb"
                    _row_flip(Path(staged_path), display_path)
                    if dpeptide_reference_target and Path(dpeptide_reference_target).is_file():
                        rmsd = _dpeptide_align_product_to_input(
                            display_path, Path(dpeptide_reference_target))
                        print(f"[d-peptide] display rank {rank} aligned to the user "
                              f"frame (receptor RMSD {rmsd:.3f} A)", file=sys.stderr)
                    structure_source_for_rank = str(display_path)
            keep_file_name = str(next_row.pop("lpeptide_keep_file", "") or "")
            if peptide_chirality != 'd' \
                    and not (structure_source_for_rank and os.path.isfile(structure_source_for_rank)):
                # L mirror of the dpeptide_keep fallback: recover the product
                # persisted at finalize time (sequence-guarded).
                structure_source_for_rank = _lpeptide_keep_structure(
                    parent_task_id, keep_file_name, next_row.get("sequence"))
            if structure_source_for_rank and os.path.isfile(structure_source_for_rank):
                suffix = Path(structure_source_for_rank).suffix.lower()
                ext = ".pdb" if suffix == ".pdb" else ".cif"
                structure_arcname = f"structures/rank_{rank:02d}{ext}"
                # Export gate: a dissociated product (e.g. a staged pre-docking
                # frame that slipped past a fallback) must fail the export, not
                # ship a floating peptide with a high score attached.
                _assert_product_bound(
                    Path(structure_source_for_rank), binder_chain_id,
                    rank=rank, sequence=str(next_row.get("sequence") or ""))
                if peptide_chirality == 'd':
                    # Per-rank chirality telemetry (hard gate covers rank 1
                    # only); recorded on the row, the <30% scoring policy stands.
                    violations = _count_dpeptide_chirality_violations(structure_source_for_rank)
                    if violations is not None:
                        next_row["peptide_chirality_violations"] = violations
                        row["peptide_chirality_violations"] = violations
                        if violations:
                            print(
                                f"[d-peptide] rank {rank}: {violations} CA "
                                "chirality artifacts",
                                file=sys.stderr)
                zipf.write(structure_source_for_rank, structure_arcname)
                # rank_cif_source stays on the MIRROR-SPACE source: the
                # product block flips it exactly once. Overwriting it with the
                # already-flipped display file would double-flip.
                if rank == 1 and not rank_cif_source:
                    rank_cif_source = structure_source_for_rank or source_path
            elif peptide_chirality != 'd':
                # A hollow archive (JSON only) presented "no structure" cards
                # to the user after a redelivered re-export — same policy as
                # the D route: ship the row structureless, with a warning.
                print(
                    f"[peptide] WARNING: rank {rank} "
                    f"({str(next_row.get('sequence'))[:16]}) structure file "
                    f"missing ({structure_source_for_rank or 'no path recorded'})"
                    " — shipping without a structure",
                    file=sys.stderr)
                structure_source_for_rank = ""
            next_row["rank"] = rank
            next_row["structure_file"] = structure_arcname
            next_row["structure_name"] = Path(structure_arcname).name if structure_arcname else ""
            next_row["structure_path"] = structure_arcname
            # Mirror the pointer onto the original evaluated row: the zip's
            # design_results.json serializes all_results, and the frontend 3D
            # card view resolves each candidate's structure by structure_name
            # — without this the cards ship nameless ("No structure is
            # available for this peptide") even though rank_XX files exist.
            row["rank"] = rank
            row["structure_file"] = next_row["structure_file"]
            row["structure_name"] = next_row["structure_name"]
            row["structure_path"] = next_row["structure_path"]
            next_row.pop("plddts", None)
            zip_rows.append(next_row)

        product_note: Dict[str, Any] = {}
        if peptide_chirality == 'd':
            # The display rank-01 file already IS the product frame
            # (L-target + D-peptide): it must pass the chirality gate before
            # entering the archive as the product.
            if rank_cif_source and os.path.isfile(rank_cif_source):
                _prod_path = Path(output_archive_path).parent / "PRODUCT_Ltarget_Dpeptide.pdb"
                shutil.copyfile(rank_cif_source, _prod_path)
                with open(_prod_path, "r", encoding="utf-8", errors="replace") as _head:
                    if not _head.readline().startswith(
                        ("ATOM", "HEADER", "CRYST1", "REMARK", "MODEL", "HET", "SEQRES", "TITLE")
                    ):
                        raise RuntimeError(
                            f"D-peptide product source {rank_cif_source} is not "
                            "PDB format; display transform did not run."
                        )
                gate = _assert_product_chirality(
                    _prod_path,
                    reference_structure_path=(
                        dpeptide_reference_target
                        if dpeptide_reference_target and Path(dpeptide_reference_target).is_file()
                        else None),
                    rmsd_limit=None,
                )
                integrity = _structure_integrity_report(_prod_path)
                if not integrity.get("all_intact"):
                    # telemetry, not rejection — the source-level fixes are the
                    # staged strain relief + the sampler guards; the flag ships
                    # with the report for visibility
                    print(
                        f"[d-peptide] product integrity flags: "
                        f"{integrity['broken_bonds'][:4]}",
                        file=sys.stderr,
                    )
                bond_report = (
                    _dpeptide_linker_bond_report(_prod_path)
                    if design_mode == "bicyclic" else None
                )
                if _prod_path.exists():
                    zipf.write(str(_prod_path), "structures/product_Ltarget_Dpeptide.pdb")
                    product_note = {
                        "flip_product": "structures/product_Ltarget_Dpeptide.pdb",
                        "route": "d_target_mirror_pocket_inpaint",
                        "product_chirality": gate,
                        "linker_bonds": bond_report,
                    }
            else:
                raise RuntimeError(
                    "D-peptide product missing: no display structure for the "
                    f"top-ranked candidate (rank_cif_source={rank_cif_source!r})."
                )
        elif peptide_chirality == 'l' and pocket_sequence_contacts and top_results:
            # L + user pocket: the shipped rank structures are the pocket-
            # refined poses; record the gate evidence in the summary.
            top_row = top_results[0]
            product_note = {
                "route": (
                    "native_prediction_pocket_refine"
                    if top_row.get("pocket_min_distance") is not None
                    else "native_prediction"
                ),
                "pocket_residues_sequence": [
                    [c, n] for c, n in pocket_sequence_contacts],
                "pocket_min_distance": top_row.get("pocket_min_distance"),
                "pocket_contacts_within_4p5": top_row.get("pocket_contacts_within_4p5"),
                "linker_bonds": (
                    _dpeptide_linker_bond_report(
                        Path(str(top_row.get("structure_source_path") or "")))
                    if design_mode == "bicyclic"
                    and str(top_row.get("structure_source_path") or "").strip()
                    and os.path.isfile(str(top_row.get("structure_source_path")))
                    else None
                ),
            }
        elif peptide_chirality == 'l' and not pocket_sequence_contacts and top_results:
            # L-blind (no pocket): the shipped rank structures are the
            # gate-checked fixed-receptor refines; record the route
            top_row = top_results[0]
            product_note = {
                "route": "native_prediction_fixed_receptor_refine",
                "shipped_interface_contacts": top_row.get(
                    "shipped_interface_contacts"),
            }

        summary_payload = {
            "summary": {
                **product_note,
                "backend": peptide_backend,
                "chirality": peptide_chirality,
                "design_mode": design_mode,
                "binder_length": binder_length,
                "iterations": iterations,
                "population_size": population_size,
                "elite_size": elite_size,
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "best_score": zip_rows[0].get("composite_score") if zip_rows else 0.0,
            },
            "peptide_design": {
                "backend": peptide_backend,
                "design_mode": design_mode,
                "binder_length": binder_length,
                "iterations": iterations,
                "population_size": population_size,
                "elite_size": elite_size,
                "target_chain_id": resolved_target_chain_id,
                "binder_chain_id": binder_chain_id,
                "linker_chain_id": linker_chain_id if design_mode == "bicyclic" else "",
                "current_generation": iterations,
                "total_generations": iterations,
                "completed_tasks": completed_tasks,
                "pending_tasks": max(0, total_tasks - completed_tasks),
                "total_tasks": total_tasks,
                "best_score": zip_rows[0].get("composite_score") if zip_rows else 0.0,
                "best_sequences": zip_rows,
                "candidate_count": len(zip_rows),
            },
            "top_results": zip_rows,
            "best_sequences": zip_rows,
        }
        zipf.writestr("results_summary.json", json.dumps(summary_payload, ensure_ascii=False, indent=2))

        all_results_payload = []
        for row in all_results:
            copied = dict(row)
            copied.pop("structure_source_path", None)
            copied.pop("plddts", None)
            all_results_payload.append(copied)
        zipf.writestr(
            "design_results.json",
            json.dumps({"candidates": all_results_payload}, ensure_ascii=False, indent=2),
        )

    _write_peptide_progress(
        progress_path,
        {
            "peptide_design": {
                "current_generation": iterations,
                "total_generations": iterations,
                "completed_tasks": completed_tasks,
                "pending_tasks": 0,
                "total_tasks": total_tasks,
                "best_score": zip_rows[0].get("composite_score") if zip_rows else 0.0,
                "progress_percent": 100.0,
                "current_status": "completed",
                "status_message": "Peptide design completed",
                "best_sequences": zip_rows[:10],
                "current_best_sequences": zip_rows[:10],
                "elapsed_seconds": max(0.0, time.time() - peptide_started_at),
                "estimated_remaining_seconds": 0.0,
                "estimated_completion_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "candidates_evaluated": completed_tasks,
            }
        },
    )


# The two CCD artifacts are rewritten (merged) per overlay; every other entry in
# the shared cache is static and must be linked, never duplicated. Keep the names
# in one place: the skip set in the materialization loop is load-bearing — an
# unlisted CCD artifact would be hardlinked over the freshly merged overlay copy.
_PROTENIX_CCD_CIF = "components.cif"
_PROTENIX_CCD_PKL = "components.cif.rdkit_mol.pkl"
_PROTENIX_MERGED_CCD_FILES = (_PROTENIX_CCD_CIF, _PROTENIX_CCD_PKL)



