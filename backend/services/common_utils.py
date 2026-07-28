from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y'}


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Type-rich bool parser for task/payload values (bool, int, float, str).

    Single source of truth shared by the Celery worker and the prediction subprocess so
    flags like low_vram resolve identically in both. Unknown strings fall back to ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def is_msa_disabled(value: Any) -> bool:
    """Return whether a YAML MSA value explicitly requests no MSA generation."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "empty", "none", "null"}
    return False


def parse_int(value: Optional[str], default: Optional[int] = None) -> Optional[int]:
    if value is None or value == '':
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_float(value: Optional[str], default: Optional[float] = None) -> Optional[float]:
    if value is None or value == '':
        return default
    try:
        return float(value)
    except ValueError:
        return default


def parse_json_field(value: Optional[str], field_name: str) -> Dict:
    if value is None:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid '{field_name}' JSON format: {exc}")
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"'{field_name}' must be a JSON object.")
    return parsed


def normalize_chain_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    normalized = str(value).strip()
    return [normalized] if normalized else []


class ProteinMsaMode(str, Enum):
    EXTERNAL = "external"
    DISABLED = "disabled"
    PROVIDED = "provided"


@dataclass(frozen=True)
class ProteinMsaPolicy:
    chain_ids: Tuple[str, ...]
    sequence: str
    mode: ProteinMsaMode
    value: Any = None


def classify_protein_msa(value: Any) -> ProteinMsaMode:
    if is_msa_disabled(value):
        return ProteinMsaMode.DISABLED
    if isinstance(value, (bool, int, float)):
        return ProteinMsaMode.EXTERNAL
    if value is None or (isinstance(value, str) and not value.strip()):
        return ProteinMsaMode.EXTERNAL
    return ProteinMsaMode.PROVIDED


def extract_protein_msa_policies(yaml_content: str) -> list[ProteinMsaPolicy]:
    if not yaml_content.strip():
        return []
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        return []
    sequences = yaml_data.get('sequences')
    if not isinstance(sequences, list):
        return []

    policies: list[ProteinMsaPolicy] = []
    for item in sequences:
        if not isinstance(item, dict):
            continue
        protein = item.get('protein')
        if not isinstance(protein, dict):
            continue
        value = protein.get('msa')
        policies.append(
            ProteinMsaPolicy(
                chain_ids=tuple(normalize_chain_id_list(protein.get('id'))),
                sequence=str(protein.get('sequence') or '').strip(),
                mode=classify_protein_msa(value),
                value=value,
            )
        )
    return policies


def infer_use_msa_server_from_yaml_text(yaml_content: str) -> bool:
    return any(
        policy.mode is ProteinMsaMode.EXTERNAL
        for policy in extract_protein_msa_policies(yaml_content)
    )


def extract_template_meta_from_yaml(yaml_content: str) -> Dict[str, Dict]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        return {}

    template_entries = yaml_data.get('templates')
    if not isinstance(template_entries, list):
        return {}

    metadata_by_file: Dict[str, Dict] = {}
    for entry in template_entries:
        if not isinstance(entry, dict):
            continue
        path_ref = entry.get('cif') or entry.get('mmcif') or entry.get('pdb')
        if not path_ref:
            continue
        file_name = Path(str(path_ref)).name
        if not file_name:
            continue

        fmt = 'pdb' if 'pdb' in entry else 'cif'
        template_chain_id = entry.get('template_id') or entry.get('template_chain_id')
        target_chain_ids = normalize_chain_id_list(
            entry.get('chain_id') or entry.get('target_chain_ids') or entry.get('chain_ids')
        )
        metadata_by_file[file_name] = {
            'format': fmt,
            'template_chain_id': str(template_chain_id).strip() if template_chain_id else None,
            'target_chain_ids': target_chain_ids,
        }
    return metadata_by_file


def load_progress(redis_key: str, *, get_redis_client_fn, logger) -> Optional[Dict]:
    try:
        redis_client = get_redis_client_fn()
        raw = redis_client.get(redis_key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning('Failed to read progress from redis: %s', exc)
        return None


def has_worker_for_queue(queue_name: str, *, celery_app, logger) -> bool:
    try:
        inspector = celery_app.control.inspect(timeout=1.0)
        active_queues = inspector.active_queues() or {}
        for queues in active_queues.values():
            if not isinstance(queues, list):
                continue
            for queue in queues:
                queue_token = str((queue or {}).get('name') or '').strip()
                if queue_token == queue_name:
                    return True
        if active_queues:
            return False
    except Exception as exc:
        logger.warning('Failed to inspect Celery worker queues: %s', exc)
        return False
    return False
