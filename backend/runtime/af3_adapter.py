from __future__ import annotations

import json
import hashlib
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml

_AF3_SRC_DIR = Path(__file__).resolve().parents[2] / "AF3Score" / "src"
if _AF3_SRC_DIR.exists():
    af3_src = str(_AF3_SRC_DIR)
    if af3_src not in sys.path:
        sys.path.insert(0, af3_src)

from alphafold3.constants import residue_names as af3_residue_names


class MolType(Enum):
    RNA = ("sequence", "rna")
    DNA = ("sequence", "dna")
    CCD = ("ccdCodes", "ligand")
    SMILES = ("smiles", "ligand")

    def __init__(self, af3code: str, upperclass: str):
        self.af3code = af3code
        self.upperclass = upperclass


@dataclass
class MoleculeComponent:
    moltype: MolType
    payload: Union[str, List[str]]
    ids: List[str]
    unpaired_msa: Optional[str] = None


def _ensure_unique_label(label: str, used: Set[str]) -> str:
    candidate = label or "CHAIN"
    if candidate not in used:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in used:
        suffix += 1
    return f"{candidate}_{suffix}"


def _coerce_int(value: Union[str, int], context: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value for {context}: {value}") from exc


def _parse_bond_atom(
    atom_data: object, chain_map: Dict[str, str], known_ids: Set[str]
) -> Tuple[str, int, str]:
    if not isinstance(atom_data, (list, tuple)) or len(atom_data) != 3:
        raise ValueError(
            "Bond atom specification must be a sequence of [chain_id, residue_index, atom_name]."
        )

    chain_raw, residue_raw, atom_name = atom_data
    chain_key = str(chain_raw).strip()
    if not chain_key:
        raise ValueError("Bond atom chain ID cannot be empty.")

    resolved = chain_map.get(chain_key) or chain_map.get(chain_key.upper())
    if not resolved or resolved not in known_ids:
        raise ValueError(f"Unknown chain ID '{chain_raw}' referenced in bond constraint.")

    residue_index = _coerce_int(residue_raw, f"bond residue for chain {resolved}")
    atom_label = str(atom_name).strip()
    if not atom_label:
        raise ValueError("Bond atom name cannot be empty.")

    return (resolved, residue_index, atom_label)


class AF3Utils:
    def __init__(
        self,
        name: str,
        query_seqs_unique: List[str],
        query_seqs_cardinality: List[int],
        unpairedmsa: Optional[List[str]],
        pairedmsa: Optional[List[str]],
        sequence_id_map: Optional[Dict[str, List[str]]] = None,
        extra_molecules: Optional[List[MoleculeComponent]] = None,
        skip_msa_fields: bool = False,
        model_seeds: Optional[List[int]] = None,
        query_sequence_keys: Optional[List[str]] = None,
        query_modifications: Optional[List[List[Dict[str, object]]]] = None,
    ) -> None:
        self._id_counter = 1
        sequence_id_map = sequence_id_map or {}
        content = self.make_af3_input(
            name,
            query_seqs_unique,
            query_seqs_cardinality,
            unpairedmsa,
            pairedmsa,
            sequence_id_map,
            skip_msa_fields,
            model_seeds,
            query_sequence_keys,
            query_modifications,
        )
        if extra_molecules:
            content = self.add_extra_molecules(content, extra_molecules)
        self.content = content

    def _int_id_to_str_id(self, i: int) -> str:
        if i <= 0:
            raise ValueError(f"int_id_to_str_id: Only positive integers allowed, got {i}")
        i = i - 1  # convert to zero-based
        output = []
        while i >= 0:
            output.append(chr(i % 26 + ord("A")))
            i = i // 26 - 1
        return "".join(output)

    def make_af3_input(
        self,
        name: str,
        query_seqs_unique: List[str],
        query_seqs_cardinality: List[int],
        unpairedmsa: Optional[List[str]],
        pairedmsa: Optional[List[str]],
        sequence_id_map: Dict[str, List[str]],
        skip_msa_fields: bool,
        model_seeds: Optional[List[int]] = None,
        query_sequence_keys: Optional[List[str]] = None,
        query_modifications: Optional[List[List[Dict[str, object]]]] = None,
    ) -> Dict[str, object]:
        sequences: List[Dict[str, object]] = []
        used_ids: Set[str] = set()
        for i, query_seq in enumerate(query_seqs_unique):
            expected_copies = query_seqs_cardinality[i]
            group_key = query_sequence_keys[i] if query_sequence_keys and i < len(query_sequence_keys) else query_seq
            provided_ids = list(sequence_id_map.get(group_key, sequence_id_map.get(query_seq, [])))
            chain_ids: List[str] = []
            for cid in provided_ids[:expected_copies]:
                normalized = str(cid).strip()
                if not normalized:
                    continue
                if normalized in used_ids:
                    raise ValueError(f"Duplicate chain ID detected: {normalized}")
                used_ids.add(normalized)
                chain_ids.append(normalized)
            if len(chain_ids) < expected_copies:
                generated = self._generate_unique_ids(expected_copies - len(chain_ids), used_ids)
                chain_ids.extend(generated)
            protein_entry: Dict[str, object] = {
                "protein": {
                    "id": chain_ids,
                    "sequence": query_seq,
                    "modifications": _af3_json_modifications(query_modifications[i]) if query_modifications and i < len(query_modifications) else [],
                    "templates": [],
                }
            }
            if not skip_msa_fields:
                if unpairedmsa and unpairedmsa[i]:
                    protein_entry["protein"]["unpairedMsa"] = unpairedmsa[i]
                else:
                    protein_entry["protein"]["unpairedMsa"] = ""
                if pairedmsa and pairedmsa[i]:
                    protein_entry["protein"]["pairedMsa"] = pairedmsa[i]
                else:
                    protein_entry["protein"]["pairedMsa"] = ""
            sequences.append(protein_entry)
        self._used_ids = set().union(*(entry["protein"]["id"] for entry in sequences if "protein" in entry))
        if model_seeds and all(isinstance(seed, int) for seed in model_seeds):
            resolved_seeds = model_seeds
        else:
            resolved_seeds = [1]
        content: Dict[str, object] = {
            "dialect": "alphafold3",
            "version": 1,
            "name": f"{name}",
            "sequences": sequences,
            "modelSeeds": resolved_seeds,
            "bondedAtomPairs": None,
            "userCCD": None,
        }
        return content

    def add_extra_molecules(
        self, content: Dict[str, object], molecules: List[MoleculeComponent]
    ) -> Dict[str, object]:
        used_ids = getattr(self, "_used_ids", set())

        def ensure_unique(ids: List[str]) -> List[str]:
            result: List[str] = []
            for cid in ids:
                normalized = str(cid).strip()
                if not normalized:
                    continue
                if normalized in used_ids:
                    raise ValueError(f"Chain ID '{normalized}' assigned to multiple entities.")
                result.append(normalized)
                used_ids.add(normalized)
            return result

        for molecule in molecules:
            desired = ensure_unique(molecule.ids)
            if not desired:
                desired = self._generate_unique_ids(1, used_ids)
            moldict: Dict[str, Dict[str, object]] = {molecule.moltype.upperclass: {"id": desired}}
            payload = molecule.payload
            af3code = molecule.moltype.af3code

            if molecule.moltype == MolType.CCD:
                if isinstance(payload, list):
                    moldict[molecule.moltype.upperclass][af3code] = payload
                else:
                    moldict[molecule.moltype.upperclass][af3code] = [payload]
            else:
                moldict[molecule.moltype.upperclass][af3code] = payload
                if molecule.moltype == MolType.RNA and molecule.unpaired_msa is not None:
                    moldict[molecule.moltype.upperclass]["unpairedMsa"] = molecule.unpaired_msa

            content["sequences"].append(moldict)  # type: ignore[arg-type]

        self._used_ids = used_ids
        return content

    def _generate_unique_ids(self, count: int, used_ids: Set[str]) -> List[str]:
        generated: List[str] = []
        while len(generated) < count:
            candidate = self._int_id_to_str_id(self._id_counter)
            self._id_counter += 1
            if candidate in used_ids or candidate in generated:
                continue
            generated.append(candidate)
        used_ids.update(generated)
        return generated


@dataclass
class ProteinComponent:
    ids: List[str]
    sequence: str
    modifications: List[Dict[str, object]]


@dataclass
class AF3Preparation:
    jobname: str
    proteins: List[ProteinComponent]
    chain_id_to_sequence: Dict[str, str]
    sequence_to_chain_ids: Dict[str, List[str]]
    header_labels: List[str]
    sequence_parts: List[str]
    other_molecules: List[MoleculeComponent]
    query_sequences_unique: List[str]
    query_sequences_cardinality: List[int]
    query_sequence_keys: List[str]
    query_group_to_chain_ids: Dict[str, List[str]]
    query_modifications: List[List[Dict[str, object]]]
    chain_id_label_map: Dict[str, str]
    bond_constraints: List[Tuple[Tuple[str, int, str], Tuple[str, int, str]]]
    ignored_constraints: List[Dict[str, Any]]


def _sanitize_label(label: Optional[str], fallback: str) -> str:
    text = (label or "").strip()
    if not text:
        text = fallback
    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-"):
            safe.append(ch)
        else:
            safe.append("_")
    sanitized = "".join(safe)
    return sanitized or fallback


def _normalize_protein_modifications(raw_modifications: object, sequence_length: int) -> List[Dict[str, object]]:
    if raw_modifications in (None, ""):
        return []
    if not isinstance(raw_modifications, list):
        raise ValueError("Protein modifications must be a list of {position, ccd} entries.")

    normalized: List[Dict[str, object]] = []
    seen_positions: Set[int] = set()
    for raw in raw_modifications:
        if not isinstance(raw, dict):
            raise ValueError("Each protein modification must be an object.")
        ccd = str(raw.get("ccd") or raw.get("ptmType") or "").strip().upper()
        if not ccd:
            raise ValueError("Protein modification requires a CCD code.")
        if ccd.startswith("CCD_"):
            raise ValueError("AlphaFold3 protein ptmType must be a CCD code without the CCD_ prefix.")
        position = _coerce_int(raw.get("position", raw.get("ptmPosition")), f"protein modification {ccd} position")
        if position < 1 or position > sequence_length:
            raise ValueError(
                f"Protein modification {ccd} position {position} is outside sequence length {sequence_length}."
            )
        if position in seen_positions:
            raise ValueError(f"Multiple protein modifications target residue position {position}.")
        seen_positions.add(position)
        normalized_mod: Dict[str, object] = {"ptmType": ccd, "ptmPosition": position}
        base_residue = str(raw.get("baseResidue") or raw.get("base_residue") or "").strip().upper()[:1]
        if base_residue:
            if base_residue not in "ARNDCQEGHILKMFPSTWYV":
                raise ValueError(f"Protein modification {ccd} has invalid baseResidue: {base_residue}.")
            normalized_mod["baseResidue"] = base_residue
        normalized.append(normalized_mod)
    return normalized


def _af3_ccd_to_one_letter(ccd: object, base_residue: object = "") -> str:
    base = str(base_residue or "").strip().upper()[:1]
    if base in "ARNDCQEGHILKMFPSTWYV":
        return base
    code = str(ccd or "").strip().upper()
    if code.startswith("CCD_"):
        code = code[4:]
    if len(code) == 1 and code.isalpha():
        return code
    return af3_residue_names.CCD_NAME_TO_ONE_LETTER.get(code, "X")


def _af3_json_modifications(modifications: List[Dict[str, object]]) -> List[Dict[str, object]]:
    json_mods: List[Dict[str, object]] = []
    for mod in modifications or []:
        ptm_type = str(mod.get("ptmType") or mod.get("ccd") or "").strip().upper()
        if not ptm_type:
            continue
        ptm_position = _coerce_int(mod.get("ptmPosition", mod.get("position")), f"protein modification {ptm_type} position")
        json_mods.append({"ptmType": ptm_type, "ptmPosition": ptm_position})
    return json_mods


def _af3_effective_query_sequence(sequence: str, modifications: List[Dict[str, object]]) -> str:
    chars = list(str(sequence or "").strip().upper())
    for mod in modifications or []:
        position = _coerce_int(mod.get("ptmPosition", mod.get("position")), "AF3 modification position")
        if position < 1 or position > len(chars):
            continue
        chars[position - 1] = _af3_ccd_to_one_letter(
            mod.get("ptmType", mod.get("ccd")),
            mod.get("baseResidue", mod.get("base_residue")),
        )
    return "".join(chars)


def _protein_group_key(sequence: str, modifications: List[Dict[str, object]]) -> str:
    if not modifications:
        return sequence
    return f"{sequence}|mods={json.dumps(modifications, sort_keys=True, separators=(',', ':'))}"


def parse_yaml_for_af3(
    yaml_content: str, default_jobname: str = "boltz_af3"
) -> AF3Preparation:
    data = yaml.safe_load(yaml_content) or {}
    if "sequences" not in data or not data["sequences"]:
        raise ValueError("YAML content must contain at least one sequence entry.")

    jobname = data.get("name") or data.get("title") or default_jobname
    jobname = _sanitize_label(str(jobname), default_jobname)

    used_chain_ids: Set[str] = set()
    proteins: List[ProteinComponent] = []
    chain_id_to_sequence: Dict[str, str] = {}
    sequence_to_chain_ids: Dict[str, List[str]] = {}
    header_labels: List[str] = []
    sequence_parts: List[str] = []
    other_molecules: List[MoleculeComponent] = []
    query_sequences_unique: List[str] = []
    query_sequences_cardinality: List[int] = []
    query_sequence_keys: List[str] = []
    query_group_to_chain_ids: Dict[str, List[str]] = {}
    query_modifications: List[List[Dict[str, object]]] = []
    sequence_index_lookup: Dict[str, int] = {}
    chain_id_label_map: Dict[str, str] = {}
    bond_constraints: List[Tuple[Tuple[str, int, str], Tuple[str, int, str]]] = []
    ignored_constraints: List[Dict[str, Any]] = []

    def prepare_chain_ids(
        raw_ids: Optional[List[str]], default_prefix: str, desired_count: int
    ) -> List[str]:
        sanitized_ids: List[str] = []
        raw_list = list(raw_ids) if raw_ids else []
        desired_count = max(desired_count, 1)
        for idx in range(desired_count):
            if idx < len(raw_list):
                candidate = raw_list[idx]
            else:
                candidate = f"{default_prefix}_{idx + 1}"
            fallback = f"{default_prefix}_{idx + 1}"
            sanitized = _sanitize_label(candidate, fallback)
            sanitized = _ensure_unique_label(sanitized, used_chain_ids)
            used_chain_ids.add(sanitized)
            existing = chain_id_label_map.get(candidate)
            if existing and existing != sanitized:
                raise ValueError(f"Duplicate chain ID '{candidate}' detected in YAML input.")
            chain_id_label_map[candidate] = sanitized
            chain_id_label_map[sanitized] = sanitized
            sanitized_ids.append(sanitized)
        return sanitized_ids

    for entry in data["sequences"]:
        if not entry:
            continue
        entity_key = next(iter(entry))
        entity_type = entity_key.lower()
        info = entry[entity_key]

        raw_ids_value = info.get("id")
        if raw_ids_value is None:
            raw_ids_list: Optional[List[str]] = None
        elif isinstance(raw_ids_value, list):
            raw_ids_list = [str(x) for x in raw_ids_value]
        else:
            raw_ids_list = [str(raw_ids_value)]

        if entity_type == "protein":
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError("Protein entries must include a non-empty sequence.")

            modifications = _normalize_protein_modifications(info.get("modifications"), len(sequence))
            count = len(raw_ids_list) if raw_ids_list else 1
            sanitized_ids = prepare_chain_ids(raw_ids_list, "CHAIN", count)
            proteins.append(ProteinComponent(ids=sanitized_ids, sequence=sequence, modifications=modifications))

            group_key = _protein_group_key(sequence, modifications)
            index = sequence_index_lookup.get(group_key)
            if index is None:
                sequence_index_lookup[group_key] = len(query_sequences_unique)
                query_sequences_unique.append(sequence)
                query_sequences_cardinality.append(len(sanitized_ids))
                query_sequence_keys.append(group_key)
                query_modifications.append(modifications)
            else:
                query_sequences_cardinality[index] += len(sanitized_ids)
            query_group_to_chain_ids.setdefault(group_key, []).extend(sanitized_ids)

            for chain_id in sanitized_ids:
                header_labels.append(chain_id)
                sequence_parts.append(sequence)
                chain_id_to_sequence[chain_id] = sequence
                sequence_to_chain_ids.setdefault(sequence, []).append(chain_id)

        elif entity_type in {"dna", "rna"}:
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError(f"{entity_type.upper()} entries require a sequence.")
            moltype = MolType.DNA if entity_type == "dna" else MolType.RNA
            count = len(raw_ids_list) if raw_ids_list else 1
            sanitized_ids = prepare_chain_ids(raw_ids_list, entity_type.upper(), count)

            rna_unpaired_msa: Optional[str] = None
            if moltype == MolType.RNA:
                rna_unpaired_msa = info.get("unpairedMsa")
                if rna_unpaired_msa is None:
                    rna_unpaired_msa = info.get("msa")
                if isinstance(rna_unpaired_msa, str):
                    rna_unpaired_msa = rna_unpaired_msa.strip()
                    if not rna_unpaired_msa:
                        rna_unpaired_msa = ""

            other_molecules.append(
                MoleculeComponent(
                    moltype=moltype,
                    payload=sequence,
                    ids=sanitized_ids,
                    unpaired_msa=rna_unpaired_msa,
                )
            )
            if sanitized_ids:
                header_labels.append(sanitized_ids[0])

            seq_repr = f"{entity_type}|{sequence}"
            if len(sanitized_ids) > 1:
                seq_repr += f"|{len(sanitized_ids)}"
            sequence_parts.append(seq_repr)

            for chain_id in sanitized_ids:
                chain_id_to_sequence[chain_id] = sequence

        elif entity_type == "ligand":
            count = len(raw_ids_list) if raw_ids_list else 1
            payload: Union[str, List[str]]
            sequence_label: str

            if "ccd" in info or "ccdCodes" in info:
                ccd_value = info.get("ccd", info.get("ccdCodes"))
                if isinstance(ccd_value, list):
                    codes = [str(code).strip().upper() for code in ccd_value if str(code).strip()]
                else:
                    codes = [str(ccd_value).strip().upper()]
                if not codes:
                    raise ValueError("Ligand entries with CCD codes must be non-empty.")
                payload = codes
                sequence_label = ",".join(codes)
                seq_repr = f"ccd|{sequence_label}"
                moltype = MolType.CCD
            elif "smiles" in info:
                smiles_value = str(info["smiles"]).strip()
                if not smiles_value:
                    raise ValueError("Ligand SMILES entry must be non-empty.")
                payload = smiles_value
                sequence_label = smiles_value.replace(":", ";")
                seq_repr = f"smiles|{sequence_label}"
                moltype = MolType.SMILES
            else:
                raise ValueError("Ligand entries must define either 'ccd' (or 'ccdCodes') or 'smiles'.")

            sanitized_ids = prepare_chain_ids(raw_ids_list, "LIG", count)
            other_molecules.append(
                MoleculeComponent(moltype=moltype, payload=payload, ids=sanitized_ids)
            )
            if sanitized_ids:
                header_labels.append(sanitized_ids[0])
            if len(sanitized_ids) > 1:
                seq_repr += f"|{len(sanitized_ids)}"
            sequence_parts.append(seq_repr)
            for chain_id in sanitized_ids:
                chain_id_to_sequence[chain_id] = sequence_label

        else:
            raise ValueError(f"Unsupported entity type '{entity_type}' in YAML.")

    if not proteins:
        raise ValueError("AlphaFold3 preparation requires at least one protein entry.")

    if not query_sequences_unique:
        raise ValueError("Failed to collect protein sequences for AF3 preparation.")

    constraints_data = data.get("constraints") or []
    if constraints_data:
        known_chain_ids = set(chain_id_label_map.values())
        for constraint in constraints_data:
            if not isinstance(constraint, dict):
                continue
            if "bond" in constraint:
                bond_info = constraint.get("bond") or {}
                try:
                    atom1 = _parse_bond_atom(bond_info.get("atom1"), chain_id_label_map, known_chain_ids)
                    atom2 = _parse_bond_atom(bond_info.get("atom2"), chain_id_label_map, known_chain_ids)
                except ValueError as exc:
                    raise ValueError(f"Invalid bond constraint: {exc}") from exc
                bond_constraints.append((atom1, atom2))
            else:
                raise ValueError("This backend supports bond constraints only; remove contact/pocket constraints.")

    return AF3Preparation(
        jobname=jobname,
        proteins=proteins,
        chain_id_to_sequence=chain_id_to_sequence,
        sequence_to_chain_ids=sequence_to_chain_ids,
        header_labels=header_labels,
        sequence_parts=sequence_parts,
        other_molecules=other_molecules,
        query_sequences_unique=query_sequences_unique,
        query_sequences_cardinality=query_sequences_cardinality,
        query_sequence_keys=query_sequence_keys,
        query_group_to_chain_ids=query_group_to_chain_ids,
        query_modifications=query_modifications,
        chain_id_label_map=chain_id_label_map,
        bond_constraints=bond_constraints,
        ignored_constraints=ignored_constraints,
    )


def build_af3_fasta(prep: AF3Preparation) -> str:
    header = f">{prep.jobname}|{':'.join(prep.header_labels)}"
    sequence_line = ":".join(prep.sequence_parts)
    return f"{header}\n{sequence_line}\n"


def collect_chain_msa_paths(
    prep: AF3Preparation, temp_dir: str, cache_dir: Optional[str] = None
) -> Dict[str, Path]:
    temp_path = Path(temp_dir)
    cache_path = Path(cache_dir) if cache_dir else None
    chain_to_path: Dict[str, Path] = {}

    for chain_id, sequence in prep.chain_id_to_sequence.items():
        candidate = temp_path / f"{chain_id}_msa.a3m"
        if candidate.exists():
            chain_to_path[chain_id] = candidate
            continue

        if cache_path:
            hash_path = cache_path / f"msa_{_sequence_hash(sequence)}.a3m"
            if hash_path.exists():
                chain_to_path[chain_id] = hash_path

    return chain_to_path


def _sequence_hash(sequence: str) -> str:
    return hashlib.md5(sequence.encode("utf-8")).hexdigest()


def _count_non_lowercase(seq: str) -> int:
    return sum(1 for ch in seq if not ch.islower())


def _normalize_a3m_content(a3m_text: str) -> str:
    """Pad ragged A3M rows so non-lowercase lengths match the longest row."""
    header = None
    seq_chunks = []
    entries = []
    changed = False
    for line in (a3m_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):  # header line
            if header is not None:
                entries.append((header, "".join(seq_chunks)))
            header = line
            seq_chunks = []
        else:
            seq_chunks.append(line)
    if header is not None:
        entries.append((header, "".join(seq_chunks)))
    if not entries:
        return a3m_text
    target = max(_count_non_lowercase(seq) for _, seq in entries)
    fixed = []
    for hdr, seq in entries:
        count = _count_non_lowercase(seq)
        if count < target:
            seq = seq + ("-" * (target - count))
            changed = True
        fixed.append(f"{hdr}\n{seq}")
    return ("\n".join(fixed) + "\n") if changed else a3m_text


def _replace_a3m_first_query_sequence(a3m_text: str, query_sequence: str) -> str:
    """Make the first A3M row match AF3's effective query sequence after PTMs."""
    query = str(query_sequence or "").strip().upper()
    if not query:
        return a3m_text

    lines = (a3m_text or "").splitlines()
    first_header_index: Optional[int] = None
    first_sequence_start: Optional[int] = None
    first_sequence_end: Optional[int] = None
    for idx, line in enumerate(lines):
        if line.startswith(">"):
            if first_header_index is None:
                first_header_index = idx
                first_sequence_start = idx + 1
                continue
            first_sequence_end = idx
            break
    if first_header_index is None or first_sequence_start is None:
        return a3m_text
    if first_sequence_end is None:
        first_sequence_end = len(lines)

    original_seq = "".join(line.strip() for line in lines[first_sequence_start:first_sequence_end] if line.strip())
    if not original_seq or original_seq == query:
        return a3m_text

    if _count_non_lowercase(original_seq) == len(query):
        query_iter = iter(query)
        rewritten_seq = "".join(ch if ch.islower() else next(query_iter) for ch in original_seq)
    else:
        rewritten_seq = query

    next_lines = lines[:first_sequence_start] + [rewritten_seq] + lines[first_sequence_end:]
    return "\n".join(next_lines).rstrip("\n") + "\n"


def load_unpaired_msa(
    prep: AF3Preparation, chain_msa_paths: Dict[str, Path]
) -> List[str]:
    unpaired: List[str] = []
    for index, sequence in enumerate(prep.query_sequences_unique):
        msa_content: Optional[str] = None
        modifications = prep.query_modifications[index] if index < len(prep.query_modifications) else []
        group_key = prep.query_sequence_keys[index] if index < len(prep.query_sequence_keys) else sequence
        chain_ids = prep.query_group_to_chain_ids.get(group_key) or prep.sequence_to_chain_ids.get(sequence, [])
        for chain_id in chain_ids:
            path = chain_msa_paths.get(chain_id)
            if path and path.exists():
                msa_content = _replace_a3m_first_query_sequence(
                    _normalize_a3m_content(path.read_text()),
                    _af3_effective_query_sequence(sequence, modifications),
                )
                break
        unpaired.append(msa_content or "")
    return unpaired


def build_af3_json(
    prep: AF3Preparation,
    unpaired_msa: Optional[List[str]],
    use_external_msa: bool = True,
    model_seeds: Optional[List[int]] = None,
    user_ccd: Optional[str] = None,
) -> Dict[str, object]:
    af3 = AF3Utils(
        prep.jobname,
        prep.query_sequences_unique,
        prep.query_sequences_cardinality,
        unpaired_msa if use_external_msa else None,
        None,
        prep.query_group_to_chain_ids or prep.sequence_to_chain_ids,
        prep.other_molecules,
        skip_msa_fields=not use_external_msa,
        model_seeds=model_seeds,
        query_sequence_keys=prep.query_sequence_keys,
        query_modifications=prep.query_modifications,
    )
    content = af3.content
    content["userCCD"] = user_ccd if user_ccd else None
    if prep.bond_constraints:
        content["bondedAtomPairs"] = [
            [
                [atom1[0], atom1[1], atom1[2]],
                [atom2[0], atom2[1], atom2[2]],
            ]
            for atom1, atom2 in prep.bond_constraints
        ]
    else:
        content["bondedAtomPairs"] = None
    return content


def serialize_af3_json(content: Dict[str, object]) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False)


def safe_filename(name: str) -> str:
    return _sanitize_label(name, "file")
