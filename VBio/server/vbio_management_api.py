#!/usr/bin/env python3
"""VBio management API gateway.

This service keeps VBio API-token/project authorization in the VBio layer,
then proxies runtime calls to the original Boltz-WebUI backend unchanged.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import yaml
from flask import Flask, Response, jsonify, request

LOG_LEVEL = os.environ.get("VBIO_MGMT_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("vbio-management-api")

VBIO_POSTGREST_URL = os.environ.get("VBIO_POSTGREST_URL", "http://127.0.0.1:54321").rstrip("/")
VBIO_POSTGREST_APIKEY = os.environ.get("VBIO_POSTGREST_APIKEY", "").strip()
VBIO_POSTGREST_TIMEOUT_SECONDS = float(os.environ.get("VBIO_POSTGREST_TIMEOUT_SECONDS", "8"))

RUNTIME_API_BASE_URL = os.environ.get("VBIO_RUNTIME_API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
RUNTIME_API_TOKEN = (
    os.environ.get("VBIO_RUNTIME_API_TOKEN", "").strip() or os.environ.get("BOLTZ_API_TOKEN", "").strip()
)
RUNTIME_TIMEOUT_SECONDS = float(os.environ.get("VBIO_RUNTIME_TIMEOUT_SECONDS", "180"))

SERVER_HOST = os.environ.get("VBIO_MGMT_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("VBIO_MGMT_PORT", "5055"))

FORM_FIELDS_INTERNAL = {"project_id", "task_name", "task_summary", "operation_mode"}
AFFINITY_TARGET_UPLOAD_COMPONENT_ID = "__affinity_target_upload__"
AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = "__affinity_ligand_upload__"
DEFAULT_PROTENIX_PREDICT_SEED = 42


@dataclass
class TokenContext:
    token_id: str
    user_id: str
    name: str
    project_id: Optional[str]
    allow_submit: bool
    allow_delete: bool
    allow_cancel: bool


app = Flask(__name__)


def _postgrest_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if VBIO_POSTGREST_APIKEY:
        headers["apikey"] = VBIO_POSTGREST_APIKEY
        headers["Authorization"] = f"Bearer {VBIO_POSTGREST_APIKEY}"
    if extra:
        headers.update(extra)
    return headers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _postgrest_request(
    method: str,
    table_or_view: str,
    *,
    query: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    expect_json: bool = True,
) -> Any:
    url = f"{VBIO_POSTGREST_URL}/{table_or_view.lstrip('/')}"
    response = requests.request(
        method,
        url,
        params=query,
        json=payload,
        headers=_postgrest_headers(headers),
        timeout=VBIO_POSTGREST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        text = response.text.strip()
        raise RuntimeError(f"PostgREST {response.status_code}: {text}")
    if not expect_json or response.status_code == 204:
        return None
    if not response.content:
        return None
    return response.json()


def _record_usage(
    token: Optional[TokenContext],
    *,
    action: str,
    status_code: int,
    succeeded: bool,
    started_at: float,
    project_id: Optional[str],
    task_id: Optional[str],
) -> None:
    duration_ms = int(max(0.0, (time.perf_counter() - started_at) * 1000.0))
    meta: Dict[str, Any] = {}
    if project_id:
        meta["project_id"] = project_id
    if task_id:
        meta["task_id"] = task_id

    payload = {
        "token_id": token.token_id if token else None,
        "user_id": token.user_id if token else None,
        "method": request.method,
        "path": request.path,
        "action": action,
        "status_code": int(status_code),
        "succeeded": bool(succeeded),
        "duration_ms": duration_ms,
        "client": (request.headers.get("User-Agent") or "")[:255],
        "meta": meta,
    }

    try:
        _postgrest_request(
            "POST",
            "api_token_usage",
            payload=payload,
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )
        if token and succeeded:
            _postgrest_request(
                "PATCH",
                "api_tokens",
                query={"id": f"eq.{token.token_id}"},
                payload={"last_used_at": _now_iso()},
                headers={"Prefer": "return=minimal"},
                expect_json=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record API token usage: %s", exc)


def _forbidden(message: str, token: Optional[TokenContext], action: str, started_at: float, project_id: Optional[str]) -> Tuple[Response, int]:
    _record_usage(
        token,
        action=action,
        status_code=403,
        succeeded=False,
        started_at=started_at,
        project_id=project_id,
        task_id=None,
    )
    return jsonify({"error": message}), 403


def _validate_token(token_plain: str) -> TokenContext:
    if not token_plain:
        raise PermissionError("Missing X-API-Token header")

    token_hash = hashlib.sha256(token_plain.encode("utf-8")).hexdigest()
    rows = _postgrest_request(
        "GET",
        "api_tokens",
        query={
            "select": "id,user_id,name,project_id,allow_submit,allow_delete,allow_cancel,is_active,revoked_at,expires_at",
            "token_hash": f"eq.{token_hash}",
            "limit": "1",
        },
    )
    if not rows:
        raise PermissionError("Invalid API token")

    row = rows[0]
    if not bool(row.get("is_active", False)):
        raise PermissionError("API token is inactive")

    if row.get("revoked_at"):
        raise PermissionError("API token was revoked")

    expires_at = _parse_iso(row.get("expires_at"))
    if expires_at and expires_at <= datetime.now(timezone.utc):
        raise PermissionError("API token expired")

    return TokenContext(
        token_id=str(row.get("id") or ""),
        user_id=str(row.get("user_id") or ""),
        name=str(row.get("name") or ""),
        project_id=str(row.get("project_id") or "").strip() or None,
        allow_submit=bool(row.get("allow_submit", False)),
        allow_delete=bool(row.get("allow_delete", False)),
        allow_cancel=bool(row.get("allow_cancel", False)),
    )


def _ensure_project_exists(project_id: str) -> None:
    rows = _postgrest_request(
        "GET",
        "projects",
        query={
            "select": "id",
            "id": f"eq.{project_id}",
            "deleted_at": "is.null",
            "limit": "1",
        },
    )
    if not rows:
        raise PermissionError("Unknown project_id; create project in VBio web first")


def _authorize_submit(project_id: str, token_plain: str) -> TokenContext:
    token = _validate_token(token_plain)
    if not token.allow_submit:
        raise PermissionError("This token does not allow submit")
    if not token.project_id:
        raise PermissionError("This token is not bound to a project")
    if token.project_id != project_id:
        raise PermissionError("Token project_id does not match submitted project_id")
    _ensure_project_exists(project_id)
    return token


def _authorize_task_action(
    project_id: str,
    token_plain: str,
    *,
    require_delete: bool,
) -> TokenContext:
    token = _validate_token(token_plain)
    if require_delete:
        if not token.allow_delete:
            raise PermissionError("This token does not allow delete")
    else:
        if not token.allow_cancel:
            raise PermissionError("This token does not allow cancel")

    if not token.project_id:
        raise PermissionError("This token is not bound to a project")
    if token.project_id != project_id:
        raise PermissionError("Token project_id does not match request project_id")
    return token


def _authorize_project_read(project_id: str, token_plain: str) -> TokenContext:
    token = _validate_token(token_plain)
    if not token.project_id:
        raise PermissionError("This token is not bound to a project")
    if token.project_id != project_id:
        raise PermissionError("Token project_id does not match request project_id")
    return token


def _proxy_multipart(upstream_path: str) -> requests.Response:
    data: List[Tuple[str, str]] = []
    for key in request.form.keys():
        if key in FORM_FIELDS_INTERNAL:
            continue
        for value in request.form.getlist(key):
            data.append((key, value))

    if upstream_path == "/predict":
        raw_use_msa = request.form.get("use_msa_server")
        if raw_use_msa is None or not str(raw_use_msa).strip():
            yaml_upload = request.files.get("yaml_file")
            yaml_text = _read_upload_text(yaml_upload)
            inferred_use_msa = _infer_use_msa_server_from_yaml_text(yaml_text)
            data.append(("use_msa_server", "true" if inferred_use_msa else "false"))
            logger.info(
                "Auto-filled use_msa_server=%s for /predict because the form field was missing.",
                inferred_use_msa,
            )

        raw_seed = request.form.get("seed")
        form_backend = str(request.form.get("backend") or "boltz").strip().lower()
        if (raw_seed is None or not str(raw_seed).strip()) and form_backend == "protenix":
            data.append(("seed", str(DEFAULT_PROTENIX_PREDICT_SEED)))
            logger.info(
                "Auto-filled seed=%s for /predict because backend=protenix and the form field was missing.",
                DEFAULT_PROTENIX_PREDICT_SEED,
            )

    files: List[Tuple[str, Tuple[str, Any, str]]] = []
    for key in request.files.keys():
        for fs in request.files.getlist(key):
            filename = fs.filename or "upload.bin"
            mimetype = fs.mimetype or "application/octet-stream"
            fs.stream.seek(0)
            files.append((key, (filename, fs.stream, mimetype)))

    if not RUNTIME_API_TOKEN:
        raise RuntimeError("VBIO_RUNTIME_API_TOKEN (or BOLTZ_API_TOKEN) is not configured")

    return requests.post(
        f"{RUNTIME_API_BASE_URL}{upstream_path}",
        headers={"X-API-Token": RUNTIME_API_TOKEN, "Accept": "application/json"},
        data=data,
        files=files,
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )


def _proxy_delete(upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
    if not RUNTIME_API_TOKEN:
        raise RuntimeError("VBIO_RUNTIME_API_TOKEN (or BOLTZ_API_TOKEN) is not configured")

    return requests.delete(
        f"{RUNTIME_API_BASE_URL}{upstream_path}",
        params=passthrough_query,
        headers={"X-API-Token": RUNTIME_API_TOKEN, "Accept": "application/json"},
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )


def _proxy_get(upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
    if not RUNTIME_API_TOKEN:
        raise RuntimeError("VBIO_RUNTIME_API_TOKEN (or BOLTZ_API_TOKEN) is not configured")

    return requests.get(
        f"{RUNTIME_API_BASE_URL}{upstream_path}",
        params=passthrough_query,
        headers={"X-API-Token": RUNTIME_API_TOKEN, "Accept": "application/json"},
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )


def _build_flask_response(upstream: requests.Response) -> Tuple[Response, int]:
    content_type = upstream.headers.get("Content-Type", "application/json")
    response = Response(upstream.content, status=upstream.status_code, content_type=content_type)
    return response, upstream.status_code


def _task_backend_label(path: str, form_backend: str) -> str:
    backend = (form_backend or "").strip().lower()
    if backend:
        return backend
    if path.endswith("/api/protenix2score"):
        return "protenix"
    return "boltz"


def _parse_bool_form(field: str, default: bool = False) -> bool:
    raw = request.form.get(field)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _read_upload_text(upload: Any) -> str:
    if upload is None:
        return ""
    stream = getattr(upload, "stream", None)
    try:
        if stream is not None:
            stream.seek(0)
        raw = upload.read()
        if isinstance(raw, str):
            text = raw
        else:
            try:
                text = raw.decode("utf-8")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        return text
    except Exception:
        return ""
    finally:
        try:
            if stream is not None:
                stream.seek(0)
        except Exception:
            pass


def _parse_ligand_smiles_map_from_form() -> Dict[str, str]:
    raw = (request.form.get("ligand_smiles_map") or "").strip()
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


def _infer_use_msa_server_from_yaml_text(yaml_text: str) -> bool:
    """
    Infer `use_msa_server` from submitted prediction YAML.

    Returns True when at least one protein entry requires external MSA
    (no explicit `msa` field). Returns False when there are no proteins,
    all proteins use `msa: empty`, or all proteins already provide MSA.
    """
    if not yaml_text.strip():
        return False

    try:
        yaml_data = yaml.safe_load(yaml_text) or {}
    except Exception:
        return False
    if not isinstance(yaml_data, dict):
        return False

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        return False

    has_protein = False
    needs_external_msa = False
    for item in sequences:
        if not isinstance(item, dict):
            continue
        protein = item.get("protein")
        if not isinstance(protein, dict):
            continue
        has_protein = True
        msa_value = protein.get("msa")
        if msa_value is None:
            needs_external_msa = True
            continue
        if isinstance(msa_value, str):
            normalized = msa_value.strip().lower()
            if not normalized:
                needs_external_msa = True
                continue
            if normalized in {"empty", "none", "null"}:
                continue
            # Non-empty string path/reference: assume user provided MSA.
            continue
        # dict/list/object payload is treated as provided MSA.

    return has_protein and needs_external_msa


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


def _build_prediction_task_snapshot_from_yaml() -> Dict[str, Any]:
    yaml_upload = request.files.get("yaml_file")
    yaml_text = _read_upload_text(yaml_upload)
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


def _build_affinity_task_snapshot(upstream_path: str) -> Dict[str, Any]:
    if upstream_path not in {"/api/boltz2score", "/api/protenix2score"}:
        return {}

    target_chain = (request.form.get("target_chain") or "").strip()
    ligand_chain = (request.form.get("ligand_chain") or "").strip()
    ligand_smiles = (request.form.get("ligand_smiles") or "").strip()
    ligand_smiles_map = _parse_ligand_smiles_map_from_form()

    if not ligand_smiles and ligand_chain and ligand_chain in ligand_smiles_map:
        ligand_smiles = ligand_smiles_map[ligand_chain]
    if not ligand_smiles and ligand_smiles_map:
        ligand_smiles = next(iter(ligand_smiles_map.values()))

    enable_affinity = _parse_bool_form("enable_affinity", False)
    activity_enabled = bool(enable_affinity and target_chain and ligand_chain and ligand_smiles)
    properties: Dict[str, Any] = {
        "affinity": activity_enabled,
        "target": target_chain or None,
        "ligand": ligand_chain or None,
        "binder": ligand_chain or None,
    }

    components: List[Dict[str, Any]] = []
    if upstream_path == "/api/boltz2score":
        protein_upload = request.files.get("protein_file")
        ligand_upload = request.files.get("ligand_file")
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
                        "content": _read_upload_text(protein_upload),
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
                        "content": _read_upload_text(ligand_upload),
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


def _insert_project_task_snapshot(
    *,
    project_id: str,
    task_id: str,
    task_name: str,
    task_summary: str,
    backend: str,
    seed: Optional[int],
    extra_payload: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "project_id": project_id,
        "name": task_name,
        "summary": task_summary,
        "task_id": task_id,
        "task_state": "QUEUED",
        "status_text": "Submitted via API",
        "error_text": "",
        "backend": backend,
        "seed": seed,
        "submitted_at": _now_iso(),
    }
    if isinstance(extra_payload, dict) and extra_payload:
        payload.update(extra_payload)
    _postgrest_request(
        "POST",
        "project_tasks",
        payload=payload,
        headers={"Prefer": "return=minimal"},
        expect_json=False,
    )


def _read_seed(backend: str = "") -> Optional[int]:
    seed_raw = (request.form.get("seed") or "").strip()
    if not seed_raw:
        if str(backend).strip().lower() == "protenix":
            return DEFAULT_PROTENIX_PREDICT_SEED
        return None
    try:
        return int(seed_raw)
    except ValueError:
        return None


def _read_project_id_from_form() -> str:
    project_id = (request.form.get("project_id") or "").strip()
    if not project_id:
        raise PermissionError("project_id is required")
    return project_id


def _read_task_name(default_task_id: str) -> str:
    name = (request.form.get("task_name") or "").strip()
    if name:
        return name
    return f"Task {default_task_id[:8]}"


def _read_task_summary() -> str:
    return (request.form.get("task_summary") or "").strip()


def _read_project_id_from_query() -> str:
    project_id = (request.args.get("project_id") or "").strip()
    if not project_id:
        raise PermissionError("project_id query is required")
    return project_id


def _find_project_task(task_id: str, project_id: str) -> Optional[Dict[str, Any]]:
    rows = _postgrest_request(
        "GET",
        "project_tasks",
        query={
            "select": "id,project_id,task_id",
            "task_id": f"eq.{task_id}",
            "project_id": f"eq.{project_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _mark_task_cancelled(task_row_id: str) -> None:
    _postgrest_request(
        "PATCH",
        "project_tasks",
        query={"id": f"eq.{task_row_id}"},
        payload={
            "task_state": "REVOKED",
            "status_text": "Cancelled via API",
            "completed_at": _now_iso(),
        },
        headers={"Prefer": "return=minimal"},
        expect_json=False,
    )


def _delete_task_row(task_row_id: str) -> None:
    _postgrest_request(
        "DELETE",
        "project_tasks",
        query={"id": f"eq.{task_row_id}"},
        headers={"Prefer": "return=minimal"},
        expect_json=False,
    )


def _forward_submit(upstream_path: str, action: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token: Optional[TokenContext] = None

    try:
        project_id = _read_project_id_from_form()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = _authorize_submit(project_id, token_plain)
        backend = _task_backend_label(upstream_path, request.form.get("backend", ""))
        if upstream_path == "/predict":
            extra_snapshot_payload = _build_prediction_task_snapshot_from_yaml()
        else:
            extra_snapshot_payload = _build_affinity_task_snapshot(upstream_path)

        upstream = _proxy_multipart(upstream_path)
        response, status_code = _build_flask_response(upstream)

        task_id: Optional[str] = None
        succeeded = 200 <= status_code < 300
        if succeeded:
            try:
                payload = upstream.json()
                task_id = str(payload.get("task_id") or "").strip() or None
            except Exception:  # noqa: BLE001
                task_id = None

            if task_id:
                _insert_project_task_snapshot(
                    project_id=project_id,
                    task_id=task_id,
                    task_name=_read_task_name(task_id),
                    task_summary=_read_task_summary(),
                    backend=backend,
                    seed=_read_seed(backend),
                    extra_payload=extra_snapshot_payload,
                )

        _record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return response, status_code
    except PermissionError as exc:
        return _forbidden(str(exc), token, action, started, project_id)
    except requests.Timeout:
        _record_usage(
            token,
            action=action,
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except Exception as exc:  # noqa: BLE001
        logger.exception("Submit forward failed")
        _record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500


def _forward_task_read(task_id: str, upstream_prefix: str, action: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token: Optional[TokenContext] = None

    try:
        project_id = _read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = _authorize_project_read(project_id, token_plain)

        task_row = _find_project_task(task_id, project_id)
        if not task_row:
            raise PermissionError("Task not found in this project")

        passthrough_query = {
            key: value
            for key, value in request.args.items()
            if key != "project_id"
        }
        upstream = _proxy_get(f"{upstream_prefix}/{quote(task_id, safe='')}", passthrough_query)
        response, status_code = _build_flask_response(upstream)
        succeeded = 200 <= status_code < 300

        _record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return response, status_code
    except PermissionError as exc:
        return _forbidden(str(exc), token, action, started, project_id)
    except requests.Timeout:
        _record_usage(
            token,
            action=action,
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except Exception as exc:  # noqa: BLE001
        logger.exception("Task read forward failed")
        _record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": str(exc)}), 500


@app.get("/vbio-api/healthz")
def healthz() -> Tuple[Response, int]:
    return jsonify({
        "ok": True,
        "runtime_api_base_url": RUNTIME_API_BASE_URL,
        "postgrest_url": VBIO_POSTGREST_URL,
    }), 200


@app.post("/vbio-api/predict")
def submit_predict() -> Tuple[Response, int]:
    return _forward_submit("/predict", "submit_predict")


@app.post("/vbio-api/api/boltz2score")
def submit_boltz2score() -> Tuple[Response, int]:
    return _forward_submit("/api/boltz2score", "submit_boltz2score")


@app.post("/vbio-api/api/protenix2score")
def submit_protenix2score() -> Tuple[Response, int]:
    return _forward_submit("/api/protenix2score", "submit_protenix2score")


@app.get("/vbio-api/status/<task_id>")
def get_status(task_id: str) -> Tuple[Response, int]:
    return _forward_task_read(task_id, "/status", "read_status")


@app.get("/vbio-api/results/<task_id>")
def get_results(task_id: str) -> Tuple[Response, int]:
    return _forward_task_read(task_id, "/results", "read_results")


@app.delete("/vbio-api/tasks/<task_id>")
def cancel_or_delete_task(task_id: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token: Optional[TokenContext] = None

    try:
        project_id = _read_project_id_from_query()
        mode = (request.args.get("operation_mode") or "cancel").strip().lower()
        require_delete = mode == "delete"

        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = _authorize_task_action(project_id, token_plain, require_delete=require_delete)

        task_row = _find_project_task(task_id, project_id)
        if not task_row:
            raise PermissionError("Task not found in this project")

        passthrough_query = {
            key: value
            for key, value in request.args.items()
            if key not in {"project_id", "operation_mode"}
        }
        upstream = _proxy_delete(f"/tasks/{quote(task_id, safe='')}", passthrough_query)
        response, status_code = _build_flask_response(upstream)

        succeeded = 200 <= status_code < 300
        if succeeded:
            if require_delete:
                _delete_task_row(task_row["id"])
            else:
                _mark_task_cancelled(task_row["id"])

        _record_usage(
            token,
            action="delete_task" if require_delete else "cancel_task",
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return response, status_code
    except PermissionError as exc:
        return _forbidden(
            str(exc),
            token,
            "delete_task" if (request.args.get("operation_mode") or "").strip().lower() == "delete" else "cancel_task",
            started,
            project_id,
        )
    except requests.Timeout:
        _record_usage(
            token,
            action="cancel_or_delete_task",
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cancel/Delete forward failed")
        _record_usage(
            token,
            action="cancel_or_delete_task",
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
