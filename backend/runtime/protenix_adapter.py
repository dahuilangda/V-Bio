from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml


_ELEMENT_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,2}$")


@dataclass
class ChainAssignment:
    chain_id: str
    entity: int
    copy_index: int
    entity_kind: str


@dataclass
class ProtenixPreparation:
    input_name: str
    payload: List[Dict[str, Any]]
    chain_assignments: Dict[str, ChainAssignment]
    chain_alias_map: Dict[str, str]
    entity_chain_ids: Dict[int, List[str]]
    entity_kinds: Dict[int, str]
    entity_seq_positions: Dict[int, int]
    ignored_constraints: List[Dict[str, Any]]


def _ensure_unique_label(label: str, used: set[str]) -> str:
    candidate = label or "CHAIN"
    if candidate not in used:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in used:
        suffix += 1
    return f"{candidate}_{suffix}"


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


def _normalize_ids(raw_ids_value: Any) -> Optional[List[str]]:
    if raw_ids_value is None:
        return None
    if isinstance(raw_ids_value, list):
        return [str(item) for item in raw_ids_value]
    return [str(raw_ids_value)]


def _coerce_positive_int(value: Any, context: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer for {context}: {value}") from exc
    if parsed <= 0:
        raise ValueError(f"{context} must be > 0, got {parsed}")
    return parsed


def _normalize_ion_symbol(smiles_value: str) -> Optional[str]:
    text = str(smiles_value).strip()
    match = re.fullmatch(r"\[([A-Za-z]{1,2})\]", text)
    if not match:
        return None
    symbol = match.group(1)
    if len(symbol) == 1:
        return symbol.upper()
    return symbol[0].upper() + symbol[1].lower()


def _normalize_protein_modifications(raw_mods: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_mods, list):
        return []
    mods: List[Dict[str, Any]] = []
    for item in raw_mods:
        if not isinstance(item, dict):
            continue
        ptm_type = (
            item.get("ptmType")
            or item.get("ccd")
            or item.get("ccdCode")
            or item.get("modification")
            or item.get("modificationType")
        )
        ptm_pos = item.get("ptmPosition")
        if ptm_pos is None:
            ptm_pos = item.get("position")
        if ptm_pos is None:
            ptm_pos = item.get("residuePosition")
        if ptm_type is None or ptm_pos is None:
            continue
        ptm_type_text = str(ptm_type).strip()
        if not ptm_type_text:
            continue
        mods.append(
            {
                "ptmType": ptm_type_text,
                "ptmPosition": _coerce_positive_int(ptm_pos, "protein modification position"),
            }
        )
    return mods


def _normalize_nucleic_modifications(raw_mods: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_mods, list):
        return []
    mods: List[Dict[str, Any]] = []
    for item in raw_mods:
        if not isinstance(item, dict):
            continue
        mod_type = item.get("modificationType") or item.get("modification") or item.get("ccd")
        base_pos = item.get("basePosition")
        if base_pos is None:
            base_pos = item.get("position")
        if base_pos is None:
            base_pos = item.get("residuePosition")
        if mod_type is None or base_pos is None:
            continue
        mod_type_text = str(mod_type).strip()
        if not mod_type_text:
            continue
        mods.append(
            {
                "modificationType": mod_type_text,
                "basePosition": _coerce_positive_int(base_pos, "nucleic acid modification position"),
            }
        )
    return mods


def _parse_bond_atom(
    atom_data: Any,
    chain_alias_map: Dict[str, str],
    chain_assignments: Dict[str, ChainAssignment],
) -> Tuple[ChainAssignment, int, str]:
    if not isinstance(atom_data, (list, tuple)) or len(atom_data) != 3:
        raise ValueError(
            "Bond atom specification must be [chain_id, residue_index, atom_name]."
        )

    chain_raw, residue_raw, atom_name = atom_data
    chain_key = str(chain_raw).strip()
    if not chain_key:
        raise ValueError("Bond atom chain ID cannot be empty.")

    resolved = (
        chain_alias_map.get(chain_key)
        or chain_alias_map.get(chain_key.upper())
        or chain_alias_map.get(chain_key.lower())
    )
    if not resolved:
        raise ValueError(f"Unknown chain ID '{chain_raw}' referenced in bond constraint.")

    assignment = chain_assignments.get(resolved)
    if assignment is None:
        raise ValueError(f"Unknown chain ID '{chain_raw}' referenced in bond constraint.")

    residue_index = _coerce_positive_int(residue_raw, f"bond residue for chain {resolved}")
    atom_label = str(atom_name).strip()
    if not atom_label:
        raise ValueError("Bond atom name cannot be empty.")

    return assignment, residue_index, atom_label


def _build_covalent_bond_payload(
    assignment1: ChainAssignment,
    residue1: int,
    atom1: str,
    assignment2: ChainAssignment,
    residue2: int,
    atom2: str,
) -> Dict[str, Any]:
    return {
        "entity1": assignment1.entity,
        "copy1": assignment1.copy_index,
        "position1": residue1,
        "atom1": atom1,
        "entity2": assignment2.entity,
        "copy2": assignment2.copy_index,
        "position2": residue2,
        "atom2": atom2,
    }


def parse_yaml_for_protenix(
    yaml_content: str,
    default_input_name: str = "boltz_protenix",
) -> ProtenixPreparation:
    data = yaml.safe_load(yaml_content) or {}
    if "sequences" not in data or not data["sequences"]:
        raise ValueError("YAML content must contain at least one sequence entry.")

    input_name = data.get("name") or data.get("title") or default_input_name
    input_name = _sanitize_label(str(input_name), default_input_name)

    used_chain_ids: set[str] = set()
    chain_assignments: Dict[str, ChainAssignment] = {}
    chain_alias_map: Dict[str, str] = {}
    entity_chain_ids: Dict[int, List[str]] = {}
    entity_kinds: Dict[int, str] = {}
    entity_seq_positions: Dict[int, int] = {}

    sequences_payload: List[Dict[str, Any]] = []
    entity_index = 0

    def prepare_chain_ids(raw_ids: Optional[List[str]], default_prefix: str, desired_count: int) -> List[str]:
        ids: List[str] = []
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
            raw_text = str(candidate).strip()
            if raw_text:
                chain_alias_map[raw_text] = sanitized
                chain_alias_map[raw_text.upper()] = sanitized
                chain_alias_map[raw_text.lower()] = sanitized
            chain_alias_map[sanitized] = sanitized
            chain_alias_map[sanitized.upper()] = sanitized
            chain_alias_map[sanitized.lower()] = sanitized
            ids.append(sanitized)
        return ids

    for entry in data["sequences"]:
        if not isinstance(entry, dict) or not entry:
            continue
        entity_key = next(iter(entry))
        entity_type = str(entity_key).lower()
        info = entry.get(entity_key) or {}
        if not isinstance(info, dict):
            raise ValueError(f"Invalid payload for '{entity_key}' entry; expected object.")

        raw_ids_list = _normalize_ids(info.get("id"))
        count = len(raw_ids_list) if raw_ids_list else 1

        entity_index += 1

        if entity_type == "protein":
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError("Protein entries must include a non-empty sequence.")
            chain_ids = prepare_chain_ids(raw_ids_list, "CHAIN", count)
            payload: Dict[str, Any] = {
                "sequence": sequence,
                "count": len(chain_ids),
            }
            raw_msa = info.get("msa")
            if raw_msa is not None:
                if isinstance(raw_msa, dict):
                    legacy_msa = dict(raw_msa)
                    if legacy_msa:
                        payload["msa"] = legacy_msa
                else:
                    msa_text = str(raw_msa).strip()
                    if msa_text and msa_text.lower() != "empty":
                        payload["unpairedMsaPath"] = msa_text
            mods = _normalize_protein_modifications(info.get("modifications"))
            if mods:
                payload["modifications"] = mods
            sequences_payload.append({"proteinChain": payload})
            entity_kinds[entity_index] = "protein"

        elif entity_type == "dna":
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError("DNA entries must include a non-empty sequence.")
            chain_ids = prepare_chain_ids(raw_ids_list, "DNA", count)
            payload = {
                "sequence": sequence,
                "count": len(chain_ids),
            }
            mods = _normalize_nucleic_modifications(info.get("modifications"))
            if mods:
                payload["modifications"] = mods
            sequences_payload.append({"dnaSequence": payload})
            entity_kinds[entity_index] = "dna"

        elif entity_type == "rna":
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError("RNA entries must include a non-empty sequence.")
            chain_ids = prepare_chain_ids(raw_ids_list, "RNA", count)
            payload = {
                "sequence": sequence,
                "count": len(chain_ids),
            }
            mods = _normalize_nucleic_modifications(info.get("modifications"))
            if mods:
                payload["modifications"] = mods
            sequences_payload.append({"rnaSequence": payload})
            entity_kinds[entity_index] = "rna"

        elif entity_type == "ligand":
            chain_ids = prepare_chain_ids(raw_ids_list, "LIG", count)
            ligand_text: Optional[str] = None
            ligand_from_ccd = "ccd" in info or "ccdCodes" in info
            if ligand_from_ccd:
                ccd_value = info.get("ccd", info.get("ccdCodes"))
                if isinstance(ccd_value, list):
                    raw_codes = [str(code).strip().upper() for code in ccd_value if str(code).strip()]
                else:
                    raw_codes = [str(ccd_value).strip().upper()]
                codes: List[str] = []
                for code in raw_codes:
                    code_text = code[4:] if code.startswith("CCD_") else code
                    if code_text:
                        codes.append(code_text)
                if not codes:
                    raise ValueError("Ligand CCD code must be non-empty.")
                ligand_text = f"CCD_{'_'.join(codes)}"
            elif "smiles" in info:
                ligand_text = str(info.get("smiles", "")).strip()
            if not ligand_text:
                raise ValueError("Ligand entries must define either non-empty 'ccd' or 'smiles'.")

            ion_symbol = None
            if not ligand_from_ccd:
                ion_symbol = _normalize_ion_symbol(ligand_text)
                if ion_symbol:
                    ion_symbol = ion_symbol.upper()
            if ion_symbol and _ELEMENT_SYMBOL_RE.match(ion_symbol):
                sequences_payload.append({"ion": {"ion": ion_symbol, "count": len(chain_ids)}})
                entity_kinds[entity_index] = "ion"
            else:
                sequences_payload.append({"ligand": {"ligand": ligand_text, "count": len(chain_ids)}})
                entity_kinds[entity_index] = "ligand"

        else:
            raise ValueError(f"Unsupported entity type '{entity_type}' in YAML for Protenix backend.")

        entity_chain_ids[entity_index] = list(chain_ids)
        entity_seq_positions[entity_index] = len(sequences_payload) - 1
        for copy_idx, chain_id in enumerate(chain_ids, start=1):
            chain_assignments[chain_id] = ChainAssignment(
                chain_id=chain_id,
                entity=entity_index,
                copy_index=copy_idx,
                entity_kind=entity_kinds[entity_index],
            )

    if not sequences_payload:
        raise ValueError("Failed to parse any valid sequence entries for Protenix input.")

    covalent_bonds: List[Dict[str, Any]] = []
    ignored_constraints: List[Dict[str, Any]] = []

    constraints_data = data.get("constraints") or []
    if constraints_data:
        for constraint in constraints_data:
            if not isinstance(constraint, dict):
                continue
            if "bond" in constraint:
                bond_info = constraint.get("bond") or {}
                atom1_info = bond_info.get("atom1")
                atom2_info = bond_info.get("atom2")
                assignment1, residue1, atom1 = _parse_bond_atom(atom1_info, chain_alias_map, chain_assignments)
                assignment2, residue2, atom2 = _parse_bond_atom(atom2_info, chain_alias_map, chain_assignments)
                covalent_bonds.append(
                    _build_covalent_bond_payload(
                        assignment1,
                        residue1,
                        atom1,
                        assignment2,
                        residue2,
                        atom2,
                    )
                )
            else:
                ignored_constraints.append(constraint)

    payload: List[Dict[str, Any]] = [{
        "name": input_name,
        "sequences": sequences_payload,
        "covalent_bonds": covalent_bonds,
    }]

    return ProtenixPreparation(
        input_name=input_name,
        payload=payload,
        chain_assignments=chain_assignments,
        chain_alias_map=chain_alias_map,
        entity_chain_ids=entity_chain_ids,
        entity_kinds=entity_kinds,
        entity_seq_positions=entity_seq_positions,
        ignored_constraints=ignored_constraints,
    )


def apply_protein_msa_paths(
    prep: ProtenixPreparation,
    chain_msa_paths: Dict[str, str],
) -> int:
    if not prep.payload:
        return 0
    root_item = prep.payload[0]
    sequences = root_item.get("sequences", []) if isinstance(root_item, dict) else []

    assigned = 0
    for entity_index, chain_ids in prep.entity_chain_ids.items():
        if prep.entity_kinds.get(entity_index) != "protein":
            continue
        seq_pos = prep.entity_seq_positions.get(entity_index)
        if seq_pos is None or seq_pos < 0 or seq_pos >= len(sequences):
            continue
        entry = sequences[seq_pos]
        protein = entry.get("proteinChain") if isinstance(entry, dict) else None
        if not isinstance(protein, dict):
            continue

        protein.pop("pairedMsaPath", None)
        protein.pop("unpairedMsaPath", None)
        protein.pop("msa", None)

        selected_path: Optional[str] = None
        for chain_id in chain_ids:
            candidate = chain_msa_paths.get(chain_id)
            if candidate:
                selected_path = str(candidate)
                break

        if selected_path:
            protein["unpairedMsaPath"] = selected_path
        if selected_path:
            assigned += 1
    return assigned


def serialize_protenix_json(content: Any) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False)
