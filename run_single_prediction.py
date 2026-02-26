# run_single_prediction.py
import sys
import os
import json
import tempfile
import shutil
import traceback
import yaml
import hashlib
import glob
import csv
import zipfile
import shlex
import requests
import time
import tarfile
import io
import itertools
import re
import base64
import random
import copy
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Iterable
import subprocess

sys.path.append(os.getcwd())
from boltz_wrapper import predict
from config import (
    MSA_SERVER_URL,
    MSA_SERVER_MODE,
    COLABFOLD_JOBS_DIR,
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
)
from af3_adapter import (
    AF3Preparation,
    build_af3_fasta,
    build_af3_json,
    collect_chain_msa_paths,
    load_unpaired_msa,
    parse_yaml_for_af3,
    safe_filename,
    serialize_af3_json,
)
from protenix_adapter import (
    ProtenixPreparation,
    apply_protein_msa_paths,
    parse_yaml_for_protenix,
    serialize_protenix_json,
)
from Bio import Align
from Bio.PDB import PDBParser, MMCIFParser, Select
from Bio.PDB.Polypeptide import is_aa
import gemmi

# MSA 缓存配置
MSA_CACHE_CONFIG = {
    'cache_dir': '/tmp/boltz_msa_cache',
    'enable_cache': True
}

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
    parser = PDBParser(QUIET=True) if fmt == "pdb" else MMCIFParser(QUIET=True)
    structure = parser.get_structure("template", io.StringIO(content))
    sequences: Dict[str, str] = {}
    first_model = next(iter(structure), None)
    if first_model is None:
        return sequences

    for chain in first_model:
        seq_chars: List[str] = []
        for residue in chain:
            if not is_aa(residue, standard=False):
                continue
            resname = residue.get_resname()
            aa = AMINO_ACID_MAPPING.get(resname.upper(), "X")
            seq_chars.append(aa)
        if seq_chars:
            sequences[chain.id] = "".join(seq_chars)
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


class _ChainSelect(Select):
    def __init__(self, chain_id: str):
        self.chain_id = chain_id

    def accept_model(self, model):
        return model.id == 0

    def accept_chain(self, chain):
        return chain.id == self.chain_id


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
    # Drop any pre-existing sequence tables that may not match the selected chain
    structure.clear_sequences()
    structure.setup_entities()

    chain = model[selected_chain]
    removed_count, renamed_count = _sanitize_template_chain_residues(chain)
    if removed_count or renamed_count:
        print(
            f"⚠️ 模板链 {selected_chain} 已清理残基：移除 {removed_count} 个，标准化 {renamed_count} 个。",
            file=sys.stderr,
        )
    if len(chain) == 0:
        raise ValueError(
            f"Template chain '{selected_chain}' has no supported amino-acid residues after cleanup."
        )
    residue_names = [gemmi.Entity.first_mon(res.name) for res in chain]
    subchains = {res.subchain for res in chain}
    for entity in structure.entities:
        if any(sc in entity.subchains for sc in subchains):
            if not entity.full_sequence or len(entity.full_sequence) < len(residue_names):
                entity.full_sequence = residue_names

    # Ensure label_seq_id and related tables are consistent with the sequence
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
            # We need to reconstruct the atom_site loop with the required field
            # This is complex, so let's use a different approach:
            # Parse the text and add the missing field
            lines = cif_text.splitlines()
            result_lines = []
            in_atom_site_loop = False
            atom_site_tags_found = False
            model_num_idx = -1

            for i, line in enumerate(lines):
                stripped = line.strip()

                if stripped.startswith("loop_"):
                    # Check if next lines contain _atom_site tags
                    j = i + 1
                    atom_site_tags = []
                    while j < len(lines) and lines[j].strip().startswith("_"):
                        atom_site_tags.append(lines[j].strip())
                        j += 1

                    if atom_site_tags and any(t.startswith("_atom_site.") for t in atom_site_tags):
                        in_atom_site_loop = True
                        atom_site_tags_found = True

                        # Find where to insert pdbx_PDB_model_num
                        # It should be after group_PDB and before id
                        insert_idx = -1
                        for idx, tag in enumerate(atom_site_tags):
                            if tag == "_atom_site.group_PDB":
                                # Insert after group_PDB
                                insert_idx = idx + 1
                                break
                            elif tag == "_atom_site.id" and insert_idx == -1:
                                # Insert before id if group_PDB not found
                                insert_idx = idx
                                break

                        # Write loop_ and modified tags
                        result_lines.append(line)
                        for k, tag in enumerate(atom_site_tags):
                            if k == insert_idx:
                                result_lines.append("_atom_site.pdbx_PDB_model_num")
                                model_num_idx = insert_idx
                            result_lines.append(lines[i + 1 + k])

                        # If we didn't find a place to insert, add at the end
                        if insert_idx == -1:
                            result_lines.append("_atom_site.pdbx_model_num")
                            model_num_idx = len(atom_site_tags)

                        continue

                if in_atom_site_loop and atom_site_tags_found:
                    # Check if we've reached the data rows
                    if not stripped.startswith("_") and stripped and not stripped.startswith("loop_") and not stripped.startswith("data_"):
                        # This is a data row - add model_num value
                        parts = stripped.split()
                        if model_num_idx >= 0 and model_num_idx < len(parts) + 1:
                            # Insert "1" at the model_num position
                            parts.insert(model_num_idx, "1")
                            result_lines.append(" ".join(parts))
                        else:
                            result_lines.append(line)
                        continue
                    elif stripped.startswith("_") or stripped.startswith("loop_") or stripped.startswith("data_"):
                        # End of atom_site data
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
            print("⚠️ 模板内容为空，跳过。", file=sys.stderr)
            continue
        try:
            raw_bytes = base64.b64decode(content_b64)
        except Exception:
            print("⚠️ 模板内容解码失败，跳过。", file=sys.stderr)
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
            print("⚠️ 模板未解析出蛋白质链，跳过。", file=sys.stderr)
            continue
        if template_chain_id not in chain_sequences:
            template_chain_id = first_chain or next(iter(chain_sequences.keys()))
        template_seq = chain_sequences.get(template_chain_id, "")

        templates_dir.mkdir(parents=True, exist_ok=True)
        raw_path = templates_dir / file_name
        try:
            raw_path.write_bytes(raw_bytes)
        except Exception as exc:
            print(f"⚠️ 保存模板文件失败 {raw_path}: {exc}", file=sys.stderr)
            continue

        if fmt == "pdb":
            filtered_path = templates_dir / f"{Path(file_name).stem}_chain{template_chain_id}.pdb"
            try:
                _write_filtered_pdb_by_chain(text, str(template_chain_id or ""), filtered_path)
                raw_path = filtered_path
            except Exception as exc:
                print(f"⚠️ 过滤 PDB 模板失败 {raw_path}: {exc}", file=sys.stderr)

        cif_stem = Path(file_name).stem or f"template_{idx}"
        cif_path = templates_dir / f"{cif_stem}.cif"
        try:
            cif_path, cif_text, resolved_chain_id, cif_template_seq = convert_structure_to_single_chain_mmcif(
                raw_path, str(template_chain_id or ""), cif_path
            )
        except Exception as exc:
            print(f"⚠️ 模板转换失败，已跳过 {file_name}: {exc}", file=sys.stderr)
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
            print(f"⚠️ 模板 CIF 文件不存在，跳过: {cif_path}", file=sys.stderr)
            continue
        suffix = cif_path.suffix.lower()
        fmt = "cif" if suffix in (".cif", ".mmcif") else "pdb"
        try:
            text = cif_path.read_text()
        except Exception as exc:
            print(f"⚠️ 读取模板文件失败 {cif_path}: {exc}", file=sys.stderr)
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
                print(f"⚠️ 转换模板失败，改用原始 mmCIF: {cif_path} ({exc})", file=sys.stderr)
                cif_text = text
            else:
                print(f"⚠️ 转换模板为单链 mmCIF 失败 {cif_path}: {exc}", file=sys.stderr)
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
            "AlphaFold3 backend 需要 NVIDIA GPU，但当前环境未检测到可用的 CUDA 设备。"
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
                print(f"⚠️ 忽略无效的 Docker 参数: {token} (缺少值)", file=sys.stderr)
                i += 1
                continue

            value = raw_args[i + 1]
            if "=" not in value:
                print(f"⚠️ 忽略无效的 Docker 参数: {token} {value} (缺少 KEY=VALUE 形式)", file=sys.stderr)
                i += 2
                continue

            sanitized.extend([token, value])
            i += 2
            continue

        sanitized.append(token)
        i += 1

    return sanitized


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
        print(f"⚠️ 检测到并移除非法字符\\x00{msg_context}", file=sys.stderr)
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
        print(f"⚠️ 无法读取 A3M 文件进行清理: {path}, {e}", file=sys.stderr)
        return

    sanitized = sanitize_a3m_content(content, context=context or path)
    if sanitized != content:
        try:
            with open(path, "w") as f:
                f.write(sanitized)
        except OSError as e:
            print(f"⚠️ 无法写入清理后的 A3M 文件: {path}, {e}", file=sys.stderr)


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
        print(f"⚠️ 无法读取 CIF 文件 {cif_path}: {err}", file=sys.stderr)
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
        print(f"⚠️ 无法使用 gemmi 解析 {cif_path}: {err}", file=sys.stderr)
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
            "⚠️ 未安装 gemmi，无法清理结构原子名，直接使用原始结构。",
            file=sys.stderr,
        )
        return source_path

    try:
        structure = gemmi.read_structure(str(source_path))
    except Exception as err:
        print(f"⚠️ 无法读取结构 {source_path} 进行清理: {err}", file=sys.stderr)
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
        print(f"⚠️ 写入清理后的结构失败，回退到原始结构: {err}", file=sys.stderr)
        return source_path

    print(
        f"🧼 已生成用于亲和力预测的清理结构: {sanitized_path}",
        file=sys.stderr,
    )
    return sanitized_path


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
        f"🔍 从归档文件提取 AlphaFold3 结构: {selected_archive} -> {dest_path}",
        file=sys.stderr,
    )
    return dest_path


