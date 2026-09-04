# run_single_prediction.py
import sys
import os
import json
import tempfile
import shutil
import traceback
import yaml
import hashlib
import csv
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PROJECT_ROOT / "capabilities"


def _resolve_capability_dir(name: str) -> Path:
    return CAPABILITIES_DIR / name


if CAPABILITIES_DIR.is_dir() and str(CAPABILITIES_DIR) not in sys.path:
    sys.path.insert(0, str(CAPABILITIES_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BOLTZ2SCORE_SCRIPT = "/workspace/vbio/capabilities/boltz2score/boltz2score.py"

sys.path.append(str(PROJECT_ROOT))
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
    PROTENIX_USE_HOST_USER,
    PROTENIX_CONTAINER_APP_DIR,
    PROTENIX_CONTAINER_MODEL_DIR,
    PROTENIX_CONTAINER_CHECKPOINT_PATH,
    PROTENIX_COMMON_CACHE_DIR,
    PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS,
    PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX,
    RESULTS_BASE_DIR,
)
from backend.scheduling.capability_router import build_capability_queue
from backend.services.common_utils import (
    ProteinMsaMode,
    coerce_bool,
    extract_protein_msa_policies,
    infer_use_msa_server_from_yaml_text,
    is_msa_disabled,
)
from backend.runtime.nesso_backend import run_nesso_backend
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
from Bio.PDB import PDBParser
import gemmi
from rdkit import Chem
from rdkit.Chem import AllChem, rdFMCS, rdMolAlign
from backend.runtime.custom_ccd_builder import (
    _append_custom_residues_ccd,
    _append_custom_residues_ccd_from_molecules,
    _boltz_custom_ccd_aliases,
    _build_custom_ccd_bundle,
    _build_custom_ccd_mol,
    _normalize_custom_ccd_molecules,
)
from backend.runtime.ccd_contract import validate_ccd_additions

# MSA 缓存配置（目录可通过 BOLTZ_MSA_CACHE_DIR 覆盖；默认指向 /data 挂载，
# 避免缓存放大容器可写层导致宿主机根分区膨胀）
MSA_CACHE_CONFIG = {
    'cache_dir': os.environ.get(
        'BOLTZ_MSA_CACHE_DIR',
        '/data/boltz_msa_cache'
    ),
    'enable_cache': True
}

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
AF3_DEFAULT_MODEL_SEED_COUNT = 5
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
        if not isinstance(item, dict) or "protein" not in item:
            continue
        protein = item.get("protein", {})
        seq = protein.get("sequence", "")
        ids = protein.get("id")
        if isinstance(ids, list):
            chain_ids = ids
        else:
            chain_ids = [ids] if ids is not None else []
        for chain_id in chain_ids:
            mapping[chain_id] = seq
    return mapping


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
        if fmt == "pdb":
            # the converted mmcif renumbers residues 1..N; pocket-residue
            # translation needs the author numbering of the original upload
            boltz_entry["author_pdb"] = str(raw_path)
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


def _normalize_chain_id_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


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


def discover_cuda_devices() -> List[str]:
    """Return detected CUDA device indices present on the host."""
    devices: List[str] = []

    try:
        smi_proc = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        smi_proc = None

    if smi_proc and smi_proc.returncode == 0:
        for line in smi_proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("GPU "):
                continue
            prefix = line.split(':', 1)[0]
            parts = prefix.split()
            if len(parts) >= 2 and parts[1].isdigit():
                devices.append(parts[1])

    if devices:
        return sorted(set(devices), key=int)

    node_paths = Path('/dev').glob('nvidia[0-9]*')
    for node in node_paths:
        suffix = node.name.replace('nvidia', '', 1)
        if suffix.isdigit():
            devices.append(suffix)

    return sorted(set(devices), key=int)


def determine_docker_gpu_arg(visible_devices: Optional[str]) -> str:
    """Validate CUDA availability and build docker --gpus argument."""
    available = discover_cuda_devices()
    if not available:
        raise RuntimeError(
            "当前后端需要 NVIDIA GPU，但当前环境未检测到可用的 CUDA 设备。"
        )

    if not visible_devices:
        return "all"

    tokens = [token.strip() for token in visible_devices.split(',') if token.strip()]
    if not tokens:
        raise RuntimeError("检测到 CUDA_VISIBLE_DEVICES 已设置，但未包含有效设备索引。")

    numeric_tokens = [token for token in tokens if token.isdigit()]
    invalid = [token for token in numeric_tokens if token not in available]
    if invalid:
        raise RuntimeError(
            "请求使用的 GPU 索引在当前机器上不可用: "
            f"{', '.join(invalid)}。可用索引: {', '.join(available)}"
        )

    return f"device={','.join(tokens)}"


def resolve_low_vram(predict_args: Optional[Dict[str, Any]]) -> bool:
    return coerce_bool((predict_args or {}).get("low_vram"))


def collect_gpu_device_group_ids() -> List[int]:
    """Capture host group IDs owning GPU device files to re-add inside the container."""
    candidate_nodes = [
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia-uvm"),
        Path("/dev/nvidia-uvm-tools"),
    ]

    candidate_nodes.extend(sorted(Path("/dev").glob("nvidia[0-9]*")))
    candidate_nodes.extend(sorted(Path("/dev/dri").glob("renderD*") if Path("/dev/dri").exists() else []))

    group_ids: List[int] = []
    for node in candidate_nodes:
        try:
            stat_result = node.stat()
        except FileNotFoundError:
            continue
        gid = stat_result.st_gid
        if gid not in group_ids:
            group_ids.append(gid)

    return group_ids


def sanitize_docker_extra_args(raw_args: list) -> list:
    """
    清理 Docker 额外参数，忽略不完整的 --env/-e 标志以免吞掉镜像名称。
    """
    sanitized = []
    i = 0

    while i < len(raw_args):
        token = raw_args[i]

        if token in ("--env", "-e"):
            if i + 1 >= len(raw_args):
                print(f"[WARN] 忽略无效的 Docker 参数: {token} (缺少值)", file=sys.stderr)
                i += 1
                continue

            value = raw_args[i + 1]
            if "=" not in value:
                print(f"[WARN] 忽略无效的 Docker 参数: {token} {value} (缺少 KEY=VALUE 形式)", file=sys.stderr)
                i += 2
                continue

            sanitized.extend([token, value])
            i += 2
            continue

        sanitized.append(token)
        i += 1

    return sanitized


def docker_args_has_flag(args: List[str], flag: str) -> bool:
    """Check whether a Docker CLI flag is already present in extra args."""
    normalized = str(flag or "").strip()
    if not normalized:
        return False
    for token in args:
        if token == normalized or token.startswith(f"{normalized}="):
            return True
    return False


def make_task_scoped_container_name(task_id: Optional[str]) -> Optional[str]:
    raw_task_id = str(task_id or "").strip()
    if not raw_task_id:
        return None
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_task_id).strip(".-_").lower()
    if not token:
        token = hashlib.sha1(raw_task_id.encode("utf-8")).hexdigest()[:12]
    return f"boltz-af3-{token[:48]}"


def sanitize_a3m_content(content: str, context: str = "") -> str:
    """
    移除 A3M 内容中的非法控制字符（例如 \\x00）。
    """
    sanitized = content.replace("\x00", "")
    if sanitized != content:
        msg_context = f" ({context})" if context else ""
        print(f"[WARN] 检测到并移除非法字符\\x00{msg_context}", file=sys.stderr)
    return sanitized


def sanitize_a3m_file(path: str, context: str = "") -> None:
    """
    对 A3M 文件进行清理，移除非法控制字符。
    """
    if not os.path.exists(path):
        return

    try:
        with open(path, "r") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[WARN] 无法读取 A3M 文件进行清理: {path}, {e}", file=sys.stderr)
        return

    sanitized = sanitize_a3m_content(content, context=context or path)
    if sanitized != content:
        try:
            with open(path, "w") as f:
                f.write(sanitized)
        except OSError as e:
            print(f"[WARN] 无法写入清理后的 A3M 文件: {path}, {e}", file=sys.stderr)


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


def _iter_affinity_entries(properties: Any) -> Iterable[Dict[str, Any]]:
    """标准化 properties 字段，支持 list / dict 等多种写法。"""
    if properties is None:
        return []

    if isinstance(properties, dict):
        # 单个字典，直接作为候选
        return [properties]

    if isinstance(properties, list):
        # 已经是列表，过滤出字典条目
        return [entry for entry in properties if isinstance(entry, dict)]

    # 其他类型不支持
    return []


