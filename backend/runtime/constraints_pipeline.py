"""平台 yaml 约束管线：链冲突归一、唯一性校验、链长清洗、
模板对齐重映射与残基摘要。供各后端 post_validate 使用。"""
from __future__ import annotations

import re
from pathlib import Path

import gemmi
import yaml






def validate_unique_sequence_chain_ids(yaml_content: str) -> None:
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return
    if not isinstance(yaml_data, dict):
        return
    from backend.runtime.run_single_prediction import _extract_sequence_chain_types_from_yaml
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




def normalize_ligand_chain_collisions(yaml_content: str) -> str:
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




def sanitize_constraints_for_chain_lengths(yaml_content: str) -> str:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        raise ValueError("constraint sanitization expects a YAML mapping at the top level.")
    constraints = yaml_data.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        return yaml_content

    _extract_sequence_chain_types_from_yaml(yaml_data)
    from backend.runtime.run_single_prediction import _extract_protein_chain_lengths_from_yaml
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




def remap_constraints_by_template_alignment(yaml_content: str) -> str:
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




def print_constraint_residue_summary(yaml_content: str) -> None:
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