def run_af3_affinity_pipeline(
    temp_dir: str,
    yaml_data: Dict[str, Any],
    prep: AF3Preparation,
    af3_output_dir: str,
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
        print("ℹ️ 亲和力配置未提供有效的 binder，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = str(binder_chain).strip()
    if not binder_chain:
        print("ℹ️ 亲和力配置 binder 为空，跳过亲和力预测。", file=sys.stderr)
        return []

    ligand_entries = [
        entry for entry in yaml_data.get("sequences", [])
        if isinstance(entry, dict) and "ligand" in entry
    ]
    if not ligand_entries:
        print("ℹ️ 未检测到配体条目，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = prep.chain_id_label_map.get(binder_chain, safe_filename(binder_chain))

    af3_output_path = Path(af3_output_dir)
    model_path = locate_af3_structure_file(af3_output_path, prep.jobname)

    if not model_path or not model_path.exists():
        extracted_path = extract_af3_structure_from_archives(
            af3_output_path,
            Path(temp_dir) / "af3_extracted_structures",
            prep.jobname,
        )
        model_path = extracted_path

    if not model_path or not model_path.exists():
        print(
            "⚠️ 未找到 AlphaFold3 预测的结构文件，无法进行亲和力预测。",
            file=sys.stderr,
        )
        return []

    print(
        f"🔍 使用 AlphaFold3 结构进行亲和力评估: {model_path}",
        file=sys.stderr,
    )

    ligand_resname = find_ligand_resname_in_cif(model_path, binder_chain)
    if not ligand_resname:
        print(
            f"⚠️ 未能在结构中找到链 {binder_chain} 的配体残基，跳过亲和力预测。",
            file=sys.stderr,
        )
        return []

    try:
        from affinity.main import Boltzina
    except ImportError as err:
        print(f"⚠️ 无法导入 Boltz-2 亲和力模块：{err}，跳过亲和力预测。", file=sys.stderr)
        return []

    affinity_base = Path(temp_dir) / "af3_affinity"
    output_dir = affinity_base / "boltzina_output"
    work_dir = affinity_base / "boltzina_work"
    sanitized_struct_dir = affinity_base / "sanitized_structures"

    model_for_affinity = prepare_structure_for_affinity(model_path, sanitized_struct_dir)

    affinity_entries: List[Tuple[Path, str]] = []
    try:
        print(
            f"⚙️ 开始运行 Boltz-2 亲和力评估，配体链: {binder_chain}, 残基名: {ligand_resname}",
            file=sys.stderr,
        )
        boltzina = Boltzina(
            output_dir=str(output_dir),
            work_dir=str(work_dir),
            ligand_resname=ligand_resname,
        )
        boltzina.predict([str(model_for_affinity)])

        if not boltzina.results:
            print("⚠️ 亲和力预测未产生结果，跳过生成 affinity_data.json。", file=sys.stderr)
            return []

        affinity_result = dict(boltzina.results[0])
        affinity_result["ligand_resname"] = ligand_resname
        affinity_result["binder_chain"] = binder_chain
        affinity_result["source"] = "alphafold3"

        affinity_base.mkdir(parents=True, exist_ok=True)
        affinity_json_path = affinity_base / "affinity_data.json"
        with affinity_json_path.open("w") as json_file:
            json.dump(affinity_result, json_file, indent=2)
        affinity_entries.append((affinity_json_path, "affinity_data.json"))

        affinity_csv_path = output_dir / "affinity_results.csv"
        if affinity_csv_path.exists():
            affinity_entries.append((affinity_csv_path, "af3/affinity_results.csv"))

        print("✅ 亲和力预测完成，结果已写入 affinity_data.json。", file=sys.stderr)
    except Exception as err:
        print(f"⚠️ 运行 Boltz-2 亲和力预测失败: {err}", file=sys.stderr)

    return affinity_entries


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
        print("ℹ️ 亲和力配置未提供有效的 binder，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain_raw = str(binder_chain_raw).strip()
    if not binder_chain_raw:
        print("ℹ️ 亲和力配置 binder 为空，跳过亲和力预测。", file=sys.stderr)
        return []

    ligand_entries = [
        entry for entry in yaml_data.get("sequences", [])
        if isinstance(entry, dict) and "ligand" in entry
    ]
    if not ligand_entries:
        print("ℹ️ 未检测到配体条目，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = (
        prep.chain_alias_map.get(binder_chain_raw)
        or prep.chain_alias_map.get(binder_chain_raw.upper())
        or prep.chain_alias_map.get(binder_chain_raw.lower())
        or binder_chain_raw
    )

    model_path = locate_protenix_structure_file(Path(protenix_output_dir), prep.input_name)
    if not model_path or not model_path.exists():
        print("⚠️ 未找到 Protenix 预测的结构文件，无法进行亲和力预测。", file=sys.stderr)
        return []

    print(f"🔍 使用 Protenix 结构进行亲和力评估: {model_path}", file=sys.stderr)

    ligand_resname = find_ligand_resname_in_cif(model_path, binder_chain)
    if not ligand_resname:
        inferred = _find_ligand_chain_and_resname_in_structure(model_path)
        if inferred:
            inferred_chain, inferred_resname = inferred
            print(
                f"ℹ️ 未在链 {binder_chain} 找到配体，自动回退到链 {inferred_chain} ({inferred_resname})。",
                file=sys.stderr,
            )
            binder_chain = inferred_chain
            ligand_resname = inferred_resname

    if not ligand_resname:
        print(
            f"⚠️ 未能在结构中找到链 {binder_chain} 的配体残基，跳过亲和力预测。",
            file=sys.stderr,
        )
        return []

    try:
        from affinity.main import Boltzina
    except ImportError as err:
        print(f"⚠️ 无法导入 Boltz-2 亲和力模块：{err}，跳过亲和力预测。", file=sys.stderr)
        return []

    affinity_base = Path(temp_dir) / "protenix_affinity"
    output_dir = affinity_base / "boltzina_output"
    work_dir = affinity_base / "boltzina_work"
    sanitized_struct_dir = affinity_base / "sanitized_structures"
    model_for_affinity = prepare_structure_for_affinity(model_path, sanitized_struct_dir)

    affinity_entries: List[Tuple[Path, str]] = []
    try:
        print(
            f"⚙️ 开始运行 Boltz-2 亲和力评估，配体链: {binder_chain}, 残基名: {ligand_resname}",
            file=sys.stderr,
        )
        boltzina = Boltzina(
            output_dir=str(output_dir),
            work_dir=str(work_dir),
            ligand_resname=ligand_resname,
        )
        boltzina.predict([str(model_for_affinity)])

        if not boltzina.results:
            print("⚠️ 亲和力预测未产生结果，跳过生成 affinity_data.json。", file=sys.stderr)
            return []

        affinity_result = dict(boltzina.results[0])
        affinity_result["ligand_resname"] = ligand_resname
        affinity_result["binder_chain"] = binder_chain
        affinity_result["source"] = "protenix"

        affinity_base.mkdir(parents=True, exist_ok=True)
        affinity_json_path = affinity_base / "affinity_data.json"
        with affinity_json_path.open("w") as json_file:
            json.dump(affinity_result, json_file, indent=2)
        affinity_entries.append((affinity_json_path, "affinity_data.json"))

        affinity_csv_path = output_dir / "affinity_results.csv"
        if affinity_csv_path.exists():
            affinity_entries.append((affinity_csv_path, "protenix/affinity_results.csv"))

        print("✅ 亲和力预测完成，结果已写入 affinity_data.json。", file=sys.stderr)
    except Exception as err:
        print(f"⚠️ 运行 Boltz-2 亲和力预测失败: {err}", file=sys.stderr)

    return affinity_entries


def get_sequence_hash(sequence: str) -> str:
    """计算序列的MD5哈希值作为缓存键"""
    return hashlib.md5(sequence.encode('utf-8')).hexdigest()

def request_msa_from_server(sequence: str, timeout: int = 600) -> dict:
    """
    从 ColabFold MSA 服务器请求多序列比对
    
    Args:
        sequence: 蛋白质序列（FASTA 格式）
        timeout: 请求超时时间（秒）
    
    Returns:
        包含 MSA 结果的字典，如果失败则返回 None
    """
    try:
        print(f"🔍 正在从 MSA 服务器请求多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        
        # 准备请求数据
        # 确保序列是 FASTA 格式
        if not sequence.startswith('>'):
            sequence = f">query\n{sequence}"
        
        # ColabFold MSA 服务器使用 form data 格式
        payload = {
            "q": sequence,
            "mode": MSA_SERVER_MODE
        }
        print(f"📦 MSA 请求参数: mode={MSA_SERVER_MODE}", file=sys.stderr)
        
        # 提交搜索任务
        submit_url = f"{MSA_SERVER_URL}/ticket/msa"
        print(f"📤 提交 MSA 搜索任务到: {submit_url}", file=sys.stderr)
        
        response = requests.post(submit_url, data=payload, timeout=30)
        if response.status_code != 200:
            print(f"❌ MSA 任务提交失败: {response.status_code} - {response.text}", file=sys.stderr)
            return None
        
        result = response.json()
        ticket_id = result.get("id")
        if not ticket_id:
            print(f"❌ 未获取到有效的任务 ID: {result}", file=sys.stderr)
            return None
        
        print(f"✅ MSA 任务已提交，任务 ID: {ticket_id}", file=sys.stderr)
        
        # 轮询结果
        result_url = f"{MSA_SERVER_URL}/ticket/{ticket_id}"
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                print(f"⏳ 检查 MSA 任务状态...", file=sys.stderr)
                response = requests.get(result_url, timeout=30)
                
                if response.status_code == 200:
                    result_data = response.json()
                    if result_data.get("status") == "COMPLETE":
                        print(f"✅ MSA 搜索完成，获取到结果", file=sys.stderr)
                        download_url = result_data.get("result_url") or f"{MSA_SERVER_URL}/result/download/{ticket_id}"
                        print(f"📥 下载 MSA 结果: {download_url}", file=sys.stderr)
                        try:
                            download_response = requests.get(download_url, timeout=60)
                        except requests.exceptions.RequestException as download_error:
                            print(f"❌ 下载 MSA 结果请求失败: {download_error}", file=sys.stderr)
                            return None
                        if download_response.status_code != 200:
                            print(
                                f"❌ 下载 MSA 结果失败: {download_response.status_code} - {download_response.text}",
                                file=sys.stderr,
                            )
                            return None

                        try:
                            tar_bytes = io.BytesIO(download_response.content)
                            with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
                                a3m_content = None
                                extracted_filename = None
                                for member in tar.getmembers():
                                    if member.name.lower().endswith(".a3m"):
                                        file_obj = tar.extractfile(member)
                                        if file_obj:
                                            a3m_content = file_obj.read().decode("utf-8")
                                            extracted_filename = member.name
                                            break

                            if not a3m_content:
                                print("❌ 未在下载的结果中找到 A3M 文件", file=sys.stderr)
                                return None

                            print(f"✅ 成功提取 A3M 文件: {extracted_filename}", file=sys.stderr)
                            a3m_content = sanitize_a3m_content(a3m_content, context=extracted_filename)
                            entries = parse_a3m_content(a3m_content)
                            return {
                                "entries": entries,
                                "a3m_content": a3m_content,
                                "source": extracted_filename,
                                "ticket_id": ticket_id,
                            }
                        except tarfile.TarError as tar_error:
                            print(f"❌ 解析 MSA 压缩包失败: {tar_error}", file=sys.stderr)
                            return None
                    elif result_data.get("status") == "ERROR":
                        print(f"❌ MSA 搜索失败: {result_data.get('error', '未知错误')}", file=sys.stderr)
                        print(
                            f"   ↳ 服务器返回: {json.dumps(result_data, ensure_ascii=False)}",
                            file=sys.stderr,
                        )
                        return None
                    else:
                        print(f"⏳ MSA 任务状态: {result_data.get('status', 'PENDING')}", file=sys.stderr)
                elif response.status_code == 404:
                    print(f"⏳ 任务尚未完成或不存在", file=sys.stderr)
                else:
                    print(f"⚠️ 检查状态时出现错误: {response.status_code}", file=sys.stderr)
                
            except requests.exceptions.RequestException as e:
                print(f"⚠️ 检查状态时网络错误: {e}", file=sys.stderr)
            
            # 等待一段时间再次检查
            time.sleep(10)
        
        print(f"⏰ MSA 搜索超时 ({timeout}秒)", file=sys.stderr)
        return None
        
    except Exception as e:
        print(f"❌ MSA 服务器请求失败: {e}", file=sys.stderr)
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
            print(f"❌ MSA 结果格式不支持: {msa_result.keys()}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"❌ 保存 MSA 结果失败: {e}", file=sys.stderr)
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
        print(f"🧬 开始为蛋白质序列生成 MSA", file=sys.stderr)
        
        # 解析 YAML 获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("❌ 未找到蛋白质序列，跳过 MSA 生成", file=sys.stderr)
            return False
        
        print(f"🔍 找到 {len(protein_sequences)} 个蛋白质序列需要生成 MSA", file=sys.stderr)
        
        # 为每个蛋白质序列生成 MSA
        success_count = 0
        for protein_id, sequence in protein_sequences.items():
            print(f"🧬 正在为蛋白质 {protein_id} 生成 MSA...", file=sys.stderr)
            
            # 检查临时目录中是否已经存在
            output_path = os.path.join(temp_dir, f"{protein_id}_msa.a3m")
            if os.path.exists(output_path):
                print(f"✅ 临时目录中已存在 MSA 文件: {output_path}", file=sys.stderr)
                sanitize_a3m_file(output_path, context=f"{protein_id} 临时文件")
                success_count += 1
                continue
            
            # 检查缓存（统一使用 msa_ 前缀）
            sequence_hash = get_sequence_hash(sequence)
            cache_dir = MSA_CACHE_CONFIG['cache_dir']
            cached_msa_path = os.path.join(cache_dir, f"msa_{sequence_hash}.a3m")
            
            if MSA_CACHE_CONFIG['enable_cache'] and os.path.exists(cached_msa_path):
                print(f"✅ 找到缓存的 MSA 文件: {cached_msa_path}", file=sys.stderr)
                sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存原文件")
                # 复制到临时目录
                shutil.copy2(cached_msa_path, output_path)
                sanitize_a3m_file(output_path, context=f"{protein_id} 缓存复制")
                success_count += 1
                continue
            
            # 从服务器请求 MSA
            msa_result = request_msa_from_server(sequence)
            if msa_result:
                # 保存到临时目录
                if save_msa_result_to_file(msa_result, output_path):
                    sanitize_a3m_file(output_path, context=f"{protein_id} 下载写入")
                    success_count += 1
                    
                    # 缓存结果（统一使用 msa_ 前缀）
                    if MSA_CACHE_CONFIG['enable_cache']:
                        os.makedirs(cache_dir, exist_ok=True)
                        shutil.copy2(output_path, cached_msa_path)
                        sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存写入")
                        print(f"💾 MSA 结果已缓存: {cached_msa_path}", file=sys.stderr)
                else:
                    print(f"❌ 保存 MSA 文件失败: {protein_id}", file=sys.stderr)
            else:
                print(f"❌ 获取 MSA 失败: {protein_id}", file=sys.stderr)
        
        print(f"✅ MSA 生成完成: {success_count}/{len(protein_sequences)} 个成功", file=sys.stderr)
        return success_count > 0
        
    except Exception as e:
        print(f"❌ 生成 MSA 时出现错误: {e}", file=sys.stderr)
        return False


def _inject_local_msa_paths_into_yaml(yaml_content: str, temp_dir: str) -> Tuple[str, int]:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return yaml_content, 0
    if not isinstance(yaml_data, dict):
        return yaml_content, 0

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        return yaml_content, 0

    local_files: Dict[str, str] = {}
    for root, _, files in os.walk(temp_dir):
        for file_name in files:
            if not (file_name.endswith(".a3m") or file_name.endswith(".csv")):
                continue
            local_files[file_name] = os.path.join(root, file_name)

    injected = 0
    for entity in sequences:
        if not isinstance(entity, dict):
            continue
        protein = entity.get("protein")
        if not isinstance(protein, dict):
            continue
        current_msa = protein.get("msa")
        if isinstance(current_msa, str) and current_msa.strip() and current_msa.strip() not in {"0", "empty"}:
            continue
        ids = protein.get("id")
        if isinstance(ids, list):
            chain_ids = [str(item or "").strip() for item in ids if str(item or "").strip()]
        else:
            chain_ids = [str(ids or "").strip()] if str(ids or "").strip() else []
        if not chain_ids:
            continue
        selected_path = ""
        for chain_id in chain_ids:
            candidates = (
                f"{chain_id}_msa.a3m",
                f"{chain_id}.a3m",
                f"{chain_id}_msa.csv",
                f"{chain_id}.csv",
            )
            for candidate in candidates:
                candidate_path = local_files.get(candidate, "")
                if candidate_path:
                    selected_path = candidate_path
                    break
            if selected_path:
                break
        if not selected_path:
            continue
        protein["msa"] = selected_path
        injected += 1

    if injected <= 0:
        return yaml_content, 0
    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False), injected


def cache_msa_files_from_temp_dir(temp_dir: str, yaml_content: str):
    """
    从临时目录中缓存生成的MSA文件
    支持从colabfold server生成的CSV格式MSA文件
    为每个蛋白质组分单独缓存MSA，适用于结构预测和分子设计
    """
    if not MSA_CACHE_CONFIG['enable_cache']:
        return
    
    try:
        # 解析YAML获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        # 提取所有蛋白质序列（支持结构预测和分子设计）
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("未找到蛋白质序列，跳过MSA缓存", file=sys.stderr)
            return
        
        print(f"需要缓存的蛋白质组分: {list(protein_sequences.keys())}", file=sys.stderr)
        
        # 设置缓存目录
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        os.makedirs(cache_dir, exist_ok=True)
        
        # 递归搜索临时目录中的MSA文件
        print(f"递归搜索临时目录中的MSA文件: {temp_dir}", file=sys.stderr)
        
        # 为每个蛋白质组分单独查找对应的MSA文件
        protein_msa_map = {}  # protein_id -> [msa_files]
        
        # 搜索所有MSA文件
        all_msa_files = []
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.csv') or file.endswith('.a3m'):
                    file_path = os.path.join(root, file)
                    all_msa_files.append(file_path)
        
        if not all_msa_files:
            print(f"在临时目录中未找到任何MSA文件: {temp_dir}", file=sys.stderr)
            return
        
        print(f"找到 {len(all_msa_files)} 个MSA文件: {[os.path.basename(f) for f in all_msa_files]}", file=sys.stderr)
        
        # 为每个蛋白质组分匹配对应的MSA文件
        for protein_id in protein_sequences.keys():
            protein_msa_map[protein_id] = []
            
            for msa_file in all_msa_files:
                filename = os.path.basename(msa_file)
                
                # 精确匹配：文件名包含protein ID
                if protein_id.lower() in filename.lower():
                    protein_msa_map[protein_id].append(msa_file)
                    continue
                    
                # 索引匹配：如果protein_id是字母，尝试匹配对应的数字索引
                # 例如：protein A -> _0.csv, protein B -> _1.csv
                if len(protein_id) == 1 and protein_id.isalpha():
                    protein_index = ord(protein_id.upper()) - ord('A')
                    if f"_{protein_index}." in filename:
                        protein_msa_map[protein_id].append(msa_file)
                        continue
                
                # 通用匹配：如果只有一个蛋白质组分，使用通用MSA文件
                if len(protein_sequences) == 1 and any(pattern in filename.lower() for pattern in ['msa', '_0.csv', '_0.a3m']):
                    protein_msa_map[protein_id].append(msa_file)
        
        # 处理每个蛋白质组分的MSA文件
        cached_count = 0
        for protein_id, msa_files in protein_msa_map.items():
            if not msa_files:
                print(f"❌ 蛋白质组分 {protein_id} 未找到对应的MSA文件", file=sys.stderr)
                continue
                
            print(f"🔍 处理蛋白质组分 {protein_id} 的 {len(msa_files)} 个MSA文件", file=sys.stderr)
            
            for msa_file in msa_files:
                if cache_single_protein_msa(protein_id, protein_sequences[protein_id], msa_file, cache_dir):
                    cached_count += 1
                    break  # 成功缓存一个就够了
        
        print(f"✅ MSA缓存完成，成功缓存 {cached_count}/{len(protein_sequences)} 个蛋白质组分", file=sys.stderr)
                
    except Exception as e:
        print(f"❌ 缓存MSA文件失败: {e}", file=sys.stderr)

def cache_single_protein_msa(protein_id: str, protein_sequence: str, msa_file: str, cache_dir: str) -> bool:
    """
    为单个蛋白质组分缓存MSA文件
    返回是否成功缓存
    """
    try:
        filename = os.path.basename(msa_file)
        file_ext = os.path.splitext(filename)[1].lower()
        
        print(f"  📂 处理MSA文件: {filename}", file=sys.stderr)
        
        if file_ext == '.csv':
            # 处理CSV格式的MSA文件（来自colabfold server）
            with open(msa_file, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header and len(header) >= 2 and 'sequence' in header:
                    sequences = []
                    for row in reader:
                        if len(row) >= 2 and row[1]:
                            sequences.append(row[1])
                    
                    if sequences:
                        # 第一个序列通常是查询序列
                        query_sequence = sequences[0]
                        print(f"    从CSV提取的查询序列: {query_sequence[:50]}...", file=sys.stderr)
                        
                        # 验证序列是否匹配
                        if is_sequence_match(protein_sequence, query_sequence):
                            # 转换CSV格式到A3M格式
                            a3m_content = f">{protein_id}\n{query_sequence}\n"
                            for i, seq in enumerate(sequences[1:], 1):
                                a3m_content += f">seq_{i}\n{seq}\n"
                            
                            # 缓存转换后的A3M文件
                            seq_hash = get_sequence_hash(protein_sequence)
                            cache_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
                            with open(cache_path, 'w') as cache_file:
                                cache_file.write(sanitize_a3m_content(a3m_content, context=f"{protein_id} CSV 转换"))
                            print(f"    ✅ 成功缓存蛋白质组分 {protein_id} 的MSA (从CSV转换): {cache_path}", file=sys.stderr)
                            print(f"       序列哈希: {seq_hash}", file=sys.stderr)
                            print(f"       MSA序列数: {len(sequences)}", file=sys.stderr)
                            return True
                        else:
                            print(f"    ❌ CSV文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
                            return False
        
        elif file_ext == '.a3m':
            # 处理A3M格式的MSA文件
            sanitize_a3m_file(msa_file, context=f"{protein_id} 源MSA")
            with open(msa_file, 'r') as f:
                msa_content = sanitize_a3m_content(f.read(), context=msa_file)
            
            # 从MSA内容中提取查询序列（第一个序列）
            lines = msa_content.strip().split('\n')
            if len(lines) >= 2 and lines[0].startswith('>'):
                query_sequence = lines[1]
                
                # 验证序列是否匹配
                if is_sequence_match(protein_sequence, query_sequence):
                    # 缓存MSA文件
                    seq_hash = get_sequence_hash(protein_sequence)
                    cache_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
                    with open(cache_path, 'w') as cache_file:
                        cache_file.write(msa_content)
                    print(f"    ✅ 成功缓存蛋白质组分 {protein_id} 的MSA: {cache_path}", file=sys.stderr)
                    print(f"       序列哈希: {seq_hash}", file=sys.stderr)
                    return True
                else:
                    print(f"    ❌ A3M文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
                    return False
        
        return False
        
    except Exception as e:
        print(f"    ❌ 处理蛋白质组分 {protein_id} 的MSA文件失败 {msa_file}: {e}", file=sys.stderr)
        return False

def is_sequence_match(protein_sequence: str, query_sequence: str) -> bool:
    """
    检查蛋白质序列和查询序列是否匹配
    支持完全匹配、容错匹配和相似度匹配
    """
    # 完全匹配
    if protein_sequence == query_sequence:
        return True
    
    # 容错匹配：去除空格和特殊字符后比较
    clean_protein = protein_sequence.replace('-', '').replace(' ', '').upper()
    clean_query = query_sequence.replace('-', '').replace(' ', '').upper()
    if clean_protein == clean_query:
        return True
    
    # 子序列匹配：查询序列可能是蛋白质序列的一部分
    if clean_query in clean_protein or clean_protein in clean_query:
        # 计算相似度
        similarity = len(set(clean_query) & set(clean_protein)) / max(len(clean_query), len(clean_protein))
        if similarity > 0.8:  # 80%相似度阈值
            return True
    
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
    """
    获取与当前预测任务相关的a3m缓存文件
    返回缓存文件路径列表
    """
    cached_a3m_files = []
    
    if not MSA_CACHE_CONFIG['enable_cache']:
        return cached_a3m_files
    
    try:
        # 解析YAML获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        # 提取所有蛋白质序列
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("未找到蛋白质序列，跳过a3m文件收集", file=sys.stderr)
            return cached_a3m_files
        
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        if not os.path.exists(cache_dir):
            return cached_a3m_files
        
        print(f"查找缓存的a3m文件，蛋白质组分: {list(protein_sequences.keys())}", file=sys.stderr)
        
        # 为每个蛋白质序列查找对应的缓存文件
        for protein_id, sequence in protein_sequences.items():
            seq_hash = get_sequence_hash(sequence)
            cache_file_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
            
            if os.path.exists(cache_file_path):
                cached_a3m_files.append({
                    'path': cache_file_path,
                    'protein_id': protein_id,
                    'filename': f"{protein_id}_msa.a3m"
                })
                print(f"找到缓存文件: {protein_id} -> {cache_file_path}", file=sys.stderr)
        
        print(f"总共找到 {len(cached_a3m_files)} 个a3m缓存文件", file=sys.stderr)
        
    except Exception as e:
        print(f"获取a3m缓存文件失败: {e}", file=sys.stderr)
    
    return cached_a3m_files

def create_archive_with_a3m(output_archive_path: str, output_directory_path: str, yaml_content: str):
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
                
                print(f"✅ 成功添加 {len(cached_a3m_files)} 个a3m缓存文件到zip归档", file=sys.stderr)
            else:
                print("⚠️ 未找到相关的a3m缓存文件", file=sys.stderr)
        
        print(f"✅ 归档创建完成: {output_archive_path}", file=sys.stderr)
        
    except Exception as e:
        print(f"❌ 创建包含a3m文件的归档失败: {e}", file=sys.stderr)
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


def _next_available_chain_id(occupied: set[str]) -> str:
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
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return yaml_content
    if not isinstance(yaml_data, dict):
        return yaml_content

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
                    mapped = _next_available_chain_id(occupied_ids)
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
        f"ℹ️ Normalized ligand chain collisions: {ligand_id_mapping}",
        file=sys.stderr,
    )
    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False)


def _sanitize_constraints_for_chain_lengths(yaml_content: str) -> str:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return yaml_content
    if not isinstance(yaml_data, dict):
        return yaml_content
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


def _remap_constraints_by_template_alignment(yaml_content: str) -> str:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return yaml_content
    if not isinstance(yaml_data, dict):
        return yaml_content

    constraints = yaml_data.get("constraints")
    templates = yaml_data.get("templates")
    if not isinstance(constraints, list) or not constraints:
        return yaml_content
    if not isinstance(templates, list) or not templates:
        return yaml_content

    chain_seq_map = build_chain_sequence_map(yaml_data)
    if not chain_seq_map:
        return yaml_content

    mapping_by_chain: Dict[str, Dict[int, int]] = {}
    for entry in templates:
        if not isinstance(entry, dict):
            continue
        template_path_raw = entry.get("cif") or entry.get("mmcif") or entry.get("pdb")
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
            f"ℹ️ Remapped pocket contacts by template/query alignment: replaced={replaced_contacts}",
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
        f"ℹ️ Constraint summary: total_contacts={total_contacts}, max_residue_by_chain={chain_max_residue}, chain_lengths={chain_lengths}",
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
                print("⚠️ 未找到AF3所需的MSA文件，JSON中将留空", file=sys.stderr)

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
                print("ℹ️ AF3输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            instructions = (
                "AlphaFold3 input assets generated by Boltz-WebUI.\n"
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
                        print(f"⚠️ 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)

        print(f"✅ AF3 归档创建完成: {output_archive_path}", file=sys.stderr)
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
                print("ℹ️ Protenix 输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            readme = (
                "Protenix input assets generated by Boltz-WebUI.\n"
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

        print(f"✅ Protenix 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as exc:
        raise RuntimeError(f"Failed to create Protenix archive: {exc}") from exc


def run_protenix_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    use_msa_server: bool,
    seed: Optional[int] = None,
    task_id: Optional[str] = None,
) -> None:
    print("🚀 Using Protenix backend", file=sys.stderr)

    prep = parse_yaml_for_protenix(yaml_content)
    protenix_json = prep.payload

    chain_msa_paths_local: Dict[str, str] = {}
    host_msa_paths_for_archive: Dict[str, str] = {}

    if use_msa_server and MSA_SERVER_URL and MSA_SERVER_URL != "":
        print(f"🧬 开始使用 MSA 服务器生成多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        msa_generated = generate_msa_for_sequences(yaml_content, temp_dir)
        if msa_generated:
            print("✅ MSA 生成成功，将用于 Protenix 输入", file=sys.stderr)
            if MSA_CACHE_CONFIG["enable_cache"]:
                cache_msa_files_from_temp_dir(temp_dir, yaml_content)
        else:
            print("⚠️ 未能获取外部 MSA，将回退为无 MSA 推理", file=sys.stderr)

    protenix_input_dir = os.path.join(temp_dir, "protenix_input")
    protenix_output_dir = os.path.join(temp_dir, "protenix_output")
    protenix_msa_dir = os.path.join(protenix_input_dir, "msa")
    os.makedirs(protenix_input_dir, exist_ok=True)
    os.makedirs(protenix_output_dir, exist_ok=True)
    os.makedirs(protenix_msa_dir, exist_ok=True)

    effective_use_msa = False
    if use_msa_server:
        # Reuse AF3 chain-MSA lookup logic (same YAML chain semantics).
        try:
            af3_prep = parse_yaml_for_af3(yaml_content, default_jobname=prep.input_name)
            cache_dir = MSA_CACHE_CONFIG["cache_dir"] if MSA_CACHE_CONFIG["enable_cache"] else None
            chain_msa_paths = collect_chain_msa_paths(af3_prep, temp_dir, cache_dir)
            for chain_id, path in chain_msa_paths.items():
                if not path or not path.exists():
                    continue
                dst_name = f"{safe_filename(chain_id)}.a3m"
                dst_host_path = os.path.join(protenix_msa_dir, dst_name)
                shutil.copyfile(str(path), dst_host_path)
                chain_msa_paths_local[chain_id] = f"/workspace/protenix_input/msa/{dst_name}"
                host_msa_paths_for_archive[chain_id] = str(path)
        except Exception as msa_err:
            print(f"⚠️ Protenix MSA 路径解析失败，将回退无 MSA 模式: {msa_err}", file=sys.stderr)

        assigned_count = apply_protein_msa_paths(prep, chain_msa_paths_local)
        protenix_json = prep.payload
        effective_use_msa = assigned_count > 0
        if effective_use_msa:
            print(f"✅ 已为 {assigned_count} 个蛋白实体挂载外部 MSA", file=sys.stderr)
        else:
            print("⚠️ 没有可用的外部 MSA 文件，Protenix 将以无 MSA 模式运行", file=sys.stderr)
    else:
        # Ensure no stale MSA field remains when user disables MSA server.
        apply_protein_msa_paths(prep, {})
        protenix_json = prep.payload

    input_json_path = os.path.join(protenix_input_dir, "input.json")
    with open(input_json_path, "w", encoding="utf-8") as f:
        json.dump(protenix_json, f, indent=2, ensure_ascii=False)

    model_dir = PROTENIX_MODEL_DIR
    source_dir = PROTENIX_SOURCE_DIR
    model_name_raw = (PROTENIX_MODEL_NAME or "protenix_base_20250630_v1.0.0").strip()
    model_name = model_name_raw[:-3] if model_name_raw.endswith(".pt") else model_name_raw
    if not model_name:
        raise ValueError("PROTENIX_MODEL_NAME 不能为空。")
    checkpoint_filename = f"{model_name}.pt"
    image = PROTENIX_DOCKER_IMAGE or "ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4"
    raw_extra_args = shlex.split(PROTENIX_DOCKER_EXTRA_ARGS) if PROTENIX_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    infer_extra_args = shlex.split(PROTENIX_INFER_EXTRA_ARGS) if PROTENIX_INFER_EXTRA_ARGS else []

    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("PROTENIX_MODEL_DIR 未配置或目录不存在，无法运行 Protenix 容器。")
    if not source_dir or not os.path.isdir(source_dir):
        raise FileNotFoundError(
            "PROTENIX_SOURCE_DIR 未配置或目录不存在。"
            "新版 Protenix Docker 镜像不内置源码，请先 git clone Protenix 并配置该路径。"
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

    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        gpu_arg = determine_docker_gpu_arg(visible_devices)
    except RuntimeError as gpu_err:
        print(f"❌ 无法准备 Protenix GPU 环境: {gpu_err}", file=sys.stderr)
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

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--env",
            "PYTHONPATH=/app",
            "--volume",
            f"{protenix_input_dir}:/workspace/protenix_input",
            "--volume",
            f"{protenix_output_dir}:/workspace/protenix_output",
            "--volume",
            f"{model_dir}:/workspace/model",
            "--volume",
            f"{source_dir}:/app",
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
        print(f"🔐 Protenix 容器使用宿主机用户: {host_uid}:{host_gid}", file=sys.stderr)
    else:
        print("🔐 Protenix 容器使用默认 root 用户（官方镜像推荐）", file=sys.stderr)

    docker_command.extend(extra_args)

    docker_command.append(image)
    docker_command.extend(
        [
            (PROTENIX_PYTHON_BIN or "python3"),
            "/app/runner/inference.py",
            "--model_name",
            model_name,
            "--load_checkpoint_dir",
            "/workspace/model",
            "--load_checkpoint_path",
            f"/workspace/model/{checkpoint_filename}",
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

    print(f"🐳 运行 Protenix Docker: {display_command}", file=sys.stderr)
    protenix_log_path = os.path.join(temp_dir, "protenix_docker.log")
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
        raise RuntimeError(
            f"Protenix Docker run failed with exit code {return_code}. "
            f"Last output:\n{tail_text}"
            f"{f'\nTraceback:\n{traceback_text}' if traceback_text else ''}"
            f"{hint}\nFull log: {protenix_log_path}"
        )

    yaml_data: Dict[str, Any] = {}
    try:
        parsed_yaml = yaml.safe_load(yaml_content)
        if isinstance(parsed_yaml, dict):
            yaml_data = parsed_yaml
    except Exception as yaml_err:
        print(f"⚠️ Protenix 亲和力流程解析 YAML 失败，将跳过亲和力预测: {yaml_err}", file=sys.stderr)

    extra_files: List[Tuple[Path, str]] = [(Path(protenix_log_path), "protenix/protenix_docker.log")]
    extra_files.extend(
        run_protenix_affinity_pipeline(
            temp_dir=temp_dir,
            yaml_data=yaml_data,
            prep=prep,
            protenix_output_dir=protenix_output_dir,
        )
    )

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
    cmd = [
        "docker",
        "run",
        "--rm",
        "--user",
        "root",
        "--volume",
        f"{temp_dir}:{temp_dir}",
        image,
        "sh",
        "-lc",
        fix_script,
    ]
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as perm_err:
        print(f"⚠️ 无法自动修复 Protenix 输出目录权限: {perm_err}", file=sys.stderr)


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


def _read_float_option(
    options: Dict[str, Any],
    key: str,
    default: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    raw = options.get(key, default)
    try:
        parsed = float(raw)
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
    return "cyclic"


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


def _enforce_bicyclic_cys_layout(
    sequence: str,
    *,
    binder_length: int,
    cys_positions: Optional[List[int]],
) -> str:
    amino_no_c = "ARNDQEGHILKMFPSTWYV"
    seq = list(sequence[:binder_length].upper())
    if len(seq) < binder_length:
        seq.extend(random.choice(amino_no_c) for _ in range(binder_length - len(seq)))

    for idx in range(binder_length):
        if seq[idx] == "C":
            seq[idx] = random.choice(amino_no_c)

    terminal_idx = binder_length - 1
    seq[terminal_idx] = "C"

    chosen_positions: List[int] = []
    if cys_positions:
        for pos in cys_positions:
            if isinstance(pos, int) and 0 <= pos < terminal_idx and pos not in chosen_positions:
                chosen_positions.append(pos)
            if len(chosen_positions) == 2:
                break
    if len(chosen_positions) < 2:
        pool = [idx for idx in range(terminal_idx) if idx not in chosen_positions]
        if len(pool) >= (2 - len(chosen_positions)):
            chosen_positions.extend(random.sample(pool, k=2 - len(chosen_positions)))

    for pos in chosen_positions[:2]:
        seq[pos] = "C"

    return "".join(seq)


def _normalize_initial_sequence(
    raw_sequence: Any,
    *,
    binder_length: int,
    sequence_mask: str,
    default_sequence: str,
) -> str:
    cleaned = "".join(ch for ch in str(raw_sequence or "").upper() if "A" <= ch <= "Z")
    if not cleaned:
        cleaned = default_sequence
    if len(cleaned) < binder_length:
        cleaned = cleaned + default_sequence[len(cleaned):binder_length]
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
) -> str:
    yaml_data = copy.deepcopy(base_yaml_data)
    if not isinstance(yaml_data.get("sequences"), list):
        yaml_data["sequences"] = []

    binder_entry: Dict[str, Any] = {
        "protein": {
            "id": binder_chain_id,
            "sequence": binder_sequence,
            "msa": "empty",
        }
    }
    if design_mode == "cyclic":
        binder_entry["protein"]["cyclic"] = True

    yaml_data["sequences"].append(binder_entry)

    if design_mode == "bicyclic":
        yaml_data["sequences"].append({"ligand": {"id": linker_chain_id, "ccd": linker_ccd}})
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


def _select_primary_structure_file(results_dir: str) -> Optional[Path]:
    path_obj = Path(results_dir)
    candidates = [p for p in path_obj.glob("*.cif")]
    if not candidates:
        candidates = [p for p in path_obj.glob("*.pdb")]
    if not candidates:
        return None

    def _score(path: Path) -> Tuple[int, str]:
        name = path.name.lower()
        score = 100
        if "rank_1" in name:
            score = 1
        elif "rank_" in name:
            score = 10
        elif "model_0" in name:
            score = 20
        elif "model_" in name:
            score = 30
        return (score, name)

    return sorted(candidates, key=_score)[0]


def _write_peptide_progress(progress_path: Optional[str], payload: Dict[str, Any]) -> None:
    if not progress_path:
        return
    try:
        path_obj = Path(progress_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"⚠️ Failed to write peptide progress file: {exc}", file=sys.stderr)


def run_peptide_design_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    predict_args: Dict[str, Any],
    model_name: Optional[str],
    seed: Optional[int],
    options: Dict[str, Any],
    target_chain_id: Optional[str],
    progress_path: Optional[str],
) -> None:
    designer_dir = os.path.join(os.getcwd(), "designer")
    if designer_dir not in sys.path:
        sys.path.append(designer_dir)

    from design_utils import generate_random_sequence, mutate_sequence, parse_confidence_metrics  # type: ignore

    try:
        base_yaml_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML for peptide design: {exc}") from exc
    if not isinstance(base_yaml_data, dict):
        raise ValueError("YAML root must be a mapping for peptide design.")

    random_seed = seed if isinstance(seed, int) else int(time.time())
    random.seed(random_seed)

    design_mode = _normalize_peptide_design_mode(options.get("peptideDesignMode"))
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
    mutation_rate = _read_float_option(options, "peptideMutationRate", 0.25, min_value=0.01, max_value=1.0)
    use_initial_sequence = _read_bool_option(options, "peptideUseInitialSequence", False)
    sequence_mask = _normalize_sequence_mask(options.get("peptideSequenceMask"), binder_length)
    linker_ccd = str(options.get("peptideBicyclicLinkerCcd") or "SEZ").strip().upper() or "SEZ"

    used_chain_ids = _extract_chain_ids_from_yaml(base_yaml_data)
    chain_order = list(used_chain_ids)
    binder_chain_id = _next_available_chain_id(used_chain_ids, "B")
    chain_order.append(binder_chain_id)
    linker_chain_id = _next_available_chain_id(chain_order, "L")

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
    if design_mode == "bicyclic":
        cys_position_mode = str(options.get("peptideBicyclicCysPositionMode") or "auto").strip().lower()
        cys1_pos = _read_int_option(options, "peptideBicyclicCys1Pos", 3, min_value=1, max_value=max(1, binder_length - 1))
        cys2_pos = _read_int_option(
            options,
            "peptideBicyclicCys2Pos",
            max(2, binder_length // 2),
            min_value=1,
            max_value=max(1, binder_length - 1),
        )
        if cys1_pos == cys2_pos:
            cys2_pos = min(max(1, binder_length - 1), cys2_pos + 1 if cys2_pos < binder_length - 1 else cys2_pos - 1)
        if cys_position_mode == "manual":
            design_params["cys_positions"] = [cys1_pos - 1, cys2_pos - 1]

    linker_atom_map = {
        "SEZ": ["CD", "C1", "C2"],
        "29N": ["C16", "C19", "C25"],
        "BS3": ["BI", "BI", "BI"],
    }
    if design_mode == "bicyclic" and linker_ccd not in linker_atom_map:
        linker_ccd = "SEZ"

    baseline_sequence = generate_random_sequence(binder_length, design_params)
    initial_sequence = ""
    if use_initial_sequence:
        initial_sequence = _normalize_initial_sequence(
            options.get("peptideInitialSequence"),
            binder_length=binder_length,
            sequence_mask=sequence_mask,
            default_sequence=baseline_sequence,
        )
        if design_mode == "bicyclic":
            initial_sequence = _enforce_bicyclic_cys_layout(
                initial_sequence,
                binder_length=binder_length,
                cys_positions=design_params.get("cys_positions"),
            )

    total_tasks = iterations * population_size
    completed_tasks = 0
    evaluated_sequences: set[str] = set()
    elite_population: List[Dict[str, Any]] = []
    all_results: List[Dict[str, Any]] = []

    runtime_predict_args = dict(predict_args)
    if "seed" in runtime_predict_args:
        runtime_predict_args.pop("seed", None)

    for generation in range(1, iterations + 1):
        generation_candidates: List[str] = []
        attempts = 0
        max_attempts = max(population_size * 30, 60)

        while len(generation_candidates) < population_size and attempts < max_attempts:
            attempts += 1
            if generation == 1 and initial_sequence and initial_sequence not in evaluated_sequences:
                candidate_sequence = initial_sequence
            elif not elite_population:
                candidate_sequence = generate_random_sequence(binder_length, design_params)
            else:
                parent = random.choice(elite_population)
                parent_seq = str(parent.get("sequence") or "")
                parent_plddts = parent.get("plddts") if isinstance(parent.get("plddts"), list) else None
                candidate_sequence = mutate_sequence(
                    parent_seq,
                    mutation_rate=mutation_rate,
                    plddt_scores=parent_plddts,
                    design_params=design_params,
                )

            candidate_sequence = _apply_sequence_mask(candidate_sequence, sequence_mask)
            if design_mode == "bicyclic":
                candidate_sequence = _enforce_bicyclic_cys_layout(
                    candidate_sequence,
                    binder_length=binder_length,
                    cys_positions=design_params.get("cys_positions"),
                )

            if candidate_sequence in evaluated_sequences:
                continue
            evaluated_sequences.add(candidate_sequence)
            generation_candidates.append(candidate_sequence)

        if not generation_candidates:
            break

        for idx, candidate_sequence in enumerate(generation_candidates, start=1):
            candidate_yaml = _build_peptide_candidate_yaml(
                base_yaml_data,
                binder_chain_id=binder_chain_id,
                binder_sequence=candidate_sequence,
                design_mode=design_mode,
                linker_ccd=linker_ccd,
                linker_chain_id=linker_chain_id,
                linker_atom_map=linker_atom_map,
            )
            candidate_dir = os.path.join(temp_dir, "peptide_design", f"gen_{generation:03d}", f"cand_{idx:03d}")
            os.makedirs(candidate_dir, exist_ok=True)
            archive_path = os.path.join(candidate_dir, "result.zip")

            per_candidate_args = dict(runtime_predict_args)
            if isinstance(seed, int):
                per_candidate_args["seed"] = int(seed) + completed_tasks + idx

            run_boltz_backend(
                candidate_dir,
                candidate_yaml,
                archive_path,
                per_candidate_args,
                model_name,
            )
            result_dir = find_results_dir(candidate_dir)
            metrics = parse_confidence_metrics(
                result_dir,
                binder_chain_id=binder_chain_id,
                target_chain_id=resolved_target_chain_id or None,
                chain_order=chain_order,
            )

            pair_iptm_raw = metrics.get("pair_iptm")
            pair_iptm = float(pair_iptm_raw) if isinstance(pair_iptm_raw, (int, float)) else None
            binder_avg_plddt = float(metrics.get("binder_avg_plddt") or 0.0)
            composite_score = (0.7 * pair_iptm) + (0.3 * (binder_avg_plddt / 100.0)) if pair_iptm is not None else None
            structure_file = _select_primary_structure_file(result_dir)
            result_row = {
                "sequence": candidate_sequence,
                "generation": generation,
                "iptm": pair_iptm,
                "pair_iptm": pair_iptm,
                "binder_avg_plddt": binder_avg_plddt,
                "composite_score": composite_score,
                "score": composite_score,
                "plddt": binder_avg_plddt,
                "model": "Boltz",
                "structure_source_path": str(structure_file) if structure_file else "",
                "structure_format": (
                    "pdb"
                    if structure_file and structure_file.suffix.lower() == ".pdb"
                    else "cif"
                ),
                "plddts": metrics.get("plddts") if isinstance(metrics.get("plddts"), list) else [],
            }
            all_results.append(result_row)
            completed_tasks += 1

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
                "plddts": row.get("plddts") if isinstance(row.get("plddts"), list) else [],
            }
            for row in all_results[:elite_size]
        ]

        current_best_rows = []
        for rank, row in enumerate(all_results[: min(8, len(all_results))], start=1):
            current_best_rows.append(
                {
                    "rank": rank,
                    "sequence": row.get("sequence"),
                    "generation": row.get("generation"),
                    "score": row.get("composite_score"),
                    "iptm": row.get("iptm"),
                    "binder_avg_plddt": row.get("binder_avg_plddt"),
                }
            )
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
                "status_message": f"Completed generation {generation} of {iterations}",
                "current_best_sequences": current_best_rows,
            }
        }
        _write_peptide_progress(progress_path, progress_payload)

    all_results.sort(
        key=lambda item: (
            1 if isinstance(item.get("composite_score"), (int, float)) else 0,
            float(item.get("composite_score")) if isinstance(item.get("composite_score"), (int, float)) else float("-inf"),
        ),
        reverse=True,
    )
    top_results = all_results[:24]
    if not top_results:
        raise RuntimeError("Peptide design produced no valid candidates.")

    zip_rows: List[Dict[str, Any]] = []
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for rank, row in enumerate(top_results, start=1):
            next_row = dict(row)
            source_path = str(next_row.pop("structure_source_path", "") or "")
            structure_arcname = ""
            if source_path and os.path.isfile(source_path):
                suffix = Path(source_path).suffix.lower()
                ext = ".pdb" if suffix == ".pdb" else ".cif"
                structure_arcname = f"structures/rank_{rank:02d}{ext}"
                zipf.write(source_path, structure_arcname)
            next_row["rank"] = rank
            next_row["structure_file"] = structure_arcname
            next_row["structure_name"] = Path(structure_arcname).name if structure_arcname else ""
            next_row["structure_path"] = structure_arcname
            next_row.pop("plddts", None)
            zip_rows.append(next_row)

        summary_payload = {
            "summary": {
                "design_mode": design_mode,
                "binder_length": binder_length,
                "iterations": iterations,
                "population_size": population_size,
                "elite_size": elite_size,
                "mutation_rate": mutation_rate,
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "best_score": zip_rows[0].get("composite_score") if zip_rows else 0.0,
            },
            "peptide_design": {
                "design_mode": design_mode,
                "binder_length": binder_length,
                "iterations": iterations,
                "population_size": population_size,
                "elite_size": elite_size,
                "mutation_rate": mutation_rate,
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
                "best_sequences": zip_rows[:8],
            }
        },
    )


def run_boltz_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    predict_args: dict,
    model_name: Optional[str],
) -> None:
    normalized_yaml = _normalize_ligand_chain_collisions(yaml_content)
    _validate_unique_sequence_chain_ids(normalized_yaml)
    normalized_yaml = _remap_constraints_by_template_alignment(normalized_yaml)
    _validate_unique_sequence_chain_ids(normalized_yaml)
    normalized_yaml = _sanitize_constraints_for_chain_lengths(normalized_yaml)
    _print_constraint_residue_summary(normalized_yaml)

    cli_args = dict(predict_args)
    if model_name:
        cli_args['model'] = model_name
        print(f"DEBUG: Using model: {model_name}", file=sys.stderr)

    if 'diffusion_samples' not in cli_args or cli_args['diffusion_samples'] is None:
        effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
        if effective_model == 'boltz2':
            cli_args['diffusion_samples'] = 5
    if 'trainer_precision' not in cli_args or cli_args['trainer_precision'] is None:
        effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
        if effective_model == 'boltz2':
            cli_args['trainer_precision'] = '32'

    if MSA_SERVER_URL and MSA_SERVER_URL != "":
        print(f"🧬 开始使用 MSA 服务器生成多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        msa_generated = generate_msa_for_sequences(normalized_yaml, temp_dir)
        if msa_generated:
            print(f"✅ MSA 生成成功，将用于结构预测", file=sys.stderr)
        else:
            print(f"⚠️ MSA 生成失败，将使用默认方法进行预测", file=sys.stderr)
        normalized_yaml, injected_count = _inject_local_msa_paths_into_yaml(normalized_yaml, temp_dir)
        if injected_count > 0:
            print(f"ℹ️ Injected local MSA paths into YAML: {injected_count}", file=sys.stderr)
        # If YAML still has missing MSA entries, let boltz_wrapper call MSA server explicitly.
        cli_args['use_msa_server'] = True
        cli_args['msa_server_url'] = MSA_SERVER_URL
    else:
        print(f"ℹ️ 未配置 MSA 服务器，跳过 MSA 生成", file=sys.stderr)

    tmp_yaml_path = os.path.join(temp_dir, 'data.yaml')
    with open(tmp_yaml_path, 'w') as tmp_yaml:
        tmp_yaml.write(normalized_yaml)
    cli_args['data'] = tmp_yaml_path
    cli_args['out_dir'] = temp_dir

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

    print(f"DEBUG: Invoking predict with args: {cmd_args}", file=sys.stderr)
    predict.main(args=cmd_args, standalone_mode=False)

    cache_msa_files_from_temp_dir(temp_dir, normalized_yaml)
    assert_boltz_preprocessing_succeeded(temp_dir, normalized_yaml)

    output_directory_path = find_results_dir(temp_dir)
    if not os.listdir(output_directory_path):
        raise NotADirectoryError(
            f"Prediction result directory was found but is empty: {output_directory_path}"
        )

    create_archive_with_a3m(output_archive_path, output_directory_path, normalized_yaml)


def run_alphafold3_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    use_msa_server: bool,
    seed: Optional[int] = None,
    template_payloads: Optional[List[dict]] = None,
    task_id: Optional[str] = None,
) -> None:
    print("🚀 Using AlphaFold3 backend (AF3 input preparation)", file=sys.stderr)

    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as err:
        print(f"⚠️ 无法解析 YAML，亲和力后处理将被跳过: {err}", file=sys.stderr)
        yaml_data = {}

    if use_msa_server and MSA_SERVER_URL and MSA_SERVER_URL != "":
        print(f"🧬 开始使用 MSA 服务器生成多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        msa_generated = generate_msa_for_sequences(yaml_content, temp_dir)
        if msa_generated:
            print(f"✅ MSA 生成成功，将用于AF3输入", file=sys.stderr)
        else:
            print(f"⚠️ 未能获取MSA，AF3 JSON将含空MSA字段", file=sys.stderr)
        if MSA_CACHE_CONFIG['enable_cache']:
            # 尽早缓存，方便按序列哈希回查
            cache_msa_files_from_temp_dir(temp_dir, yaml_content)
    else:
        print("ℹ️ 未请求外部 MSA，将跳过 MSA 生成与缓存，AlphaFold3 将使用内置流程。", file=sys.stderr)

    prep = parse_yaml_for_af3(yaml_content)
    cache_dir = MSA_CACHE_CONFIG['cache_dir'] if (MSA_CACHE_CONFIG['enable_cache'] and use_msa_server) else None
    if use_msa_server:
        chain_msa_paths = collect_chain_msa_paths(prep, temp_dir, cache_dir)
        unpaired_msa = load_unpaired_msa(prep, chain_msa_paths)
    else:
        chain_msa_paths = {}
        unpaired_msa = None

    fasta_content = build_af3_fasta(prep)
    model_seeds = build_af3_model_seeds(seed)
    af3_json = build_af3_json(
        prep,
        unpaired_msa,
        use_external_msa=use_msa_server,
        model_seeds=model_seeds,
    )

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

    af3_input_dir = os.path.join(temp_dir, "af3_input")
    af3_output_dir = os.path.join(temp_dir, "af3_output")
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
    image = ALPHAFOLD3_DOCKER_IMAGE or "alphafold3"
    raw_extra_args = shlex.split(ALPHAFOLD3_DOCKER_EXTRA_ARGS) if ALPHAFOLD3_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"⚠️ 已忽略部分 ALPHAFOLD3_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
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
        print(f"❌ 无法准备 AlphaFold3 GPU 环境: {gpu_err}", file=sys.stderr)
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
        print(f"⚠️ 无法创建 JAX 编译缓存目录 {jax_cache_host_dir}: {exc}", file=sys.stderr)

    # 添加 ColabFold jobs 目录挂载（如果配置了 MSA 服务器）
    if use_msa_server and MSA_SERVER_URL and COLABFOLD_JOBS_DIR and os.path.exists(COLABFOLD_JOBS_DIR):
        docker_command.extend([
            "--volume",
            f"{COLABFOLD_JOBS_DIR}:{container_colabfold_jobs_dir}",
        ])
        print(f"🔗 挂载 ColabFold jobs 目录: {COLABFOLD_JOBS_DIR} -> {container_colabfold_jobs_dir}", file=sys.stderr)
    elif use_msa_server:
        print("⚠️ 未找到 ColabFold jobs 目录或未配置 MSA 服务器", file=sys.stderr)
    else:
        print("ℹ️ 未启用外部 MSA，跳过 ColabFold jobs 目录挂载", file=sys.stderr)

    host_uid = os.getuid()
    host_gid = os.getgid()
    docker_command += [
        "--user",
        f"{host_uid}:{host_gid}",
    ]

    gpu_device_groups = collect_gpu_device_group_ids()
    if not gpu_device_groups:
        print("⚠️ 未能检测到 GPU 设备的所属用户组，容器可能无法访问 GPU。", file=sys.stderr)
    else:
        for gid in gpu_device_groups:
            docker_command.extend(["--group-add", str(gid)])
        print(
            f"🔐 为容器添加 GPU 相关用户组: {', '.join(str(g) for g in gpu_device_groups)}",
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
    print(f"🐳 运行 AlphaFold3 Docker: {display_command}", file=sys.stderr)
    af3_log_path = os.path.join(temp_dir, "af3_docker.log")
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
        print(f"❌ AlphaFold3 Docker 运行失败: {tail_text}", file=sys.stderr)
        raise RuntimeError(
            f"AlphaFold3 Docker run failed with exit code {return_code}. "
            f"Last output:\n{tail_text}\n"
            f"Full log: {af3_log_path}"
        )

    print(f"✅ AlphaFold3 Docker 运行完成，日志已保存: {af3_log_path}", file=sys.stderr)

    af3_output_contents = list(Path(af3_output_dir).rglob("*"))
    if not any(p.is_file() for p in af3_output_contents):
        print("⚠️ AlphaFold3 输出目录为空，可能推理未产生结果。", file=sys.stderr)

    extra_archive_files = run_af3_affinity_pipeline(
        temp_dir=temp_dir,
        yaml_data=yaml_data,
        prep=prep,
        af3_output_dir=af3_output_dir,
    )

    create_af3_archive(
        output_archive_path,
        fasta_content,
        af3_json,
        chain_msa_paths if use_msa_server else {},
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
        print("Usage: python run_single_prediction.py <args_file_path>")
        sys.exit(1)

    args_file_path = sys.argv[1]

    try:
        with open(args_file_path, 'r') as f:
            predict_args = json.load(f)

        output_archive_path = predict_args.pop("output_archive_path")
        runtime_task_id = str(predict_args.pop("task_id", "")).strip() or None
        yaml_content = predict_args.pop("yaml_content")
        backend = str(predict_args.pop("backend", "boltz")).strip().lower()
        if backend not in ("boltz", "alphafold3", "protenix"):
            raise ValueError(f"Unsupported backend '{backend}'.")
        workflow = str(predict_args.pop("workflow", "prediction")).strip().lower()
        if workflow in {"peptide", "peptide_designer", "designer"}:
            workflow = "peptide_design"
        if workflow not in {"prediction", "peptide_design"}:
            workflow = "prediction"
        peptide_design_options = predict_args.pop("peptide_design_options", {})
        if not isinstance(peptide_design_options, dict):
            peptide_design_options = {}
        peptide_design_target_chain = str(predict_args.pop("peptide_design_target_chain", "")).strip() or None
        peptide_progress_path = str(predict_args.pop("peptide_progress_path", "")).strip() or None

        model_name = predict_args.pop("model_name", None)
        seed = predict_args.pop("seed", None)
        template_inputs = predict_args.pop("template_inputs", None)

        use_msa_raw = predict_args.get("use_msa_server", True)
        if isinstance(use_msa_raw, bool):
            use_msa_server = use_msa_raw
        elif isinstance(use_msa_raw, (int, float)):
            use_msa_server = bool(use_msa_raw)
        else:
            use_msa_server = str(use_msa_raw).strip().lower() in {"1", "true", "yes", "y"}

        with tempfile.TemporaryDirectory() as temp_dir:
            processed_yaml = yaml_content
            af3_template_payloads: List[dict] = []
            if template_inputs and (backend in ("boltz", "alphafold3") or workflow == "peptide_design"):
                processed_yaml, af3_template_payloads = prepare_template_payloads(
                    yaml_content,
                    template_inputs,
                    temp_dir,
                )
            if workflow == "peptide_design":
                if backend != "boltz":
                    print(
                        f"⚠️ Peptide design currently supports Boltz backend only. Requested '{backend}', fallback to 'boltz'.",
                        file=sys.stderr,
                    )
                validate_template_paths(processed_yaml)
                run_peptide_design_backend(
                    temp_dir=temp_dir,
                    yaml_content=processed_yaml,
                    output_archive_path=output_archive_path,
                    predict_args=predict_args,
                    model_name=model_name,
                    seed=seed,
                    options=peptide_design_options,
                    target_chain_id=peptide_design_target_chain,
                    progress_path=peptide_progress_path,
                )
            elif backend == "alphafold3":
                if not af3_template_payloads:
                    af3_template_payloads = prepare_yaml_template_payloads(processed_yaml, temp_dir)
                run_alphafold3_backend(
                    temp_dir,
                    processed_yaml,
                    output_archive_path,
                    use_msa_server,
                    seed=seed,
                    template_payloads=af3_template_payloads,
                    task_id=runtime_task_id,
                )
            elif backend == "protenix":
                if template_inputs:
                    print("ℹ️ Protenix backend 当前未启用模板输入，已忽略 template_files。", file=sys.stderr)
                run_protenix_backend(
                    temp_dir=temp_dir,
                    yaml_content=processed_yaml,
                    output_archive_path=output_archive_path,
                    use_msa_server=use_msa_server,
                    seed=seed,
                    task_id=runtime_task_id,
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
                )

            if not os.path.exists(output_archive_path):
                raise FileNotFoundError(
                    f"CRITICAL ERROR: Archive not found at {output_archive_path} immediately after creation."
                )

            print(f"DEBUG: Archive successfully created at: {output_archive_path}", file=sys.stderr)

    except Exception as e:
        print(f"Error during prediction subprocess: {e}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
