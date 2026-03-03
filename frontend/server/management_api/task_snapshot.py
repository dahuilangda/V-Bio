from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import yaml

from management_api.runtime_proxy import read_upload_text


AFFINITY_TARGET_UPLOAD_COMPONENT_ID = "__affinity_target_upload__"
AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = "__affinity_ligand_upload__"


def _normalize_chain_id_list(value: Any) -> List[str]:
    if isinstance(value, str):
        chain_id = value.strip()
        return [chain_id] if chain_id else []
    if isinstance(value, list):
        output: List[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            chain_id = item.strip()
            if not chain_id or chain_id in seen:
                continue
            seen.add(chain_id)
            output.append(chain_id)
        return output
    return []


def _to_positive_int(value: Any, fallback: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _parse_prediction_properties(raw: Any) -> Dict[str, Any]:
    default: Dict[str, Any] = {
        "affinity": False,
        "target": None,
        "ligand": None,
        "binder": None,
    }
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        entries = [raw]
    elif isinstance(raw, list):
        entries = [item for item in raw if isinstance(item, dict)]
    if not entries:
        return default

    affinity_entry: Optional[Dict[str, Any]] = None
    for entry in entries:
        nested = entry.get("affinity")
        if isinstance(nested, dict):
            affinity_entry = nested
            break
    if affinity_entry is None:
        return default

    binder = str(affinity_entry.get("binder") or "").strip() or None
    target = str(affinity_entry.get("target") or "").strip() or None
    ligand = str(affinity_entry.get("ligand") or "").strip() or binder
    affinity_flag = bool(binder)

    return {
        "affinity": affinity_flag,
        "target": target,
        "ligand": ligand,
        "binder": binder,
    }


def _parse_prediction_constraints(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    output: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue

        contact = item.get("contact")
        if isinstance(contact, dict):
            token1 = contact.get("token1")
            token2 = contact.get("token2")
            token1_chain = token1[0] if isinstance(token1, list) and token1 else "A"
            token2_chain = token2[0] if isinstance(token2, list) and token2 else "B"
            token1_residue = token1[1] if isinstance(token1, list) and len(token1) > 1 else 1
            token2_residue = token2[1] if isinstance(token2, list) and len(token2) > 1 else 1
            output.append(
                {
                    "id": f"yaml-contact-{idx + 1}",
                    "type": "contact",
                    "token1_chain": str(token1_chain or "A").strip() or "A",
                    "token1_residue": _to_positive_int(token1_residue, 1),
                    "token2_chain": str(token2_chain or "B").strip() or "B",
                    "token2_residue": _to_positive_int(token2_residue, 1),
                    "max_distance": max(1, _to_positive_int(contact.get("max_distance"), 5)),
                    "force": bool(contact.get("force", True)),
                }
            )
            continue

        bond = item.get("bond")
        if isinstance(bond, dict):
            atom1 = bond.get("atom1")
            atom2 = bond.get("atom2")
            atom1_chain = atom1[0] if isinstance(atom1, list) and atom1 else "A"
            atom2_chain = atom2[0] if isinstance(atom2, list) and atom2 else "B"
            atom1_residue = atom1[1] if isinstance(atom1, list) and len(atom1) > 1 else 1
            atom2_residue = atom2[1] if isinstance(atom2, list) and len(atom2) > 1 else 1
            atom1_atom = atom1[2] if isinstance(atom1, list) and len(atom1) > 2 else "CA"
            atom2_atom = atom2[2] if isinstance(atom2, list) and len(atom2) > 2 else "CA"
            output.append(
                {
                    "id": f"yaml-bond-{idx + 1}",
                    "type": "bond",
                    "atom1_chain": str(atom1_chain or "A").strip() or "A",
                    "atom1_residue": _to_positive_int(atom1_residue, 1),
                    "atom1_atom": str(atom1_atom or "CA").strip() or "CA",
                    "atom2_chain": str(atom2_chain or "B").strip() or "B",
                    "atom2_residue": _to_positive_int(atom2_residue, 1),
                    "atom2_atom": str(atom2_atom or "CA").strip() or "CA",
                }
            )
            continue

        pocket = item.get("pocket")
        if not isinstance(pocket, dict):
            continue
        contacts_raw = pocket.get("contacts")
        contacts: List[List[Any]] = []
        if isinstance(contacts_raw, list):
            for contact_item in contacts_raw:
                if not isinstance(contact_item, list) or len(contact_item) < 2:
                    continue
                chain_id = str(contact_item[0] or "").strip()
                if not chain_id:
                    continue
                contacts.append([chain_id, _to_positive_int(contact_item[1], 1)])
        if not contacts:
            continue
        binder = str(pocket.get("binder") or "").strip() or "A"
        output.append(
            {
                "id": f"yaml-pocket-{idx + 1}",
                "type": "pocket",
                "binder": binder,
                "contacts": contacts,
                "max_distance": max(1, _to_positive_int(pocket.get("max_distance"), 6)),
                "force": bool(pocket.get("force", True)),
            }
        )

    return output


def parse_bool_form(request_obj: Any, field: str, default: bool = False) -> bool:
    raw = request_obj.form.get(field)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def parse_ligand_smiles_map_from_form(request_obj: Any) -> Dict[str, str]:
    raw = (request_obj.form.get("ligand_smiles_map") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}

    output: Dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        chain_id = key.strip()
        smiles = value.strip()
        if chain_id and smiles:
            output[chain_id] = smiles
    return output


def build_prediction_task_snapshot_from_yaml(request_obj: Any, logger: Any) -> Dict[str, Any]:
    yaml_upload = request_obj.files.get("yaml_file")
    yaml_text = read_upload_text(yaml_upload)
    if not yaml_text.strip():
        return {}

    try:
        yaml_data = yaml.safe_load(yaml_text) or {}
    except Exception:
        logger.warning("Failed to parse submitted yaml_file for task snapshot backfill")
        return {}
    if not isinstance(yaml_data, dict):
        return {}

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        sequences = []

    components: List[Dict[str, Any]] = []
    first_protein_sequence = ""
    first_ligand_sequence = ""

    for index, sequence_item in enumerate(sequences):
        if not isinstance(sequence_item, dict):
            continue
        entry_type: Optional[str] = None
        entry_value: Optional[Dict[str, Any]] = None
        for candidate in ("protein", "dna", "rna", "ligand"):
            value = sequence_item.get(candidate)
            if isinstance(value, dict):
                entry_type = candidate
                entry_value = value
                break
        if not entry_type or not isinstance(entry_value, dict):
            continue

        chain_ids = _normalize_chain_id_list(entry_value.get("id"))
        num_copies = len(chain_ids) if chain_ids else 1
        component_id = f"yaml-{entry_type}-{index + 1}"

        if entry_type == "ligand":
            smiles = str(entry_value.get("smiles") or "").strip()
            ccd = str(entry_value.get("ccd") or "").strip()
            sequence = smiles or ccd
            input_method = "smiles" if smiles else "ccd" if ccd else "smiles"
            component = {
                "id": component_id,
                "type": "ligand",
                "numCopies": max(1, num_copies),
                "sequence": sequence,
                "inputMethod": input_method,
            }
            components.append(component)
            if sequence and not first_ligand_sequence:
                first_ligand_sequence = sequence
            continue

        sequence = "".join(str(entry_value.get("sequence") or "").split())
        component = {
            "id": component_id,
            "type": entry_type,
            "numCopies": max(1, num_copies),
            "sequence": sequence,
        }
        if entry_type == "protein":
            component["cyclic"] = bool(entry_value.get("cyclic", False))
            msa_value = entry_value.get("msa")
            component["useMsa"] = not (isinstance(msa_value, str) and msa_value.strip().lower() == "empty")
            if sequence and not first_protein_sequence:
                first_protein_sequence = sequence
        components.append(component)

    if not components:
        return {}

    properties = _parse_prediction_properties(yaml_data.get("properties"))
    constraints = _parse_prediction_constraints(yaml_data.get("constraints"))

    return {
        "protein_sequence": first_protein_sequence,
        "ligand_smiles": first_ligand_sequence,
        "components": components,
        "constraints": constraints,
        "properties": properties,
        "confidence": {},
        "affinity": {},
        "structure_name": "",
    }


def build_affinity_task_snapshot(request_obj: Any, upstream_path: str) -> Dict[str, Any]:
    if upstream_path != "/api/boltz2score":
        return {}

    target_chain = (request_obj.form.get("target_chain") or "").strip()
    ligand_chain = (request_obj.form.get("ligand_chain") or "").strip()
    ligand_smiles = (request_obj.form.get("ligand_smiles") or "").strip()
    ligand_smiles_map = parse_ligand_smiles_map_from_form(request_obj)

    if not ligand_smiles and ligand_chain and ligand_chain in ligand_smiles_map:
        ligand_smiles = ligand_smiles_map[ligand_chain]
    if not ligand_smiles and ligand_smiles_map:
        ligand_smiles = next(iter(ligand_smiles_map.values()))

    enable_affinity = parse_bool_form(request_obj, "enable_affinity", False)
    activity_enabled = bool(enable_affinity and target_chain and ligand_chain and ligand_smiles)
    properties: Dict[str, Any] = {
        "affinity": activity_enabled,
        "target": target_chain or None,
        "ligand": ligand_chain or None,
        "binder": ligand_chain or None,
    }

    components: List[Dict[str, Any]] = []
    if upstream_path == "/api/boltz2score":
        protein_upload = request_obj.files.get("protein_file")
        ligand_upload = request_obj.files.get("ligand_file")
        if protein_upload and protein_upload.filename:
            components.append(
                {
                    "id": AFFINITY_TARGET_UPLOAD_COMPONENT_ID,
                    "type": "protein",
                    "numCopies": 1,
                    "sequence": "",
                    "useMsa": False,
                    "cyclic": False,
                    "affinityUpload": {
                        "role": "target",
                        "fileName": str(protein_upload.filename),
                        "content": read_upload_text(protein_upload),
                    },
                }
            )
        if ligand_upload and ligand_upload.filename:
            components.append(
                {
                    "id": AFFINITY_LIGAND_UPLOAD_COMPONENT_ID,
                    "type": "ligand",
                    "numCopies": 1,
                    "sequence": ligand_smiles,
                    "inputMethod": "jsme",
                    "affinityUpload": {
                        "role": "ligand",
                        "fileName": str(ligand_upload.filename),
                        "content": read_upload_text(ligand_upload),
                    },
                }
            )

    return {
        "protein_sequence": "",
        "ligand_smiles": ligand_smiles,
        "components": components,
        "constraints": [],
        "properties": properties,
        "confidence": {},
        "affinity": {},
        "structure_name": "",
    }


def read_seed(request_obj: Any, backend: str = "", default_protenix_predict_seed: int = 42) -> Optional[int]:
    seed_raw = (request_obj.form.get("seed") or "").strip()
    if not seed_raw:
        if str(backend).strip().lower() == "protenix":
            return int(default_protenix_predict_seed)
        return None
    try:
        return int(seed_raw)
    except ValueError:
        return None


def read_task_name(request_obj: Any, default_task_id: str) -> str:
    name = (request_obj.form.get("task_name") or "").strip()
    if name:
        return name
    return f"Task {default_task_id[:8]}"


def read_task_summary(request_obj: Any) -> str:
    return (request_obj.form.get("task_summary") or "").strip()