def extract_affinity_config_from_yaml(yaml_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    从 YAML 数据中提取亲和力配置，兼容 list / dict 等写法。
    支持两种格式：
    1. affinity: true
    2. affinity: {binder: "B"}
    """
    for entry in _iter_affinity_entries(yaml_data.get("properties")):
        affinity_info = entry.get("affinity")

        # 格式1: affinity: {binder: "B"} 或 affinity: {chain: "B"}
        if isinstance(affinity_info, dict):
            binder = affinity_info.get("binder") or affinity_info.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

        # 格式2: affinity: true (需要单独查找binder)
        elif affinity_info is True:
            # 在同一层级或properties层级查找binder字段
            binder = entry.get("binder") or entry.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

            # 如果entry中没有binder，尝试从properties的其他条目中查找
            for other_entry in _iter_affinity_entries(yaml_data.get("properties")):
                binder = other_entry.get("binder") or other_entry.get("chain")
                if binder:
                    return {"binder": str(binder).strip()}

    return None


def _legacy_parse_ligand_from_text(cif_path: Path, binder_chain: str) -> Optional[str]:
    """在缺少 gemmi 时回退到文本解析。"""
    try:
        with cif_path.open("r") as cif_file:
            for line in cif_file:
                if not line.startswith("HETATM"):
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                comp_id = parts[5]
                chain_id = parts[6]
                if chain_id == binder_chain:
                    return comp_id
    except OSError as err:
        print(f"[WARN] 无法读取 CIF 文件 {cif_path}: {err}", file=sys.stderr)
    return None


def find_ligand_resname_in_cif(cif_path: Path, binder_chain: str) -> Optional[str]:
    """
    在结构文件中查找指定链的配体残基名称。
    优先使用 gemmi 解析 mmCIF / PDB，若不可用则退回文本解析。
    """
    try:
        import gemmi  # type: ignore
    except ImportError:
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as err:
        print(f"[WARN] 无法使用 gemmi 解析 {cif_path}: {err}", file=sys.stderr)
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    for model in structure:
        chain = next((ch for ch in model if ch.name == binder_chain), None)
        if chain is None:
            continue
        for residue in chain:
            resname = residue.name.strip()
            if resname:
                return resname
    return None


def _sanitize_atom_name_for_affinity(name: str) -> str:
    """Normalize atom names to avoid unsupported characters in Boltz featurizer."""
    cleaned = name.strip()
    if not cleaned:
        return name

    sanitized_chars: List[str] = []
    for ch in cleaned:
        if ch.isalpha():
            sanitized_chars.append(ch.upper())
        elif ch.isdigit():
            sanitized_chars.append(ch)
        else:
            sanitized_chars.append('X')

    sanitized = ''.join(sanitized_chars)
    return sanitized or name


def prepare_structure_for_affinity(source_path: Path, work_dir: Path) -> Path:
    """Create a sanitized copy of the structure with normalized atom names."""
    try:
        import gemmi  # type: ignore
    except ImportError:
        print(
            "[WARN] 未安装 gemmi，无法清理结构原子名，直接使用原始结构。",
            file=sys.stderr,
        )
        return source_path

    try:
        structure = gemmi.read_structure(str(source_path))
    except Exception as err:
        print(f"[WARN] 无法读取结构 {source_path} 进行清理: {err}", file=sys.stderr)
        return source_path

    changed = False
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    sanitized = _sanitize_atom_name_for_affinity(atom.name)
                    if sanitized != atom.name:
                        atom.name = sanitized
                        changed = True

    if not changed:
        return source_path

    work_dir.mkdir(parents=True, exist_ok=True)
    sanitized_path = work_dir / f"{source_path.stem}_sanitized{source_path.suffix}"

    try:
        if source_path.suffix.lower() == '.cif':
            doc = structure.make_mmcif_document()
            doc.write_file(str(sanitized_path))
        else:
            structure.write_minimal_pdb(str(sanitized_path))
    except Exception as err:
        print(f"[WARN] 写入清理后的结构失败，回退到原始结构: {err}", file=sys.stderr)
        return source_path

    print(
        f"已生成用于亲和力预测的清理结构: {sanitized_path}",
        file=sys.stderr,
    )
    return sanitized_path


def _stage_structure_for_affinity_container(source_path: Path, work_dir: Path) -> Path:
    """Place the scoring input inside the directory mounted into Boltz2Score."""
    source_path = Path(source_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    source_resolved = source_path.resolve()
    work_dir_resolved = work_dir.resolve()
    try:
        source_resolved.relative_to(work_dir_resolved)
        return source_path
    except ValueError:
        pass

    staged_path = work_dir / source_path.name
    shutil.copy2(source_path, staged_path)
    print(
        f"已将亲和力评分输入暂存到 Boltz2Score 容器挂载目录: {staged_path}",
        file=sys.stderr,
    )
    return staged_path


def _is_protein_like_residue_name(resname: str) -> bool:
    normalized = str(resname or "").strip().upper()
    return normalized in AMINO_ACID_MAPPING


def _infer_affinity_chain_plan(
    structure_path: Path,
    requested_ligand_chain: str,
) -> Optional[Dict[str, Any]]:
    solvent_names = {"HOH", "WAT"}
    ligand_chain_requested = str(requested_ligand_chain or "").strip()
    if not ligand_chain_requested:
        return None

    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception as err:
        print(f"[WARN] 无法解析结构以推断 affinity 链信息: {err}", file=sys.stderr)
        return None

    resolved_ligand_chain: Optional[str] = None
    target_chain_ids: List[str] = []

    for model in structure:
        for chain in model:
            chain_name = str(chain.name or "").strip()
            if not chain_name:
                continue
            residue_names = [
                str(residue.name or "").strip().upper()
                for residue in chain
                if str(residue.name or "").strip()
            ]
            if not residue_names:
                continue

            has_protein = any(_is_protein_like_residue_name(name) for name in residue_names)
            has_non_solvent_nonpolymer = any(
                name not in solvent_names and not _is_protein_like_residue_name(name)
                for name in residue_names
            )

            if chain_name == ligand_chain_requested:
                if has_non_solvent_nonpolymer:
                    resolved_ligand_chain = chain_name
                continue

            if has_protein:
                target_chain_ids.append(chain_name)

    if not resolved_ligand_chain:
        return None
    if not target_chain_ids:
        return None

    return {
        "ligand_chain": resolved_ligand_chain,
        "target_chain_ids": target_chain_ids,
    }


def _load_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload at {path} is not an object: {type(payload).__name__}")
    return payload


def _run_boltz2score_ipsae_postprocess_in_docker(
    *,
    stage_root: Path,
    record_id: str,
    log_path: Path,
) -> None:
    image = str(BOLTZ2_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 IPSAE 后处理。")

    raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    runtime_task_id = str(os.environ.get("BOLTZ_TASK_ID") or record_id).strip()
    task_container_name = make_task_scoped_container_name(f"{runtime_task_id}-{record_id}-ipsae")

    docker_command = ["docker", "run", "--rm"]
    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=boltz2score-ipsae"])

    docker_command.extend(
        [
            "--volume",
            f"{stage_root}:{stage_root}",
            "--volume",
            f"{PROJECT_ROOT}:/workspace/vbio:ro",
            "--workdir",
            "/workspace/vbio",
            "--env",
            "PYTHONPATH=/workspace/vbio",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
        ]
    )
    for gid in collect_gpu_device_group_ids():
        docker_command.extend(["--group-add", str(gid)])

    docker_command.extend(extra_args)
    docker_command.append(image)
    docker_command.extend(
        [
            "python",
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, '/workspace/vbio/capabilities/boltz2score'); "
                "from core.results import compute_and_write_ipsae, rerank_diffusion_samples; "
                "from pathlib import Path; "
                "output_dir = Path(sys.argv[1]); "
                "record_id = sys.argv[2]; "
                "pae_cutoff = float(sys.argv[3]); "
                "dist_cutoff = float(sys.argv[4]); "
                "compute_and_write_ipsae(output_dir=output_dir, record_id=record_id, pae_cutoff=pae_cutoff, dist_cutoff=dist_cutoff); "
                "rerank_diffusion_samples(output_dir, record_id)"
            ),
            str(stage_root),
            record_id,
            str(IPSAE_PAE_CUTOFF),
            str(IPSAE_DIST_CUTOFF),
        ]
    )

    if task_container_name:
        try:
            subprocess.run(
                ["docker", "rm", "-f", task_container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            docker_command,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return_code = proc.wait()

    if return_code != 0:
        raise RuntimeError(
            "Boltz Docker IPSAE 后处理失败。"
            f" Tail:\n{_tail_lines(log_path, 120)}\n"
            f"Full log: {log_path}"
        )


def _to_finite_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _choose_preferred_existing_path(paths: Iterable[Path]) -> Optional[Path]:
    candidates = [Path(path) for path in paths if Path(path).exists()]
    if not candidates:
        return None
    candidates.sort(
        key=lambda path: (
            1 if "seed-" in str(path).lower() else 0,
            len(str(path)),
            str(path),
        )
    )
    return candidates[0]


def _collect_structure_chain_ids(structure_path: Path) -> List[str]:
    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception:
        return []

    chain_ids: List[str] = []
    seen: set[str] = set()
    for model in structure:
        for chain in model:
            chain_id = str(chain.name or "").strip()
            if not chain_id or chain_id in seen:
                continue
            seen.add(chain_id)
            chain_ids.append(chain_id)
        if chain_ids:
            break
    return chain_ids


def _build_structure_chain_map(structure_path: Path) -> Dict[str, str]:
    return {
        str(index): chain_id
        for index, chain_id in enumerate(_collect_structure_chain_ids(structure_path))
    }


def _cif_chain_has_nonpolymer_atom_site(structure_path: Path, chain_id: str) -> Optional[bool]:
    if structure_path.suffix.lower() not in {".cif", ".mmcif"}:
        return None

    chain_id = str(chain_id or "").strip()
    if not chain_id:
        return False

    try:
        lines = structure_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    solvent_names = {"HOH", "WAT"}
    atom_fields: List[str] = []
    atom_field_index: Dict[str, int] = {}
    inside_atom_loop = False
    saw_chain = False

    for line in lines:
        stripped = line.strip()
        if stripped == "loop_":
            atom_fields = []
            atom_field_index = {}
            inside_atom_loop = False
            continue

        if line.startswith("_atom_site."):
            inside_atom_loop = True
            atom_fields.append(line.strip().split(".", 1)[1])
            atom_field_index = {name: index for index, name in enumerate(atom_fields)}
            continue

        if inside_atom_loop and atom_fields and (line.startswith("ATOM") or line.startswith("HETATM")):
            parts = line.split()
            if len(parts) < len(atom_fields):
                continue
            chain_values = []
            for field_name in ("auth_asym_id", "label_asym_id"):
                field_index = atom_field_index.get(field_name)
                if field_index is not None and field_index < len(parts):
                    chain_values.append(parts[field_index])
            if chain_id not in chain_values:
                continue
            saw_chain = True
            residue_name = ""
            comp_index = atom_field_index.get("label_comp_id")
            if comp_index is not None and comp_index < len(parts):
                residue_name = str(parts[comp_index]).strip().upper()
            seq_index = atom_field_index.get("label_seq_id")
            seq_id = parts[seq_index] if seq_index is not None and seq_index < len(parts) else ""
            if seq_id == "." and residue_name not in solvent_names:
                return True
            continue

        if inside_atom_loop and atom_fields and stripped and not stripped.startswith("_atom_site."):
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                inside_atom_loop = False
                atom_fields = []
                atom_field_index = {}

    return False if saw_chain else None


def _structure_chain_has_nonpolymer_ligand(structure_path: Path, chain_id: str) -> bool:
    chain_id = str(chain_id or "").strip()
    if not chain_id:
        return False

    cif_result = _cif_chain_has_nonpolymer_atom_site(structure_path, chain_id)
    if cif_result is not None:
        return cif_result

    solvent_names = {"HOH", "WAT"}
    polymer_like_names = set(AMINO_ACID_MAPPING.keys()) | {
        "A", "C", "G", "U", "I",
        "DA", "DC", "DG", "DT", "DI", "DU",
    }

    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception:
        return False

    for model in structure:
        for chain in model:
            current_chain_id = str(chain.name or "").strip()
            if current_chain_id != chain_id:
                continue
            residue_names = [
                str(residue.name or "").strip().upper()
                for residue in chain
                if str(residue.name or "").strip()
            ]
            return any(
                residue_name not in solvent_names and residue_name not in polymer_like_names
                for residue_name in residue_names
            )
    return False


def _structure_chain_exists(structure_path: Path, chain_id: str) -> bool:
    chain_key = str(chain_id or "").strip()
    if not chain_key:
        return False
    return chain_key in set(_collect_structure_chain_ids(structure_path))


def _extract_ligand_chain_ids_from_yaml_data(
    yaml_data: Dict[str, Any],
    alias_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    if not isinstance(yaml_data, dict):
        return []

    resolved: List[str] = []
    seen: set[str] = set()

    def add_chain_id(value: Any) -> None:
        chain_id = str(value or "").strip()
        if not chain_id:
            return
        if alias_map:
            chain_id = (
                alias_map.get(chain_id)
                or alias_map.get(chain_id.upper())
                or alias_map.get(chain_id.lower())
                or chain_id
            )
        if chain_id in seen:
            return
        seen.add(chain_id)
        resolved.append(chain_id)

    for entry in yaml_data.get("sequences", []) or []:
        if not isinstance(entry, dict):
            continue
        ligand = entry.get("ligand")
        if not isinstance(ligand, dict):
            continue
        raw_ids = ligand.get("id")
        if isinstance(raw_ids, list):
            values = raw_ids
        elif raw_ids is None:
            values = []
        else:
            values = [raw_ids]
        for value in values:
            add_chain_id(value)

    for entry in _iter_affinity_entries(yaml_data.get("properties")):
        add_chain_id(entry.get("ligand"))
        add_chain_id(entry.get("binder"))
        affinity_info = entry.get("affinity")
        if isinstance(affinity_info, dict):
            add_chain_id(affinity_info.get("binder") or affinity_info.get("chain"))
    return resolved


def _resolve_ligand_chain_annotations(
    requested_chain_ids: Iterable[str],
    structure_path: Path,
) -> Optional[Dict[str, str]]:
    requested = [str(chain_id or "").strip() for chain_id in requested_chain_ids if str(chain_id or "").strip()]
    valid_requested = [
        chain_id
        for chain_id in requested
        if _structure_chain_has_nonpolymer_ligand(structure_path, chain_id)
    ]
    if len(valid_requested) == 1:
        requested_ligand_chain = requested[0] if len(requested) == 1 else valid_requested[0]
        return {
            "requested_ligand_chain_id": requested_ligand_chain,
            "model_ligand_chain_id": valid_requested[0],
        }

    existing_requested = [
        chain_id
        for chain_id in requested
        if _structure_chain_exists(structure_path, chain_id)
    ]
    if len(existing_requested) == 1:
        return {
            "requested_ligand_chain_id": existing_requested[0],
            "model_ligand_chain_id": existing_requested[0],
        }

    inferred = _find_ligand_chain_and_resname_in_structure(structure_path)
    if not inferred:
        return None

    inferred_chain = str(inferred[0] or "").strip()
    if not inferred_chain:
        return None

    if not requested:
        requested_ligand_chain = inferred_chain
    elif len(requested) == 1:
        requested_ligand_chain = requested[0]
    elif inferred_chain in requested:
        requested_ligand_chain = inferred_chain
    else:
        return None

    return {
        "requested_ligand_chain_id": requested_ligand_chain,
        "model_ligand_chain_id": inferred_chain,
    }


def _log_ipsae_ligand_annotation_skip(
    source: str,
    requested_chain_ids: Iterable[str],
    structure_path: Path,
) -> None:
    requested = [str(chain_id or "").strip() for chain_id in requested_chain_ids if str(chain_id or "").strip()]
    structure_chain_ids = _collect_structure_chain_ids(structure_path)
    requested_text = ",".join(requested) if requested else "未声明"
    structure_text = ",".join(structure_chain_ids) if structure_chain_ids else "未解析到"
    print(
        f"[WARN] {source} IPSAE 后处理跳过：YAML 未声明可在结构中解析的 ligand/binder 链。"
        f" requested={requested_text}; structure_chains={structure_text}。"
        "请在 yaml_file properties 中明确写入 target、ligand、binder。",
        file=sys.stderr,
    )


def _convert_pair_iptm_matrix_to_map(matrix: Any, chain_map: Dict[str, str]) -> Dict[str, Dict[str, float]]:
    if not isinstance(matrix, list):
        return {}

    pair_map: Dict[str, Dict[str, float]] = {}
    chain_count = len(chain_map)
    for row_index, row in enumerate(matrix[:chain_count]):
        if not isinstance(row, list):
            continue
        row_map: Dict[str, float] = {}
        for col_index, value in enumerate(row[:chain_count]):
            parsed = _to_finite_float(value)
            if parsed is None:
                continue
            row_map[str(col_index)] = parsed
        if row_map:
            pair_map[str(row_index)] = row_map
    return pair_map


def _write_ipsae_json(path: Path, payload: Dict[str, Any]) -> None:
    def _normalize_json_value(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(key): _normalize_json_value(nested) for key, nested in value.items()}
        if isinstance(value, (list, tuple)):
            return [_normalize_json_value(item) for item in value]
        return value

    path.write_text(
        json.dumps(_normalize_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_pae_matrix(path: Path, pae_matrix: Any) -> None:
    matrix = np.asarray(pae_matrix, dtype=float)
    np.savez_compressed(path, pae=matrix)


def _copy_structure_with_ipsae_ligand_layout(
    source_path: Path,
    dest_path: Path,
    ligand_chain_id: str,
) -> None:
    ligand_chain_id = str(ligand_chain_id or "").strip()
    if source_path.suffix.lower() not in {".cif", ".mmcif"} or not ligand_chain_id:
        shutil.copy2(source_path, dest_path)
        return
    if not _structure_chain_has_nonpolymer_ligand(source_path, ligand_chain_id):
        shutil.copy2(source_path, dest_path)
        return

    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        shutil.copy2(source_path, dest_path)
        return

    output_lines: List[str] = []
    atom_fields: List[str] = []
    atom_field_index: Dict[str, int] = {}
    inside_atom_loop = False

    for line in lines:
        stripped = line.strip()
        if stripped == "loop_":
            atom_fields = []
            atom_field_index = {}
            inside_atom_loop = False
            output_lines.append(line)
            continue

        if line.startswith("_atom_site."):
            inside_atom_loop = True
            atom_fields.append(line.strip().split(".", 1)[1])
            atom_field_index = {name: index for index, name in enumerate(atom_fields)}
            output_lines.append(line)
            continue

        if inside_atom_loop and (line.startswith("ATOM") or line.startswith("HETATM")):
            parts = line.split()
            required_fields = {"label_seq_id", "auth_asym_id"}
            if required_fields.issubset(atom_field_index) and len(parts) >= len(atom_fields):
                chain_value = parts[atom_field_index.get("auth_asym_id", -1)]
                if chain_value == ligand_chain_id:
                    parts[atom_field_index["label_seq_id"]] = "."
                    line = " ".join(parts)
            output_lines.append(line)
            continue

        if inside_atom_loop and atom_fields and stripped and not stripped.startswith("_atom_site."):
            output_lines.append(line)
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                inside_atom_loop = False
                atom_fields = []
                atom_field_index = {}
            continue

        output_lines.append(line)

    dest_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


def _finalize_ipsae_archive_entries(record_dir: Path) -> List[Tuple[Path, str]]:
    best_confidence_path = record_dir / "best_confidence.json"
    best_structure_path = _find_first_existing(
        [record_dir / "best_model.cif", record_dir / "best_model.mmcif"]
    )
    if best_confidence_path.exists():
        confidence_best_model_path = record_dir / "confidence_best_model.json"
        shutil.copy2(best_confidence_path, confidence_best_model_path)

    entries: List[Tuple[Path, str]] = []
    for candidate_name in (
        "best_ipsae.json",
        "best_confidence.json",
        "confidence_best_model.json",
        "best_model.cif",
        "best_model.mmcif",
    ):
        candidate_path = record_dir / candidate_name
        if candidate_path.exists():
            entries.append((candidate_path, candidate_name))
    if best_structure_path and best_structure_path.exists():
        best_structure_alias = record_dir / best_structure_path.name
        if best_structure_alias.exists():
            entries.append((best_structure_alias, best_structure_alias.name))

    seen_arcnames: set[str] = set()
    deduped: List[Tuple[Path, str]] = []
    for file_path, arcname in entries:
        if arcname in seen_arcnames:
            continue
        seen_arcnames.add(arcname)
        deduped.append((file_path, arcname))
    return deduped


def _run_standalone_ipsae_postprocess(
    *,
    postprocess_base: Path,
    source: str,
    record_id: str,
    model_entries: List[Dict[str, Any]],
) -> List[Tuple[Path, str]]:
    if not model_entries:
        print(f"{source} 未收集到可用于 IPSAE 的模型结果，跳过后处理。", file=sys.stderr)
        return []

    postprocess_base.mkdir(parents=True, exist_ok=True)
    stage_root = postprocess_base / "staged_output"
    record_dir = stage_root / record_id
    if record_dir.exists():
        shutil.rmtree(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)

    first_structure = Path(model_entries[0]["structure_path"])
    chain_map = _build_structure_chain_map(first_structure)
    if chain_map:
        _write_ipsae_json(record_dir / "chain_map.json", chain_map)

    for entry in model_entries:
        model_index = int(entry["model_index"])
        structure_path = Path(entry["structure_path"])
        structure_suffix = structure_path.suffix.lower() or ".cif"
        staged_structure_path = record_dir / f"{record_id}_model_{model_index}{structure_suffix}"
        _copy_structure_with_ipsae_ligand_layout(
            structure_path,
            staged_structure_path,
            str(entry["confidence_payload"].get("model_ligand_chain_id") or ""),
        )
        _write_ipsae_json(
            record_dir / f"confidence_{record_id}_model_{model_index}.json",
            dict(entry["confidence_payload"]),
        )
        _write_pae_matrix(
            record_dir / f"pae_{record_id}_model_{model_index}.npz",
            entry["pae_matrix"],
        )

    ipsae_log_path = record_dir / "boltz2score_ipsae.log"
    _run_boltz2score_ipsae_postprocess_in_docker(
        stage_root=stage_root,
        record_id=record_id,
        log_path=ipsae_log_path,
    )

    entries = _finalize_ipsae_archive_entries(record_dir)
    if ipsae_log_path.exists():
        entries.append((ipsae_log_path, ipsae_log_path.name))
    if not entries:
        print(f"[WARN] {source} IPSAE 后处理未生成可归档文件。", file=sys.stderr)
        return []

    print(f"{source} IPSAE 后处理完成，生成 {len(entries)} 个归档文件。", file=sys.stderr)
    return entries


def _run_boltz_ipsae_postprocess(
    *,
    postprocess_base: Path,
    results_dir: Path,
    yaml_data: Dict[str, Any],
    explicit_ligand_chain: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(yaml_data)
    # Same as the protenix side: peptide-design candidates declare their binder chain
    # explicitly — without it the ligand-only YAML extraction skips interface scoring.
    explicit_chain = str(explicit_ligand_chain or "").strip()
    if explicit_chain and explicit_chain not in requested_chain_ids:
        requested_chain_ids.insert(0, explicit_chain)
    model_entries: List[Dict[str, Any]] = []
    record_id: Optional[str] = None

    for confidence_path in sorted(results_dir.glob("confidence_*_model_*.json")):
        match = _BOLTZ_RESULT_CONF_RE.match(confidence_path.name)
        if not match:
            continue
        current_record_id = str(match.group(1))
        model_index = int(match.group(2))
        structure_path = _find_first_existing(
            [
                results_dir / f"{current_record_id}_model_{model_index}.cif",
                results_dir / f"{current_record_id}_model_{model_index}.mmcif",
            ]
        )
        pae_path = results_dir / f"pae_{current_record_id}_model_{model_index}.npz"
        if not structure_path or not structure_path.exists() or not pae_path.exists():
            continue

        annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
        if not annotations:
            _log_ipsae_ligand_annotation_skip("boltz", requested_chain_ids, structure_path)
            continue

        confidence_payload = _load_json_object(confidence_path)
        if not confidence_payload:
            continue
        confidence_payload.update(annotations)
        pae_matrix = np.load(pae_path)["pae"]
        model_entries.append(
            {
                "model_index": model_index,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        )
        if record_id is None:
            record_id = current_record_id

    if not record_id:
        return []

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="boltz",
        record_id=record_id,
        model_entries=model_entries,
    )


def _build_af3_ipsae_confidence_payload(
    *,
    summary_payload: Dict[str, Any],
    confidences_payload: Dict[str, Any],
    chain_map: Dict[str, str],
    annotations: Dict[str, str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(annotations)

    for metric_key in ("ptm", "iptm", "ranking_score", "fraction_disordered"):
        parsed = _to_finite_float(summary_payload.get(metric_key))
        if parsed is not None:
            payload[metric_key] = parsed
    if "ranking_score" in payload and "confidence_score" not in payload:
        payload["confidence_score"] = payload["ranking_score"]

    pair_chains_iptm = _convert_pair_iptm_matrix_to_map(summary_payload.get("chain_pair_iptm"), chain_map)
    if pair_chains_iptm:
        payload["pair_chains_iptm"] = pair_chains_iptm

    atom_plddts = [
        float(value)
        for value in confidences_payload.get("atom_plddts", []) or []
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    atom_chain_ids = [str(value) for value in confidences_payload.get("atom_chain_ids", []) or []]
    if atom_plddts:
        payload["complex_plddt"] = float(sum(atom_plddts) / len(atom_plddts))
    if atom_plddts and len(atom_plddts) == len(atom_chain_ids):
        chain_values: Dict[str, List[float]] = {}
        for chain_id, atom_plddt in zip(atom_chain_ids, atom_plddts):
            chain_values.setdefault(chain_id, []).append(atom_plddt)
        if chain_values:
            payload["chain_mean_plddt"] = {
                chain_id: float(sum(values) / len(values))
                for chain_id, values in chain_values.items()
                if values
            }
        ligand_chain_id = str(annotations.get("model_ligand_chain_id") or "").strip()
        ligand_values = chain_values.get(ligand_chain_id) or []
        if ligand_values:
            payload["ligand_atom_plddts"] = ligand_values
            payload["ligand_atom_plddts_by_chain"] = {ligand_chain_id: ligand_values}

    return payload


def _run_af3_ipsae_postprocess(
    *,
    postprocess_base: Path,
    yaml_data: Dict[str, Any],
    prep: AF3Preparation,
    af3_output_dir: Path,
) -> List[Tuple[Path, str]]:
    structure_path = locate_af3_structure_file(af3_output_dir, prep.jobname)
    if not structure_path or not structure_path.exists():
        return []

    job_dir = structure_path.parent
    summary_path = _choose_preferred_existing_path(job_dir.rglob("*summary_confidences.json"))
    confidences_path = _choose_preferred_existing_path(
        path
        for path in job_dir.rglob("*confidences.json")
        if "summary_confidences" not in path.name.lower()
    )
    if not summary_path or not confidences_path:
        return []

    summary_payload = _load_json_object(summary_path)
    confidences_payload = _load_json_object(confidences_path)
    pae_matrix = confidences_payload.get("pae")
    if not isinstance(pae_matrix, list):
        return []

    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(
        yaml_data,
        alias_map=prep.chain_id_label_map,
    )
    annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
    if not annotations:
        _log_ipsae_ligand_annotation_skip("alphafold3", requested_chain_ids, structure_path)
        return []

    chain_map = _build_structure_chain_map(structure_path)
    confidence_payload = _build_af3_ipsae_confidence_payload(
        summary_payload=summary_payload,
        confidences_payload=confidences_payload,
        chain_map=chain_map,
        annotations=annotations,
    )

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="alphafold3",
        record_id=prep.jobname,
        model_entries=[
            {
                "model_index": 0,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        ],
    )


def _build_protenix_ipsae_confidence_payload(
    *,
    summary_payload: Dict[str, Any],
    full_data_payload: Dict[str, Any],
    chain_map: Dict[str, str],
    annotations: Dict[str, str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(annotations)

    for metric_key in ("ptm", "iptm", "ranking_score", "plddt"):
        parsed = _to_finite_float(summary_payload.get(metric_key))
        if parsed is not None:
            payload[metric_key] = parsed
    if "ranking_score" in payload and "confidence_score" not in payload:
        payload["confidence_score"] = payload["ranking_score"]

    pair_chains_iptm = _convert_pair_iptm_matrix_to_map(summary_payload.get("chain_pair_iptm"), chain_map)
    if pair_chains_iptm:
        payload["pair_chains_iptm"] = pair_chains_iptm

    atom_plddts = [
        float(value)
        for value in full_data_payload.get("atom_plddt", []) or []
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    if atom_plddts:
        payload["complex_plddt"] = float(sum(atom_plddts) / len(atom_plddts))

    atom_to_token_idx = full_data_payload.get("atom_to_token_idx", []) or []
    token_asym_id = full_data_payload.get("token_asym_id", []) or []
    ligand_chain_id = str(annotations.get("model_ligand_chain_id") or "").strip()
    if atom_plddts and isinstance(atom_to_token_idx, list) and isinstance(token_asym_id, list):
        ligand_values: List[float] = []
        for atom_index, atom_plddt in enumerate(atom_plddts):
            if atom_index >= len(atom_to_token_idx):
                break
            token_index = atom_to_token_idx[atom_index]
            if not isinstance(token_index, int) or token_index < 0 or token_index >= len(token_asym_id):
                continue
            chain_pos = token_asym_id[token_index]
            chain_id = chain_map.get(str(chain_pos))
            if chain_id != ligand_chain_id:
                continue
            ligand_values.append(atom_plddt)
        if ligand_values:
            payload["ligand_atom_plddts"] = ligand_values
            payload["ligand_atom_plddts_by_chain"] = {ligand_chain_id: ligand_values}

    return payload


def _run_protenix_ipsae_postprocess(
    *,
    postprocess_base: Path,
    yaml_data: Dict[str, Any],
    prep: ProtenixPreparation,
    protenix_output_dir: Path,
    explicit_ligand_chain: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    summary_candidates = list(protenix_output_dir.rglob("*_summary_confidence_sample_*.json"))
    if not summary_candidates:
        return []

    scored_candidates: List[Tuple[float, Path]] = []
    for summary_path in summary_candidates:
        payload = _load_json_object(summary_path)
        ranking_score = _to_finite_float(payload.get("ranking_score"))
        if ranking_score is None:
            continue
        scored_candidates.append((ranking_score, summary_path))
    if not scored_candidates:
        return []

    scored_candidates.sort(key=lambda item: (-item[0], len(str(item[1]))))
    summary_path = scored_candidates[0][1]
    sample_match = _PROTENIX_SUMMARY_CONF_RE.search(summary_path.name)
    if not sample_match:
        return []

    sample_index = sample_match.group(1)
    summary_payload = _load_json_object(summary_path)
    structure_stem = summary_path.name[: summary_path.name.rfind(f"_summary_confidence_sample_{sample_index}.json")]
    structure_path = _find_first_existing(
        [
            summary_path.parent / f"{structure_stem}_sample_{sample_index}.cif",
            summary_path.parent / f"{structure_stem}_sample_{sample_index}.mmcif",
        ]
    )
    full_data_path = summary_path.parent / f"{structure_stem}_full_data_sample_{sample_index}.json"
    if not structure_path or not structure_path.exists() or not full_data_path.exists():
        return []

    full_data_payload = _load_json_object(full_data_path)
    pae_matrix = full_data_payload.get("token_pair_pae")
    if not isinstance(pae_matrix, list):
        return []

    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(
        yaml_data,
        alias_map=prep.chain_alias_map,
    )
    # Peptide-design candidates carry no small-molecule ligand, so the YAML-based
    # extraction finds nothing and interface scoring was silently skipped for every
    # candidate (all rows shipped ligand_ipsae_max=None and the UI showed "IPSAE -").
    # The design loop declares the binder chain explicitly; it resolves through the
    # existing chain-exists branch (a polymer chain is a valid interface "ligand").
    explicit_chain = str(explicit_ligand_chain or "").strip()
    if explicit_chain and explicit_chain not in requested_chain_ids:
        requested_chain_ids.insert(0, explicit_chain)
    annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
    if not annotations:
        _log_ipsae_ligand_annotation_skip("protenix", requested_chain_ids, structure_path)
        return []

    chain_map = _build_structure_chain_map(structure_path)
    confidence_payload = _build_protenix_ipsae_confidence_payload(
        summary_payload=summary_payload,
        full_data_payload=full_data_payload,
        chain_map=chain_map,
        annotations=annotations,
    )

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="protenix",
        record_id=prep.input_name,
        model_entries=[
            {
                "model_index": 0,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        ],
    )


def _run_boltz2score_affinity_postprocess(
    *,
    affinity_base: Path,
    model_path: Path,
    requested_ligand_chain: str,
    ligand_resname: Optional[str],
    source: str,
    archive_prefix: str,
) -> List[Tuple[Path, str]]:
    output_dir = affinity_base / "boltz2score_output"
    work_dir = affinity_base / "boltz2score_work"
    sanitized_struct_dir = affinity_base / "sanitized_structures"
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    sanitized_struct_dir.mkdir(parents=True, exist_ok=True)

    model_for_affinity = _stage_structure_for_affinity_container(
        prepare_structure_for_affinity(model_path, sanitized_struct_dir),
        sanitized_struct_dir,
    )
    chain_plan = _infer_affinity_chain_plan(model_for_affinity, requested_ligand_chain)
    if not chain_plan:
        print(
            f"[WARN] 无法从结构中解析 affinity 所需的 target/ligand 链，跳过亲和力预测: {model_for_affinity}",
            file=sys.stderr,
        )
        return []

    resolved_ligand_chain = str(chain_plan["ligand_chain"])
    target_chain_ids = [
        str(chain_id).strip()
        for chain_id in chain_plan["target_chain_ids"]
        if str(chain_id).strip()
    ]
    if not target_chain_ids:
        print("[WARN] 未识别到蛋白 target 链，跳过亲和力预测。", file=sys.stderr)
        return []

    print(
        "开始运行 Boltz2Score 亲和力后处理，"
        f"target链: {','.join(target_chain_ids)}, 配体链: {resolved_ligand_chain}",
        file=sys.stderr,
    )

    score_cmd = [
        "python",
        BOLTZ2SCORE_SCRIPT,
        "--output_dir",
        str(output_dir),
        "--work_dir",
        str(work_dir),
        "--accelerator",
        "gpu",
        "--devices",
        "1",
        "--num_workers",
        "0",
        "--mode",
        "score",
        "--compute_ipsae",
        "--input",
        str(model_for_affinity),
        "--enable_affinity",
        "--target_chain",
        ",".join(target_chain_ids),
        "--ligand_chain",
        resolved_ligand_chain,
    ]

    try:
        gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))
    except RuntimeError as err:
        print(f"[WARN] 无法准备 Boltz2Score GPU 环境，跳过亲和力预测: {err}", file=sys.stderr)
        return []

    image = str(BOLTZ2_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 affinity 后处理 Boltz2Score。")

    raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"[WARN] 已忽略部分 BOLTZ2_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
            file=sys.stderr,
        )
    shm_size = str(BOLTZ2_DOCKER_SHM_SIZE or "").strip()

    runtime_task_id = str(os.environ.get("BOLTZ_TASK_ID") or affinity_base.name).strip()
    task_container_name = make_task_scoped_container_name(f"{runtime_task_id}-{archive_prefix}-boltz2score")
    runtime_overridden = any(token == "--runtime" for token in extra_args)

    docker_command = ["docker", "run", "--rm"]
    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=boltz2score"])
    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])
    if shm_size and not docker_args_has_flag(extra_args, "--shm-size") and not docker_args_has_flag(extra_args, "--ipc"):
        docker_command.extend(["--shm-size", shm_size])

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--volume",
            f"{affinity_base}:{affinity_base}",
            "--volume",
            f"{PROJECT_ROOT}:/workspace/vbio:ro",
            "--workdir",
            "/workspace/vbio",
            "--env",
            "PYTHONPATH=/workspace/vbio",
        ]
    )

    passthrough_env_keys = [
        "BOLTZ_DOWNLOAD_RETRIES",
        "BOLTZ_CCD_URL",
        "BOLTZ1_MODEL_URL",
        "BOLTZ2_MOLS_URL",
        "BOLTZ2_MODEL_URL",
        "BOLTZ2_AFFINITY_MODEL_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ]
    for env_key in passthrough_env_keys:
        env_val = str(os.environ.get(env_key, "") or "").strip()
        if env_val:
            docker_command.extend(["--env", f"{env_key}={env_val}"])

    if runtime_task_id:
        docker_command.extend(["--env", f"BOLTZ_TASK_ID={runtime_task_id}"])

    host_cache_dir = str(BOLTZ2_HOST_CACHE_DIR or "").strip()
    container_cache_dir = str(BOLTZ2_CONTAINER_CACHE_DIR or "/root/.boltz").strip() or "/root/.boltz"
    if host_cache_dir:
        os.makedirs(host_cache_dir, exist_ok=True)
        docker_command.extend(["--volume", f"{host_cache_dir}:{container_cache_dir}"])
        docker_command.extend(["--env", f"BOLTZ_CACHE={container_cache_dir}"])

    docker_command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for gid in collect_gpu_device_group_ids():
        docker_command.extend(["--group-add", str(gid)])

    docker_command.extend(extra_args)
    docker_command.append(image)
    docker_command.extend(score_cmd)

    if task_container_name:
        try:
            subprocess.run(
                ["docker", "rm", "-f", task_container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    score_log = affinity_base / "boltz2score.log"
    print(
        f"运行 affinity 后处理 Boltz2Score: {' '.join(shlex.quote(part) for part in docker_command)}",
        file=sys.stderr,
    )
    with score_log.open("w", encoding="utf-8") as logf:
        score_proc = subprocess.Popen(
            docker_command,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        score_return = score_proc.wait()
    if score_return != 0:
        print(
            "[WARN] Boltz2Score affinity 后处理失败，跳过 affinity_data.json。"
            f" Tail:\n{_tail_lines(score_log, 120)}",
            file=sys.stderr,
        )
        # The requester explicitly opted into affinity — record the failure inside the output
        # archive instead of degrading silently, so the frontend/user can see WHY affinity data
        # is missing instead of parsing stderr.
        try:
            (output_dir / "affinity_error.txt").write_text(
                f"Boltz2Score affinity postprocess failed with exit code {score_return}.\n"
                f"Log tail:\n{_tail_lines(score_log, 120)}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return []

    affinity_result_path = _find_first_existing(sorted(output_dir.rglob("affinity_*.json")))
    if affinity_result_path is None or not affinity_result_path.exists():
        print("[WARN] Boltz2Score affinity 未产生 affinity JSON，跳过 affinity_data.json。", file=sys.stderr)
        try:
            (output_dir / "affinity_error.txt").write_text(
                "Boltz2Score affinity finished but produced no affinity JSON.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return []

    try:
        affinity_result = _load_json_object(affinity_result_path)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[WARN] 读取 Boltz2Score affinity JSON 失败 ({exc})，跳过 affinity_data.json。", file=sys.stderr)
        return []
    if not affinity_result:
        print("[WARN] Boltz2Score affinity JSON 为空，跳过 affinity_data.json。", file=sys.stderr)
        return []

    affinity_result["source"] = source
    affinity_result["binder_chain"] = resolved_ligand_chain
    affinity_result["requested_ligand_chain"] = resolved_ligand_chain
    affinity_result["requested_target_chain"] = ",".join(target_chain_ids)
    affinity_result["target_chain"] = target_chain_ids[0]
    affinity_result["target_chain_ids"] = target_chain_ids
    if ligand_resname:
        affinity_result["ligand_resname"] = ligand_resname

    affinity_json_path = affinity_base / "affinity_data.json"
    affinity_json_path.write_text(
        json.dumps(affinity_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    affinity_entries: List[Tuple[Path, str]] = [(affinity_json_path, "affinity_data.json")]
    best_confidence_path = _find_first_existing(sorted(output_dir.rglob("best_confidence.json")))
    best_ipsae_path = _find_first_existing(sorted(output_dir.rglob("best_ipsae.json")))
    if best_confidence_path and best_confidence_path.exists():
        affinity_entries.append((best_confidence_path, f"{archive_prefix}/best_confidence.json"))
    if best_ipsae_path and best_ipsae_path.exists():
        affinity_entries.append((best_ipsae_path, f"{archive_prefix}/best_ipsae.json"))
    if score_log.exists():
        affinity_entries.append((score_log, f"{archive_prefix}/boltz2score.log"))

    print("亲和力预测完成，结果已写入 affinity_data.json。", file=sys.stderr)
    return affinity_entries


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


def _find_ligand_chain_and_resname_in_structure(path: Path) -> Optional[Tuple[str, str]]:
    """Fallback ligand locator when binder chain ID does not match output chain naming."""
    polymer_like_names = set(AMINO_ACID_MAPPING.keys()) | {
        "A", "C", "G", "U", "I",
        "DA", "DC", "DG", "DT", "DI", "DU",
    }
    solvent_names = {"HOH", "WAT"}

    try:
        structure = gemmi.read_structure(str(path))
        for model in structure:
            for chain in model:
                chain_id = (chain.name or "").strip()
                for residue in chain:
                    resname = residue.name.strip().upper()
                    if not resname or resname in solvent_names or resname in polymer_like_names:
                        continue
                    return (chain_id, residue.name.strip())
    except Exception:
        pass

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith("HETATM"):
                    continue

                if len(line) >= 22:
                    chain_id = line[21].strip()
                    resname = line[17:20].strip().upper()
                    if resname and resname not in solvent_names:
                        return (chain_id, resname)

                parts = line.split()
                if len(parts) >= 7:
                    resname = parts[5].strip().upper()
                    chain_id = parts[6].strip()
                    if resname and resname not in solvent_names:
                        return (chain_id, resname)
    except OSError:
        return None

    return None


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
        )
    except Exception as err:
        print(f"[WARN] 运行 Boltz2Score 亲和力后处理失败: {err}", file=sys.stderr)
        return []


def get_sequence_hash(sequence: str) -> str:
    """计算序列的MD5哈希值作为缓存键"""
    return hashlib.md5(sequence.encode('utf-8')).hexdigest()

def _merge_a3m_texts(first: str, second: str) -> str:
    """Concatenate two a3m texts, dropping duplicate aligned sequences.

    The query (first entry of `first`) is kept exactly once; the second
    file's query row and any sequence already present are skipped."""
    def _entries(text):
        header, block = None, []
        for line in text.splitlines():
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(block)
                header, block = line, []
            elif header is not None:
                block.append(line)
        if header is not None:
            yield header, "".join(block)

    out, seen = [], set()
    for header, seq in list(_entries(first)) + list(_entries(second)):
        if seq in seen:
            continue
        seen.add(seq)
        out.append((header, seq))
    return "".join(f"{h}\n{s}\n" for h, s in out)


def _cancel_msa_ticket(ticket_id: str) -> None:
    """Best-effort DELETE of an MSA ticket: the patched colabfold server kills the
    search's processes and frees its GPU slot. Never raises — a plain (unpatched)
    server answers 405 and we simply keep the old abandon behaviour."""
    try:
        requests.delete(f"{MSA_SERVER_URL}/ticket/{ticket_id}", timeout=15)
    except requests.RequestException:
        pass


def _msa_search_concurrency() -> int:
    """Concurrent MSA searches against the shared colabfold server. Policy: one
    search per GPU (each envdb search pins a full card); default 4, override
    with VBIO_MSA_SEARCH_CONCURRENCY."""
    try:
        value = int(os.environ.get("VBIO_MSA_SEARCH_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(8, value))


def request_msa_from_server(
    sequence: str, timeout: Optional[int] = None, msa_mode: str = "auto",
) -> Optional[dict]:
    """
    从 ColabFold MSA 服务器请求多序列比对
    
    Args:
        sequence: 蛋白质序列（FASTA 格式）
        timeout: 请求超时时间（秒）
    
    Returns:
        包含 MSA 结果的字典，如果失败则返回 None
    """
    try:
        effective_timeout = timeout if timeout and timeout > 0 else MSA_SERVER_TIMEOUT_SECONDS
        print(f"正在从 MSA 服务器请求多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        
        # 准备请求数据
        # 确保序列是 FASTA 格式
        if not sequence.startswith('>'):
            sequence = f">query\n{sequence}"
        
        # ColabFold MSA 服务器使用 form data 格式。
        # mode=env 启用宏基因组库搜索并在结果包里附带
        # bfd.mgnify30.metaeuk30.smag30.a3m；只取第一个 a3m（uniref）会
        # 丢掉整个宏基因组深度，下面下载时合并两份。
        # 智能分层(auto): 长链(>=50aa, 蛋白)用 env — 良性成本;短链(肽)用
        # uniref — env 的 CPU result2msa 阶段对短查询爆炸(实测 20aa 设计肽
        # 30-60min vs 22aa 蛋白样序列 20s)。显式 env/uniref 可覆盖。
        if msa_mode == "auto":
            normalized = "".join(
                aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
                for aa in str(sequence or "").strip().upper())
            msa_mode = "env" if len(normalized) >= 50 else "uniref"
        payload = {
            "q": sequence,
            "mode": msa_mode
        }
        print(f"MSA 请求参数: mode={msa_mode}", file=sys.stderr)
        
        # 提交搜索任务
        submit_url = f"{MSA_SERVER_URL}/ticket/msa"
        print(f"提交 MSA 搜索任务到: {submit_url}", file=sys.stderr)
        
        response = requests.post(submit_url, data=payload, timeout=30)
        if response.status_code != 200:
            print(f"[ERROR] MSA 任务提交失败: {response.status_code} - {response.text}", file=sys.stderr)
            return None
        
        result = response.json()
        ticket_id = result.get("id")
        if not ticket_id:
            print(f"[ERROR] 未获取到有效的任务 ID: {result}", file=sys.stderr)
            return None
        
        print(f"MSA 任务已提交，任务 ID: {ticket_id}", file=sys.stderr)
        
        # 轮询结果
        result_url = f"{MSA_SERVER_URL}/ticket/{ticket_id}"
        start_time = time.time()
        
        while time.time() - start_time < effective_timeout:
            try:
                print(f"检查 MSA 任务状态...", file=sys.stderr)
                response = requests.get(result_url, timeout=30)
                
                if response.status_code == 200:
                    result_data = response.json()
                    if result_data.get("status") == "COMPLETE":
                        print(f"MSA 搜索完成，获取到结果", file=sys.stderr)
                        download_url = result_data.get("result_url") or f"{MSA_SERVER_URL}/result/download/{ticket_id}"
                        print(f"下载 MSA 结果: {download_url}", file=sys.stderr)
                        try:
                            download_response = requests.get(download_url, timeout=60)
                        except requests.exceptions.RequestException as download_error:
                            print(f"[ERROR] 下载 MSA 结果请求失败: {download_error}", file=sys.stderr)
                            return None
                        if download_response.status_code != 200:
                            print(
                                f"[ERROR] 下载 MSA 结果失败: {download_response.status_code} - {download_response.text}",
                                file=sys.stderr,
                            )
                            return None

                        try:
                            tar_bytes = io.BytesIO(download_response.content)
                            with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
                                a3m_content = None
                                env_content = None
                                extracted_filename = None
                                for member in tar.getmembers():
                                    name_lower = member.name.lower()
                                    if not name_lower.endswith(".a3m"):
                                        continue
                                    file_obj = tar.extractfile(member)
                                    if not file_obj:
                                        continue
                                    if "mgnify" in name_lower:
                                        env_content = file_obj.read().decode("utf-8")
                                        continue
                                    if a3m_content is None:
                                        a3m_content = file_obj.read().decode("utf-8")
                                        extracted_filename = member.name
                                if a3m_content and env_content:
                                    a3m_content = _merge_a3m_texts(
                                        a3m_content, env_content)
                                    extracted_filename = (
                                        f"{extracted_filename}+mgnify(merged)")

                            if not a3m_content:
                                print("[ERROR] 未在下载的结果中找到 A3M 文件", file=sys.stderr)
                                return None

                            print(f"成功提取 A3M 文件: {extracted_filename}", file=sys.stderr)
                            a3m_content = sanitize_a3m_content(a3m_content, context=extracted_filename)
                            # merged rows with mismatched match-column counts
                            # crash downstream featurizers — validate now
                            query_line = ""
                            for ln in a3m_content.splitlines():
                                if ln and not ln.startswith(">"):
                                    query_line = ln
                                    break
                            query_len = sum(1 for c in query_line if not c.islower())
                            if query_len:
                                a3m_content = _drop_malformed_rows_len(a3m_content, query_len)
                            entries = parse_a3m_content(a3m_content)
                            return {
                                "entries": entries,
                                "a3m_content": a3m_content,
                                "source": extracted_filename,
                                "ticket_id": ticket_id,
                            }
                        except tarfile.TarError as tar_error:
                            print(f"[ERROR] 解析 MSA 压缩包失败: {tar_error}", file=sys.stderr)
                            return None
                    elif result_data.get("status") == "ERROR":
                        print(f"[ERROR] MSA 搜索失败: {result_data.get('error', '未知错误')}", file=sys.stderr)
                        print(
                            f"   ↳ 服务器返回: {json.dumps(result_data, ensure_ascii=False)}",
                            file=sys.stderr,
                        )
                        return None
                    else:
                        print(f"MSA 任务状态: {result_data.get('status', 'PENDING')}", file=sys.stderr)
                elif response.status_code == 404:
                    print(f"任务尚未完成或不存在", file=sys.stderr)
                else:
                    print(f"[WARN] 检查状态时出现错误: {response.status_code}", file=sys.stderr)
                
            except requests.exceptions.RequestException as e:
                print(f"[WARN] 检查状态时网络错误: {e}", file=sys.stderr)
            
            # 等待一段时间再次检查
            time.sleep(10)
        
        print(f"MSA 搜索超时 ({effective_timeout}秒)，取消服务器任务以释放 GPU", file=sys.stderr)
        _cancel_msa_ticket(ticket_id)
        return None
        
    except Exception as e:
        print(f"[ERROR] MSA 服务器请求失败: {e}", file=sys.stderr)
        return None

def save_msa_result_to_file(msa_result: dict, output_path: str) -> bool:
    """
    将 MSA 结果保存到文件
    
    Args:
        msa_result: MSA 服务器返回的结果
        output_path: 输出文件路径
    
    Returns:
        是否成功保存
    """
    try:
        # 根据结果格式保存为 A3M 文件
        if msa_result.get('a3m_content'):
            sanitized_content = sanitize_a3m_content(msa_result['a3m_content'], context=output_path)
            with open(output_path, 'w') as f:
                f.write(sanitized_content)
            return True
        elif 'entries' in msa_result:
            buffer = []
            for entry in msa_result['entries']:
                name = entry.get('name', 'unknown')
                sequence = entry.get('sequence', '')
                if sequence:
                    buffer.append(f">{name}\n{sequence}\n")

            sanitized_content = sanitize_a3m_content(''.join(buffer), context=output_path)
            with open(output_path, 'w') as f:
                f.write(sanitized_content)
            return True
        else:
            print(f"[ERROR] MSA 结果格式不支持: {msa_result.keys()}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"[ERROR] 保存 MSA 结果失败: {e}", file=sys.stderr)
        return False


def parse_a3m_content(a3m_content: str) -> list:
    """
    解析 A3M 文件内容为序列条目列表
    """
    sanitized_content = sanitize_a3m_content(a3m_content)
    entries = []
    current_name = None
    current_sequence_lines = []

    for line in sanitized_content.splitlines():
        if line.startswith('>'):
            if current_name is not None:
                entries.append({
                    'name': current_name or 'unknown',
                    'sequence': ''.join(current_sequence_lines),
                })
            current_name = line[1:].strip()
            current_sequence_lines = []
        else:
            current_sequence_lines.append(line.strip())

    if current_name is not None:
        entries.append({
            'name': current_name or 'unknown',
            'sequence': ''.join(current_sequence_lines),
        })

    return entries
def generate_msa_for_sequences(yaml_content: str, temp_dir: str) -> bool:
    """
    为 YAML 中的蛋白质序列生成 MSA

    Args:
        yaml_content: YAML 配置内容
        temp_dir: 临时目录

    Returns:
        是否成功生成 MSA
    """
    try:
        print(f"开始为蛋白质序列生成 MSA", file=sys.stderr)

        protein_sequences: Dict[str, str] = {}
        output_names: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is not ProteinMsaMode.EXTERNAL:
                continue
            if not policy.sequence:
                raise ValueError("External MSA generation requires a protein sequence.")
            if not policy.chain_ids:
                raise ValueError("External MSA generation requires explicit protein chain IDs.")
            for chain_id in policy.chain_ids:
                output_name = f"{safe_filename(chain_id)}_msa.a3m"
                previous_chain = output_names.get(output_name)
                if previous_chain is not None and previous_chain != chain_id:
                    raise ValueError(
                        f"Protein chain IDs resolve to the same MSA file: {previous_chain}, {chain_id}"
                    )
                previous_sequence = protein_sequences.get(chain_id)
                if previous_sequence is not None and previous_sequence != policy.sequence:
                    raise ValueError(f"Protein chain ID is assigned more than one sequence: {chain_id}")
                output_names[output_name] = chain_id
                protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("没有需要外部 MSA 的蛋白质序列，跳过 MSA 生成", file=sys.stderr)
            return True

        msa_timeout = MSA_SERVER_TIMEOUT_SECONDS if MSA_SERVER_TIMEOUT_SECONDS > 0 else 600
        print(f"找到 {len(protein_sequences)} 个蛋白质序列需要生成 MSA", file=sys.stderr)
        print(f"当前 MSA 超时配置: {msa_timeout} 秒", file=sys.stderr)

        # 为每个蛋白质序列生成 MSA
        success_count = 0
        for protein_id, sequence in protein_sequences.items():
            print(f"正在为蛋白质 {protein_id} 生成 MSA...", file=sys.stderr)

            output_path = os.path.join(temp_dir, f"{safe_filename(protein_id)}_msa.a3m")
            if os.path.exists(output_path):
                if _ensure_nonempty_a3m_file(
                    output_path,
                    sequence,
                    context=f"{protein_id} 临时文件",
                    header=protein_id,
                ):
                    print(f"临时目录中已存在可用 MSA 文件: {output_path}", file=sys.stderr)
                    success_count += 1
                    continue
                print(f"[WARN] 临时目录中的 MSA 文件不可用，准备重新生成: {output_path}", file=sys.stderr)

            # 缓存键与 af3 / boltz2score / protenix2dock 共用
            sequence_hash = get_sequence_hash(sequence)
            cache_dir = MSA_CACHE_CONFIG['cache_dir']
            cached_msa_path = os.path.join(cache_dir, f"msa_{sequence_hash}.a3m")

            if MSA_CACHE_CONFIG['enable_cache'] and os.path.exists(cached_msa_path):
                print(f"找到缓存的 MSA 文件: {cached_msa_path}", file=sys.stderr)
                sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存原文件")
                shutil.copy2(cached_msa_path, output_path)
                if _ensure_nonempty_a3m_file(
                    output_path,
                    sequence,
                    context=f"{protein_id} 缓存复制",
                    header=protein_id,
                ):
                    success_count += 1
                    continue
                print(f"[WARN] 缓存中的 MSA 文件为空，准备重新生成: {cached_msa_path}", file=sys.stderr)

            # 从服务器请求 MSA
            msa_result = request_msa_from_server(sequence, timeout=msa_timeout)
            if msa_result:
                if save_msa_result_to_file(msa_result, output_path):
                    if _ensure_nonempty_a3m_file(
                        output_path,
                        sequence,
                        context=f"{protein_id} 下载写入",
                        header=protein_id,
                    ):
                        success_count += 1

                        if MSA_CACHE_CONFIG['enable_cache']:
                            os.makedirs(cache_dir, exist_ok=True)
                            shutil.copy2(output_path, cached_msa_path)
                            _ensure_nonempty_a3m_file(
                                cached_msa_path,
                                sequence,
                                context=f"{protein_id} 缓存写入",
                                header=protein_id,
                            )
                            print(f"MSA 结果已缓存: {cached_msa_path}", file=sys.stderr)
                    else:
                        print(f"[ERROR] 保存后的 MSA 文件仍不可用: {protein_id}", file=sys.stderr)
                else:
                    print(f"[ERROR] 保存 MSA 文件失败: {protein_id}", file=sys.stderr)
            else:
                print(f"[ERROR] 获取 MSA 失败: {protein_id}", file=sys.stderr)

        total_sequences = len(protein_sequences)
        print(f"MSA 生成完成: {success_count}/{total_sequences} 个成功", file=sys.stderr)
        if success_count != total_sequences:
            print("[ERROR] MSA 生成不完整：必须为所有蛋白序列生成 MSA。", file=sys.stderr)
            return False
        return True

    except Exception as e:
        print(f"[ERROR] 生成 MSA 时出现错误: {e}", file=sys.stderr)
        return False


def _require_complete_external_msa(yaml_content: str, temp_dir: str, backend_label: str) -> None:
    ok = generate_msa_for_sequences(yaml_content, temp_dir)
    if not ok:
        raise RuntimeError(
            f"{backend_label} requires ColabFold MSA for every protein sequence, but MSA generation was incomplete."
        )


def _inject_local_msa_paths_into_yaml(yaml_content: str, temp_dir: str) -> Tuple[str, int]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        raise ValueError("MSA path injection expects a YAML mapping at the top level.")

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        return yaml_content, 0

    injected = 0
    for entity in sequences:
        if not isinstance(entity, dict):
            continue
        protein = entity.get("protein")
        if not isinstance(protein, dict):
            continue
        current_msa = protein.get("msa")
        if is_msa_disabled(current_msa):
            continue
        if isinstance(current_msa, str) and current_msa.strip() and current_msa.strip() not in {"0", "empty"}:
            continue
        ids = protein.get("id")
        if isinstance(ids, list):
            chain_ids = [str(item or "").strip() for item in ids if str(item or "").strip()]
        else:
            chain_ids = [str(ids or "").strip()] if str(ids or "").strip() else []
        if not chain_ids:
            continue

        selected_path: Optional[Path] = None
        for chain_id in chain_ids:
            candidate_path = Path(temp_dir) / f"{safe_filename(chain_id)}_msa.a3m"
            if not candidate_path.is_file():
                continue
            if not _ensure_nonempty_a3m_file(
                str(candidate_path),
                protein.get("sequence", ""),
                context=f"{chain_id} 注入校验",
                header=chain_id,
            ):
                continue
            selected_path = candidate_path
            if selected_path:
                break
        if not selected_path:
            continue
        protein["msa"] = str(selected_path)
        injected += 1

    if injected <= 0:
        return yaml_content, 0
    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False), injected


def cache_msa_files_from_temp_dir(temp_dir: str, yaml_content: str):
    """Cache the declared external A3M output for each protein chain."""
    if not MSA_CACHE_CONFIG['enable_cache']:
        return

    try:
        protein_sequences: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is not ProteinMsaMode.EXTERNAL:
                continue
            if not policy.sequence or not policy.chain_ids:
                raise ValueError("External MSA caching requires protein sequences and chain IDs.")
            for chain_id in policy.chain_ids:
                previous_sequence = protein_sequences.get(chain_id)
                if previous_sequence is not None and previous_sequence != policy.sequence:
                    raise ValueError(f"Protein chain ID is assigned more than one sequence: {chain_id}")
                protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("没有需要缓存的外部 MSA，跳过缓存", file=sys.stderr)
            return

        print(f"需要缓存的蛋白质组分: {list(protein_sequences)}", file=sys.stderr)
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        os.makedirs(cache_dir, exist_ok=True)

        cached_count = 0
        for protein_id, sequence in protein_sequences.items():
            msa_path = Path(temp_dir) / f"{safe_filename(protein_id)}_msa.a3m"
            if not msa_path.is_file():
                print(f"[ERROR] 蛋白质组分 {protein_id} 缺少声明的 A3M 文件", file=sys.stderr)
                continue
            if cache_single_protein_msa(protein_id, sequence, str(msa_path), cache_dir):
                cached_count += 1

        print(f"MSA缓存完成，成功缓存 {cached_count}/{len(protein_sequences)} 个蛋白质组分", file=sys.stderr)

    except Exception as e:
        print(f"[ERROR] 缓存MSA文件失败: {e}", file=sys.stderr)

def _drop_malformed_rows_len(a3m_text: str, query_len: int) -> str:
    out, dropped, header = [], 0, None
    for line in a3m_text.splitlines():
        if line.startswith(">"):
            header = line
        elif header is not None:
            if sum(1 for c in line if not c.islower()) == query_len:
                out.append(header)
                out.append(line)
            else:
                dropped += 1
            header = None
    if dropped:
        print(f"    [MSA] 丢弃 {dropped} 行匹配列数异常的序列", file=sys.stderr)
    return "".join(f"{l}\n" for l in out)


def _drop_malformed_rows(a3m_text: str, protein_sequence: str) -> str:
    """Drop a3m rows whose match-state count differs from the query length.

    Cross-source merges (uniref + metagenome) can carry rows aligned to a
    different match column count; downstream featurizers (Protenix, AF3)
    hard-fail on them. The query row defines the expected count."""
    query_len = len((protein_sequence or "").strip())
    if query_len == 0:
        return a3m_text
    out, dropped, header = [], 0, None
    for line in a3m_text.splitlines():
        if line.startswith(">"):
            header = line
        elif header is not None:
            if sum(1 for c in line if not c.islower()) == query_len:
                out.append(header)
                out.append(line)
            else:
                dropped += 1
            header = None
    if dropped:
        print(f"    [MSA] 丢弃 {dropped} 行匹配列数异常的序列", file=sys.stderr)
    return "".join(f"{l}\n" for l in out)


def cache_single_protein_msa(protein_id: str, protein_sequence: str, msa_file: str, cache_dir: str) -> bool:
    """Validate and cache one explicitly selected A3M file."""
    try:
        source_path = Path(msa_file)
        filename = source_path.name
        print(f"  处理MSA文件: {filename}", file=sys.stderr)
        if source_path.suffix.lower() != '.a3m' or not source_path.is_file():
            return False

        sanitize_a3m_file(str(source_path), context=f"{protein_id} 源MSA")
        msa_content = sanitize_a3m_content(source_path.read_text(), context=str(source_path))
        entries = parse_a3m_content(msa_content)
        if not entries:
            return False

        query_sequence = str(entries[0].get('sequence') or '')
        if not is_sequence_match(protein_sequence, query_sequence):
            print(f"    [ERROR] A3M文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
            return False

        seq_hash = get_sequence_hash(protein_sequence)
        validated = _drop_malformed_rows(msa_content, protein_sequence)
        cache_path = Path(cache_dir) / f"msa_{seq_hash}.a3m"
        tmp_cache_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        tmp_cache_path.write_text(validated)
        os.replace(tmp_cache_path, cache_path)
        print(f"    成功缓存蛋白质组分 {protein_id} 的MSA: {cache_path}", file=sys.stderr)
        print(f"       序列哈希: {seq_hash}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"    [ERROR] 处理蛋白质组分 {protein_id} 的MSA文件失败 {msa_file}: {e}", file=sys.stderr)
        return False


def is_sequence_match(protein_sequence: str, query_sequence: str) -> bool:
    """Compare normalized protein and A3M query sequences."""
    clean_protein = protein_sequence.replace('-', '').replace(' ', '').upper()
    clean_query = query_sequence.replace('-', '').replace(' ', '').upper()
    return clean_protein == clean_query


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


def _resolve_backend_results_root(backend: str, task_id: Optional[str], temp_dir: str) -> Path:
    backend_token = str(backend or "unknown").strip().lower() or "unknown"
    runtime_token = _safe_runtime_token(task_id or os.environ.get("BOLTZ_TASK_ID") or Path(temp_dir).name)
    base_dir = Path(str(RESULTS_BASE_DIR or "/data/boltz_central_results")).expanduser()
    root = base_dir / backend_token / runtime_token
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_backend_work_root(results_root: Path) -> Path:
    work_root = results_root / "runtime"
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root


def assert_boltz_preprocessing_succeeded(base_dir: str, yaml_content: str) -> None:
    manifest_path = Path(base_dir) / "processed" / "manifest.json"
    if not manifest_path.exists():
        return

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return

    records = manifest_data.get("records") if isinstance(manifest_data, dict) else None
    if isinstance(records, list) and records:
        return

    template_hint = ""
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
        if yaml_data.get("templates"):
            template_hint = (
                " 检测到 templates 输入，模板可能包含不受支持的 CCD 组分。"
                "请移除该模板，或替换为标准氨基酸残基模板后重试。"
            )
    except Exception:
        pass

    raise RuntimeError(
        "Boltz 输入预处理失败：没有生成任何有效记录，任务无法继续。"
        + template_hint
    )


def get_cached_a3m_files(yaml_content: str) -> list:
    """Collect sequence-addressed A3M cache files for declared MSA inputs."""
    cached_a3m_files = []

    if not MSA_CACHE_CONFIG['enable_cache']:
        return cached_a3m_files

    try:
        protein_sequences: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is ProteinMsaMode.DISABLED:
                continue
            for chain_id in policy.chain_ids:
                if policy.sequence:
                    protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("未找到蛋白质序列，跳过a3m文件收集", file=sys.stderr)
            return cached_a3m_files

        cache_dir = Path(MSA_CACHE_CONFIG['cache_dir'])
        if not cache_dir.is_dir():
            return cached_a3m_files

        print(f"查找缓存的a3m文件，蛋白质组分: {list(protein_sequences)}", file=sys.stderr)
        for protein_id, sequence in protein_sequences.items():
            seq_hash = get_sequence_hash(sequence)
            cache_file_path = cache_dir / f"msa_{seq_hash}.a3m"

            if cache_file_path.is_file():
                cached_a3m_files.append({
                    'path': str(cache_file_path),
                    'protein_id': protein_id,
                    'filename': f"{safe_filename(protein_id)}_msa.a3m",
                })
                print(f"找到缓存文件: {protein_id} -> {cache_file_path}", file=sys.stderr)

        print(f"总共找到 {len(cached_a3m_files)} 个a3m缓存文件", file=sys.stderr)

    except Exception as e:
        print(f"获取a3m缓存文件失败: {e}", file=sys.stderr)

    return cached_a3m_files

def create_archive_with_a3m(
    output_archive_path: str,
    output_directory_path: str,
    yaml_content: str,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
):
    """
    创建包含预测结果和a3m缓存文件的zip归档
    """
    try:
        # 获取相关的a3m缓存文件
        cached_a3m_files = get_cached_a3m_files(yaml_content)
        
        # 创建zip文件
        with zipfile.ZipFile(output_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 添加预测结果文件
            for root, dirs, files in os.walk(output_directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    # 计算相对路径，保持目录结构
                    arcname = os.path.relpath(file_path, output_directory_path)
                    zipf.write(file_path, arcname)
                    print(f"添加结果文件: {arcname}", file=sys.stderr)
            
            # 添加a3m缓存文件
            if cached_a3m_files:
                # 在zip中创建msa目录
                for a3m_info in cached_a3m_files:
                    cache_file_path = a3m_info['path']
                    filename = a3m_info['filename']
                    # 将a3m文件放在msa子目录中
                    arcname = f"msa/{filename}"
                    zipf.write(cache_file_path, arcname)
                    print(f"添加a3m缓存文件: {arcname}", file=sys.stderr)
                
                print(f"成功添加 {len(cached_a3m_files)} 个a3m缓存文件到zip归档", file=sys.stderr)
            else:
                print("[WARN] 未找到相关的a3m缓存文件", file=sys.stderr)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        print(f"[WARN] 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)
        
        print(f"归档创建完成: {output_archive_path}", file=sys.stderr)
        
    except Exception as e:
        print(f"[ERROR] 创建包含a3m文件的归档失败: {e}", file=sys.stderr)
        # 如果失败，回退到原来的方式
        archive_base_name = output_archive_path.rsplit('.', 1)[0]
        created_archive_path = shutil.make_archive(
            base_name=archive_base_name,
            format='zip',
            root_dir=output_directory_path
        )
        print(f"回退到标准归档方式: {created_archive_path}", file=sys.stderr)


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


def _validate_unique_sequence_chain_ids(yaml_content: str) -> None:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return
    if not isinstance(yaml_data, dict):
        return
    _extract_sequence_chain_types_from_yaml(yaml_data)


def _next_available_ligand_chain_id(occupied: set[str]) -> str:
    chain_pool = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    for token in chain_pool:
        if token not in occupied:
            return token
    index = 1
    while True:
        token = f"L{index}"
        if token not in occupied:
            return token
        index += 1


def _normalize_ligand_chain_collisions(yaml_content: str) -> str:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        raise ValueError("ligand-chain collision normalization expects a YAML mapping at the top level.")

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list) or not sequences:
        return yaml_content

    non_ligand_ids: set[str] = set()
    occupied_ids: set[str] = set()
    ligand_id_mapping: Dict[str, str] = {}

    for entity in sequences:
        if not isinstance(entity, dict):
            continue
        for key in ("protein", "rna", "dna"):
            payload = entity.get(key)
            if not isinstance(payload, dict):
                continue
            ids = payload.get("id")
            chain_ids = [str(item or "").strip() for item in ids] if isinstance(ids, list) else [str(ids or "").strip()]
            for chain_id in chain_ids:
                if chain_id:
                    non_ligand_ids.add(chain_id)
                    occupied_ids.add(chain_id)

    for entity in sequences:
        if not isinstance(entity, dict):
            continue
        ligand = entity.get("ligand")
        if not isinstance(ligand, dict):
            continue
        ids = ligand.get("id")
        chain_ids = [str(item or "").strip() for item in ids] if isinstance(ids, list) else [str(ids or "").strip()]
        next_ids: List[str] = []
        for chain_id in chain_ids:
            if not chain_id:
                continue
            if chain_id in non_ligand_ids or chain_id in occupied_ids:
                mapped = ligand_id_mapping.get(chain_id)
                if not mapped:
                    mapped = _next_available_ligand_chain_id(occupied_ids)
                    ligand_id_mapping[chain_id] = mapped
                next_ids.append(mapped)
                occupied_ids.add(mapped)
            else:
                next_ids.append(chain_id)
                occupied_ids.add(chain_id)
        if isinstance(ids, list):
            ligand["id"] = next_ids
        else:
            ligand["id"] = next_ids[0] if next_ids else ""

    if not ligand_id_mapping:
        return yaml_content

    for prop in yaml_data.get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        for key in ("ligand", "binder"):
            chain_id = str(prop.get(key) or "").strip()
            if chain_id in ligand_id_mapping:
                prop[key] = ligand_id_mapping[chain_id]
        affinity = prop.get("affinity")
        if isinstance(affinity, dict):
            binder = str(affinity.get("binder") or "").strip()
            if binder in ligand_id_mapping:
                affinity["binder"] = ligand_id_mapping[binder]

    for constraint in yaml_data.get("constraints", []) or []:
        if not isinstance(constraint, dict):
            continue
        pocket = constraint.get("pocket")
        if isinstance(pocket, dict):
            binder = str(pocket.get("binder") or "").strip()
            if binder in ligand_id_mapping:
                pocket["binder"] = ligand_id_mapping[binder]
        bond = constraint.get("bond")
        if isinstance(bond, dict):
            atom1 = bond.get("atom1")
            if isinstance(atom1, list) and len(atom1) >= 1:
                chain_id = str(atom1[0] or "").strip()
                if chain_id in ligand_id_mapping:
                    atom1[0] = ligand_id_mapping[chain_id]

    print(
        f"Normalized ligand chain collisions: {ligand_id_mapping}",
        file=sys.stderr,
    )
    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False)


def _sanitize_constraints_for_chain_lengths(yaml_content: str) -> str:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        raise ValueError("constraint sanitization expects a YAML mapping at the top level.")
    constraints = yaml_data.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        return yaml_content

    _extract_sequence_chain_types_from_yaml(yaml_data)
    chain_lengths = _extract_protein_chain_lengths_from_yaml(yaml_data)
    if not chain_lengths:
        return yaml_content

    invalid_pocket_contacts: List[str] = []
    invalid_bonds: List[str] = []
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        pocket = constraint.get("pocket")
        if isinstance(pocket, dict):
            contacts = pocket.get("contacts")
            if not isinstance(contacts, list):
                continue
            for contact in contacts:
                if not isinstance(contact, (list, tuple)) or len(contact) < 2:
                    invalid_pocket_contacts.append(str(contact))
                    continue
                chain_id = str(contact[0] or "").strip()
                try:
                    residue_number = int(contact[1])
                except Exception:
                    residue_number = 0
                chain_len = int(chain_lengths.get(chain_id) or 0)
                if not chain_id or residue_number <= 0 or (chain_len > 0 and residue_number > chain_len):
                    invalid_pocket_contacts.append(f"{chain_id}:{residue_number}")
            continue

        bond = constraint.get("bond")
        if isinstance(bond, dict):
            atom2 = bond.get("atom2")
            if isinstance(atom2, (list, tuple)) and len(atom2) >= 2:
                chain_id = str(atom2[0] or "").strip()
                try:
                    residue_number = int(atom2[1])
                except Exception:
                    residue_number = 0
                chain_len = int(chain_lengths.get(chain_id) or 0)
                if not (chain_id and residue_number > 0 and (chain_len <= 0 or residue_number <= chain_len)):
                    invalid_bonds.append(f"{chain_id}:{residue_number}")
                continue

    if invalid_pocket_contacts or invalid_bonds:
        pocket_preview = ", ".join(invalid_pocket_contacts[:8]) if invalid_pocket_contacts else ""
        bond_preview = ", ".join(invalid_bonds[:8]) if invalid_bonds else ""
        raise ValueError(
            "Invalid constraints for protein chain length mapping. "
            f"invalid_pocket_contacts=[{pocket_preview}] invalid_bonds=[{bond_preview}]"
        )
    return yaml_content


def _load_template_residue_number_mapping(
    template_path: Path,
    preferred_chain: Optional[str] = None,
) -> Tuple[str, List[int]]:
    structure = gemmi.read_structure(str(template_path))
    structure.setup_entities()
    if len(structure) == 0:
        return "", []
    model = structure[0]
    selected_chain = None
    preferred = str(preferred_chain or "").strip()
    if preferred:
        for chain in model:
            if str(chain.name or "").strip() == preferred:
                selected_chain = chain
                break
    if selected_chain is None:
        for chain in model:
            if any(residue.het_flag == "A" for residue in chain):
                selected_chain = chain
                break
    if selected_chain is None:
        selected_chain = next(iter(model), None)
    if selected_chain is None:
        return "", []

    aa3_to1 = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E", "GLY": "G",
        "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P", "SER": "S",
        "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V", "SEC": "U", "PYL": "O",
    }
    seen: set[Tuple[int, str]] = set()
    sequence_chars: List[str] = []
    residue_numbers: List[int] = []
    for residue in selected_chain:
        if residue.het_flag != "A":
            continue
        residue_key = (int(residue.seqid.num), str(residue.seqid.icode or "").strip())
        if residue_key in seen:
            continue
        seen.add(residue_key)
        residue_numbers.append(int(residue.seqid.num))
        residue_name = str(residue.name or "").strip().upper()
        sequence_chars.append(aa3_to1.get(residue_name, "X"))
    return "".join(sequence_chars), residue_numbers


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
        if not chain_ids:
            continue
        preferred_chain = str(entry.get("template_id") or entry.get("template_chain_id") or "").strip() or None
        try:
            template_seq, residue_numbers = _load_template_residue_number_mapping(template_path, preferred_chain)
        except Exception:
            continue
        if not template_seq or not residue_numbers:
            continue

        for query_chain in chain_ids:
            query_seq = str(chain_seq_map.get(query_chain) or "").strip()
            if not query_seq:
                continue
            query_indices, template_indices = build_alignment_indices(query_seq, template_seq)
            if not query_indices or not template_indices:
                continue
            template_to_query = {int(t): int(q) + 1 for q, t in zip(query_indices, template_indices)}
            residue_map: Dict[int, int] = {}
            for template_idx, residue_number in enumerate(residue_numbers):
                mapped_pos = template_to_query.get(int(template_idx))
                if mapped_pos is not None:
                    residue_map[int(residue_number)] = int(mapped_pos)
            if residue_map:
                mapping_by_chain[query_chain] = residue_map

    return mapping_by_chain


def _remap_constraints_by_template_alignment(yaml_content: str) -> str:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return yaml_content
    if not isinstance(yaml_data, dict):
        return yaml_content

    constraints = yaml_data.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        return yaml_content

    mapping_by_chain = _build_template_residue_maps(yaml_data)

    if not mapping_by_chain:
        return yaml_content

    replaced_contacts = 0
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        pocket = constraint.get("pocket")
        if not isinstance(pocket, dict):
            continue
        contacts = pocket.get("contacts")
        if not isinstance(contacts, list):
            continue
        next_contacts: List[List[Any]] = []
        for contact in contacts:
            if not isinstance(contact, (list, tuple)) or len(contact) < 2:
                continue
            chain_id = str(contact[0] or "").strip()
            try:
                residue_number = int(contact[1])
            except Exception:
                residue_number = 0
            mapped = mapping_by_chain.get(chain_id, {}).get(residue_number)
            if mapped is not None and mapped != residue_number:
                replaced_contacts += 1
                residue_number = mapped
            next_contacts.append([chain_id, residue_number])
        pocket["contacts"] = next_contacts

    if replaced_contacts > 0:
        print(
            f"Remapped pocket contacts by template/query alignment: replaced={replaced_contacts}",
            file=sys.stderr,
        )
        yaml_data["constraints"] = constraints
        return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False)
    return yaml_content


def _print_constraint_residue_summary(yaml_content: str) -> None:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return
    if not isinstance(yaml_data, dict):
        return
    chain_lengths = _extract_protein_chain_lengths_from_yaml(yaml_data)
    constraints = yaml_data.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        return
    chain_max_residue: Dict[str, int] = {}
    total_contacts = 0
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        pocket = constraint.get("pocket")
        if not isinstance(pocket, dict):
            continue
        contacts = pocket.get("contacts")
        if not isinstance(contacts, list):
            continue
        for contact in contacts:
            if not isinstance(contact, (list, tuple)) or len(contact) < 2:
                continue
            chain_id = str(contact[0] or "").strip()
            try:
                residue_number = int(contact[1])
            except Exception:
                continue
            if not chain_id:
                continue
            total_contacts += 1
            prev = int(chain_max_residue.get(chain_id) or 0)
            if residue_number > prev:
                chain_max_residue[chain_id] = residue_number
    if total_contacts <= 0:
        return
    print(
        f"Constraint summary: total_contacts={total_contacts}, max_residue_by_chain={chain_max_residue}, chain_lengths={chain_lengths}",
        file=sys.stderr,
    )


def create_af3_archive(
    output_archive_path: str,
    fasta_content: str,
    af3_json: dict,
    chain_msa_paths: dict,
    yaml_content: str,
    prep: AF3Preparation,
    af3_output_dir: Optional[str] = None,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
) -> None:
    """
    Create an archive containing AF3-compatible assets (FASTA, JSON, and MSAs).
    """
    try:
        with zipfile.ZipFile(output_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(f"af3/{prep.jobname}_input.fasta", fasta_content)
            zipf.writestr(f"af3/{prep.jobname}_input.json", serialize_af3_json(af3_json))
            zipf.writestr("af3/input.yaml", yaml_content)

            metadata = {
                "jobname": prep.jobname,
                "chain_labels": prep.header_labels,
                "sequence_cardinality": prep.query_sequences_cardinality,
                "chain_id_label_map": prep.chain_id_label_map,
            }
            zipf.writestr("af3/metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))

            if chain_msa_paths:
                for chain_id, path in chain_msa_paths.items():
                    if not path or not os.path.exists(path):
                        continue
                    arcname = f"af3/msa/{safe_filename(chain_id)}.a3m"
                    zipf.write(path, arcname)
                    print(f"添加AF3 MSA文件: {arcname}", file=sys.stderr)
            else:
                print("[WARN] 未找到AF3所需的MSA文件，JSON中将留空", file=sys.stderr)

            output_files_added = False
            if af3_output_dir and os.path.isdir(af3_output_dir):
                for root, _, files in os.walk(af3_output_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, af3_output_dir)
                        arcname = os.path.join("af3/output", arcname)
                        zipf.write(file_path, arcname)
                        print(f"添加AF3输出文件: {arcname}", file=sys.stderr)
                        output_files_added = True
            if not output_files_added:
                print("AF3输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            instructions = (
                "AlphaFold3 input assets generated by V-Bio.\n"
                "Files included:\n"
                " - af3_input.fasta / af3_input.json: ready for AlphaFold3 jobs\n"
                " - msa directory: cached MSAs per chain (if available)\n"
                " - input.yaml: original request payload\n"
                " - output/: files produced by AlphaFold3 (if the docker run succeeded)\n"
                "\n"
                "Upload the JSON file to AlphaFold3 alongside the FASTA sequence.\n"
            )
            zipf.writestr("af3/README.txt", instructions)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        print(f"[WARN] 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)

        print(f"AF3 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as e:
        raise RuntimeError(f"Failed to create AF3 archive: {e}") from e


def create_protenix_archive(
    output_archive_path: str,
    protenix_json: Any,
    yaml_content: str,
    input_name: str,
    chain_msa_paths: Dict[str, str],
    protenix_output_dir: Optional[str] = None,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
) -> None:
    """
    Create an archive containing Protenix-compatible assets and outputs.
    """
    try:
        with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(f"protenix/{input_name}.json", serialize_protenix_json(protenix_json))
            zipf.writestr("protenix/input.yaml", yaml_content)

            if chain_msa_paths:
                for chain_id, path in chain_msa_paths.items():
                    path_obj = Path(path)
                    if not path_obj.exists():
                        continue
                    arcname = f"protenix/msa/{safe_filename(chain_id)}.a3m"
                    zipf.write(str(path_obj), arcname)
                    print(f"添加 Protenix MSA 文件: {arcname}", file=sys.stderr)

            output_files_added = False
            if protenix_output_dir and os.path.isdir(protenix_output_dir):
                for root, _, files in os.walk(protenix_output_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, protenix_output_dir)
                        arcname = os.path.join("protenix/output", arcname)
                        zipf.write(file_path, arcname)
                        output_files_added = True
                        print(f"添加 Protenix 输出文件: {arcname}", file=sys.stderr)

            if not output_files_added:
                print("Protenix 输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            readme = (
                "Protenix input assets generated by V-Bio.\n"
                "Files included:\n"
                f" - {input_name}.json: Protenix input JSON\n"
                " - input.yaml: original request payload\n"
                " - msa/: external MSA files used by protein entities (if available)\n"
                " - output/: files produced by Protenix docker run (if succeeded)\n"
            )
            zipf.writestr("protenix/README.txt", readme)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        continue
                    zipf.write(str(file_path), arcname)

        print(f"Protenix 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as exc:
        raise RuntimeError(f"Failed to create Protenix archive: {exc}") from exc


def run_protenix_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    use_msa_server: bool,
    seed: Optional[int] = None,
    task_id: Optional[str] = None,
    custom_ccd_molecules: Optional[List[Dict[str, Any]]] = None,
    low_vram: bool = False,
    ipsae_ligand_chain_id: Optional[str] = None,
) -> None:
    print("Using Protenix backend", file=sys.stderr)
    # Same normalization as the boltz path: pocket contacts arrive in author
    # numbering of the uploaded structure, while both engines number polymer
    # residues 1..N over the input sequence.
    yaml_content = _remap_constraints_by_template_alignment(yaml_content)
    yaml_content = _sanitize_constraints_for_chain_lengths(yaml_content)
    prep = parse_yaml_for_protenix(yaml_content)
    protenix_json = prep.payload
    protein_entity_indices = {
        entity_index
        for entity_index, kind in prep.entity_kinds.items()
        if str(kind).lower() == "protein"
    }
    if not protein_entity_indices:
        raise RuntimeError("Protenix input does not contain protein entities.")
    required_msa_entity_indices = {
        entity_index
        for entity_index in protein_entity_indices
        if prep.entity_msa_modes[entity_index] is not ProteinMsaMode.DISABLED
    }
    external_msa_entity_indices = {
        entity_index
        for entity_index in protein_entity_indices
        if prep.entity_msa_modes[entity_index] is ProteinMsaMode.EXTERNAL
    }
    use_msa_server = bool(external_msa_entity_indices)

    chain_msa_paths_local: Dict[str, str] = {}
    host_msa_paths_for_archive: Dict[str, str] = {}

    protenix_results_root = _resolve_backend_results_root("protenix", task_id, temp_dir)
    protenix_work_root = _resolve_backend_work_root(protenix_results_root)
    custom_molecules = _merge_referenced_preset_modification_molecules(
        _normalize_custom_ccd_molecules(custom_ccd_molecules or []),
        yaml_content,
    )
    _validate_amidated_terminal_constraints(yaml_content, custom_molecules)
    linker_codes = _detect_bicyclic_linker_codes(yaml_content)
    linker_extra_cif = _linker_ccd_mmcif_bundle(linker_codes) if linker_codes else ""
    linker_extra_mols = _linker_ccd_mols(linker_codes) if linker_codes else {}
    protenix_common_overlay_root: Optional[Path] = None
    if custom_molecules or linker_extra_cif:
        protenix_common_overlay_root = _merge_custom_ccd_with_existing_cache(
            Path(str(PROTENIX_COMMON_CACHE_DIR).strip()),
            protenix_work_root / "protenix_common_overlay",
            custom_molecules,
            extra_cif_text=linker_extra_cif,
            extra_mols=linker_extra_mols,
        )

    if use_msa_server:
        msa_server_url = _assert_msa_server_configured("protenix")
        print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
        _require_complete_external_msa(yaml_content, str(protenix_work_root), "Protenix")
        print("MSA 生成成功，将用于 Protenix 输入", file=sys.stderr)
        if MSA_CACHE_CONFIG["enable_cache"]:
            cache_msa_files_from_temp_dir(str(protenix_work_root), yaml_content)
    else:
        print("Protenix 输入不需要外部 MSA 生成。", file=sys.stderr)

    protenix_input_dir = str(protenix_work_root / "input")
    protenix_output_dir = str(protenix_results_root / "output")
    protenix_msa_dir = os.path.join(protenix_input_dir, "msa")
    os.makedirs(protenix_input_dir, exist_ok=True)
    os.makedirs(protenix_output_dir, exist_ok=True)
    os.makedirs(protenix_msa_dir, exist_ok=True)

    try:
        # MSA resolution only needs sequences; strip constraints (pocket/bond)
        # so the AF3 parser doesn't reject constraint types it doesn't model —
        # the protenix candidate input.json keeps its own native constraint.
        msa_yaml_data = yaml.safe_load(yaml_content) or {}
        if isinstance(msa_yaml_data, dict):
            msa_yaml_data.pop("constraints", None)
        msa_yaml_content = yaml.safe_dump(msa_yaml_data, sort_keys=False)
        af3_prep = parse_yaml_for_af3(msa_yaml_content, default_jobname=prep.input_name)
        cache_dir = MSA_CACHE_CONFIG["cache_dir"] if MSA_CACHE_CONFIG["enable_cache"] else None
        chain_msa_paths = collect_chain_msa_paths(af3_prep, str(protenix_work_root), cache_dir)
        for chain_id, path in chain_msa_paths.items():
            if not path or not path.exists():
                continue
            dst_name = f"{safe_filename(chain_id)}.a3m"
            dst_host_path = os.path.join(protenix_msa_dir, dst_name)
            shutil.copyfile(str(path), dst_host_path)
            chain_msa_paths_local[chain_id] = f"/workspace/protenix_input/msa/{dst_name}"
            host_msa_paths_for_archive[chain_id] = str(path)
    except Exception as msa_err:
        raise RuntimeError(f"Protenix MSA path resolution failed: {msa_err}") from msa_err

    effective_use_msa = bool(required_msa_entity_indices)
    disabled_msa_path_local: Optional[str] = None
    if effective_use_msa and required_msa_entity_indices != protein_entity_indices:
        disabled_msa_host_path = Path(protenix_msa_dir) / "_disabled.a3m"
        disabled_msa_host_path.write_text("")
        disabled_msa_path_local = "/workspace/protenix_input/msa/_disabled.a3m"

    assigned_count = apply_protein_msa_paths(
        prep,
        chain_msa_paths_local,
        disabled_msa_path=disabled_msa_path_local,
    )
    protenix_json = prep.payload
    required_protein_entities = len(required_msa_entity_indices)
    if assigned_count != required_protein_entities:
        raise RuntimeError(
            f"Protenix external MSA assignment incomplete: assigned={assigned_count}, required={required_protein_entities}"
        )
    if assigned_count:
        print(f"已为 {assigned_count} 个蛋白实体挂载 MSA", file=sys.stderr)

    input_json_path = os.path.join(protenix_input_dir, "input.json")
    with open(input_json_path, "w", encoding="utf-8") as f:
        json.dump(protenix_json, f, indent=2, ensure_ascii=False)

    model_dir = PROTENIX_MODEL_DIR
    source_dir = PROTENIX_SOURCE_DIR
    container_app_dir = str(PROTENIX_CONTAINER_APP_DIR or "/app").strip() or "/app"
    container_model_dir = str(PROTENIX_CONTAINER_MODEL_DIR or "/workspace/model").strip() or "/workspace/model"
    model_name_raw = (PROTENIX_MODEL_NAME or "protenix-v2").strip()
    model_name = model_name_raw[:-3] if model_name_raw.endswith(".pt") else model_name_raw
    if not model_name:
        raise ValueError("PROTENIX_MODEL_NAME 不能为空。")
    checkpoint_filename = f"{model_name}.pt"
    image = PROTENIX_DOCKER_IMAGE or "vbio-protenix-v2-runtime:2.0.0"
    raw_extra_args = shlex.split(PROTENIX_DOCKER_EXTRA_ARGS) if PROTENIX_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    infer_extra_args = shlex.split(PROTENIX_INFER_EXTRA_ARGS) if PROTENIX_INFER_EXTRA_ARGS else []
    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("PROTENIX_MODEL_DIR 未配置或目录不存在，无法运行 Protenix 容器。")
    if not source_dir or not os.path.isdir(source_dir):
        raise FileNotFoundError(
            "PROTENIX_SOURCE_DIR 未配置或目录不存在。"
            "官方 Protenix Docker 镜像默认不内置源码，请先 clone Protenix 并配置该路径。"
        )
    inference_script_path = os.path.join(source_dir, "runner", "inference.py")
    if not os.path.isfile(inference_script_path):
        raise FileNotFoundError(
            f"在 PROTENIX_SOURCE_DIR 下未找到 runner/inference.py: {inference_script_path}"
        )

    checkpoint_path = os.path.join(model_dir, checkpoint_filename)
    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(
            f"未找到 Protenix 模型文件: {checkpoint_path}. "
            "请确认 PROTENIX_MODEL_DIR 与 PROTENIX_MODEL_NAME 配置正确。"
        )
    protenix_common_cache_dir = os.path.abspath(
        str(PROTENIX_COMMON_CACHE_DIR).strip()
    )
    os.makedirs(protenix_common_cache_dir, exist_ok=True)
    protenix_common_cache_mount = protenix_common_cache_dir
    if protenix_common_overlay_root is not None:
        protenix_common_cache_mount = str(protenix_common_overlay_root / "common")

    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        gpu_arg = determine_docker_gpu_arg(visible_devices)
    except RuntimeError as gpu_err:
        print(f"[protenix] GPU env unavailable: {gpu_err}", file=sys.stderr)
        raise

    runtime_task_id = str(task_id or os.environ.get("BOLTZ_TASK_ID") or "").strip()
    task_container_name = make_task_scoped_container_name(runtime_task_id)

    runtime_overridden = any(token == "--runtime" for token in extra_args)
    docker_command = ["docker", "run", "--rm"]

    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=protenix"])

    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])

    protenix_container_env = [
        f"PYTHONPATH={container_app_dir}",
        "PROTENIX_ROOT_DIR=/cache",
        # Cut CUDA allocator fragmentation (PyTorch caches freed blocks; large pair/template
        # tensors leave holes that the next ~1 GB alloc can't reuse → OOM with GBs idle).
        # Zero speed cost; LMI4Boltz sets this by default.
        "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
    ]
    if low_vram:
        protenix_container_env.append("PROTENIX_LOW_VRAM=1")

    docker_command.extend(["--gpus", gpu_arg])
    for env_kv in protenix_container_env:
        docker_command.extend(["--env", env_kv])
    docker_command.extend(
        [
            "--volume",
            f"{protenix_input_dir}:/workspace/protenix_input",
            "--volume",
            f"{protenix_output_dir}:/workspace/protenix_output",
            "--volume",
            f"{protenix_common_cache_mount}:/root/common",
            "--volume",
            f"{protenix_common_cache_mount}:/cache/common",
            "--volume",
            f"{model_dir}:{container_model_dir}",
            "--volume",
            f"{source_dir}:{container_app_dir}",
        ]
    )
    if os.path.exists("/dev/shm"):
        docker_command.extend(["--volume", "/dev/shm:/dev/shm"])

    use_host_user = str(PROTENIX_USE_HOST_USER or "").strip().lower() in {"1", "true", "yes", "on"}
    if use_host_user:
        host_uid = os.getuid()
        host_gid = os.getgid()
        docker_command.extend(["--user", f"{host_uid}:{host_gid}"])

        gpu_device_groups = collect_gpu_device_group_ids()
        for gid in gpu_device_groups:
            docker_command.extend(["--group-add", str(gid)])
        print(f"Protenix 容器使用宿主机用户: {host_uid}:{host_gid}", file=sys.stderr)
    else:
        print("Protenix 容器使用默认 root 用户（官方镜像推荐）", file=sys.stderr)
    print("Protenix 资源模式: host-mounted（源码 + 权重 + common）", file=sys.stderr)
    print(f"Protenix 缓存挂载: {protenix_common_cache_mount} -> /cache/common", file=sys.stderr)
    if protenix_common_cache_mount != protenix_common_cache_dir:
        print(f"Protenix 原始 common cache: {protenix_common_cache_dir}", file=sys.stderr)

    docker_command.extend(extra_args)

    container_checkpoint_path = str(PROTENIX_CONTAINER_CHECKPOINT_PATH or "").strip()
    if not container_checkpoint_path:
        container_checkpoint_path = f"{container_model_dir}/{checkpoint_filename}"

    docker_command.append(image)
    docker_command.extend(
        [
            (PROTENIX_PYTHON_BIN or "python3"),
            f"{container_app_dir}/runner/inference.py",
            "--model_name",
            model_name,
            "--load_checkpoint_dir",
            container_model_dir,
            "--load_checkpoint_path",
            container_checkpoint_path,
            "--input_json_path",
            "/workspace/protenix_input/input.json",
            "--dump_dir",
            "/workspace/protenix_output",
            "--need_atom_confidence",
            "True",
            "--use_msa",
            "true" if effective_use_msa else "false",
        ]
    )
    if seed is not None:
        docker_command.extend(["--seeds", str(int(seed))])
    docker_command.extend(infer_extra_args)

    display_command = " ".join(shlex.quote(part) for part in docker_command)
    if task_container_name:
        try:
            subprocess.run(
                ["docker", "rm", "-f", task_container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    print(f"运行 Protenix Docker: {display_command}", file=sys.stderr)
    protenix_log_path = str(protenix_results_root / "protenix_docker.log")
    with open(protenix_log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            docker_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_tail: List[str] = []
        if proc.stdout:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                print(line, end="", file=sys.stderr)
                output_tail.append(line)
                if len(output_tail) > 200:
                    output_tail.pop(0)
        return_code = proc.wait()

    # Protenix official image runs as root by default; fix ownership on mounted temp dirs
    # so TemporaryDirectory cleanup in host Python can remove them.
    _normalize_protenix_output_permissions(
        temp_dir=temp_dir,
        image=image,
        paths=[protenix_input_dir, protenix_output_dir, protenix_log_path],
    )

    if return_code != 0:
        tail_text = "".join(output_tail[-200:])
        traceback_text = ""
        try:
            if os.path.isfile(protenix_log_path):
                with open(protenix_log_path, "r", encoding="utf-8", errors="replace") as f:
                    full_lines = f.readlines()
                trace_idx = -1
                for idx in range(len(full_lines) - 1, -1, -1):
                    if "Traceback (most recent call last):" in full_lines[idx]:
                        trace_idx = idx
                        break
                if trace_idx >= 0:
                    traceback_text = "".join(full_lines[trace_idx:]).strip()
        except Exception:
            traceback_text = ""

        hint = ""
        if "python: not found" in tail_text or "python3: not found" in tail_text:
            hint = (
                "\nHint: container Python executable not found. "
                "Set PROTENIX_PYTHON_BIN (e.g. python3 or python)."
            )
        elif "No module named 'torch'" in tail_text or 'No module named "torch"' in tail_text:
            hint = (
                "\nHint: torch is missing in the selected container Python env. "
                "For official Protenix image, keep PROTENIX_USE_HOST_USER=false "
                "(run as container default root user)."
            )
        traceback_suffix = ""
        if traceback_text:
            traceback_suffix = f"\nTraceback:\n{traceback_text}"
        raise RuntimeError(
            f"Protenix Docker run failed with exit code {return_code}. "
            f"Last output:\n{tail_text}"
            f"{traceback_suffix}"
            f"{hint}\nFull log: {protenix_log_path}"
        )

    _raise_if_protenix_reported_error_without_structure(Path(protenix_output_dir), prep.input_name)

    yaml_data: Dict[str, Any] = {}
    try:
        parsed_yaml = yaml.safe_load(yaml_content)
        if isinstance(parsed_yaml, dict):
            yaml_data = parsed_yaml
    except Exception as yaml_err:
        print(f"[WARN] Protenix 亲和力流程解析 YAML 失败，将跳过亲和力预测: {yaml_err}", file=sys.stderr)

    extra_files: List[Tuple[Path, str]] = [(Path(protenix_log_path), "protenix/protenix_docker.log")]
    try:
        ipsae_entries = _run_protenix_ipsae_postprocess(
            postprocess_base=protenix_results_root / "ipsae",
            yaml_data=yaml_data,
            prep=prep,
            protenix_output_dir=Path(protenix_output_dir),
            explicit_ligand_chain=ipsae_ligand_chain_id,
        )
        extra_files.extend(ipsae_entries)
        # Also mirror the IPSAE json outputs into the engine output dir: the output walker
        # archives everything under protenix/output/**, which is where the peptide-design
        # candidate parser scans (**/*.json) for ipsae_dom / ligand_ipsae_max. The flat
        # archive-root copies below stay for the legacy result-archive consumers.
        if ipsae_entries:
            ipsae_mirror_dir = Path(protenix_output_dir) / "ipsae"
            ipsae_mirror_dir.mkdir(parents=True, exist_ok=True)
            for entry_path, _arcname in ipsae_entries:
                if entry_path.suffix.lower() == ".json":
                    shutil.copyfile(entry_path, ipsae_mirror_dir / entry_path.name)
    except Exception as err:
        print(f"[WARN] 运行 Protenix IPSAE 后处理失败: {err}", file=sys.stderr)
    extra_files.extend(
        run_protenix_affinity_pipeline(
            temp_dir=temp_dir,
            yaml_data=yaml_data,
            prep=prep,
            protenix_output_dir=protenix_output_dir,
            results_root=protenix_results_root,
        )
    )

    _append_custom_residues_ccd_from_molecules(extra_files, custom_molecules, temp_dir, "protenix")

    create_protenix_archive(
        output_archive_path=output_archive_path,
        protenix_json=protenix_json,
        yaml_content=yaml_content,
        input_name=prep.input_name,
        chain_msa_paths=host_msa_paths_for_archive,
        protenix_output_dir=protenix_output_dir,
        extra_files=extra_files,
    )


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
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw or "").strip()).strip("._-")
    if token:
        return token[:72]
    return f"pxm_{int(time.time())}_{random.randint(1000, 9999)}"


def _tail_lines(path: Path, count: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-count:])


def _find_first_existing(paths: List[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists():
            return path
    return None


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
    token = str(raw or "").strip().lower()
    if token in {"linear"}:
        return "linear"
    if token in {"cyclic", "cycle", "ring"}:
        return "cyclic"
    if token in {"bicyclic", "bicycle", "bi-cyclic"}:
        return "bicyclic"
    return "linear"


def _normalize_peptide_backend(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in {"alphafold3", "protenix"}:
        return token
    # docking engines map onto their corresponding full predictors at the
    # engine level (queue selection, prediction calls); the docking semantics
    # themselves are carried by _is_docking_peptide_backend / peptideChirality
    if token in {"protenix2dock", "protenix-2-dock"}:
        return "protenix"
    return "boltz"


def _is_docking_peptide_backend(raw: Any) -> bool:
    """Structure-based peptide docking engines (boltz2dock / protenix2dock).

    They map onto the corresponding full predictors at the engine level but
    carry docking semantics: a target structure is required (uploaded, or
    predicted first with the full engine when absent) and D-peptide design
    (mirror workflow) is available.
    """
    return str(raw or "").strip().lower() in {"boltz2dock", "boltz-2-dock",
                                              "protenix2dock", "protenix-2-dock"}


# Bicyclic linkers and the 3 linker atoms each Cys-SG bonds to (matches the atom names in
# backend/runtime/linker_ccd/<code>.cif, generated from Boltz's CCD cache).
BICYCLIC_LINKER_ATOM_MAP: Dict[str, List[str]] = {
    "SEZ": ["CD", "C1", "C2"],
    "29N": ["C16", "C19", "C25"],
    "BS3": ["BI", "BI", "BI"],
}


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


def _normalize_sequence_mask(raw_mask: Any, binder_length: int) -> str:
    if raw_mask is None:
        return ""
    text = str(raw_mask).strip()
    if not text:
        return ""
    mask = text.replace("-", "").replace("_", "").replace(" ", "").upper()
    if len(mask) != binder_length:
        return ""
    valid = {"X", "A", "C", "D", "E", "F", "G", "H", "I", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "V", "W", "Y"}
    if any(char not in valid for char in mask):
        return ""
    return mask


def _apply_sequence_mask(sequence: str, sequence_mask: str) -> str:
    if not sequence_mask:
        return sequence
    seq_chars = list(sequence)
    for idx, mask_char in enumerate(sequence_mask):
        if idx >= len(seq_chars):
            break
        if mask_char != "X":
            seq_chars[idx] = mask_char
    return "".join(seq_chars)


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
            mod_entry: Dict[str, Any] = {"position": int(item.get("position") or item.get("ptmPosition") or 1), "ccd": ccd}
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
        pipe.expire(key, 24 * 3600)
        pipe.execute()
    except Exception:
        pass


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

    while pending or running:
        while pending and first_error is None:
            next_job = pending.pop(0)
            async_result = _submit_peptide_candidate_worker_job(
                next_job,
                queue_name=queue_name,
                parent_task_id=parent_task_id,
            )
            running.append((async_result, next_job))

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
    candidates that need no GPU refine. Historically the refine ran inline in
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
    while pending or running:
        while pending:
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


def _dpeptide_target_sequence(base_yaml_data: Dict[str, Any], target_chain_id: str) -> str:
    """First (or requested) protein chain sequence from the design YAML."""
    target = (target_chain_id or "").strip()
    fallback = ""
    for block in base_yaml_data.get("sequences", []) or []:
        if not isinstance(block, dict):
            continue
        protein = block.get("protein")
        if isinstance(protein, dict) and protein.get("sequence"):
            if target and block.get("id") != target:
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
        import base64 as _b64

        raw = _b64.b64decode(content)
        fmt = str(entry.get("format") or "pdb").lower()
        suffix = ".cif" if fmt in ("cif", "mmcif") else ".pdb"
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        path = path / f"uploaded_target{suffix}"
        path.write_bytes(raw)
        return path
    return None


def _binder_msa_assignment(binder_sequence: str) -> str:
    """MSA for one designed binder, cache-first, HARD failure on miss.

    Policy (2026-09-04, "MSA everywhere" + no-fallback): the env-database MSA
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
    # Measured boundary (2026-09-04): the env-db CPU stages (result2msa) on a
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
        f"binder MSA 预取完成: {len(assignments)}/{len(assignments)} 就绪（{time.time() - t0:.1f}s）",
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
    import uuid as _uuid

    from backend.worker.tasks import predict_task  # same entry used by routes

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
    # historical sub_task_id-keyed root never existed, so a sequence-only D-peptide
    # target prediction spun here until the 1-hour deadline and failed the task.
    result_roots = [
        results_base / engine_cap / str(async_result.id),
    ]

    deadline = time.time() + 3600
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
                cifs.extend(c for c in root.rglob("*.cif") if cif_pat.search(str(c)))
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
            raise RuntimeError(f"D-peptide target structure prediction failed: {async_result.state}")
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
    raw_residues = str(options.get("peptidePocketResidues") or options.get("peptide_pocket_residues") or "").strip()
    author_contacts: List[Tuple[str, int]] = []
    plain_positions: List[int] = []
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

    raw_center = str(options.get("peptidePocketCenter") or options.get("peptide_pocket_center") or "").strip()
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


def _dpeptide_pick_clash_free_placement(
    free_coords: np.ndarray,
    receptor_coords: np.ndarray,
    pocket_center: np.ndarray,
    seed: int,
    pocket_coords: Optional[np.ndarray] = None,
    bond_pairs: Optional[list[tuple[int, int]]] = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Rigid placement search for a staged binder around the user pocket.

    Multiple random rotation axes x full turn per axis x radial offsets along
    the outward normal, scored by (clashes <2.2 A against the receptor,
    floating penalty, centroid gap). The floating penalty drives the nearest
    free atom to a ~3.2-4.2 A contact with the pocket residues' own atoms —
    the pocket residue list may be a consecutive stretch whose CA centroid
    sits inside the protein, so the binder must hug the residue patch from
    the surface, not park its centroid on the CA mean (which buries it,
    measured 0.3 A min distance) nor float 12 A off (which leaves the
    refine's 8 A pocket conditioning nothing to anchor to).

    A single-axis rotation family (the original 24-trial search) cannot
    cover orientation space: on buried pockets every trial overlapped and
    the least-bad pose still entered the sampler 0.2 A deep (measured on the
    2026-09-04 MDM2 runs: 95 <2 A clashes in staged, chirality inversions
    in every refined sample). The search now covers many axes; callers
    hard-fail when no zero-clash pose exists (see staging self-check)."""
    rng = np.random.default_rng(seed)
    free_c = free_coords.mean(axis=0)
    rec_c = receptor_coords.mean(axis=0)
    outward = pocket_center - rec_c
    norm = np.linalg.norm(outward)
    outward = outward / norm if norm > 1e-6 else np.array([0.0, 0.0, 1.0])
    anchor_coords = pocket_coords if pocket_coords is not None else receptor_coords
    # KD-tree free collision query: the O(n*m) distance matrix at 24x7=168
    # poses was fine; 16 axes x 24 angles x 9 offsets = 3456 poses needs it.
    from scipy.spatial import cKDTree

    rec_tree = cKDTree(receptor_coords)
    anchor_tree = cKDTree(anchor_coords)
    best = None
    n_axes, n_angles = 16, 24
    for axis_i in range(n_axes):
        axis = rng.normal(size=3)
        axis /= np.linalg.norm(axis)
        K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
        for angle_i in range(n_angles):
            theta = 2 * np.pi * angle_i / n_angles
            R = np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)
            rot_free = (R @ free_coords.T).T
            for offset in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0):
                moved = rot_free + (pocket_center + outward * offset - R @ free_c)
                clashes = int(rec_tree.query_ball_point(
                    moved, 2.2, return_length=True).sum())
                # NOTE on the placement scoring policy: a penetration-first
                # term (surface poses, min distance >= 2.8 A) eliminates
                # residual clashes but forces a large folding excursion in
                # the refine; candidate-dependent projections then deform the
                # peptide (CA-CB up to 32 A, integrity-gate-rejected) or it
                # drifts off-pocket. Clash-count-first with the ~3.7 A
                # contact target is the validated combination (full e2e
                # SUCCESS with all gates green).
                anchor_d = float(anchor_tree.query(moved, k=1)[0].min())
                floating = abs(anchor_d - 3.7)
                centroid_gap = float(np.linalg.norm(moved.mean(axis=0) - pocket_center))
                # Bicyclic chemistry: the linker anchors ride along rigidly
                # with the peptide; a pose that parks an anchor 10 A from its
                # Cys-SG cannot be rescued by the post-placement strain relief
                # (measured 3.37 A residual). Penalise deviation from the
                # ~2.0 A bond distance directly in the search.
                bond_penalty = 0.0
                if bond_pairs:
                    for ia, ib in bond_pairs:
                        bond_penalty += abs(float(
                            np.linalg.norm(moved[ia] - moved[ib]) - 2.0))
                key = (clashes, round(bond_penalty, 1), round(floating, 1),
                       centroid_gap)
                if best is None or key < best[0]:
                    best = (key, R, pocket_center + outward * offset - R @ free_c)
    # callers apply v = R @ (x - c) + c + shift, so return the shift in that
    # convention (the scored pose used R @ x + shift_v; see emit sites)
    R_best, s_scored = best[1], best[2]
    return R_best, s_scored - free_c + R_best @ free_c


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
    bond_lo: float = 1.75,
    bond_hi: float = 2.05,
    cb_lo: float = 1.35,
    cb_hi: float = 1.75,
    sweeps: int = 120,
) -> Dict[str, Any]:
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
    pep = polymer[1] if len(polymer) > 1 else None
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
    # or linker atoms into the receptor wall (measured on the bs3 fixture:
    # a 2.00 A contact after relief). Every sweep, push any free heavy atom
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
            "cb_renormalized": fixed_cb}


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
    import base64 as _b64

    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = str(entry.get("format") or "pdb").lower()
    suffix = ".cif" if fmt in ("cif", "mmcif") else ".pdb"
    path = out_dir / f"reference_peptide{suffix}"
    path.write_bytes(_b64.b64decode(entry["content_base64"]))

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
    target_l = gemmi.read_structure(str(uploaded))
    target_l.setup_entities()
    target_l.remove_alternative_conformations()

    target_d = gemmi.read_structure(str(uploaded))
    target_d.setup_entities()
    peplm_root = str(_resolve_capability_dir("peptide_lm"))
    if peplm_root not in sys.path:
        sys.path.insert(0, peplm_root)
    from peplm.dpeptide import mirror as dpm
    dpm.mirror_structure(target_d)

    d_path = work_root / "d_target.pdb"
    lines: List[str] = []
    serial = 1
    poly = None
    for chain in target_d[0]:
        if sum(1 for r in chain if r.het_flag != "H") >= 3:
            poly = chain
            break
    if poly is None:
        raise RuntimeError("D-target preparation: no polymer chain found")
    for ordinal, residue in enumerate(poly, start=1):
        for atom in residue:
            if atom.element.name == "H":
                continue
            name_field = f" {atom.name:<3}" if len(atom.name) < 4 else atom.name
            lines.append(
                f"ATOM  {serial:5d} {name_field} {residue.name:>3} A{ordinal:4d}    "
                f"{atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}"
                f"{1.00:6.2f}{0.00:6.2f}          {atom.element.name:>2}"
            )
            serial += 1
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

    rec_chain = target[0][0]
    rec_atoms = np.array([
        [a.pos.x, a.pos.y, a.pos.z] for r in rec_chain for a in r
        if a.element != gemmi.Element("H")])
    pocket_pts, pocket_atoms = [], []
    wanted = {n for _, n in pocket_sequence_contacts}
    for ordinal, residue in enumerate(rec_chain, start=1):
        if ordinal not in wanted:
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
        # a pocket the binder gets buried at the receptor centroid (measured
        # 0.16 A min distance on the 2026-09-04 MDM2 runs). pose_matters=False
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
        # bicyclic SG<->linker-anchor pairs as free-atom index pairs for the
        # placement bond penalty (both chains transform rigidly together)
        bond_pair_idx: list[tuple[int, int]] = []
        if len(conf_chains) > 1 and any(
                r.name in BICYCLIC_LINKER_ATOM_MAP for r in conf_chains[1]):
            heavy_counts = [
                sum(1 for r in c for a in r if a.element != gemmi.Element("H"))
                for c in conf_chains]
            pep_offsets = np.concatenate(([0], np.cumsum(heavy_counts)))[:-1]
            pep_sg: list[int] = []            # peptide Cys-SG row indices, seq order
            link_anchor: dict[str, int] = {}  # linker anchor atom name -> row idx
            for ci, c in enumerate(conf_chains[:2]):
                row = int(pep_offsets[ci])
                for r in c:
                    for a in r:
                        if a.element == gemmi.Element("H"):
                            continue
                        if ci == 0 and a.name == "SG":
                            pep_sg.append(row)
                        if ci == 1:
                            link_anchor[a.name] = row
                        row += 1
            anchors = BICYCLIC_LINKER_ATOM_MAP.get(conf_chains[1][0].name, ())
            for k, anchor_name in enumerate(anchors):
                li = link_anchor.get(anchor_name)
                if li is not None and k < len(pep_sg):
                    bond_pair_idx.append((pep_sg[k], li))
        rot, shift = _dpeptide_pick_clash_free_placement(
            free_atoms, rec_atoms, center, seed=seed,
            pocket_coords=(np.array(pocket_atoms) if pocket_atoms else None),
            bond_pairs=bond_pair_idx or None)
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

    for ordinal, residue in enumerate(rec_chain, start=1):
        for atom in residue:
            if atom.element.name == "H":
                continue
            _emit("ATOM", atom.name, residue.name, "A", ordinal,
                  (atom.pos.x, atom.pos.y, atom.pos.z), atom.element.name)

    pep_chain = conf_chains[0]
    sg_rows: List[Tuple[int, np.ndarray]] = []
    for ordinal, residue in enumerate(pep_chain, start=1):
        for atom in residue:
            if atom.element.name == "H":
                continue
            v = rot @ (np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - free_center) \
                + free_center + shift
            _emit("ATOM", atom.name, residue.name, "B", ordinal,
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
                "SG", "CYS", "B", best[0], anchor, linker_name, "L", 1))

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
                # so EVERY anchor relaxed against Cys-1 (2026-09-04: staged
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
    # garbage reference — measured on 2026-09-04, a 95-clash staged start
    # flipped >50% of binder CA chiralities in every refined sample. The
    # sampler must never receive a buried pose silently: zero <2.2 A pairs
    # and at least one <6 A contact (anchored, not floating) or we fail.
    # Skipped when pose_matters=False (blind route: peptide rows are
    # re-noised; only the receptor + sequences feed the engine).
    if pose_matters:
        rec_atoms_chk = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for r in check[0]["A"] for a in r if a.element.name != "H"])
        pep_atoms_chk = np.array([
            [a.pos.x, a.pos.y, a.pos.z]
            for r in check[0]["B"] for a in r if a.element.name != "H"])
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
    polymer = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"),
    )
    if not polymer:
        return None
    receptor, peptide = polymer[0], polymer[1]
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
) -> Any:
    """Dispatch one protenix2dock ``peptide`` refine task and return its
    AsyncResult immediately (pair with :func:`_dpeptide_collect_refine`).

    The receptor is pinned to the staged coordinates for every diffusion step
    (vendor inpainting side channel), the peptide enters the input json as a
    proteinChain, and bicyclic ring bonds (peptide SG <-> linker anchors) are
    carried both as input.json covalent_bonds and as hard TFG contacts.
    """
    from backend.core.celery_app import celery_app as _celery

    bond_pairs = _staged_bicyclic_bond_pairs(staged_path)
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
        except Exception:  # noqa: BLE001
            pass
    return _celery.send_task(
        "backend.worker.tasks.protenix2dock_task",
        kwargs={"score_args": {
            "mode": "peptide",
            "input_file_content": staged_path.read_text(),
            "input_filename": f"{staged_path.stem}.pdb",
            "peptide_chain": "B",
            # interface metrics scoped to Dtarget<->Lpeptide (A-B); the
            # linker's chain pair (B-C) would drag the reported iptm
            "interface_chains": "A,B",
            "bond_pairs": bond_pairs or "",
            "linker_chain": "L" if bond_pairs else "",
            "linker_ccd": linker_ccd,
            "seed": int(seed),
            # pocket anchor cap: the TFG/anchor upper bound must sit under
            # the acceptance gate (POCKET_CONTACT_MAX_A) or the refined pose
            # can legally drift out of the user pocket (measured 10-11 A with
            # the 8.0 default) and every candidate gets rejected.
            "pocket_upper": float(POCKET_CONTACT_MAX_A) + 1.0,
            "dpeptide_contract": True,
            "blind_peptide": bool(blind),
        }},
        queue=queue,
    )


def _dpeptide_refined_chirality_gate(refined_path: Path) -> None:
    """Hard per-residue chirality gate on a REFINED mirror-space complex.

    Mirror-space contract: receptor chain must be all-D, designed peptide
    all-L (the product flip then yields L-target + D-peptide). The diffusion
    sampler has no improper-dihedral term — under contradictory guidance it
    inverts CA centres (measured 2026-09-04: 11-15 of 17 residues flipped on
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
    receptor, peptide = protein_chains[0], protein_chains[1]
    rec_bad = chirality_violations(st, receptor.name, "D")
    pep_bad = chirality_violations(st, peptide.name, "L")
    if rec_bad or pep_bad:
        parts = []
        if rec_bad:
            parts.append(
                f"受体链 {receptor.name} 应为 D, 违规 {len(rec_bad)}: "
                + ",".join(f"{n}{r}" for n, r, _ in rec_bad[:6]))
        if pep_bad:
            parts.append(
                f"肽链 {peptide.name} 应为 L, 违规 {len(pep_bad)}: "
                + ",".join(f"{n}{r}" for n, r, _ in pep_bad[:6]))
        raise RuntimeError(
            "精修产物手性违规(镜像空间契约: 受体全D/肽全L) — " + "; ".join(parts))


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
    overrates strained/anchored poses (measured on the 2026-09-04 A/B: the
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
) -> Dict[str, Any]:
    """Wait for a dispatched refine task, then rank its diffusion samples.

    The confidence head scores the refined coordinates; the best sample (by
    ipTM, gated on covalent integrity + clash-freedom) is copied to
    refined_cif and its metrics returned.
    """
    task_tmp = Path("/data/boltz_central_results/_runtime_tmp") / \
        f"dpeptide_task_{async_result.id}"
    conf_path = task_tmp / "out" / "confidence.json"
    # Poll no longer than the dispatched GPU task itself is allowed to run
    # (SUBPROCESS_TIMEOUT) plus a small grace for result writes.
    from backend.worker import tasks as _worker_tasks

    refine_deadline = time.time() + int(
        getattr(_worker_tasks, "SUBPROCESS_TIMEOUT", 3 * 60 * 60)
    ) + 120
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
    # exactly when a defect needed diagnosing
    runtime_task_id = str(os.environ.get("BOLTZ_TASK_ID") or "").strip().replace(":", "_")
    keep_dir = Path("/data/boltz_central_results/_runtime_tmp") / \
        f"dpeptide_keep_{runtime_task_id or 'local'}_{Path(refined_cif).parent.name}"
    keep_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged_path, keep_dir / "staged.pdb")
    structure_dir = Path(str(metrics.get("structure_dir") or ""))
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
    # Covalent integrity, then clash-freedom, break ties BEFORE ipTM: the
    # anchor projections of the vendored sampler historically tore single
    # side-chain atoms off their residues (measured: 30+ detached atoms
    # across one task's shipped ranks, CZ-OH 1.37 -> 2.6 A); a chemically
    # intact sample with slightly lower ipTM ships instead of a torn one.
    # The sampler now also carries covalent bond bands + VDW clash floors
    # (official tfg.potentials semantics), so clean samples should dominate.
    ranked = []
    for iptm, cif in scored:
        try:
            detached = _covalent_detached_atoms(Path(cif))
            clashes = _interchain_clash_count(Path(cif))
        except Exception:  # noqa: BLE001
            detached = ["integrity-check-failed"]
            clashes = 10**6
        ranked.append((0 if detached else 1, -clashes, iptm, cif.name, cif, detached))
    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]), reverse=True)
    best = ranked[0]
    shutil.copyfile(best[4], refined_cif)
    metrics["refined_iptm"] = best[2]
    metrics["refined_covalent_intact"] = bool(best[0])
    metrics["refined_interchain_clashes"] = -int(best[1])
    for r in ranked:
        shutil.copyfile(r[4], keep_dir / Path(r[4]).name)
    if best[5]:
        metrics["refined_detached_atoms"] = best[5][:8]
        print(
            f"[d-peptide] refined sample picked with {len(best[5])} detached "
            f"atoms (no intact sample survived): {best[5][:4]}",
            file=sys.stderr,
        )
    # Per-chain mean pLDDT from the confidence B-factors of the best sample —
    # the design candidate rows need a native pLDDT (binder_avg_plddt); the
    # engine never emits it as a scalar, but the CIF B-factor column carries
    # the per-atom confidence.
    try:
        metrics["chain_mean_plddt"] = _chain_mean_plddt_from_structure(scored[0][1])
    except Exception:  # noqa: BLE001
        metrics["chain_mean_plddt"] = {}
    return metrics


def _celery_revoke_quiet(async_result: Any, *, terminate: bool = True) -> None:
    """Best-effort revoke; a control-bus hiccup must not mask the real error."""
    try:
        from backend.core.celery_app import celery_app as _celery

        _celery.control.revoke(async_result.id, terminate=terminate)
    except Exception:  # noqa: BLE001
        pass


def _dpeptide_refine_and_validate(
    staged_path: Path,
    refined_cif: Path,
    seed: int,
    queue: str,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Blocking dispatch+collect convenience wrapper around the split halves
    (kept for call sites that refine one candidate at a time)."""
    del options  # historical signature; the refine payload needs no options
    return _dpeptide_collect_refine(
        _dpeptide_dispatch_refine(staged_path, seed, queue),
        staged_path,
        refined_cif,
    )


def _chain_mean_plddt_from_structure(path: Path) -> Dict[str, float]:
    """Mean B-factor (== per-atom pLDDT in engine structure output) per chain."""
    st = gemmi.read_structure(str(path))
    st.setup_entities()
    means: Dict[str, float] = {}
    for chain in st[0]:
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
    (measured on shipped ranks: 30+ detached atoms across 25 structures).
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
    reads D) and the linker gate (SG untouched) — measured on a shipped
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
    # in the pocket wall — measured 0.3 A min distance); mode A skips it —
    # the staged pose already carries the reference binding mode
    if keep_pose:
        st_pl.setup_entities()
        st_pl.write_pdb(str(staged_path))
    else:
        free_atoms = []
        for ch_mv in st_pl[0]:
            if ch_mv.name == pl_ch[0].name:
                continue
            for r_mv in ch_mv:
                for a_mv in r_mv:
                    if a_mv.element != gemmi.Element("H"):
                        free_atoms.append(np.array([a_mv.pos.x, a_mv.pos.y, a_mv.pos.z]))
        free_coords = np.stack(free_atoms)
        rec_atoms = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in pl_ch[0] for a in r
                              if a.element != gemmi.Element("H")])
        rot_place, shift_place = _dpeptide_pick_clash_free_placement(
            free_coords, rec_atoms, np.asarray(_target_center), seed=seed,
            pocket_coords=np.asarray(pocket_atom_coords))
        for ch_mv in st_pl[0]:
            if ch_mv.name == pl_ch[0].name:
                continue
            for r_mv in ch_mv:
                for a_mv in r_mv:
                    v = rot_place @ (np.array([a_mv.pos.x, a_mv.pos.y, a_mv.pos.z]) - free_coords.mean(axis=0)) \
                        + free_coords.mean(axis=0) + shift_place
                    a_mv.pos = gemmi.Position(*v)
        st_pl.setup_entities()
        st_pl.write_pdb(str(staged_path))
    # gemmi's write_pdb dropped the LINK records above — restore the covalent
    # topology or the refine diffusion breaks the ring (measured 13 A bonds)
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
) -> Dict[str, Any]:
    """GPU-result half of the pocket route (pairs with
    :func:`_pocket_place_for_refine` + dispatch): collect the dispatched
    refine, then report on the shipped structure.

    Raises ValueError when a gate fails; the caller rejects the candidate.
    """
    metrics = _dpeptide_collect_refine(async_result, staged_path, refined_cif)
    refined_path = str(refined_cif)

    # Quality TELEMETRY, not filtering: the staged strain relief upstream
    # makes the refine start chemically sound, so these reports describe the
    # shipped structure (and would expose any residual defect) without
    # rejecting the candidate — rejection hides engine problems instead of
    # solving them at the source.
    integrity = _structure_integrity_report(Path(refined_path))

    if require_bonds:
        bond_report = _dpeptide_linker_bond_report(Path(refined_path))
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
    return {
        "staged": str(staged_path),
        "refined": refined_path,
        "metrics": metrics,
        "pocket": pocket_report,
        "bonds": bond_report,
        "integrity": integrity,
        "quality_flags": flags,
    }


