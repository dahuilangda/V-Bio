from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from management_api.runtime_proxy import read_upload_text
from management_api.screening_library import parse_screening_compounds_file

logger = logging.getLogger(__name__)


AFFINITY_TARGET_UPLOAD_COMPONENT_ID = "__affinity_target_upload__"
AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = "__affinity_ligand_upload__"
TASK_INPUT_OPTIONS_KEY = "__vbio_input_options_v1"

_RESIDUE_SPEC_PATTERN = re.compile(r"^([A-Za-z]+):(\d+)$")


def _pocket_centroid_from_residues(protein_text: str, residue_specs: str) -> Optional[Tuple[float, float, float]]:
    """Cα centroid of the specified residues — mirrors the capability's
    extract_pocket_center_from_residues (first model, chain names upper-cased) so the
    snapshot's pocket box matches what the run actually used."""
    try:
        import gemmi
    except ImportError:
        return None
    specs: set[Tuple[str, int]] = set()
    for token in str(residue_specs).split(","):
        token = token.strip()
        if not token:
            continue
        match = _RESIDUE_SPEC_PATTERN.match(token)
        if not match:
            return None
        specs.add((match.group(1).upper(), int(match.group(2))))
    if not specs:
        return None
    try:
        structure = gemmi.read_pdb_string(protein_text)
        if len(structure) == 0:
            return None
        coords: List[Tuple[float, float, float]] = []
        found: set[Tuple[str, int]] = set()
        for chain in structure[0]:
            chain_name = chain.name.strip().upper()
            for residue in chain:
                key = (chain_name, residue.seqid.num)
                if key in specs and key not in found:
                    for atom in residue:
                        if atom.name.strip().upper() == "CA":
                            coords.append((atom.pos.x, atom.pos.y, atom.pos.z))
                            found.add(key)
                            break
        if not coords or found != specs:
            # The capability hard-errors on missing residues, so a successful run always has
            # them all; anything else here means the snapshot cannot mirror the run.
            return None
        count = len(coords)
        return tuple(round(sum(coord[axis] for coord in coords) / count, 3) for axis in range(3))  # type: ignore[return-value]
    except Exception:
        return None