def _pocket_place_and_refine(
    staged_path: Path,
    *,
    pocket_sequence_contacts: List[Tuple[str, int]],
    refined_cif: Path,
    seed: int,
    options: Dict[str, Any],
    require_bonds: bool,
    chirality_label: str,
    bicyclic_cys_positions: Optional[List[int]] = None,
    linker_ccd: str = "SEZ",
    keep_pose: bool = False,
) -> Dict[str, Any]:
    """Shared pocket mechanism for both chiralities — blocking place +
    dispatch + collect convenience wrapper around the split halves (kept for
    single-candidate call sites; the design loop uses the split halves so a
    generation's refines overlap across the GPU pool)."""
    del options  # historical signature; the refine payload needs no options
    _pocket_place_for_refine(
        staged_path,
        pocket_sequence_contacts=pocket_sequence_contacts,
        seed=seed,
        require_bonds=require_bonds,
        bicyclic_cys_positions=bicyclic_cys_positions,
        linker_ccd=linker_ccd,
        keep_pose=keep_pose,
    )
    return _pocket_collect_refine(
        _dpeptide_dispatch_refine(
            staged_path, seed, build_capability_queue("protenix", "default")
        ),
        staged_path=staged_path,
        refined_cif=refined_cif,
        pocket_sequence_contacts=pocket_sequence_contacts,
        require_bonds=require_bonds,
        chirality_label=chirality_label,
    )


def _pdb_link_line(
    atom1: str, res1: str, chain1: str, seq1: int,
    atom2: str, res2: str, chain2: str, seq2: int,
) -> str:
    """Column-exact PDB LINK record. Atom names must sit right-aligned in
    cols 13-16/43-46 — a left-aligned name makes gemmi infer a metal
    coordination (MetalC) instead of a covalent bond, and boltz2 only honors
    Covale connections."""
    cols = [" "] * 78

    def put(start: int, text: str) -> None:
        for offset, char in enumerate(text):
            cols[start - 1 + offset] = char

    put(1, "LINK")
    put(13, f"{atom1:>4}")
    put(18, f"{res1:>3}")
    put(22, chain1)
    put(23, f"{seq1:4d}")
    put(43, f"{atom2:>4}")
    put(48, f"{res2:>3}")
    put(52, chain2)
    put(53, f"{seq2:4d}")
    put(60, "1555")
    put(66, "1555")
    return "".join(cols).rstrip()


def _append_staged_bicyclic_links(
    staged_path: Path,
    cys_positions: Optional[List[int]],
    linker_ccd: str,
) -> int:
    """(Re-)append the Cys-SG <-> linker-anchor LINK records to a staged PDB.

    gemmi 0.7.5's write_pdb silently drops connections, so every rewrite of
    the staged file (pocket placement, L-space staging) loses the covalent
    topology the refine engine needs to hold the ring together. Idempotent:
    returns 0 when the file already carries LINK records. Pairs each peptide
    Cys (in cys_positions, or every Cys when None) with the linker anchors in
    BICYCLIC_LINKER_ATOM_MAP order.
    """
    text = staged_path.read_text(errors="replace")
    if any(line.startswith("LINK") for line in text.splitlines()):
        return 0
    st = gemmi.read_structure(str(staged_path))
    st.setup_entities()
    chains = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 1),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"),
    )
    if not chains:
        return 0
    receptor_name = chains[0].name
    peptide_chain = None
    linker_chain_name = linker_resnum = None
    for chain in st[0]:
        if chain.name == receptor_name:
            continue
        if linker_chain_name is None and any(
                r.name == linker_ccd for r in chain):
            for residue in chain:
                if residue.name == linker_ccd:
                    linker_chain_name = chain.name
                    linker_resnum = int(residue.seqid.num)
                    break
        elif peptide_chain is None and any(r.name == "CYS" for r in chain):
            peptide_chain = chain
        if peptide_chain is not None and linker_chain_name is not None:
            break
    if peptide_chain is None or linker_chain_name is None:
        return 0
    anchors = BICYCLIC_LINKER_ATOM_MAP.get(str(linker_ccd).upper()) or []
    if cys_positions is not None:
        wanted = {int(p) + 1 for p in cys_positions}
        sg_resnums = [int(r.seqid.num) for r in peptide_chain
                      if r.name == "CYS" and int(r.seqid.num) in wanted]
    else:
        sg_resnums = [int(r.seqid.num) for r in peptide_chain if r.name == "CYS"]
    sg_resnums.sort()
    if len(sg_resnums) != len(anchors):
        raise ValueError(
            f"staged bicyclic complex has {len(sg_resnums)} Cys-SG but linker "
            f"{linker_ccd} needs {len(anchors)} anchors"
        )
    link_lines = []
    for sg_resnum, anchor in zip(sg_resnums, anchors):
        link_lines.append(_pdb_link_line(
            "SG", "CYS", peptide_chain.name, sg_resnum,
            anchor, linker_ccd, linker_chain_name, linker_resnum,
        ))
    # insert before the trailing END record — PDB parsers stop at END
    lines = [line for line in text.splitlines() if line.strip()]
    while lines and lines[-1].strip() in ("END", "ENDMDL"):
        lines.pop()
    with staged_path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
        for line in link_lines:
            handle.write(line + "\n")
        handle.write("END\n")
    return len(link_lines)