def _pocket_centroid_from_ligand(ligand_text: str, filename: str) -> Optional[Tuple[float, float, float]]:
    """Heavy-atom centroid of a reference ligand — mirrors extract_pocket_center_from_ligand."""
    try:
        from rdkit import Chem
    except ImportError:
        return None
    suffix = "." + str(filename).rsplit(".", 1)[-1].lower() if "." in str(filename) else ""
    molecule = None
    try:
        if suffix in (".sdf", ".sd", ".mol"):
            # First molecule block of the (possibly multi-record) SDF, like SDMolSupplier's first hit.
            molecule = Chem.MolFromMolBlock(ligand_text.split("$$$$")[0], removeHs=True)
        elif suffix == ".mol2":
            molecule = Chem.MolFromMol2Block(ligand_text, removeHs=True)
        elif suffix == ".pdb":
            molecule = Chem.MolFromPDBBlock(ligand_text, removeHs=True)
    except Exception:
        return None
    if molecule is None or molecule.GetNumAtoms() == 0:
        return None
    conformer = molecule.GetConformer()
    count = molecule.GetNumAtoms()
    positions = [(p.x, p.y, p.z) for p in (conformer.GetAtomPosition(i) for i in range(count))]
    return tuple(round(sum(pos[axis] for pos in positions) / count, 3) for axis in range(3))  # type: ignore[return-value]


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

    affinity_requested = False
    binder: Optional[str] = None
    target: Optional[str] = None
    ligand: Optional[str] = None
    for entry in entries:
        nested = entry.get("affinity")
        if isinstance(nested, dict):
            affinity_requested = True
            if binder is None:
                binder = str(nested.get("binder") or "").strip() or None
            if target is None:
                target = str(nested.get("target") or "").strip() or None
            if ligand is None:
                ligand = str(nested.get("ligand") or "").strip() or None
        elif nested is True:
            affinity_requested = True

        # V-Bio's YAML builder emits binder/ligand/target beside
        # ``affinity: true``. Nesso inputs may also keep target metadata in a
        # separate properties entry, so collect the first non-empty value
        # across the complete list instead of returning at the nested entry.
        if binder is None:
            binder = str(entry.get("binder") or "").strip() or None
        if target is None:
            target = str(entry.get("target") or "").strip() or None
        if ligand is None:
            ligand = str(entry.get("ligand") or "").strip() or None

    binder = binder or ligand
    ligand = ligand or binder
    affinity_flag = bool(affinity_requested and binder)

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

    # A library uploaded as compounds_file is merged into the runtime's YAML before the
    # task runs; mirror that here so the snapshot reflects what was actually submitted.
    # Both inline and file libraries are rejected by the runtime, so only one source
    # ever reaches this merge.
    compounds_upload = getattr(request_obj, "files", {}).get("compounds_file")
    if compounds_upload is not None and str(compounds_upload.filename or "").strip():
        try:
            # Strict UTF-8 like the runtime route (read_upload_text is lenient with
            # replacement chars, which would fabricate compounds the runtime rejects).
            raw = compounds_upload.read()
            compounds_text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            if isinstance(yaml_data.get("virtual_screening"), dict) and isinstance(
                yaml_data["virtual_screening"].get("compounds"), list
            ):
                inline = [
                    item
                    for item in yaml_data["virtual_screening"]["compounds"]
                    if isinstance(item, dict) and str(item.get("smiles") or "").strip()
                ]
                if inline:
                    raise ValueError("compound library provided both inline and as compounds_file")
            screening_section = yaml_data.get("virtual_screening")
            if not isinstance(screening_section, dict):
                screening_section = {}
                yaml_data["virtual_screening"] = screening_section
            screening_section["compounds"] = parse_screening_compounds_file(compounds_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse compounds_file for task snapshot backfill: %s", exc)


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
    screening = yaml_data.get("virtual_screening")
    if isinstance(screening, dict) and isinstance(screening.get("compounds"), list):
        screening_records: List[Dict[str, str]] = []
        screening_lines: List[str] = []
        for index, raw_compound in enumerate(screening["compounds"]):
            if not isinstance(raw_compound, dict):
                continue
            smiles = str(raw_compound.get("smiles") or "").strip()
            if not smiles:
                continue
            compound_id = str(raw_compound.get("id") or "").strip()
            name = str(raw_compound.get("name") or compound_id or f"Compound {index + 1}").strip()
            screening_records.append({
                "id": compound_id or f"compound-{index + 1}",
                "name": name,
                "smiles": smiles,
            })
            screening_lines.extend([f">{name}", smiles])
        if screening_records:
            properties[TASK_INPUT_OPTIONS_KEY] = {
                "virtualScreeningInput": "\n".join(screening_lines),
                "virtualScreening": {
                    "name": str(screening.get("name") or "Virtual screening").strip(),
                    "compoundCount": len(screening_records),
                    "compounds": screening_records,
                },
            }

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
    mode = (request_obj.form.get("mode") or "dock").strip().lower()
    if mode not in {"dock", "score", "pose", "refine", "interface"}:
        mode = "dock"
    # Dock pocket definition (center/size axes) rides along so an API-submitted dock task
    # round-trips its pocket into the task snapshot instead of silently losing it. Each axis
    # parses independently: the route accepts center-without-size (sizes default 22 Å), so a
    # missing size axis must not discard a valid center.
    dock_pocket = None
    if mode == "dock":
        def _axis(name: str) -> Optional[float]:
            raw = (request_obj.form.get(name) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        center = [_axis(f"center_{axis}") for axis in ("x", "y", "z")]
        size = [_axis(f"size_{axis}") for axis in ("x", "y", "z")]
        pocket_residues = (request_obj.form.get("pocket_residues") or "").strip()
        pocket_ligand = getattr(request_obj, "files", {}).get("pocket_ligand")
        pocket_ligand_name = (
            str(getattr(pocket_ligand, "filename", "") or "").strip() if pocket_ligand is not None else ""
        )

        def _sized(center_tuple: Tuple[float, float, float], method: str) -> Dict[str, Any]:
            return {
                "centerX": center_tuple[0], "centerY": center_tuple[1], "centerZ": center_tuple[2],
                "sizeX": size[0] if size[0] is not None and size[0] > 0 else 22.0,
                "sizeY": size[1] if size[1] is not None and size[1] > 0 else 22.0,
                "sizeZ": size[2] if size[2] is not None and size[2] > 0 else 22.0,
                "method": method,
            }

        if all(c is not None and c == c for c in center):
            dock_pocket = _sized((center[0], center[1], center[2]), "manual")
        elif pocket_residues:
            # Mirror the capability's Cα centroid so the UI shows (and a re-run submits) the
            # same box the original residues definition produced.
            protein_upload = getattr(request_obj, "files", {}).get("protein_file")
            centroid = None
            if protein_upload is not None and str(getattr(protein_upload, "filename", "") or "").strip():
                centroid = _pocket_centroid_from_residues(read_upload_text(protein_upload), pocket_residues)
            if centroid is not None:
                dock_pocket = _sized(centroid, "residues")
            else:
                logger.warning(
                    "Could not mirror pocket_residues into a numeric pocket box for the task snapshot"
                )
                dock_pocket = {"pocketResidues": pocket_residues, "method": "residues"}
        elif pocket_ligand_name:
            centroid = _pocket_centroid_from_ligand(
                read_upload_text(pocket_ligand), pocket_ligand_name
            )
            if centroid is not None:
                dock_pocket = _sized(centroid, "ligand")
            else:
                logger.warning(
                    "Could not mirror pocket_ligand into a numeric pocket box for the task snapshot"
                )
                # Mirror the residues-branch fallback: retain the input reference so a
                # re-run from the snapshot keeps the ligand-defined pocket.
                dock_pocket = {"pocketLigandName": pocket_ligand_name, "method": "ligand"}
    activity_enabled = bool(enable_affinity and target_chain and ligand_chain and ligand_smiles)
    properties: Dict[str, Any] = {
        "affinity": activity_enabled,
        "target": target_chain or None,
        "ligand": ligand_chain or None,
        "binder": ligand_chain or None,
        TASK_INPUT_OPTIONS_KEY: {
            "affinityMode": mode,
            **({"affinityDockPocket": dock_pocket} if dock_pocket else {}),
        },
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
        normalized_backend = str(backend).strip().lower()
        if normalized_backend in {"nesso1", "nesso-1"}:
            normalized_backend = "nesso"
        if normalized_backend in {"protenix", "nesso"}:
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