def _dpeptide_linker_bond_report(structure_path: Path) -> Dict[str, Any]:
    """Measure whether the bicyclic linker is actually bonded in a shipped
    structure: for every linker anchor atom, the distance to the nearest
    Cys-SG. Bonded C-S distances are ~1.8 A; 2.5 A is the reporting cut-off."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()

    linker_res = None
    linker_code = ""
    for chain in st[0]:
        for residue in chain:
            if residue.het_flag == "H" and residue.name in BICYCLIC_LINKER_ATOM_MAP:
                linker_res = residue
                linker_code = residue.name
                break
        if linker_res is not None:
            break
    if linker_res is None:
        return {"linker": None, "all_bonded": None, "bonds": []}

    anchors = BICYCLIC_LINKER_ATOM_MAP[linker_code]
    sg_atoms = []
    for chain in st[0]:
        for residue in chain:
            sg = residue.find_atom("SG", "*")
            if sg is not None and residue.name == "CYS":
                sg_atoms.append((residue.seqid.num, sg))
    bonds = []
    remaining = list(sg_atoms)  # one-to-one: symmetric linkers repeat an
    for anchor_name in anchors:  # anchor atom; nearest-per-anchor would let
        anchor_atom = linker_res.find_atom(anchor_name, "*")  # one SG win all
        if anchor_atom is None or not remaining:
            bonds.append({"anchor": anchor_name, "cys_resnum": None,
                          "distance": None, "bonded": False})
            continue
        best = min(
            remaining,
            key=lambda item: (
                (item[1].pos.x - anchor_atom.pos.x) ** 2
                + (item[1].pos.y - anchor_atom.pos.y) ** 2
                + (item[1].pos.z - anchor_atom.pos.z) ** 2),
        )
        remaining.remove(best)
        dist = math.sqrt(
            (best[1].pos.x - anchor_atom.pos.x) ** 2
            + (best[1].pos.y - anchor_atom.pos.y) ** 2
            + (best[1].pos.z - anchor_atom.pos.z) ** 2)
        bonds.append({
            "anchor": anchor_name,
            "cys_resnum": best[0],
            "distance": round(dist, 3),
            "bonded": bool(dist <= 2.5),
        })
    distinct = len({b["cys_resnum"] for b in bonds if b["cys_resnum"] is not None})
    return {
        "linker": linker_code,
        "all_bonded": bool(all(b["bonded"] for b in bonds)
                           and distinct == len(bonds)),
        "distinct_cys": distinct,
        "bonds": bonds,
        "cutoff_a": 2.5,
    }


def _assert_product_chirality(
    product_pdb: Path,
    reference_structure_path: Optional[Path] = None,
    rmsd_limit: float | None = 0.5,
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
    receptor_chain, peptide_chain = protein_chains[0], protein_chains[1]

    rec_report = chirality_report(st, receptor_chain.name)
    pep_report = chirality_report(st, peptide_chain.name)
    from peplm.dpeptide import chirality_violations
    # Product frame contract: receptor ALL-L, peptide ALL-D — per residue.
    # The mean-volume check silently passed 11L/9D mixtures (the mean stays on
    # the expected side); a mixed-chirality product is a failed product.
    rec_bad = chirality_violations(st, receptor_chain.name, "L")
    pep_bad = chirality_violations(st, peptide_chain.name, "D")
    if rec_report.n_scored <= 0 or pep_report.n_scored <= 0:
        raise RuntimeError(
            "D-peptide product gate: no scorable CA chiral volumes "
            f"(receptor {rec_report.n_scored}, peptide {pep_report.n_scored})")
    if rec_bad or pep_bad:
        parts = []
        if rec_bad:
            parts.append(
                f"receptor not all-L: {len(rec_bad)} violations ("
                + ",".join(f"{n}{r}" for n, r, _ in rec_bad[:6]) + ")")
        if pep_bad:
            parts.append(
                f"peptide not all-D: {len(pep_bad)} violations ("
                + ",".join(f"{n}{r}" for n, r, _ in pep_bad[:6]) + ")")
        raise RuntimeError(
            "D-peptide product chirality gate FAILED — " + "; ".join(parts))

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
        for residue in receptor_chain:
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
        "receptor_chain": receptor_chain.name,
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
    # guidance step (measured L products: 1.2-2.2 A); Boltz2's bond feature is
    # only a soft diffusion prior and lets constrained rings fall apart under
    # refinement (measured 11-15 A). Rings are therefore Protenix-only for
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
        max_value=120,
    )
    iterations = _read_int_option(options, "peptideIterations", 12, min_value=1, max_value=200)
    population_size = _read_int_option(options, "peptidePopulationSize", 16, min_value=1, max_value=200)
    elite_size = _read_int_option(options, "peptideEliteSize", 4, min_value=1, max_value=max(1, population_size))
    use_initial_sequence = _read_bool_option(options, "peptideUseInitialSequence", False)
    sequence_mask = _normalize_sequence_mask(options.get("peptideSequenceMask"), binder_length)
    linker_ccd = str(options.get("peptideBicyclicLinkerCcd") or "SEZ").strip().upper() or "SEZ"

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
    if design_mode == "bicyclic":
        cys_position_mode = str(options.get("peptideBicyclicCysPositionMode") or "auto").strip().lower()
        fix_terminal_cys = _read_bool_option(options, "peptideBicyclicFixTerminalCys", True)
        allow_extra_cys = _read_bool_option(options, "peptideBicyclicIncludeExtraCys", False)
        if cys_position_mode == "manual":
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

    def _torch_cuda_available() -> bool:
        try:
            import torch

            return torch.cuda.is_available()
        except ImportError:
            return False

    # PeptideLM proposal engine — the ONLY peptide design algorithm in
    # V-Bio (two-tier language-model design; see capabilities/peptide_lm).
    # No fallback: a failure here is a task failure. Length is optional:
    # when the user did not set peptideBinderLength the engine explores an
    # adaptive range; NCAA residues come ONLY from the user-selected pool
    # (peptideResiduePool non-natural entries + custom CCDs).
    try:
        _plm_sys_path = "/data/V-Bio/capabilities/peptide_lm"
        sys.path.insert(0, _plm_sys_path)
        try:
            from peplm.integrate.backend_proposer import BackendProposer
        finally:
            sys.path.remove(_plm_sys_path)
        # user-fixed residues from the sequence mask letters (X = free)
        _plm_fixed: List[Dict[str, Any]] = []
        for _idx, _ch in enumerate(sequence_mask or ""):
            if _ch in "ACDEFGHIKLMNPQRSTVWY":
                _plm_fixed.append({"position": _idx + 1, "residue": _ch})
        # adaptive length only when the user left it unset
        _plm_len: Optional[int] = binder_length
        if "peptideBinderLength" not in options and "peptide_binder_length" not in options:
            _plm_len = None
        # explicit length window (frontend min/max inputs) beats both: it is
        # a range for adaptive design, or collapses to a fixed value when
        # min == max
        _plm_range: Optional[Tuple[int, int]] = None
        if "peptideLengthMin" in options or "peptide_length_min" in options:
            _lo = _read_int_option(options, "peptideLengthMin", 8,
                                   min_value=min_binder_len, max_value=120)
            _hi = _read_int_option(options, "peptideLengthMax", 25,
                                   min_value=min_binder_len, max_value=120)
            if _hi < _lo:
                raise ValueError(
                    f"肽长度窗口无效：min {_lo} > max {_hi}。")
            _plm_range = (_lo, _hi)
            _plm_len = None
        # manual Cys anchors reference absolute positions, so they pin the
        # design length to the value the anchors were validated against
        if bicyclic_manual_anchors:
            if _plm_range is not None and _plm_range[0] != _plm_range[1]:
                raise ValueError(
                    "手动 Cys 位置要求固定的肽长度：请将长度范围设为同一个值，或改用 Auto 模式。"
                )
            _plm_range = None
            _plm_len = binder_length
        # user NCAA pool: preset selections + custom drawn CCDs
        _plm_pool = [str(row.get("ccd") or "").strip().upper()
                     for row in (unnatural_pool or []) if row.get("ccd")]
        peptidelm_proposer = BackendProposer(
            peptide_length=_plm_len,
            len_range=_plm_range,
            ncaa_min=nonnatural_min,
            ncaa_max=nonnatural_max,
            ncaa_pool=_plm_pool,
            cyclic=(design_mode == "cyclic"),
            design_mode=design_mode,
            cys_positions=list(design_params.get("cys_positions") or []),
            allow_extra_cys=allow_extra_cys,
            fixed_residues=_plm_fixed,
            ncaa_decode_bias=float(options.get("peptideNcaaDecodeBias") or 0.5),
            device=os.environ.get("VBIO_PEPTIDELM_DEVICE") or (
                "cuda" if _torch_cuda_available() else "cpu"),
            log=lambda m: print(f"[peptidelm] {m}", file=sys.stderr),
        )
        print(
            f"[peptidelm] 提案引擎已启用（length={'自适应' if _plm_len is None else _plm_len}, "
            f"NCAA 池 {len(_plm_pool)} 个, 固定残基 {len(_plm_fixed)} 个, "
            f"mode={design_mode}）",
            file=sys.stderr,
        )
    except Exception as exc:
        raise RuntimeError(f"PeptideLM 提案引擎初始化失败：{exc}") from exc

    # D-peptide mirror workflow context: mirror the target once so every
    # candidate is designed against the fixed D-target (see module docstring
    # block above). Chirality 'l' keeps the plain L-frame loop.
    # user pocket (optional): "chain:num,chain:num" receptor residues (author
    # numbering of the upload) or an explicit x,y,z center. Translated here to
    # 1-based sequence positions — staged structures and native predictions
    # number polymer residues 1..N — and consumed by the pocket placement in
    # the collect loop (both chiralities). Empty = global (no pocket).
    try:
        pocket_author_contacts, pocket_sequence_contacts = (
            _pocket_contacts_for_staged_space(
                base_yaml_data, options, resolved_target_chain_id))
    except ValueError as pocket_err:
        raise ValueError(f"口袋定义无效：{pocket_err}") from pocket_err
    if pocket_sequence_contacts:
        print(
            "[peptide-design] user pocket (sequence numbering): "
            + ",".join(f"{c}:{n}" for c, n in pocket_sequence_contacts),
            file=sys.stderr,
        )
    pocket_constraint_blueprint: Optional[Dict[str, Any]] = (
        {"contacts": [[c, n] for c, n in pocket_author_contacts]}
        if pocket_author_contacts else None
    )

    dpeptide_reference_target: Optional[Path] = None
    d_target_staged: Optional[Path] = None
    if peptide_chirality == 'd':
        # template_inputs were popped from predict_args at the main() boundary;
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
    blind_linear_route = (
        peptide_chirality == 'd' and design_mode == "linear"
        and d_reference_peptide is None)
    if (peptide_chirality == 'd' and not pocket_sequence_contacts
            and not blind_linear_route):
        # 无口袋 D-肽此前静默退化到"靶点质心摆位"(2026-09-04 事故: staged 埋置
        # 0.16 A,精修全面翻手性)。BICYCLIC/参考锚定模式必须有口袋或参考;
        # 线性模式走盲 inpainting(受体钉住+肽从噪声,姿态由 MSA 先验产生,
        # A/B 实测红dock RMSD 1.5-2.0 A),不需要口袋。
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
    parent_task_id = str(os.environ.get("BOLTZ_TASK_ID") or "peptide-design").strip() or "peptide-design"
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

    for generation in range(1, iterations + 1):
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

        # PeptideLM proposals — the single proposal source. When the user
        # supplied an initial (seed) sequence, generation 1 anchors edits on
        # it instead of de novo sampling. Length may be adaptive; the
        # bicyclic Cys layout and NCAA pool are enforced by the proposer's
        # decode-time constraint plan.
        try:
            _proposer_elites: List[Dict[str, Any]] = list(elite_population)
            if generation == 1 and initial_sequence and not _proposer_elites:
                _proposer_elites = [{
                    "sequence": initial_sequence,
                    "modifications": [],
                    "plddts": [],
                }]
            for lm_base, lm_mods, lm_anchors in peptidelm_proposer.propose(
                natural_pool,
                unnatural_pool,
                _proposer_elites,
                population_size,
            ):
                lm_sequence = _apply_sequence_mask(str(lm_base or "").upper(), sequence_mask)
                lm_mods = [m for m in (lm_mods or []) if isinstance(m, dict)]
                lm_key = _peptide_candidate_key(lm_sequence, lm_mods)
                if lm_key in evaluated_sequences:
                    continue
                evaluated_sequences.add(lm_key)
                generation_candidates.append({
                    "sequence": lm_sequence,
                    "modifications": lm_mods,
                    "cys_positions": [int(p) for p in (lm_anchors or [])],
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

        generation_binder_msa = _prefetch_generation_binder_msas(
            [str(c.get("sequence") or "") for c in generation_candidates],
            on_progress=_report_msa_prefetch,
        )

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
            # positions at backend entry (boltz: run_boltz_backend, protenix:
            # run_protenix_backend) and enforce the pocket natively during
            # candidate prediction.
            candidate_pocket_constraint = None
            if pocket_constraint_blueprint:
                candidate_pocket_constraint = dict(pocket_constraint_blueprint)
                candidate_pocket_constraint["binder"] = binder_chain_id
            candidate_yaml = _build_peptide_candidate_yaml(
                candidate_base_yaml_data,
                binder_chain_id=binder_chain_id,
                binder_sequence=candidate_sequence,
                design_mode=design_mode,
                linker_ccd=linker_ccd,
                linker_chain_id=linker_chain_id,
                linker_atom_map=linker_atom_map,
                modifications=candidate_modifications,
                backend=peptide_backend,
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
                    "generation": generation,
                    "candidate_index": idx,
                    "sequence": candidate_sequence,
                    "modifications": candidate_modifications,
                    "cys_positions": candidate.get("cys_positions") if isinstance(candidate.get("cys_positions"), list) else [],
                    "candidate_yaml": candidate_yaml,
                    "candidate_dir": candidate_dir,
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
        # (The GPU refine used to run inline in a strictly serial per-candidate
        # loop — one ~4-5 min protenix2dock task at a time while every other
        # GPU idled for the whole generation. The loop is now a two-stage
        # pipeline: the prepare pass below stages/dispatches, and
        # _run_dpeptide_refine_stage finalizes each candidate as its refine
        # completes. Finalization order is completion order; all_results gets
        # sorted on every append either way.)
        def _finalize_candidate(ctx: Dict[str, Any]) -> None:
            nonlocal completed_tasks, generation_done
            if ctx.get("reject"):
                print(ctx["reject"], file=sys.stderr)
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
                ps = max(0.0, min(1.0, (8.0 - float(pm)) / 3.0)) if pm > 5.0 else 1.0
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
                        )
                        d_space_refined = gate_result["refined"]
                        d_space_metrics = gate_result["metrics"]
                        pocket_report_row = gate_result["pocket"]
                        _rescore_with_refined_pocket(Path(d_space_refined))
                    else:
                        d_space_metrics = _dpeptide_collect_refine(
                            ctx["async_result"],
                            Path(ctx["staged_path"]),
                            Path(ctx["refined_cif"]),
                        )
                        d_space_refined = str(ctx["refined_cif"])
                        if design_mode == "bicyclic":
                            bond_report = _dpeptide_linker_bond_report(Path(d_space_refined))
                            if not (bond_report and bond_report.get("all_bonded")):
                                print(
                                    f"[d-peptide] candidate {candidate_sequence[:12]}… "
                                    f"rejected: refined ring bonds broken {bond_report}",
                                    file=sys.stderr,
                                )
                                return
                    # 硬手性门: 精修产物必须满足镜像空间契约(受体全D/肽全L)。
                    _dpeptide_refined_chirality_gate(Path(d_space_refined))
                    print(
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"D-space ipTM={d_space_metrics.get('iptm')}",
                        file=sys.stderr,
                    )
                except (RuntimeError, ValueError) as d_exc:
                    print(
                        f"[d-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {d_exc}",
                        file=sys.stderr,
                    )
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
                    )
                    structure_file = Path(gate_result["refined"])
                    staged_path = gate_result["staged"]
                    d_space_refined = gate_result["refined"]
                    d_space_metrics = gate_result["metrics"]
                    pocket_report_row = gate_result["pocket"]
                    _rescore_with_refined_pocket(Path(d_space_refined))
                except (RuntimeError, ValueError) as pocket_exc:
                    print(
                        f"[l-peptide] candidate {candidate_sequence[:12]}… "
                        f"rejected: {pocket_exc}",
                        file=sys.stderr,
                    )
                    return

            if peptide_chirality == 'd' and isinstance(d_space_metrics, dict):
                # The refined D-space complex is the ONLY interface readout for
                # D candidates (binder-only conformers carry none). Recompute
                # every ranking input from it — the pre-fix code merged only
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
                    if (not isinstance(chain_plddt, dict)
                            or not isinstance(chain_plddt.get("B"), (int, float))
                            or float(chain_plddt["B"]) <= 0):
                        raise RuntimeError("精修结果缺少肽链 pLDDT 读数,拒绝以空值评分")
                    chain_b = float(chain_plddt["B"])
                    binder_avg_plddt = chain_b * 100.0 if chain_b <= 1.0 else chain_b
                    resc = _dpeptide_composite_from_refined(
                        refined_ipsae=pair_iptm,
                        binder_avg_plddt=binder_avg_plddt,
                        developability_score=developability_score,
                        has_pocket=bool(pocket_sequence_contacts),
                    )
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
                # surface the raw ipSAE components on the row: the frontend's
                # interface resolver keys on ligand_ipsae_max/ipsae_dom
                metrics["ipsae_dom"] = d_space_metrics.get("ipsae_dom")
                metrics["ligand_ipsae_max"] = d_space_metrics.get("ligand_ipsae_max")

            result_row = {
                "sequence": candidate_sequence,
                "modifications": candidate_modifications,
                "cys_positions": job.get("cys_positions") if isinstance(job.get("cys_positions"), list) else [],
                "generation": generation,
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
                "sequence_liabilities": liability,
                "composite_score": composite_score,
                "score": composite_score,
                "plddt": binder_avg_plddt,
                "model": "Boltz" if peptide_backend == "boltz" else ("AlphaFold3" if peptide_backend == "alphafold3" else "Protenix"),
                "backend": peptide_backend,
                "target_chain_id": resolved_target_chain_id,
                "binder_chain_id": binder_chain_id,
                "linker_chain_id": linker_chain_id if design_mode == "bicyclic" else "",
                "structure_source_path": str(structure_file) if structure_file else "",
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
                "plddts": metrics.get("plddts") if isinstance(metrics.get("plddts"), list) else [],
            }
            all_results.append(result_row)
            completed_tasks += 1
            generation_done += 1

            all_results.sort(
                key=lambda item: (
                    1 if isinstance(item.get("composite_score"), (int, float)) else 0,
                    float(item.get("composite_score")) if isinstance(item.get("composite_score"), (int, float)) else float("-inf"),
                ),
                reverse=True,
            )
            elite_population = [
                {
                    "sequence": str(row.get("sequence") or ""),
                    "modifications": row.get("modifications") if isinstance(row.get("modifications"), list) else [],
                    "plddts": row.get("plddts") if isinstance(row.get("plddts"), list) else [],
                }
                for row in _select_nsga2_peptide_elites(all_results, elite_size)
            ]
            # PeptideLM: GRPO update on this generation's scored rows so the
            # proposal policy improves round over round
            try:
                peptidelm_proposer.learn(
                    elite_population,
                    [row for row in all_results if row.get("generation") == generation],
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
                    composite_score = (
                        0.58 * pair_iptm_confidence
                        + 0.30 * binder_confidence
                        + 0.12 * developability_score
                    )
                if pair_iptm is None:
                    pair_iptm_formula = "binder_avg_plddt_developability_only"
            else:
                composite_score = None
            structure_file = _select_primary_structure_file(result_dir)

            # D-route (chirality=d): stage the candidate's isolated conformer
            # against the prepared D-target at the user pocket, then dispatch
            # the fixed-D diffusion refine (collected in _finalize_candidate
            # as it completes). Failure here fails the task — the D-space
            # numbers are a mandatory deliverable, never skipped.
            staged_path = ""
            d_space_refined = ""
            d_space_metrics: Optional[Dict[str, Any]] = None
            pocket_report_row: Optional[Dict[str, Any]] = None
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
                })
                continue
            if peptide_chirality == 'd':
                # A candidate whose staging/refine degenerates (e.g. a
                # collapsed diffusion sample) is rejected individually; the
                # task only fails when NO candidate survives.
                try:
                    _job_seed = job.get("predict_args") if isinstance(job.get("predict_args"), dict) else {}
                    _seed_v = _job_seed.get("seed")
                    _seed_v = int(_seed_v) if isinstance(_seed_v, int) else random_seed
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
                    if pocket_sequence_contacts:
                        # Pocket placement is CPU work (mutates the staged
                        # file); the GPU refine dispatch follows.
                        _pocket_place_for_refine(
                            Path(staged_path),
                            pocket_sequence_contacts=pocket_sequence_contacts,
                            seed=_seed_v,
                            require_bonds=(design_mode == "bicyclic"),
                            bicyclic_cys_positions=(
                                job.get("cys_positions")
                                if isinstance(job.get("cys_positions"), list) else None),
                            linker_ccd=linker_ccd,
                            keep_pose=(d_reference_peptide is not None),
                        )
                        route = "pocket"
                    else:
                        route = "plain"
                    _staged_for_dispatch = Path(staged_path)
                    _seed_for_dispatch = _seed_v

                    def dispatch(_staged: Path = _staged_for_dispatch, _seed: int = _seed_for_dispatch):  # noqa: E731
                        return _dpeptide_dispatch_refine(
                            _staged, _seed, build_capability_queue("protenix", "default"),
                            blind=blind_linear_route)
                except (RuntimeError, ValueError) as d_exc:
                    stage_contexts.append({
                        "reject": (
                            f"[d-peptide] candidate {candidate_sequence[:12]}… "
                            f"rejected: {d_exc}"
                        ),
                    })
                    continue

            # L chirality + user pocket: protenix-v2 has no constraint
            # embedder, so the native prediction's peptide pose does not
            # follow the pocket. Honor it the same way the D route does:
            # rigidly place the free chains at the pocket on the native
            # product's receptor, dispatch the refine under the fixed
            # receptor (collected in _finalize_candidate); the refined
            # structure becomes the candidate's shipped product.
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
                    )
                    route = "pocket"
                    refined_cif = str(Path(candidate_dir) / "pocket_refined.cif")
                    _staged_for_dispatch = Path(staged_path)
                    _seed_for_dispatch = _seed_v

                    def dispatch(_staged: Path = _staged_for_dispatch, _seed: int = _seed_for_dispatch):  # noqa: E731
                        return _dpeptide_dispatch_refine(
                            _staged, _seed, build_capability_queue("protenix", "default"),
                            blind=blind_linear_route)
                except (RuntimeError, ValueError) as pocket_exc:
                    stage_contexts.append({
                        "reject": (
                            f"[l-peptide] candidate {candidate_sequence[:12]}… "
                            f"rejected: {pocket_exc}"
                        ),
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

    all_results.sort(
        key=lambda item: (
            1 if isinstance(item.get("composite_score"), (int, float)) else 0,
            float(item.get("composite_score")) if isinstance(item.get("composite_score"), (int, float)) else float("-inf"),
        ),
        reverse=True,
    )
    top_results = _select_nsga2_peptide_elites(all_results, min(24, len(all_results)))
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
                staged_path = str(next_row.pop("d_space_refined", "") or "")
                if not (staged_path and os.path.isfile(staged_path)):
                    staged_path = str(next_row.get("d_space_staged", "") or "")
                if not (staged_path and os.path.isfile(staged_path)):
                    raise RuntimeError(
                        "D-peptide candidate is missing its D-space refined "
                        f"structure (rank {rank}, sequence "
                        f"{str(next_row.get('sequence'))[:16]}, staged="
                        f"{staged_path!r})"
                    )
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
            if structure_source_for_rank and os.path.isfile(structure_source_for_rank):
                suffix = Path(structure_source_for_rank).suffix.lower()
                ext = ".pdb" if suffix == ".pdb" else ".cif"
                structure_arcname = f"structures/rank_{rank:02d}{ext}"
                zipf.write(structure_source_for_rank, structure_arcname)
                # rank_cif_source stays on the MIRROR-SPACE source: the
                # product block flips it exactly once. Overwriting it with the
                # already-flipped display file would double-flip.
                if rank == 1 and not rank_cif_source:
                    rank_cif_source = structure_source_for_rank or source_path
            next_row["rank"] = rank
            next_row["structure_file"] = structure_arcname
            next_row["structure_name"] = Path(structure_arcname).name if structure_arcname else ""
            next_row["structure_path"] = structure_arcname
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

    source_components = source_common_dir / "components.cif"
    source_rdkit = source_common_dir / "components.cif.rdkit_mol.pkl"
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
    (overlay_common / "components.cif").write_text(merged_cif_text, encoding="utf-8")

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
    with (overlay_common / "components.cif.rdkit_mol.pkl").open("wb") as handle:
        pickle.dump(merged_rdkit_mols, handle)

    for source_item in source_common_dir.iterdir():
        if source_item.name in {"components.cif", "components.cif.rdkit_mol.pkl"}:
            continue
        target_item = overlay_common / source_item.name
        if target_item.is_symlink():
            target_item.unlink()
        try:
            if source_item.is_dir():
                if target_item.exists() and not target_item.is_dir():
                    target_item.unlink()
                shutil.copytree(source_item, target_item, dirs_exist_ok=True)
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

def run_boltz_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    predict_args: dict,
    model_name: Optional[str],
    task_id: Optional[str] = None,
    strict_ligand_confidence_contract: bool = False,
    custom_ccd_molecules: Optional[List[Dict[str, str]]] = None,
    low_vram: bool = False,
    ipsae_ligand_chain_id: Optional[str] = None,
) -> None:
    normalized_yaml = _normalize_ligand_chain_collisions(yaml_content)
    _validate_unique_sequence_chain_ids(normalized_yaml)
    normalized_yaml = _remap_constraints_by_template_alignment(normalized_yaml)
    _validate_unique_sequence_chain_ids(normalized_yaml)
    normalized_yaml = _sanitize_constraints_for_chain_lengths(normalized_yaml)
    _print_constraint_residue_summary(normalized_yaml)

    cli_args = dict(predict_args)
    requested_use_msa = coerce_bool(cli_args.pop("use_msa_server", None), False)
    cli_args.pop("msa_server_url", None)
    requires_external_msa = infer_use_msa_server_from_yaml_text(normalized_yaml)
    # low_vram is consumed here, not by the Boltz CLI — drop it so it isn't forwarded unknown.
    cli_args.pop("low_vram", None)
    if model_name:
        cli_args['model'] = model_name
    if strict_ligand_confidence_contract:
        cli_args["strict_ligand_confidence_contract"] = True
    if low_vram:
        # Two peak-VRAM levers for large complexes on a single GPU: serialize diffusion
        # samples (default 5 parallel → 1) and drop to bf16-mixed precision. Neither alone
        # is enough for ~2000+ token complexes; both are needed.
        cli_args["max_parallel_samples"] = 1
        if not cli_args.get("trainer_precision"):
            cli_args["trainer_precision"] = "bf16-mixed"

    if 'diffusion_samples' not in cli_args or cli_args['diffusion_samples'] is None:
        effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
        if effective_model == 'boltz2':
            cli_args['diffusion_samples'] = 5
    if 'trainer_precision' not in cli_args or cli_args['trainer_precision'] is None:
        effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
        if effective_model == 'boltz2':
            cli_args['trainer_precision'] = '32'

    results_root = _resolve_backend_results_root("boltz", task_id, temp_dir)
    work_root = _resolve_backend_work_root(results_root)

    if requires_external_msa:
        msa_server_url = _assert_msa_server_configured("boltz")
        if not requested_use_msa:
            print("Boltz2 输入缺少 MSA，已启用外部 MSA。", file=sys.stderr)
        print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
        _require_complete_external_msa(normalized_yaml, str(work_root), "Boltz2")
        print("MSA 生成成功，将用于结构预测", file=sys.stderr)
        normalized_yaml, injected_count = _inject_local_msa_paths_into_yaml(normalized_yaml, str(work_root))
        if injected_count > 0:
            print(f"Injected local MSA paths into YAML: {injected_count}", file=sys.stderr)
        cli_args['use_msa_server'] = True
        cli_args['msa_server_url'] = msa_server_url
    else:
        print("Boltz2 输入已禁用或提供 MSA，跳过外部 MSA 生成。", file=sys.stderr)

    tmp_yaml_path = str(work_root / 'data.yaml')
    with open(tmp_yaml_path, 'w') as tmp_yaml:
        tmp_yaml.write(normalized_yaml)
    cli_args['data'] = tmp_yaml_path
    cli_args['out_dir'] = str(results_root)

    POSITIONAL_KEYS = ['data']
    cmd_positional = []
    cmd_options = []

    for key, value in cli_args.items():
        if key in POSITIONAL_KEYS:
            cmd_positional.append(str(value))
        else:
            if value is None:
                continue
            if isinstance(value, bool):
                if value:
                    cmd_options.append(f'--{key}')
            else:
                cmd_options.append(f'--{key}')
                cmd_options.append(str(value))

    cmd_args = cmd_positional + cmd_options

    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        gpu_arg = determine_docker_gpu_arg(visible_devices)
    except RuntimeError as gpu_err:
        print(f"[boltz2] GPU env unavailable: {gpu_err}", file=sys.stderr)
        raise

    image = (BOLTZ2_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 Boltz2 Docker。")

    raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"[WARN] 已忽略部分 BOLTZ2_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
            file=sys.stderr,
        )
    shm_size = str(BOLTZ2_DOCKER_SHM_SIZE or "").strip()

    runtime_task_id = str(os.environ.get("BOLTZ_TASK_ID") or "").strip()
    task_container_name = make_task_scoped_container_name(runtime_task_id)
    runtime_overridden = any(token == "--runtime" for token in extra_args)

    docker_command = ["docker", "run", "--rm"]
    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=boltz2"])

    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])

    if shm_size and not docker_args_has_flag(extra_args, "--shm-size") and not docker_args_has_flag(extra_args, "--ipc"):
        docker_command.extend(["--shm-size", shm_size])

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--volume",
            f"{temp_dir}:{temp_dir}",
            "--volume",
            f"{results_root}:{results_root}",
            "--volume",
            f"{PROJECT_ROOT}:/workspace/vbio:ro",
            "--workdir",
            "/workspace/vbio",
            "--env",
            "PYTHONPATH=/workspace/vbio",
        ]
    )

    # Pass through download/proxy related env vars into the Boltz runtime container.
    passthrough_env_keys = [
        "BOLTZ_DOWNLOAD_RETRIES",
        "BOLTZ_CCD_URL",
        "BOLTZ1_MODEL_URL",
        "BOLTZ2_MOLS_URL",
        "BOLTZ2_MODEL_URL",
        "BOLTZ2_AFFINITY_MODEL_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ]
    for env_key in passthrough_env_keys:
        env_val = str(os.environ.get(env_key, "") or "").strip()
        if env_val:
            docker_command.extend(["--env", f"{env_key}={env_val}"])

    if runtime_task_id:
        docker_command.extend(["--env", f"BOLTZ_TASK_ID={runtime_task_id}"])

    custom_molecules = _merge_referenced_preset_modification_molecules(
        _normalize_custom_ccd_molecules(custom_ccd_molecules or []),
        normalized_yaml,
    )
    _validate_amidated_terminal_constraints(normalized_yaml, custom_molecules)
    host_cache_dir = str(BOLTZ2_HOST_CACHE_DIR or "").strip()
    container_cache_dir = str(BOLTZ2_CONTAINER_CACHE_DIR or "/root/.boltz").strip() or "/root/.boltz"
    container_base_mols_dir = "/root/.boltz_base_mols"
    custom_mols_dir: Optional[Path] = None
    if custom_molecules and not host_cache_dir:
        raise RuntimeError("BOLTZ2_HOST_CACHE_DIR is required when using custom drawn residue CCD molecules.")
    if custom_molecules:
        custom_mols_dir = _prepare_task_boltz_custom_mols_dir(
            temp_dir,
            custom_molecules,
            Path(host_cache_dir) / "mols",
            container_base_mols_dir,
        )

    if host_cache_dir:
        os.makedirs(host_cache_dir, exist_ok=True)
        docker_command.extend(["--volume", f"{host_cache_dir}:{container_cache_dir}"])
        docker_command.extend(["--env", f"BOLTZ_CACHE={container_cache_dir}"])
        if custom_mols_dir:
            docker_command.extend(["--volume", f"{Path(host_cache_dir) / 'mols'}:{container_base_mols_dir}:ro"])
            docker_command.extend(["--volume", f"{custom_mols_dir}:{container_cache_dir}/mols:ro"])

    host_uid = os.getuid()
    host_gid = os.getgid()
    docker_command.extend(["--user", f"{host_uid}:{host_gid}"])
    for gid in collect_gpu_device_group_ids():
        docker_command.extend(["--group-add", str(gid)])

    docker_command.extend(extra_args)
    docker_command.append(image)
    docker_command.extend(
        [
            "python",
            "/workspace/vbio/backend/runtime/boltz_wrapper.py",
            "predict",
            *cmd_args,
        ]
    )

    if task_container_name:
        try:
            subprocess.run(
                ["docker", "rm", "-f", task_container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    display_command = " ".join(shlex.quote(part) for part in docker_command)
    print(f"运行 Boltz2 Docker: {display_command}", file=sys.stderr)

    boltz_log_path = str(results_root / "boltz2_docker.log")
    with open(boltz_log_path, "w", encoding="utf-8") as log_file:
        docker_proc = subprocess.Popen(
            docker_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_tail: List[str] = []
        if docker_proc.stdout:
            for line in docker_proc.stdout:
                log_file.write(line)
                log_file.flush()
                print(line, end="", file=sys.stderr)
                output_tail.append(line)
                if len(output_tail) > 200:
                    output_tail.pop(0)

        return_code = docker_proc.wait()

    if return_code != 0:
        tail_text = "".join(output_tail[-200:])
        raise RuntimeError(
            f"Boltz2 Docker run failed with exit code {return_code}. "
            f"Last output:\n{tail_text}\n"
            f"Full log: {boltz_log_path}"
        )
    print(f"Boltz2 Docker 运行完成，日志已保存: {boltz_log_path}", file=sys.stderr)

    cache_msa_files_from_temp_dir(str(work_root), normalized_yaml)
    assert_boltz_preprocessing_succeeded(str(results_root), normalized_yaml)

    output_directory_path = find_results_dir(str(results_root))
    if not os.listdir(output_directory_path):
        raise NotADirectoryError(
            f"Prediction result directory was found but is empty: {output_directory_path}"
        )

    extra_archive_files: List[Tuple[Path, str]] = []
    try:
        parsed_yaml = yaml.safe_load(normalized_yaml)
        boltz_yaml_data = parsed_yaml if isinstance(parsed_yaml, dict) else {}
    except Exception as yaml_err:
        print(f"[WARN] Boltz IPSAE 后处理解析 YAML 失败，将跳过 IPSAE: {yaml_err}", file=sys.stderr)
        boltz_yaml_data = {}
    try:
        extra_archive_files.extend(
            _run_boltz_ipsae_postprocess(
                postprocess_base=results_root / "ipsae",
                results_dir=Path(output_directory_path),
                yaml_data=boltz_yaml_data,
                explicit_ligand_chain=ipsae_ligand_chain_id,
            )
        )
    except Exception as err:
        print(f"[WARN] 运行 Boltz IPSAE 后处理失败: {err}", file=sys.stderr)

    _append_custom_residues_ccd_from_molecules(extra_archive_files, custom_molecules, temp_dir, "boltz")

    create_archive_with_a3m(
        output_archive_path,
        output_directory_path,
        normalized_yaml,
        extra_files=extra_archive_files,
    )


def run_alphafold3_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    use_msa_server: bool,
    seed: Optional[int] = None,
    template_payloads: Optional[List[dict]] = None,
    task_id: Optional[str] = None,
    custom_ccd_molecules: Optional[List[Dict[str, Any]]] = None,
    low_vram: bool = False,
) -> None:
    print("Using AlphaFold3 backend (AF3 input preparation)", file=sys.stderr)
    if low_vram:
        raise ValueError(
            "AlphaFold3 不支持低显存模式。如需低显存，请改用 Protenix 或 Boltz2 后端。"
        )
    prep = parse_yaml_for_af3(yaml_content)
    required_chain_ids = [
        chain_id
        for chain_id, mode in prep.chain_id_to_msa_mode.items()
        if mode is not ProteinMsaMode.DISABLED
    ]
    external_chain_ids = [
        chain_id
        for chain_id, mode in prep.chain_id_to_msa_mode.items()
        if mode is ProteinMsaMode.EXTERNAL
    ]
    use_msa_server = bool(external_chain_ids)

    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as err:
        print(f"[WARN] 无法解析 YAML，亲和力后处理将被跳过: {err}", file=sys.stderr)
        yaml_data = {}

    af3_results_root = _resolve_backend_results_root("alphafold3", task_id, temp_dir)
    af3_work_root = _resolve_backend_work_root(af3_results_root)
    custom_molecules = _merge_referenced_preset_modification_molecules(
        _normalize_custom_ccd_molecules(custom_ccd_molecules or []),
        yaml_content,
    )
    _validate_amidated_terminal_constraints(yaml_content, custom_molecules)
    user_ccd_text = None
    if custom_molecules:
        user_ccd_text, _ = _build_custom_ccd_bundle(custom_molecules)
    linker_codes = _detect_bicyclic_linker_codes(yaml_content)
    if linker_codes:
        linker_mmcif = _linker_ccd_mmcif_bundle(linker_codes)
        user_ccd_text = f"{user_ccd_text}\n{linker_mmcif}" if user_ccd_text else linker_mmcif

    if use_msa_server:
        msa_server_url = _assert_msa_server_configured("alphafold3")
        print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
        _require_complete_external_msa(yaml_content, str(af3_work_root), "AlphaFold3")
        print("MSA 生成成功，将用于 AF3 输入", file=sys.stderr)
        if MSA_CACHE_CONFIG['enable_cache']:
            cache_msa_files_from_temp_dir(str(af3_work_root), yaml_content)
    else:
        print("AlphaFold3 输入不需要外部 MSA 生成。", file=sys.stderr)

    cache_dir = MSA_CACHE_CONFIG['cache_dir'] if MSA_CACHE_CONFIG['enable_cache'] else None
    chain_msa_paths = collect_chain_msa_paths(prep, str(af3_work_root), cache_dir)
    missing_chain_ids = [chain_id for chain_id in required_chain_ids if chain_id not in chain_msa_paths]
    if missing_chain_ids:
        raise RuntimeError(
            f"AlphaFold3 MSA assignment incomplete; missing chains: {', '.join(sorted(set(missing_chain_ids)))}"
        )
    unpaired_msa = load_unpaired_msa(prep, chain_msa_paths)

    fasta_content = build_af3_fasta(prep)
    model_seeds = build_af3_model_seeds(seed)
    af3_json = build_af3_json(
        prep,
        unpaired_msa,
        use_external_msa=True,
        model_seeds=model_seeds,
        user_ccd=user_ccd_text,
    )

    af3_ccd_one_letter_overrides = _extract_user_ccd_one_letter_overrides(user_ccd_text)

    if template_payloads:
        for tpl in template_payloads:
            mmcif_text = tpl.get("mmcif")
            if mmcif_text:
                tpl["mmcif"] = _sanitize_release_date_text_with_gemmi(
                    mmcif_text,
                    None,
                    include_loops=True,
                )
                tpl["mmcif"] = _force_af3_release_date_text(tpl["mmcif"], None)
        for entry in af3_json.get("sequences", []):
            protein = entry.get("protein")
            if not isinstance(protein, dict):
                continue
            ids = protein.get("id", [])
            if isinstance(ids, str):
                ids = [ids]
            for tpl in template_payloads:
                target_ids = tpl.get("target_chain_ids") or []
                if not target_ids:
                    continue
                if not set(ids).intersection(target_ids):
                    continue
                if not tpl.get("queryIndices") or not tpl.get("templateIndices"):
                    continue
                protein.setdefault("templates", []).append({
                    "mmcif": tpl["mmcif"],
                    "queryIndices": tpl["queryIndices"],
                    "templateIndices": tpl["templateIndices"],
                })
                # AF3 requires MSA fields to be set when templates are provided
                # Set them to empty strings if they don't exist
                if "unpairedMsa" not in protein:
                    protein["unpairedMsa"] = ""
                if "pairedMsa" not in protein:
                    protein["pairedMsa"] = ""

    af3_input_dir = str(af3_work_root / "input")
    af3_output_dir = str(af3_results_root / "output")
    os.makedirs(af3_input_dir, exist_ok=True)
    os.makedirs(af3_output_dir, exist_ok=True)

    fasta_path = os.path.join(af3_input_dir, f"{prep.jobname}_input.fasta")
    json_path = os.path.join(af3_input_dir, "fold_input.json")
    sitecustomize_path = os.path.join(af3_input_dir, "sitecustomize.py")

    with open(fasta_path, "w") as fasta_file:
        fasta_file.write(fasta_content)
    with open(json_path, "w") as json_file:
        json.dump(af3_json, json_file, indent=2, ensure_ascii=False)

    # Patch alphafold3 inside the container to avoid StopIteration when hmmsearch returns
    # an empty Stockholm (no template hits). sitecustomize is auto-imported when present on sys.path.
    # Use a raw string to preserve backslashes in the embedded Python source.
    sitecustomize_code = r"""
import logging
try:
    from alphafold3.data import parsers as _af3_parsers
except Exception:
    _af3_parsers = None

def _count_non_lowercase(seq: str) -> int:
    return sum(1 for ch in seq if not ch.islower())

def _normalize_a3m(a3m_text: str):
    # Pad sequences so non-lowercase lengths match, avoiding featurizer shape errors.
    header = None
    seq_chunks = []
    entries = []
    changed = False
    for line in (a3m_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if header is not None:
                entries.append((header, "".join(seq_chunks)))
            header = line
            seq_chunks = []
        else:
            seq_chunks.append(line)
    if header is not None:
        entries.append((header, "".join(seq_chunks)))
    if not entries:
        return a3m_text, changed
    target = max(_count_non_lowercase(seq) for _, seq in entries)
    fixed = []
    for hdr, seq in entries:
        count = _count_non_lowercase(seq)
        if count < target:
            seq = seq + ("-" * (target - count))
            changed = True
        fixed.append(f"{hdr}\n{seq}")
    return "\n".join(fixed) + "\n", changed

# Align AlphaFold3 runtime CCD-to-one-letter mapping with userCCD parent codes.
# AF3 uses this table to derive the effective protein query sequence for PTMs
# before validating the first MSA row.
try:
    import os as _os
    import json as _json
    from alphafold3.constants import residue_names as _af3_residue_names
    _ccd_overrides = _json.loads(_os.environ.get("VBIO_AF3_CCD_ONE_LETTER_OVERRIDES", "{}"))
    if isinstance(_ccd_overrides, dict):
        for _ccd, _one in _ccd_overrides.items():
            _ccd = str(_ccd or "").strip().upper()
            _one = str(_one or "").strip().upper()[:1]
            if _ccd and _one in "ARNDCQEGHILKMFPSTWYV":
                _af3_residue_names.CCD_NAME_TO_ONE_LETTER[_ccd] = _one
except Exception as _exc:
    logging.warning("Failed to apply userCCD residue one-letter overrides: %s", _exc)

if _af3_parsers is not None:
    _orig_convert = getattr(_af3_parsers, "convert_stockholm_to_a3m", None)
    _orig_lazy = getattr(_af3_parsers, "lazy_parse_fasta_string", None)
    _orig_parse_a3m = getattr(_af3_parsers, "parse_a3m", None)

    if callable(_orig_convert):
        def _safe_convert_stockholm_to_a3m(stockholm_format, max_sequences=None, remove_first_row_gaps=True, linewidth=None):
            try:
                result = _orig_convert(stockholm_format, max_sequences=max_sequences, remove_first_row_gaps=remove_first_row_gaps, linewidth=linewidth)
            except StopIteration:
                logging.warning("alphafold3.parsers.convert_stockholm_to_a3m: no sequences found; returning empty A3M.")
                return ""
            fixed, changed = _normalize_a3m(result)
            if changed:
                logging.warning("alphafold3.parsers.convert_stockholm_to_a3m: normalized ragged A3M by right-padding gaps.")
            return fixed

        _af3_parsers.convert_stockholm_to_a3m = _safe_convert_stockholm_to_a3m

    if callable(_orig_parse_a3m):
        def _safe_parse_a3m(a3m_string: str):
            fixed, changed = _normalize_a3m(a3m_string)
            if changed:
                logging.warning("alphafold3.parsers.parse_a3m: normalized ragged A3M by right-padding gaps.")
            return _orig_parse_a3m(fixed)

        _af3_parsers.parse_a3m = _safe_parse_a3m

    if callable(_orig_lazy):
        def _safe_lazy_parse_fasta_string(fasta_string: str):
            if not fasta_string or not str(fasta_string).strip():
                logging.warning("alphafold3.parsers.lazy_parse_fasta_string: empty FASTA input; returning no sequences.")
                return iter(())
            try:
                return _orig_lazy(fasta_string)
            except Exception as exc:  # noqa: BLE001
                logging.warning(f"alphafold3.parsers.lazy_parse_fasta_string: failed to parse FASTA ({exc}); returning no sequences.")
                return iter(())

        _af3_parsers.lazy_parse_fasta_string = _safe_lazy_parse_fasta_string

# Ensure AF3 mmCIF strings always include a valid release date field.
try:
    from alphafold3 import structure as _af3_structure
    _orig_from_mmcif = getattr(_af3_structure, "from_mmcif", None)

    if callable(_orig_from_mmcif):
        def _ensure_release_date_in_mmcif_text(text: str) -> str:
            if "_pdbx_audit_revision_history.revision_date" in text:
                return text
            lines = text.splitlines()
            insert_at = 1 if lines and lines[0].lower().startswith("data_") else 0
            injection = [
                "_pdbx_database_status.recvd_initial_deposition_date 1970-01-01",
                "_pdbx_database_status.date_of_initial_deposition 1970-01-01",
                "_pdbx_database_status.date_of_release 1970-01-01",
                "loop_",
                "_pdbx_audit_revision_history.revision_ordinal",
                "_pdbx_audit_revision_history.data_content_type",
                "_pdbx_audit_revision_history.major_revision",
                "_pdbx_audit_revision_history.minor_revision",
                "_pdbx_audit_revision_history.revision_date",
                "1 'Structure model' 1 0 1970-01-01",
            ]
            merged = lines[:insert_at] + injection + lines[insert_at:]
            return "\n".join(merged) + ("\n" if merged else "")

        def _looks_like_mmcif_text(value) -> bool:
            if not isinstance(value, str):
                return False
            sample = value[:2048]
            return (
                sample.lstrip().startswith("data_")
                or "_atom_site." in sample
                or "_entry.id" in sample
                or "_pdbx_database_status." in sample
            )

        def _safe_from_mmcif(*args, **kwargs):
            safe_args = list(args)
            safe_kwargs = dict(kwargs)
            try:
                for idx, arg in enumerate(safe_args):
                    if _looks_like_mmcif_text(arg):
                        safe_args[idx] = _ensure_release_date_in_mmcif_text(arg)
                for key, value in list(safe_kwargs.items()):
                    if _looks_like_mmcif_text(value):
                        safe_kwargs[key] = _ensure_release_date_in_mmcif_text(value)
            except Exception:
                pass
            return _orig_from_mmcif(*safe_args, **safe_kwargs)

        _af3_structure.from_mmcif = _safe_from_mmcif
except Exception:
    pass
"""
    with open(sitecustomize_path, "w", encoding="utf-8") as sc_file:
        sc_file.write(sitecustomize_code)

    model_dir = ALPHAFOLD3_MODEL_DIR
    database_dir = ALPHAFOLD3_DATABASE_DIR
    image = ALPHAFOLD3_DOCKER_IMAGE or "jurgjn/alphafold3:v3.0.2"
    raw_extra_args = shlex.split(ALPHAFOLD3_DOCKER_EXTRA_ARGS) if ALPHAFOLD3_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"[WARN] 已忽略部分 ALPHAFOLD3_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
            file=sys.stderr,
        )

    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("ALPHAFOLD3_MODEL_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")
    if not database_dir or not os.path.isdir(database_dir):
        raise FileNotFoundError("ALPHAFOLD3_DATABASE_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")
    validate_af3_database_files(database_dir)

    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        gpu_arg = determine_docker_gpu_arg(visible_devices)
    except RuntimeError as gpu_err:
        print(f"[ERROR] 无法准备 AlphaFold3 GPU 环境: {gpu_err}", file=sys.stderr)
        print("   ↳ 请确认此主机安装了 NVIDIA 驱动并正确设置 CUDA_VISIBLE_DEVICES。", file=sys.stderr)
        raise

    container_input_dir = "/workspace/af_input"
    container_output_dir = "/workspace/af_output"
    container_model_dir = "/workspace/models"
    container_database_dir = "/workspace/public_databases"
    container_cache_dir = "/workspace/af_cache"
    container_colabfold_jobs_dir = "/app/jobs"
    runtime_task_id = str(task_id or os.environ.get("BOLTZ_TASK_ID") or "").strip()
    task_container_name = make_task_scoped_container_name(runtime_task_id)

    runtime_overridden = any(token == "--runtime" for token in extra_args)

    docker_command = [
        "docker",
        "run",
        "--rm",
    ]

    if task_container_name:
        # Stable naming/labeling makes termination deterministic from the API server.
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=alphafold3"])

    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--env",
            "PYTHONPATH=/workspace/af_input",
            "--volume",
            f"{af3_input_dir}:{container_input_dir}",
            "--volume",
            f"{af3_output_dir}:{container_output_dir}",
            "--volume",
            f"{model_dir}:{container_model_dir}",
            "--volume",
            f"{database_dir}:{container_database_dir}",
        ]
    )
    if af3_ccd_one_letter_overrides:
        docker_command.extend([
            "--env",
            "VBIO_AF3_CCD_ONE_LETTER_OVERRIDES=" + json.dumps(af3_ccd_one_letter_overrides, sort_keys=True),
        ])

    # Enable persistent JAX compilation cache to avoid repeated long compiles.
    jax_cache_host_dir = os.environ.get("ALPHAFOLD3_JAX_CACHE_DIR")
    if not jax_cache_host_dir:
        jax_cache_host_dir = os.path.join(os.getcwd(), ".af3_jax_cache")
    try:
        os.makedirs(jax_cache_host_dir, exist_ok=True)
        docker_command.extend([
            "--env",
            f"JAX_COMPILATION_CACHE_DIR={container_cache_dir}",
            "--volume",
            f"{jax_cache_host_dir}:{container_cache_dir}",
        ])
    except Exception as exc:
        print(f"[WARN] 无法创建 JAX 编译缓存目录 {jax_cache_host_dir}: {exc}", file=sys.stderr)

    # 添加 ColabFold jobs 目录挂载（如果配置了 MSA 服务器）
    if use_msa_server and MSA_SERVER_URL and COLABFOLD_JOBS_DIR and os.path.exists(COLABFOLD_JOBS_DIR):
        docker_command.extend([
            "--volume",
            f"{COLABFOLD_JOBS_DIR}:{container_colabfold_jobs_dir}",
        ])
        print(f"挂载 ColabFold jobs 目录: {COLABFOLD_JOBS_DIR} -> {container_colabfold_jobs_dir}", file=sys.stderr)
    elif use_msa_server:
        print("[WARN] 未找到 ColabFold jobs 目录或未配置 MSA 服务器", file=sys.stderr)
    else:
        print("未启用外部 MSA，跳过 ColabFold jobs 目录挂载", file=sys.stderr)

    host_uid = os.getuid()
    host_gid = os.getgid()
    docker_command += [
        "--user",
        f"{host_uid}:{host_gid}",
    ]

    gpu_device_groups = collect_gpu_device_group_ids()
    if not gpu_device_groups:
        print("[WARN] 未能检测到 GPU 设备的所属用户组，容器可能无法访问 GPU。", file=sys.stderr)
    else:
        for gid in gpu_device_groups:
            docker_command.extend(["--group-add", str(gid)])
        print(
            f"为容器添加 GPU 相关用户组: {', '.join(str(g) for g in gpu_device_groups)}",
            file=sys.stderr,
        )

    docker_command.extend(extra_args)

    docker_command.append(image)
    docker_command.extend(
        [
            "python",
            "run_alphafold.py",
            f"--json_path={container_input_dir}/fold_input.json",
            f"--model_dir={container_model_dir}",
            f"--output_dir={container_output_dir}",
            f"--db_dir={container_database_dir}",
        ]
    )

    display_command = " ".join(shlex.quote(part) for part in docker_command)
    if task_container_name:
        try:
            subprocess.run(
                ["docker", "rm", "-f", task_container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
        except Exception:
            pass
    print(f"运行 AlphaFold3 Docker: {display_command}", file=sys.stderr)
    af3_log_path = str(af3_results_root / "af3_docker.log")
    with open(af3_log_path, "w", encoding="utf-8") as log_file:
        docker_proc = subprocess.Popen(
            docker_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_tail: List[str] = []
        if docker_proc.stdout:
            for line in docker_proc.stdout:
                log_file.write(line)
                log_file.flush()
                print(line, end="", file=sys.stderr)
                output_tail.append(line)
                if len(output_tail) > 200:
                    output_tail.pop(0)

        return_code = docker_proc.wait()

    if return_code != 0:
        tail_text = "".join(output_tail[-200:])
        print(f"[ERROR] AlphaFold3 Docker 运行失败: {tail_text}", file=sys.stderr)
        raise RuntimeError(
            f"AlphaFold3 Docker run failed with exit code {return_code}. "
            f"Last output:\n{tail_text}\n"
            f"Full log: {af3_log_path}"
        )

    print(f"AlphaFold3 Docker 运行完成，日志已保存: {af3_log_path}", file=sys.stderr)

    af3_output_contents = list(Path(af3_output_dir).rglob("*"))
    if not any(p.is_file() for p in af3_output_contents):
        print("[WARN] AlphaFold3 输出目录为空，可能推理未产生结果。", file=sys.stderr)

    extra_archive_files: List[Tuple[Path, str]] = []
    try:
        extra_archive_files.extend(
            _run_af3_ipsae_postprocess(
                postprocess_base=af3_results_root / "ipsae",
                yaml_data=yaml_data,
                prep=prep,
                af3_output_dir=Path(af3_output_dir),
            )
        )
    except Exception as err:
        print(f"[WARN] 运行 AlphaFold3 IPSAE 后处理失败: {err}", file=sys.stderr)
    extra_archive_files.extend(
        run_af3_affinity_pipeline(
            temp_dir=temp_dir,
            yaml_data=yaml_data,
            prep=prep,
            af3_output_dir=af3_output_dir,
            results_root=af3_results_root,
        )
    )

    _append_custom_residues_ccd(extra_archive_files, user_ccd_text, temp_dir, "af3")

    create_af3_archive(
        output_archive_path,
        fasta_content,
        af3_json,
        chain_msa_paths,
        yaml_content,
        prep,
        af3_output_dir=af3_output_dir,
        extra_files=extra_archive_files,
    )

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
                    worker_gpu_id = worker_acquire_gpu_fn(
                        task_id=worker_task_id,
                        timeout=PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS,
                    )
                    os.environ["CUDA_VISIBLE_DEVICES"] = str(worker_gpu_id)

                worker_seed = worker_predict_args.pop("seed", None)
                worker_custom_ccd_molecules = worker_predict_args.pop("custom_ccd_molecules", [])
                worker_ipsae_ligand_chain = str(
                    worker_predict_args.pop("ipsaeLigandChainId", None)
                    or worker_predict_args.pop("ipsae_ligand_chain_id", None)
                    or ""
                ).strip() or None
                use_msa_raw = worker_predict_args.get("use_msa_server", True)
                if isinstance(use_msa_raw, bool):
                    worker_use_msa_server = use_msa_raw
                elif isinstance(use_msa_raw, (int, float)):
                    worker_use_msa_server = bool(use_msa_raw)
                else:
                    worker_use_msa_server = str(use_msa_raw).strip().lower() in {"1", "true", "yes", "y"}

                if worker_backend == "alphafold3":
                    run_alphafold3_backend(
                        worker_temp_dir,
                        worker_yaml_content,
                        worker_output_archive_path,
                        worker_use_msa_server,
                        seed=worker_seed,
                        task_id=worker_task_id,
                        custom_ccd_molecules=worker_custom_ccd_molecules if isinstance(worker_custom_ccd_molecules, list) else [],
                        low_vram=worker_low_vram,
                    )
                elif worker_backend == "protenix":
                    run_protenix_backend(
                        temp_dir=worker_temp_dir,
                        yaml_content=worker_yaml_content,
                        output_archive_path=worker_output_archive_path,
                        use_msa_server=worker_use_msa_server,
                        seed=worker_seed,
                        task_id=worker_task_id,
                        custom_ccd_molecules=worker_custom_ccd_molecules if isinstance(worker_custom_ccd_molecules, list) else [],
                        low_vram=worker_low_vram,
                        ipsae_ligand_chain_id=worker_ipsae_ligand_chain,
                    )
                elif worker_backend == "boltz":
                    if worker_seed is not None:
                        worker_predict_args["seed"] = worker_seed
                    run_boltz_backend(
                        worker_temp_dir,
                        worker_yaml_content,
                        worker_output_archive_path,
                        worker_predict_args,
                        worker_model_name,
                        task_id=worker_task_id,
                        custom_ccd_molecules=worker_custom_ccd_molecules if isinstance(worker_custom_ccd_molecules, list) else [],
                        low_vram=worker_low_vram,
                        ipsae_ligand_chain_id=worker_ipsae_ligand_chain,
                    )
                else:
                    raise ValueError(f"Unsupported peptide candidate backend: '{worker_backend}'.")
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
        if backend == "protenix2dock":
            backend = "protenix"
        elif backend == "boltz2dock":
            backend = "boltz"
        if backend not in ("boltz", "alphafold3", "protenix", "nesso"):
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
        if backend in {"boltz", "alphafold3", "protenix"}:
            use_msa_server = infer_use_msa_server_from_yaml_text(yaml_content)
            predict_args["use_msa_server"] = use_msa_server
            if use_msa_server:
                _assert_msa_server_configured(backend)

        runtime_temp_parent = str(Path(output_archive_path).resolve().parent)
        os.makedirs(runtime_temp_parent, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=runtime_temp_parent) as temp_dir:
            processed_yaml = yaml_content
            af3_template_payloads: List[dict] = []
            if template_inputs and (backend in ("boltz", "alphafold3") or workflow == "peptide_design"):
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
                peptide_parent_task_id = str(runtime_task_id or os.environ.get("BOLTZ_TASK_ID") or "").strip()
                try:
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
            elif backend == "alphafold3":
                if not af3_template_payloads:
                    af3_template_payloads = prepare_yaml_template_payloads(
                        processed_yaml,
                        str(_resolve_backend_work_root(_resolve_backend_results_root("alphafold3", runtime_task_id, temp_dir))),
                    )
                run_alphafold3_backend(
                    temp_dir,
                    processed_yaml,
                    output_archive_path,
                    use_msa_server,
                    seed=seed,
                    template_payloads=af3_template_payloads,
                    task_id=runtime_task_id,
                    custom_ccd_molecules=custom_ccd_molecules if isinstance(custom_ccd_molecules, list) else [],
                    low_vram=low_vram,
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
            elif backend == "protenix":
                if template_inputs:
                    print("Protenix backend 当前未启用模板输入，已忽略 template_files。", file=sys.stderr)
                run_protenix_backend(
                    temp_dir=temp_dir,
                    yaml_content=processed_yaml,
                    output_archive_path=output_archive_path,
                    use_msa_server=use_msa_server,
                    seed=seed,
                    task_id=runtime_task_id,
                    custom_ccd_molecules=custom_ccd_molecules if isinstance(custom_ccd_molecules, list) else [],
                    low_vram=low_vram,
                )
            else:
                if seed is not None:
                    predict_args["seed"] = seed
                validate_template_paths(processed_yaml)
                run_boltz_backend(
                    temp_dir,
                    processed_yaml,
                    output_archive_path,
                    predict_args,
                    model_name,
                    task_id=runtime_task_id,
                    strict_ligand_confidence_contract=strict_ligand_confidence_contract,
                    custom_ccd_molecules=custom_ccd_molecules if isinstance(custom_ccd_molecules, list) else [],
                    low_vram=low_vram,
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
