from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request
from lead_optimization import mmp_database_registry
from lead_optimization.mmp_lifecycle import engine as legacy_engine
from lead_optimization.mmp_lifecycle.admin_store import MmpLifecycleAdminStore
from lead_optimization.mmp_lifecycle.models import PostgresTarget
from lead_optimization.mmp_lifecycle.services import (
    check_service,
    property_admin_service,
    report_service,
    setup_service,
    verify_service,
)

try:
    import psycopg
except Exception:
    psycopg = None


def _safe_json_object(payload: Any) -> Dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _float_equal(left: Any, right: Any, *, eps: float = 1e-6) -> bool:
    try:
        lv = float(left)
        rv = float(right)
    except Exception:
        return False
    return math.isclose(lv, rv, rel_tol=1e-9, abs_tol=eps)


def _chunked(items: List[str], size: int = 1000) -> Iterable[List[str]]:
    chunk_size = max(1, int(size or 1))
    for idx in range(0, len(items), chunk_size):
        yield items[idx : idx + chunk_size]


def _count_actions(rows: List[Dict[str, Any]], key: str = "action") -> Dict[str, int]:
    output: Dict[str, int] = {}
    for row in rows:
        action = str(row.get(key, "") or "").strip() or "UNKNOWN"
        output[action] = output.get(action, 0) + 1
    return output


def _read_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


_LIFECYCLE_TRANSIENT_SCHEMA_RE = re.compile(r"_incs_[0-9]{8,}_[0-9]{2}$", re.IGNORECASE)


def _is_lifecycle_transient_database_entry(item: Dict[str, Any]) -> bool:
    row = _safe_json_object(item)
    schema = _read_text(row.get("schema")).lower()
    label = _read_text(row.get("label")).lower()
    # Incremental shard schemas are runtime temp artifacts, e.g.:
    # chembl36_full_incs_091824663380_01
    return bool(_LIFECYCLE_TRANSIENT_SCHEMA_RE.search(schema or label))


def _filter_lifecycle_catalog_databases(catalog: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(catalog or {}) if isinstance(catalog, dict) else {}
    databases = [
        _safe_json_object(item)
        for item in (payload.get("databases") or [])
        if isinstance(item, dict) and not _is_lifecycle_transient_database_entry(item)
    ]
    payload["databases"] = databases
    default_id = _read_text(payload.get("default_database_id"))
    if default_id and not any(_read_text(item.get("id")) == default_id for item in databases):
        payload["default_database_id"] = _read_text(databases[0].get("id")) if databases else ""
    return payload


def _normalize_property_token(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _read_text(raw).lower())


def _canonical_property_family(raw: Any) -> str:
    token = _read_text(raw).lower()
    if not token:
        return ""
    compact = token.replace(" ", "").replace("-", "_")
    matcher = re.match(
        r"^(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)\((.+)\)$",
        compact,
    )
    if matcher:
        compact = matcher.group(1)
    compact = re.sub(
        r"^(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)[_]+",
        "",
        compact,
    )
    compact = re.sub(
        r"[_]+(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)$",
        "",
        compact,
    )
    compact = re.sub(r"\((?:um|nm|mm|pm|fm)\)$", "", compact)
    compact = re.sub(r"[_]+(?:um|nm|mm|pm|fm)$", "", compact)
    compact = re.sub(r"^(?:um|nm|mm|pm|fm)[_]+", "", compact)
    normalized = _normalize_property_token(compact)
    if normalized:
        return normalized
    return _normalize_property_token(token)


def _pick_family_alias_rename_source(property_names: List[str], target_property: str) -> str:
    target = _read_text(target_property)
    if not target:
        return ""
    family = _canonical_property_family(target)
    if not family:
        return ""
    candidates = [
        _read_text(item)
        for item in property_names
        if _read_text(item)
        and _read_text(item).lower() != target.lower()
        and _canonical_property_family(item) == family
    ]
    unique: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    if len(unique) == 1:
        return unique[0]
    return ""


def _is_missing_cell_value(value: Any) -> bool:
    token = _read_text(value)
    if not token:
        return True
    token_upper = token.upper()
    return token_upper in {"*", "NA", "N/A", "NAN", "NULL", "NONE", "-"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_iso(value: Any) -> Optional[datetime]:
    token = _read_text(value)
    if not token:
        return None
    try:
        return datetime.strptime(token, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _to_nonneg_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except Exception:
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    token = _read_text(value).lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _value_error_http_status(exc: Exception, *, default_status: int = 400) -> int:
    message = _read_text(exc).lower()
    if "still building" in message:
        return 409
    if "is busy with" in message:
        return 409
    return int(default_status)


def _normalize_check_policy(raw: Dict[str, Any]) -> Dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    return {
        "max_compound_invalid_smiles_rows": _to_nonneg_int(payload.get("max_compound_invalid_smiles_rows"), 0),
        "max_experiment_invalid_rows": _to_nonneg_int(payload.get("max_experiment_invalid_rows"), 0),
        "max_unmapped_property_rows": _to_nonneg_int(payload.get("max_unmapped_property_rows"), 0),
        "max_unmatched_compound_rows": _to_nonneg_int(payload.get("max_unmatched_compound_rows"), 0),
        "require_check_for_selected_database": _to_bool(payload.get("require_check_for_selected_database"), True),
        "require_approved_status": _to_bool(payload.get("require_approved_status"), True),
        "require_importable_experiment_rows": _to_bool(payload.get("require_importable_experiment_rows"), True),
        "require_importable_compound_rows": _to_bool(payload.get("require_importable_compound_rows"), True),
    }


def _build_check_gate(
    *,
    batch: Dict[str, Any],
    database_id: str,
    import_compounds: bool,
    import_experiments: bool,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    policy_obj = _normalize_check_policy(policy)
    reasons: List[str] = []

    status_token = _read_text(batch.get("status")).lower() or "draft"
    selected_database_id = _read_text(batch.get("selected_database_id"))
    last_check = _safe_json_object(batch.get("last_check"))
    checked_at = _read_text(last_check.get("checked_at"))
    checked_database_id = _read_text(last_check.get("database_id"))
    compound_summary = _safe_json_object(last_check.get("compound_summary"))
    experiment_summary = _safe_json_object(last_check.get("experiment_summary"))
    files = _safe_json_object(batch.get("files"))

    if not checked_at:
        reasons.append("Batch has not been checked yet.")

    if policy_obj.get("require_check_for_selected_database", True):
        if selected_database_id and selected_database_id != database_id:
            reasons.append("Batch selected database does not match current apply target.")
        if checked_database_id and checked_database_id != database_id:
            reasons.append("Last check was executed against a different database.")
        if not checked_database_id:
            reasons.append("Last check database is missing.")

    if bool(policy_obj.get("require_approved_status", True)) and status_token != "approved":
        reasons.append(f"Batch status must be 'approved' before apply. Current status: {status_token}.")

    compound_invalid_smiles_rows = _to_nonneg_int(compound_summary.get("invalid_smiles_rows"), 0)
    compound_annotated_rows = _to_nonneg_int(compound_summary.get("annotated_rows"), 0)

    experiment_invalid_rows = _to_nonneg_int(experiment_summary.get("rows_invalid"), 0)
    experiment_unmapped_rows = _to_nonneg_int(experiment_summary.get("rows_unmapped"), 0)
    experiment_unmatched_compound_rows = _to_nonneg_int(experiment_summary.get("rows_unmatched_compound"), 0)
    experiment_importable_rows = _to_nonneg_int(experiment_summary.get("rows_will_import"), 0)

    if import_compounds:
        if not compound_summary:
            reasons.append("Compound check summary is missing.")
        if compound_invalid_smiles_rows > int(policy_obj["max_compound_invalid_smiles_rows"]):
            reasons.append(
                f"Compound invalid_smiles_rows={compound_invalid_smiles_rows} exceeds policy limit={policy_obj['max_compound_invalid_smiles_rows']}."
            )
        if bool(policy_obj.get("require_importable_compound_rows", True)) and compound_annotated_rows <= 0:
            reasons.append("Compound check has no importable/annotated rows.")

    if import_experiments:
        if not experiment_summary:
            reasons.append("Experiment check summary is missing.")
        if experiment_invalid_rows > int(policy_obj["max_experiment_invalid_rows"]):
            reasons.append(
                f"Experiment rows_invalid={experiment_invalid_rows} exceeds policy limit={policy_obj['max_experiment_invalid_rows']}."
            )
        if experiment_unmapped_rows > int(policy_obj["max_unmapped_property_rows"]):
            reasons.append(
                f"Experiment rows_unmapped={experiment_unmapped_rows} exceeds policy limit={policy_obj['max_unmapped_property_rows']}."
            )
        if experiment_unmatched_compound_rows > int(policy_obj["max_unmatched_compound_rows"]):
            reasons.append(
                f"Experiment rows_unmatched_compound={experiment_unmatched_compound_rows} exceeds policy limit={policy_obj['max_unmatched_compound_rows']}."
            )
        if bool(policy_obj.get("require_importable_experiment_rows", True)) and experiment_importable_rows <= 0:
            reasons.append("Experiment check has no importable mapped rows.")

    checked_at_dt = _parse_utc_iso(checked_at)
    latest_upload_at_dt: Optional[datetime] = None
    if import_compounds:
        compounds_meta = _safe_json_object(files.get("compounds"))
        uploaded = _parse_utc_iso(compounds_meta.get("uploaded_at"))
        if uploaded and (latest_upload_at_dt is None or uploaded > latest_upload_at_dt):
            latest_upload_at_dt = uploaded
    if import_experiments:
        experiments_meta = _safe_json_object(files.get("experiments"))
        uploaded = _parse_utc_iso(experiments_meta.get("uploaded_at"))
        if uploaded and (latest_upload_at_dt is None or uploaded > latest_upload_at_dt):
            latest_upload_at_dt = uploaded
    if checked_at_dt and latest_upload_at_dt and latest_upload_at_dt > checked_at_dt:
        reasons.append("Batch files were updated after last check. Run check again.")

    passed = len(reasons) == 0
    return {
        "passed": passed,
        "reasons": reasons,
        "policy": policy_obj,
        "metrics": {
            "compound_invalid_smiles_rows": compound_invalid_smiles_rows,
            "compound_annotated_rows": compound_annotated_rows,
            "experiment_invalid_rows": experiment_invalid_rows,
            "experiment_unmapped_rows": experiment_unmapped_rows,
            "experiment_unmatched_compound_rows": experiment_unmatched_compound_rows,
            "experiment_importable_rows": experiment_importable_rows,
        },
        "status": status_token,
        "checked_at": checked_at,
        "check_database_id": checked_database_id,
        "selected_database_id": selected_database_id,
        "database_id": database_id,
        "evaluated_at": _utc_now_iso(),
    }


def _pick_column(headers: List[str], preferred: str, fallback_tokens: List[str]) -> str:
    normalized = {_read_text(name).lower(): _read_text(name) for name in headers if _read_text(name)}
    preferred_token = _read_text(preferred)
    if preferred_token and preferred_token.lower() in normalized:
        return normalized[preferred_token.lower()]
    for token in fallback_tokens:
        if token.lower() in normalized:
            return normalized[token.lower()]
    return ""


def _detect_delimiter(path: str) -> str:
    try:
        return legacy_engine._detect_table_delimiter(path)
    except Exception:
        return "\t" if str(path or "").lower().endswith(".tsv") else ","


def _dedupe_preview_headers(headers: List[str]) -> List[str]:
    seen: set[str] = set()
    output: List[str] = []
    for item in headers:
        token = _read_text(item)
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        output.append(token)
    return output


def _build_compounds_preview(path: str, *, max_rows: int) -> Dict[str, Any]:
    source_path = str(path or "").strip()
    if not source_path:
        return {
            "headers": [],
            "rows": [],
            "total_rows": 0,
            "preview_truncated": False,
            "column_non_empty_counts": {},
            "column_numeric_counts": {},
            "column_positive_numeric_counts": {},
        }
    if not os.path.exists(source_path):
        return {
            "headers": [],
            "rows": [],
            "total_rows": 0,
            "preview_truncated": False,
            "column_non_empty_counts": {},
            "column_numeric_counts": {},
            "column_positive_numeric_counts": {},
        }
    lower = source_path.lower()
    if lower.endswith(".xlsx"):
        raise ValueError("Preview currently supports tabular text files (TSV/CSV/TXT) only.")

    preview_cap = max(1, int(max_rows or 1))
    delimiter = _detect_delimiter(source_path)
    column_non_empty_counts: Dict[str, int] = {}
    column_numeric_counts: Dict[str, int] = {}
    column_positive_numeric_counts: Dict[str, int] = {}
    rows: List[Dict[str, str]] = []
    total_rows = 0
    raw_headers: List[str] = []
    with open(source_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for raw_row in reader:
            normalized_row = [_read_text(cell) for cell in raw_row]
            if not any(normalized_row):
                continue
            raw_headers = normalized_row
            break
        if not raw_headers:
            raise ValueError("Uploaded file has no header row.")

        for raw_row in reader:
            values = [_read_text(cell) for cell in raw_row]
            bucket: Dict[str, str] = {}
            has_any = False
            for idx, header in enumerate(raw_headers):
                name = _read_text(header)
                if not name:
                    continue
                value = values[idx] if idx < len(values) else ""
                normalized_value = "" if _is_missing_cell_value(value) else value
                bucket[name] = normalized_value
                if _is_missing_cell_value(normalized_value):
                    continue
                has_any = True
                column_non_empty_counts[name] = int(column_non_empty_counts.get(name) or 0) + 1
                try:
                    numeric = float(normalized_value)
                except Exception:
                    numeric = None
                if numeric is not None and math.isfinite(numeric):
                    column_numeric_counts[name] = int(column_numeric_counts.get(name) or 0) + 1
                    if numeric > 0:
                        column_positive_numeric_counts[name] = int(column_positive_numeric_counts.get(name) or 0) + 1
            if not has_any:
                continue
            total_rows += 1
            if len(rows) < preview_cap:
                rows.append(bucket)

    return {
        "headers": _dedupe_preview_headers(raw_headers),
        "rows": rows,
        "total_rows": total_rows,
        "preview_truncated": total_rows > len(rows),
        "column_non_empty_counts": column_non_empty_counts,
        "column_numeric_counts": column_numeric_counts,
        "column_positive_numeric_counts": column_positive_numeric_counts,
    }


def _collect_batch_compound_smiles_for_candidates(
    *,
    store: MmpLifecycleAdminStore,
    batch: Dict[str, Any],
    candidate_smiles: List[str],
) -> set[str]:
    batch_id = _read_text(batch.get("id"))
    files = _safe_json_object(batch.get("files"))
    compounds_meta = _safe_json_object(files.get("compounds"))
    source_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=compounds_meta)
    if not source_path or not os.path.exists(source_path):
        return set()

    candidate_set = {str(item or "").strip() for item in candidate_smiles if str(item or "").strip()}
    if not candidate_set:
        return set()

    column_config = _safe_json_object(compounds_meta.get("column_config"))
    delimiter = _detect_delimiter(source_path)
    matched_smiles: set[str] = set()
    canonical_smiles_cache: Dict[str, str] = {}

    with open(source_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [_read_text(item) for item in (reader.fieldnames or []) if _read_text(item)]
        if not headers:
            return set()
        smiles_col = _pick_column(
            headers,
            _read_text(column_config.get("smiles_column")),
            ["smiles", "clean_smiles", "canonical_smiles", "molecule_smiles", "mol_smiles", "structure", "smi"],
        )
        if not smiles_col:
            return set()
        for row in reader:
            raw_smiles = _read_text((row or {}).get(smiles_col))
            if _is_missing_cell_value(raw_smiles):
                continue
            clean_smiles = _canonicalize_smiles_with_cache(raw_smiles, canonical_smiles_cache)
            if not clean_smiles:
                continue
            if clean_smiles not in candidate_set:
                continue
            matched_smiles.add(clean_smiles)
            if len(matched_smiles) >= len(candidate_set):
                break
    return matched_smiles


def _resolve_batch_file_path(
    *,
    store: MmpLifecycleAdminStore,
    batch_id: str,
    file_meta: Dict[str, Any],
) -> str:
    batch_token = _read_text(batch_id)
    meta = _safe_json_object(file_meta)

    direct_path = _read_text(meta.get("path"))
    if direct_path and os.path.exists(direct_path):
        return direct_path

    rel_path = _read_text(meta.get("path_rel"))
    if rel_path:
        candidate = (Path(str(store.root_dir)) / rel_path).resolve()
        if os.path.exists(candidate):
            return str(candidate)

    stored_name = Path(_read_text(meta.get("stored_name"))).name
    if batch_token and stored_name:
        candidate = Path(str(store.batch_upload_dir)) / batch_token / stored_name
        if os.path.exists(candidate):
            return str(candidate)

    return direct_path


def _resolve_database_target(database_id: str) -> Tuple[Dict[str, Any], PostgresTarget]:
    selected = mmp_database_registry.resolve_mmp_database(str(database_id or "").strip(), include_hidden=True)
    database_url = str(selected.get("database_url") or "").strip()
    schema = str(selected.get("schema") or "").strip()
    target = PostgresTarget.from_inputs(url=database_url, schema=schema)
    return selected, target


def _resolve_catalog_database(database_id: str, *, include_stats: bool = False) -> Dict[str, Any]:
    token = _read_text(database_id)
    if not token:
        raise ValueError("database_id is required.")
    catalog = _filter_lifecycle_catalog_databases(
        mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=include_stats)
    )
    for item in catalog.get("databases", []) if isinstance(catalog, dict) else []:
        row = _safe_json_object(item)
        if _read_text(row.get("id")) == token:
            return row
    raise ValueError(f"MMP database '{token}' is not found.")


def _is_database_ready_for_update(database_entry: Dict[str, Any]) -> bool:
    stats = _safe_json_object(database_entry.get("stats"))
    compounds = stats.get("compounds")
    rules = stats.get("rules")
    pairs = stats.get("pairs")
    return compounds is not None and rules is not None and pairs is not None


def _extract_database_property_names(database_entry: Dict[str, Any]) -> List[str]:
    rows = database_entry.get("properties") if isinstance(database_entry, dict) else []
    output: List[str] = []
    seen: set[str] = set()
    for item in rows if isinstance(rows, list) else []:
        if isinstance(item, dict):
            name = _read_text(item.get("name") or item.get("label"))
        else:
            name = _read_text(item)
        if not name:
            continue
        token = name.lower()
        if token in seen:
            continue
        seen.add(token)
        output.append(name)
    return output


def _sync_assay_methods_for_database(
    store: MmpLifecycleAdminStore,
    database_entry: Dict[str, Any],
    *,
    allow_auto_queue_updates: bool = False,
) -> Dict[str, Any]:
    db_id = _read_text(database_entry.get("id"))
    if not db_id:
        return {"database_id": "", "changed": False}
    property_names = _extract_database_property_names(database_entry)

    # Auto-reconcile legacy property aliases to current assay method output names.
    # This mutates admin state (queue entries), so it is disabled for read-only routes.
    if allow_auto_queue_updates:
        try:
            methods_for_db = []
            for item in store.list_methods():
                row = _safe_json_object(item)
                reference = _read_text(row.get("reference"))
                if reference and reference != db_id:
                    continue
                output_prop = _read_text(row.get("output_property"))
                if not output_prop:
                    continue
                methods_for_db.append(row)
            methods_for_db.sort(key=lambda row: _read_text(row.get("updated_at")), reverse=True)
            methods_for_db.sort(key=lambda row: 0 if not _read_text(row.get("id")).startswith("method_auto_") else 1)

            desired_output_by_family: Dict[str, str] = {}
            for row in methods_for_db:
                output_prop = _read_text(row.get("output_property"))
                family = _canonical_property_family(output_prop)
                if not family or family in desired_output_by_family:
                    continue
                desired_output_by_family[family] = output_prop

            for _, desired_output in desired_output_by_family.items():
                if any(_read_text(item).lower() == desired_output.lower() for item in property_names):
                    continue
                alias_source = _pick_family_alias_rename_source(property_names, desired_output)
                if not alias_source:
                    continue
                store.enqueue_database_sync(
                    database_id=db_id,
                    operation="rename_property",
                    payload={"old_name": alias_source, "new_name": desired_output},
                    dedupe_key=f"rename_property:{alias_source.lower()}->{desired_output.lower()}",
                )
        except Exception:
            # Sync should remain resilient; failed auto-reconcile should not block UI/API.
            pass

    pending_rows = store.list_pending_database_sync(database_id=db_id, pending_only=True)
    if pending_rows:
        normalized_names: List[str] = []
        seen_names: set[str] = set()
        pending_renames: List[Tuple[datetime, int, str, str]] = []

        def _append_property_name(raw_name: Any) -> None:
            token = _read_text(raw_name)
            if not token:
                return
            lower = token.lower()
            if lower in seen_names:
                return
            seen_names.add(lower)
            normalized_names.append(token)

        def _remove_property_name(raw_name: Any) -> None:
            token = _read_text(raw_name).lower()
            if not token:
                return
            if token not in seen_names:
                return
            seen_names.remove(token)
            normalized_names[:] = [name for name in normalized_names if name.lower() != token]

        for name in property_names:
            _append_property_name(name)

        for item in pending_rows:
            row = _safe_json_object(item)
            operation = _read_text(row.get("operation")).lower()
            payload = _safe_json_object(row.get("payload"))
            if operation == "rename_property":
                old_name = _read_text(payload.get("old_name"))
                new_name = _read_text(payload.get("new_name"))
                if old_name and new_name:
                    order_time = _parse_utc_iso(row.get("updated_at")) or _parse_utc_iso(row.get("created_at"))
                    pending_renames.append((order_time or datetime.min.replace(tzinfo=timezone.utc), len(pending_renames), old_name, new_name))
                _remove_property_name(old_name)
                _append_property_name(new_name)
            elif operation == "ensure_property":
                _append_property_name(payload.get("property_name"))
            elif operation == "purge_property":
                _remove_property_name(payload.get("property_name"))

        # Rename intent wins over stale ensure rows for the old token.
        for _, _, old_name, new_name in sorted(pending_renames, key=lambda item: (item[0], item[1])):
            _remove_property_name(old_name)
            _append_property_name(new_name)

        property_names = normalized_names
    return store.sync_database_methods_and_mappings(
        database_id=db_id,
        property_names=property_names,
    )


def _sync_assay_methods_for_catalog(store: MmpLifecycleAdminStore, catalog: Dict[str, Any]) -> None:
    for item in catalog.get("databases", []) if isinstance(catalog, dict) else []:
        if not isinstance(item, dict):
            continue
        _sync_assay_methods_for_database(store, item)


@dataclass(frozen=True)
class CompoundImportOptions:
    output_dir: str
    max_heavy_atoms: int
    skip_attachment_enrichment: bool
    attachment_force_recompute: bool
    fragment_jobs: int
    index_maintenance_work_mem_mb: int
    index_work_mem_mb: int
    index_parallel_workers: int
    index_commit_every_flushes: int
    incremental_index_shards: int
    incremental_index_jobs: int
    skip_incremental_analyze: bool
    build_construct_tables: bool
    build_constant_smiles_mol_index: bool

    def to_setup_kwargs(self) -> Dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "max_heavy_atoms": self.max_heavy_atoms,
            "skip_attachment_enrichment": self.skip_attachment_enrichment,
            "attachment_force_recompute": self.attachment_force_recompute,
            "fragment_jobs": self.fragment_jobs,
            "index_maintenance_work_mem_mb": self.index_maintenance_work_mem_mb,
            "index_work_mem_mb": self.index_work_mem_mb,
            "index_parallel_workers": self.index_parallel_workers,
            "index_commit_every_flushes": self.index_commit_every_flushes,
            "incremental_index_shards": self.incremental_index_shards,
            "incremental_index_jobs": self.incremental_index_jobs,
            "skip_incremental_analyze": self.skip_incremental_analyze,
            "build_construct_tables": self.build_construct_tables,
            "build_constant_smiles_mol_index": self.build_constant_smiles_mol_index,
        }


def _extract_compound_import_options(payload: Dict[str, Any]) -> CompoundImportOptions:
    cpu_count = max(1, int(os.cpu_count() or 1))
    auto_incremental_shards = max(1, min(8, cpu_count // 2))
    auto_incremental_jobs = max(1, min(auto_incremental_shards, cpu_count // 4 if cpu_count >= 4 else 1))

    def _to_int(key: str, default: int) -> int:
        value = payload.get(key)
        try:
            parsed = int(value)
            if parsed <= 0 and key in {
                "fragment_jobs",
                "index_maintenance_work_mem_mb",
                "index_work_mem_mb",
                "index_parallel_workers",
                "incremental_index_shards",
                "incremental_index_jobs",
                "max_heavy_atoms",
            }:
                return default
            return parsed
        except Exception:
            return default

    def _to_bool(key: str, default: bool) -> bool:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        token = str(value or "").strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
        return default

    incremental_shards = _to_int("pg_incremental_index_shards", auto_incremental_shards)
    incremental_jobs = _to_int("pg_incremental_index_jobs", auto_incremental_jobs)
    if incremental_jobs > incremental_shards:
        incremental_jobs = incremental_shards

    return CompoundImportOptions(
        output_dir=str(payload.get("output_dir") or "lead_optimization/data").strip() or "lead_optimization/data",
        max_heavy_atoms=_to_int("max_heavy_atoms", 50),
        skip_attachment_enrichment=_to_bool("skip_attachment_enrichment", False),
        attachment_force_recompute=_to_bool("attachment_force_recompute", False),
        fragment_jobs=_to_int("fragment_jobs", 8),
        index_maintenance_work_mem_mb=_to_int("pg_index_maintenance_work_mem_mb", 2048),
        index_work_mem_mb=_to_int("pg_index_work_mem_mb", 64),
        index_parallel_workers=_to_int("pg_index_parallel_workers", 2),
        index_commit_every_flushes=_to_int("pg_index_commit_every_flushes", 1),
        incremental_index_shards=incremental_shards,
        incremental_index_jobs=incremental_jobs,
        skip_incremental_analyze=_to_bool("pg_skip_incremental_analyze", True),
        build_construct_tables=not _to_bool("pg_skip_construct_tables", False),
        build_constant_smiles_mol_index=not _to_bool("pg_skip_constant_smiles_mol_index", False),
    )


def _canonicalize_smiles(raw: str) -> str:
    try:
        return str(legacy_engine._canonicalize_smiles_for_lookup(raw, canonicalize=True) or "").strip()
    except Exception:
        return ""


def _dataset_stats_payload(stats: verify_service.DatasetStats) -> Dict[str, int]:
    return {
        "compounds": int(stats.compounds),
        "rules": int(stats.rules),
        "pairs": int(stats.pairs),
        "rule_environments": int(stats.rule_environments),
    }


def _dataset_delta_payload(
    before: verify_service.DatasetStats,
    after: verify_service.DatasetStats,
) -> Dict[str, int]:
    return {
        "compounds": int(after.compounds - before.compounds),
        "rules": int(after.rules - before.rules),
        "pairs": int(after.pairs - before.pairs),
        "rule_environments": int(after.rule_environments - before.rule_environments),
    }


def _normalize_value_transform(value: Any) -> str:
    token = _read_text(value).lower()
    allowed = {
        "none",
        "to_pic50_from_nm",
        "to_pic50_from_um",
        "to_ic50_nm_from_pic50",
        "to_ic50_um_from_pic50",
        "log10",
        "neg_log10",
        "from_log10",
        "from_neg_log10",
    }
    return token if token in allowed else "none"


def _apply_value_transform(value: float, transform: str) -> float:
    op = _normalize_value_transform(transform)
    numeric = float(value)
    if op == "none":
        return numeric
    if op in {"to_pic50_from_nm", "to_pic50_from_um", "log10", "neg_log10"} and numeric <= 0:
        raise ValueError(f"value must be > 0 for transform '{op}'")
    if op == "to_pic50_from_nm":
        return 9.0 - float(math.log10(numeric))
    if op == "to_pic50_from_um":
        return 6.0 - float(math.log10(numeric))
    if op == "to_ic50_nm_from_pic50":
        return float(10.0 ** (9.0 - numeric))
    if op == "to_ic50_um_from_pic50":
        return float(10.0 ** (6.0 - numeric))
    if op == "log10":
        return float(math.log10(numeric))
    if op == "neg_log10":
        return float(-math.log10(numeric))
    if op == "from_log10":
        return float(10.0 ** numeric)
    return float(10.0 ** (-numeric))


def _canonicalize_smiles_with_cache(raw_smiles: str, cache: Dict[str, str]) -> str:
    token = _read_text(raw_smiles)
    if not token:
        return ""
    hit = cache.get(token)
    if hit is not None:
        return hit
    clean = _canonicalize_smiles(token)
    cache[token] = clean
    return clean


def _collect_effective_experiment_mappings(
    *,
    column_config: Dict[str, Any],
    mappings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_sources: set[str] = set()

    for item in mappings:
        row = _safe_json_object(item)
        source_property = _read_text(row.get("source_property"))
        mmp_property = _read_text(row.get("mmp_property"))
        if not source_property or not mmp_property:
            continue
        source_key = source_property.lower()
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        next_row = dict(row)
        next_row["source_property"] = source_property
        next_row["mmp_property"] = mmp_property
        next_row["value_transform"] = _normalize_value_transform(row.get("value_transform"))
        merged.append(next_row)

    activity_transform_map = _safe_json_object(column_config.get("activity_transform_map"))
    activity_output_property_map = _safe_json_object(column_config.get("activity_output_property_map"))
    activity_method_map = _safe_json_object(column_config.get("activity_method_map"))
    activity_columns = [
        _read_text(item)
        for item in list(column_config.get("activity_columns") or [])
        if _read_text(item)
    ]

    source_label_by_key: Dict[str, str] = {}
    for source in activity_columns:
        source_key = source.lower()
        if source_key and source_key not in source_label_by_key:
            source_label_by_key[source_key] = source
    for raw_source in activity_output_property_map.keys():
        source = _read_text(raw_source)
        source_key = source.lower()
        if source_key and source_key not in source_label_by_key:
            source_label_by_key[source_key] = source

    normalized_output_by_source: Dict[str, str] = {}
    for raw_source, raw_target in activity_output_property_map.items():
        source_key = _read_text(raw_source).lower()
        target = _read_text(raw_target)
        if not source_key or not target:
            continue
        normalized_output_by_source[source_key] = target

    normalized_method_by_source: Dict[str, str] = {}
    for raw_source, raw_method in activity_method_map.items():
        source_key = _read_text(raw_source).lower()
        method_id = _read_text(raw_method)
        if not source_key or not method_id:
            continue
        normalized_method_by_source[source_key] = method_id

    normalized_transform_by_source: Dict[str, str] = {}
    for raw_source, raw_transform in activity_transform_map.items():
        source_key = _read_text(raw_source).lower()
        if not source_key:
            continue
        normalized_transform_by_source[source_key] = _normalize_value_transform(raw_transform)

    for source_key, source_label in source_label_by_key.items():
        if not source_key or source_key in seen_sources:
            continue
        mapped_property = _read_text(normalized_output_by_source.get(source_key) or source_label)
        if not mapped_property:
            continue
        seen_sources.add(source_key)
        merged.append(
            {
                "source_property": source_label,
                "mmp_property": mapped_property,
                "method_id": _read_text(normalized_method_by_source.get(source_key)),
                "value_transform": _normalize_value_transform(normalized_transform_by_source.get(source_key)),
                "notes": "Batch activity mapping.",
            }
        )

    return merged


def _compute_experiment_property_import_source_signature(
    *,
    batch: Dict[str, Any],
    database_id: str,
    mappings: List[Dict[str, Any]],
) -> str:
    files = _safe_json_object(batch.get("files"))
    experiments_file = _safe_json_object(files.get("experiments"))
    column_config = _safe_json_object(experiments_file.get("column_config"))
    effective_mappings = _collect_effective_experiment_mappings(
        column_config=column_config,
        mappings=mappings,
    )

    normalized_transform_map: Dict[str, str] = {}
    for row in effective_mappings:
        source_key = _read_text(row.get("source_property")).lower()
        if not source_key:
            continue
        normalized_transform_map[source_key] = _normalize_value_transform(row.get("value_transform"))

    signature_payload = {
        "version": 1,
        "database_id": _read_text(database_id),
        "experiments_file": {
            "stored_name": _read_text(experiments_file.get("stored_name")),
            "original_name": _read_text(experiments_file.get("original_name")),
            "size": int(experiments_file.get("size") or 0),
            "uploaded_at": _read_text(experiments_file.get("uploaded_at")),
        },
        "column_config": {
            "smiles_column": _read_text(column_config.get("smiles_column")),
            "property_column": _read_text(column_config.get("property_column")),
            "value_column": _read_text(column_config.get("value_column")),
            "activity_transform_map": normalized_transform_map,
        },
        "mappings": [
            {
                "source_property": _read_text(row.get("source_property")),
                "mmp_property": _read_text(row.get("mmp_property")),
                "method_id": _read_text(row.get("method_id")),
                "value_transform": _normalize_value_transform(row.get("value_transform")),
            }
            for row in sorted(
                effective_mappings,
                key=lambda item: _read_text(_safe_json_object(item).get("source_property")).lower(),
            )
        ],
    }
    encoded = json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def _build_property_import_file_from_experiments_fast(
    *,
    logger,
    store: MmpLifecycleAdminStore,
    batch: Dict[str, Any],
    database_id: str,
    mappings: List[Dict[str, Any]],
    source_signature: str = "",
) -> Dict[str, Any]:
    batch_id = _read_text(batch.get("id"))
    experiments_file = _safe_json_object(_safe_json_object(batch.get("files")).get("experiments"))
    source_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=experiments_file)
    if not source_path or not os.path.exists(source_path):
        raise ValueError("Experiment file is missing. Upload experiments first.")

    column_config = _safe_json_object(experiments_file.get("column_config"))
    delimiter = _detect_delimiter(source_path)

    mapping_by_source: Dict[str, Dict[str, Any]] = {}
    effective_mappings = _collect_effective_experiment_mappings(
        column_config=column_config,
        mappings=mappings,
    )
    for item in effective_mappings:
        key = _read_text(_safe_json_object(item).get("source_property")).lower()
        if key and key not in mapping_by_source:
            mapping_by_source[key] = _safe_json_object(item)
    source_plan_by_key: Dict[str, Tuple[str, str]] = {}
    for source_key, mapping in mapping_by_source.items():
        mapped_property = _read_text((mapping or {}).get("mmp_property"))
        if not mapped_property:
            continue
        source_plan_by_key[source_key] = (mapped_property, _normalize_value_transform((mapping or {}).get("value_transform")))

    rows_by_smiles: Dict[str, Dict[str, float]] = {}
    property_names: List[str] = []
    property_set: set[str] = set()
    rows_total = 0
    rows_mapped = 0
    rows_invalid = 0
    rows_unmapped = 0
    duplicate_shadowed = 0
    canonical_smiles_cache: Dict[str, str] = {}

    with open(source_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        headers: List[str] = []
        header_line_no = 1
        for header_line_no, raw_row in enumerate(reader, start=1):
            candidate = [_read_text(item) for item in raw_row]
            if any(candidate):
                headers = candidate
                break
        if not headers:
            raise ValueError(f"Experiment file has no header: {source_path}")

        resolved_headers = [name for name in headers if name]
        smiles_col = _pick_column(
            resolved_headers,
            _read_text(column_config.get("smiles_column")),
            ["smiles", "clean_smiles", "canonical_smiles", "molecule_smiles"],
        )
        source_property_col = _pick_column(
            resolved_headers,
            _read_text(column_config.get("property_column")),
            ["property", "property_name", "endpoint", "assay_property", "activity_type", "readout"],
        )
        value_col = _pick_column(
            resolved_headers,
            _read_text(column_config.get("value_column")),
            ["value", "activity_value", "result", "measurement", "numeric_value"],
        )

        def _find_column_index(column_name: str) -> int:
            token = _read_text(column_name)
            if not token:
                return -1
            for idx, header in enumerate(headers):
                if _read_text(header) == token:
                    return idx
            return -1

        smiles_idx = _find_column_index(smiles_col)
        source_property_idx = _find_column_index(source_property_col)
        value_idx = _find_column_index(value_col)

        if smiles_idx < 0:
            raise ValueError("Cannot resolve SMILES column from experiment file.")
        if source_property_idx < 0:
            raise ValueError("Cannot resolve source property column from experiment file.")
        if value_idx < 0:
            raise ValueError("Cannot resolve numeric value column from experiment file.")

        for line_no, row in enumerate(reader, start=header_line_no + 1):
            rows_total += 1
            raw_smiles = _read_text(row[smiles_idx] if smiles_idx < len(row) else "")
            source_property = _read_text(row[source_property_idx] if source_property_idx < len(row) else "")
            value_raw = _read_text(row[value_idx] if value_idx < len(row) else "")
            if _is_missing_cell_value(raw_smiles):
                raw_smiles = ""
            if _is_missing_cell_value(source_property):
                source_property = ""
            if _is_missing_cell_value(value_raw):
                value_raw = ""
            if not raw_smiles or not source_property or not value_raw:
                rows_invalid += 1
                continue

            plan = source_plan_by_key.get(source_property.lower())
            if not plan:
                rows_unmapped += 1
                continue
            mapped_property, value_transform = plan

            try:
                source_value = float(value_raw)
            except Exception:
                rows_invalid += 1
                continue
            if value_transform == "none":
                incoming_value = float(source_value)
            else:
                try:
                    incoming_value = _apply_value_transform(source_value, value_transform)
                except Exception:
                    rows_invalid += 1
                    continue

            clean_smiles = _canonicalize_smiles_with_cache(raw_smiles, canonical_smiles_cache)
            if not clean_smiles:
                rows_invalid += 1
                continue

            rows_mapped += 1
            bucket = rows_by_smiles.setdefault(clean_smiles, {})
            if mapped_property in bucket:
                duplicate_shadowed += 1
            bucket[mapped_property] = float(incoming_value)
            if mapped_property not in property_set:
                property_set.add(mapped_property)
                property_names.append(mapped_property)

    generated_dir = Path(source_path).resolve().parent / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / f"property_import_{database_id or 'db'}.tsv"
    fieldnames = ["smiles"] + sorted(property_names)
    with open(generated_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for smiles, row_values in rows_by_smiles.items():
            output = {"smiles": smiles}
            for property_name in fieldnames[1:]:
                if property_name in row_values:
                    output[property_name] = row_values[property_name]
            writer.writerow(output)

    generated_size = int(generated_path.stat().st_size) if generated_path.exists() else 0
    signature = _read_text(source_signature) or _compute_experiment_property_import_source_signature(
        batch=batch,
        database_id=database_id,
        mappings=mappings,
    )
    generated_property_file = {
        "path": str(generated_path),
        "size": generated_size,
        "row_count": len(rows_by_smiles),
        "property_count": len(property_names),
        "database_id": database_id,
        "source_signature": signature,
    }
    logger.info(
        "Lifecycle experiment apply prepare batch=%s db=%s rows=%s mapped=%s deduped=%s unmapped=%s invalid=%s duplicates=%s",
        batch_id,
        database_id,
        rows_total,
        rows_mapped,
        sum(len(bucket) for bucket in rows_by_smiles.values()),
        rows_unmapped,
        rows_invalid,
        duplicate_shadowed,
    )
    return {
        "summary": {
            "rows_total": rows_total,
            "rows_mapped": rows_mapped,
            "rows_will_import": sum(len(bucket) for bucket in rows_by_smiles.values()),
            "rows_unmapped": rows_unmapped,
            "rows_invalid": rows_invalid,
            "rows_shadowed_duplicate": duplicate_shadowed,
        },
        "generated_property_file": generated_property_file,
    }


def _build_property_import_from_experiments(
    *,
    logger,
    target: PostgresTarget,
    store: MmpLifecycleAdminStore,
    batch: Dict[str, Any],
    database_id: str,
    mappings: List[Dict[str, Any]],
    row_limit: int,
    allow_batch_compound_match: bool = True,
) -> Dict[str, Any]:
    batch_id = _read_text(batch.get("id"))
    experiments_file = _safe_json_object(_safe_json_object(batch.get("files")).get("experiments"))
    source_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=experiments_file)
    if not source_path or not os.path.exists(source_path):
        raise ValueError("Experiment file is missing. Upload experiments first.")

    column_config = _safe_json_object(experiments_file.get("column_config"))
    delimiter = _detect_delimiter(source_path)

    with open(source_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [_read_text(item) for item in (reader.fieldnames or []) if _read_text(item)]
        if not headers:
            raise ValueError(f"Experiment file has no header: {source_path}")

        smiles_col = _pick_column(
            headers,
            _read_text(column_config.get("smiles_column")),
            ["smiles", "clean_smiles", "canonical_smiles", "molecule_smiles"],
        )
        source_property_col = _pick_column(
            headers,
            _read_text(column_config.get("property_column")),
            ["property", "property_name", "endpoint", "assay_property", "activity_type", "readout"],
        )
        value_col = _pick_column(
            headers,
            _read_text(column_config.get("value_column")),
            ["value", "activity_value", "result", "measurement", "numeric_value"],
        )
        method_col = _pick_column(
            headers,
            _read_text(column_config.get("method_column")),
            ["method", "assay_method", "method_name", "protocol"],
        )
        notes_col = _pick_column(
            headers,
            _read_text(column_config.get("notes_column")),
            ["notes", "note", "comment", "description"],
        )

        if not smiles_col:
            raise ValueError("Cannot resolve SMILES column from experiment file.")
        if not source_property_col:
            raise ValueError("Cannot resolve source property column from experiment file.")
        if not value_col:
            raise ValueError("Cannot resolve numeric value column from experiment file.")

        mapping_by_source: Dict[str, Dict[str, Any]] = {}
        effective_mappings = _collect_effective_experiment_mappings(
            column_config=column_config,
            mappings=mappings,
        )
        for item in effective_mappings:
            key = _read_text(_safe_json_object(item).get("source_property")).lower()
            if key and key not in mapping_by_source:
                mapping_by_source[key] = _safe_json_object(item)

        parsed_rows: List[Dict[str, Any]] = []
        duplicate_counts: Dict[str, int] = {}
        latest_line_by_key: Dict[str, int] = {}
        canonical_smiles_cache: Dict[str, str] = {}

        for line_no, row in enumerate(reader, start=2):
            raw_smiles = _read_text((row or {}).get(smiles_col))
            source_property = _read_text((row or {}).get(source_property_col))
            value_raw = _read_text((row or {}).get(value_col))
            if _is_missing_cell_value(raw_smiles):
                raw_smiles = ""
            if _is_missing_cell_value(source_property):
                source_property = ""
            if _is_missing_cell_value(value_raw):
                value_raw = ""
            method_value = _read_text((row or {}).get(method_col)) if method_col else ""
            notes_value = _read_text((row or {}).get(notes_col)) if notes_col else ""

            mapping = mapping_by_source.get(source_property.lower()) if source_property else None
            mapped_property = _read_text((mapping or {}).get("mmp_property"))
            mapping_method_id = _read_text((mapping or {}).get("method_id"))
            mapping_notes = _read_text((mapping or {}).get("notes"))
            upload_transform = (
                _normalize_value_transform((mapping or {}).get("value_transform"))
                if source_property and mapping
                else "none"
            )

            value_parsed: Optional[float] = None
            if value_raw and mapped_property:
                try:
                    value_parsed = float(value_raw)
                except Exception:
                    value_parsed = None

            clean_smiles = ""
            if raw_smiles and mapped_property and (value_parsed is not None):
                clean_smiles = _canonicalize_smiles_with_cache(raw_smiles, canonical_smiles_cache)

            incoming_value = value_parsed

            dedupe_key = ""
            if clean_smiles and mapped_property and (incoming_value is not None):
                dedupe_key = f"{clean_smiles}\t{mapped_property}"
                duplicate_counts[dedupe_key] = int(duplicate_counts.get(dedupe_key) or 0) + 1
                latest_line_by_key[dedupe_key] = line_no

            parsed_rows.append(
                {
                    "line_no": line_no,
                    "input_smiles": raw_smiles,
                    "clean_smiles": clean_smiles,
                    "source_property": source_property,
                    "mapped_property": mapped_property,
                    "value_raw": value_raw,
                    "source_value": value_parsed,
                    "incoming_value": incoming_value,
                    "value_transform": upload_transform,
                    "method": method_value,
                    "method_id": mapping_method_id,
                    "notes": notes_value,
                    "mapping_notes": mapping_notes,
                    "dedupe_key": dedupe_key,
                }
            )

    candidate_rows = [
        row
        for row in parsed_rows
        if row.get("dedupe_key") and int(row.get("line_no") or 0) == int(latest_line_by_key.get(str(row.get("dedupe_key"))) or 0)
    ]
    candidate_smiles = sorted({str(row.get("clean_smiles") or "") for row in candidate_rows if str(row.get("clean_smiles") or "")})
    candidate_props = sorted({str(row.get("mapped_property") or "") for row in candidate_rows if str(row.get("mapped_property") or "")})
    batch_compound_smiles = _collect_batch_compound_smiles_for_candidates(
        store=store,
        batch=batch,
        candidate_smiles=candidate_smiles,
    )

    compound_ids: Dict[str, int] = {}
    property_name_ids: Dict[str, int] = {}
    existing_values: Dict[Tuple[str, str], float] = {}

    if psycopg is None:
        raise RuntimeError("psycopg is required for lifecycle checks (pip install psycopg[binary]).")

    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')

            for smiles_chunk in _chunked(candidate_smiles, size=2000):
                cursor.execute(
                    "SELECT clean_smiles, id FROM compound WHERE clean_smiles = ANY(%s)",
                    [smiles_chunk],
                )
                for clean_smiles, compound_id in cursor.fetchall():
                    compound_ids[str(clean_smiles or "").strip()] = int(compound_id)

            for prop_chunk in _chunked(candidate_props, size=2000):
                cursor.execute(
                    "SELECT name, id FROM property_name WHERE name = ANY(%s)",
                    [prop_chunk],
                )
                for prop_name, prop_id in cursor.fetchall():
                    property_name_ids[str(prop_name or "").strip()] = int(prop_id)

            if candidate_smiles and candidate_props:
                for smiles_chunk in _chunked(candidate_smiles, size=1000):
                    for prop_chunk in _chunked(candidate_props, size=100):
                        cursor.execute(
                            """
                            SELECT c.clean_smiles, pn.name, cp.value
                            FROM compound_property cp
                            INNER JOIN compound c ON c.id = cp.compound_id
                            INNER JOIN property_name pn ON pn.id = cp.property_name_id
                            WHERE c.clean_smiles = ANY(%s)
                              AND pn.name = ANY(%s)
                            """,
                            [smiles_chunk, prop_chunk],
                        )
                        for clean_smiles, prop_name, value in cursor.fetchall():
                            key = (str(clean_smiles or "").strip(), str(prop_name or "").strip())
                            try:
                                existing_values[key] = float(value)
                            except Exception:
                                pass

    output_rows: List[Dict[str, Any]] = []
    import_rows: List[Dict[str, Any]] = []

    for row in parsed_rows:
        line_no = int(row.get("line_no") or 0)
        raw_smiles = _read_text(row.get("input_smiles"))
        clean_smiles = _read_text(row.get("clean_smiles"))
        source_property = _read_text(row.get("source_property"))
        mapped_property = _read_text(row.get("mapped_property"))
        incoming_value = row.get("incoming_value")
        source_value = row.get("source_value")
        value_transform = _normalize_value_transform(row.get("value_transform"))
        dedupe_key = _read_text(row.get("dedupe_key"))
        duplicate_count = int(duplicate_counts.get(dedupe_key) or 0) if dedupe_key else 0

        action = ""
        will_import = False
        note = ""
        in_compound_table = False
        in_compound_batch_file = False
        property_name_exists = False
        existing_value: Optional[float] = None

        if not raw_smiles:
            action = "SKIP_EMPTY_SMILES"
            note = "SMILES is empty."
        elif not source_property:
            action = "SKIP_EMPTY_PROPERTY"
            note = "Source property is empty."
        elif not mapped_property:
            action = "SKIP_UNMAPPED_PROPERTY"
            note = "No property mapping found for this source property in selected MMP database."
        elif incoming_value is None:
            action = "SKIP_INVALID_VALUE"
            note = "Numeric value is missing or invalid."
        elif not clean_smiles:
            action = "SKIP_INVALID_SMILES"
            note = "SMILES cannot be canonicalized by RDKit."
        elif dedupe_key and int(latest_line_by_key.get(dedupe_key) or 0) != line_no:
            action = "SKIP_SHADOWED_DUPLICATE"
            note = "Same (SMILES, mapped_property) appears later; latest row will be applied."
        else:
            in_compound_db = clean_smiles in compound_ids
            in_compound_batch_file = clean_smiles in batch_compound_smiles
            in_compound_table = in_compound_db or (allow_batch_compound_match and in_compound_batch_file)
            property_name_exists = mapped_property in property_name_ids
            existing_value = existing_values.get((clean_smiles, mapped_property))

            if not in_compound_table:
                action = "SKIP_UNMATCHED_COMPOUND"
                note = "No matching compound.clean_smiles in selected schema."
            elif not property_name_exists:
                action = "INSERT_PROPERTY_NAME_AND_COMPOUND_PROPERTY"
                will_import = True
                note = (
                    "Property will be created and value inserted for this compound."
                    if in_compound_db
                    else "Compound is from current upload batch; property will be created and value inserted after compound import."
                )
            elif existing_value is None:
                action = "INSERT_COMPOUND_PROPERTY"
                will_import = True
                note = (
                    "No existing compound_property row; will insert."
                    if in_compound_db
                    else "Compound is from current upload batch; value will be inserted after compound import."
                )
            elif _float_equal(existing_value, incoming_value):
                action = "NOOP_VALUE_UNCHANGED"
                will_import = True
                note = "Incoming value equals current value."
            else:
                action = "UPDATE_COMPOUND_PROPERTY"
                will_import = True
                note = "Incoming value differs; compound_property will be updated."

        if duplicate_count > 1:
            if note:
                note = f"{note} Duplicate count={duplicate_count}."
            else:
                note = f"Duplicate count={duplicate_count}."

        next_row = {
            "line_no": line_no,
            "input_smiles": raw_smiles,
            "clean_smiles": clean_smiles,
            "source_property": source_property,
            "mapped_property": mapped_property,
            "source_value": source_value,
            "incoming_value": incoming_value,
            "value_transform": value_transform,
            "method": _read_text(row.get("method")),
            "method_id": _read_text(row.get("method_id")),
            "notes": _read_text(row.get("notes")),
            "mapping_notes": _read_text(row.get("mapping_notes")),
            "duplicate_count": duplicate_count,
            "action": action,
            "will_import": bool(will_import),
            "in_compound_table": bool(in_compound_table),
            "in_compound_batch_file": bool(in_compound_batch_file),
            "property_name_exists": bool(property_name_exists),
            "existing_value": existing_value,
            "note": note,
        }
        output_rows.append(next_row)

        if will_import and clean_smiles and mapped_property and (incoming_value is not None):
            import_rows.append(
                {
                    "line_no": line_no,
                    "clean_smiles": clean_smiles,
                    "property_name": mapped_property,
                    "value": float(incoming_value),
                }
            )

    rows_by_smiles: Dict[str, Dict[str, float]] = {}
    property_names: List[str] = []
    property_set: set[str] = set()

    for item in sorted(import_rows, key=lambda row: int(row.get("line_no") or 0)):
        smiles = _read_text(item.get("clean_smiles"))
        prop_name = _read_text(item.get("property_name"))
        value = float(item.get("value") or 0.0)
        if not smiles or not prop_name:
            continue
        if prop_name not in property_set:
            property_set.add(prop_name)
            property_names.append(prop_name)
        bucket = rows_by_smiles.setdefault(smiles, {})
        bucket[prop_name] = value

    generated_dir = Path(source_path).resolve().parent / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / f"property_import_{database_id or 'db'}.tsv"

    with open(generated_path, "w", encoding="utf-8", newline="") as handle:
        fieldnames = ["smiles"] + sorted(property_names)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for smiles in sorted(rows_by_smiles.keys()):
            row_values = rows_by_smiles[smiles]
            output = {"smiles": smiles}
            for prop_name in fieldnames[1:]:
                if prop_name in row_values:
                    output[prop_name] = row_values[prop_name]
            writer.writerow(output)

    generated_size = int(generated_path.stat().st_size) if generated_path.exists() else 0

    property_preview: Optional[Dict[str, Any]] = None
    if generated_size > 0 and property_names:
        property_preview = check_service.preview_property_import(
            target,
            property_file=str(generated_path),
            smiles_column="smiles",
            canonicalize_smiles=False,
        )

    source_signature = _compute_experiment_property_import_source_signature(
        batch=batch,
        database_id=database_id,
        mappings=mappings,
    )

    summary = {
        "rows_total": len(output_rows),
        "rows_preview": min(max(1, int(row_limit or 200)), len(output_rows)),
        "rows_mapped": sum(1 for row in output_rows if _read_text(row.get("mapped_property"))),
        "rows_will_import": sum(1 for row in output_rows if bool(row.get("will_import"))),
        "rows_unmapped": sum(1 for row in output_rows if _read_text(row.get("action")) == "SKIP_UNMAPPED_PROPERTY"),
        "rows_unmatched_compound": sum(1 for row in output_rows if _read_text(row.get("action")) == "SKIP_UNMATCHED_COMPOUND"),
        "rows_matched_by_batch_compounds": sum(1 for row in output_rows if bool(row.get("in_compound_batch_file"))),
        "rows_invalid": sum(
            1
            for row in output_rows
            if _read_text(row.get("action"))
            in {"SKIP_EMPTY_SMILES", "SKIP_INVALID_SMILES", "SKIP_EMPTY_PROPERTY", "SKIP_INVALID_VALUE"}
        ),
        "action_counts": _count_actions(output_rows),
        "generated_property_file": {
            "path": str(generated_path),
            "size": generated_size,
            "row_count": len(rows_by_smiles),
            "property_count": len(property_names),
            "database_id": database_id,
            "source_signature": source_signature,
        },
    }

    columns = [
        "line_no",
        "input_smiles",
        "clean_smiles",
        "source_property",
        "mapped_property",
        "source_value",
        "incoming_value",
        "value_transform",
        "method",
        "method_id",
        "notes",
        "mapping_notes",
        "duplicate_count",
        "action",
        "will_import",
        "in_compound_table",
        "in_compound_batch_file",
        "property_name_exists",
        "existing_value",
        "note",
    ]

    preview_limit = max(1, int(row_limit or 200))
    preview_rows = output_rows[:preview_limit]

    logger.info(
        "Lifecycle experiment check batch=%s db=%s rows=%s importable=%s",
        _read_text(batch.get("id")),
        database_id,
        len(output_rows),
        summary["rows_will_import"],
    )

    return {
        "summary": summary,
        "columns": columns,
        "rows": preview_rows,
        "total_rows": len(output_rows),
        "truncated": len(output_rows) > preview_limit,
        "generated_property_file": summary["generated_property_file"],
        "property_preview": property_preview,
    }


def register_mmp_lifecycle_admin_routes(
    app,
    *,
    require_api_token,
    logger,
) -> None:
    store = MmpLifecycleAdminStore()
    apply_workers = max(1, int(os.getenv("LEAD_OPT_MMP_LIFECYCLE_APPLY_WORKERS", "1") or 1))
    apply_executor = ThreadPoolExecutor(max_workers=apply_workers, thread_name_prefix="mmp_lifecycle_apply")

    def _assert_database_ready_for_update(database_id: str) -> Dict[str, Any]:
        db_entry = _resolve_catalog_database(database_id, include_stats=True)
        if not _is_database_ready_for_update(db_entry):
            label = _read_text(db_entry.get("label") or db_entry.get("schema") or db_entry.get("id") or database_id)
            raise ValueError(f"Database '{label}' is still building. Please wait until it is ready.")
        return db_entry

    def _acquire_database_update_lock(
        *,
        database_id: str,
        operation: str,
        batch_id: str = "",
        task_id: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        return store.acquire_database_operation_lock(
            database_id=_read_text(database_id),
            operation=_read_text(operation),
            batch_id=_read_text(batch_id),
            task_id=_read_text(task_id),
            note=_read_text(note),
        )

    def _release_database_update_lock(lock_id: str, *, failed: bool = False, error: str = "") -> None:
        token = _read_text(lock_id)
        if not token:
            return
        try:
            store.release_database_operation_lock(
                token,
                status="failed" if failed else "released",
                error=_read_text(error),
            )
        except Exception as exc:
            logger.warning("Failed releasing database operation lock %s: %s", token, exc)

    def _apply_batch_sync(batch_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload_obj = _safe_json_object(payload)
        task_id = _read_text(payload_obj.get("task_id"))
        timings_s: Dict[str, float] = {}
        stage_starts: Dict[str, float] = {}

        def _mark_progress(stage: str, message: str = "") -> None:
            if not task_id:
                return
            try:
                store.mark_apply_progress(
                    batch_id,
                    task_id=task_id,
                    stage=stage,
                    message=message,
                    timings_s=timings_s,
                )
            except Exception as exc:
                logger.warning("Failed to update apply progress: batch=%s task=%s stage=%s err=%s", batch_id, task_id, stage, exc)

        def _start_stage(stage: str, message: str) -> None:
            stage_starts[stage] = time.perf_counter()
            _mark_progress(stage, message)

        def _finish_stage(stage: str) -> None:
            started = stage_starts.get(stage)
            if started is None:
                return
            timings_s[stage] = max(0.0, time.perf_counter() - started)

        batch = store.get_batch(batch_id)
        database_id = _read_text(payload_obj.get("database_id") or batch.get("selected_database_id"))
        if not database_id:
            raise ValueError("database_id is required.")

        _start_stage("preflight", "Preparing batch")
        selected, target = _resolve_database_target(database_id)
        _sync_assay_methods_for_database(store, selected, allow_auto_queue_updates=True)
        files = _safe_json_object(batch.get("files"))
        compounds_file = _safe_json_object(files.get("compounds"))
        experiments_file = _safe_json_object(files.get("experiments"))
        import_compounds = bool(payload_obj.get("import_compounds", True)) and bool(compounds_file)
        import_experiments = bool(payload_obj.get("import_experiments", True)) and bool(experiments_file)
        if _read_text(batch.get("selected_database_id")) != database_id:
            batch = store.update_batch(batch_id, {"selected_database_id": database_id})

        if not import_compounds and not import_experiments:
            raise ValueError("Nothing to apply. Upload compounds and/or experiments first.")

        last_check = _safe_json_object(batch.get("last_check"))
        check_policy = _normalize_check_policy(_safe_json_object(payload_obj.get("check_policy") or last_check.get("check_policy")))
        check_policy["require_approved_status"] = False
        check_gate = _build_check_gate(
            batch=batch,
            database_id=database_id,
            import_compounds=import_compounds,
            import_experiments=import_experiments,
            policy=check_policy,
        )
        _finish_stage("preflight")

        _start_stage("pending_sync", "Applying pending property sync")
        pending_sync_result = _apply_pending_property_sync(database_id, target)
        _finish_stage("pending_sync")

        import_batch_id = _read_text(payload_obj.get("import_batch_id")) or _read_text(batch.get("id"))
        import_label = _read_text(payload_obj.get("import_label") or batch.get("name"))
        import_notes = _read_text(payload_obj.get("import_notes") or batch.get("notes"))

        before = verify_service.fetch_dataset_stats(target)
        compound_ok = None
        property_ok = None
        generated_property_meta: Dict[str, Any] = {}
        compounds_skipped_reason = ""

        compound_options = _extract_compound_import_options(payload_obj)
        compound_summary = _safe_json_object(last_check.get("compound_summary"))
        checked_database_id = _read_text(last_check.get("database_id"))
        checked_at = _read_text(last_check.get("checked_at"))
        checked_at_dt = _parse_utc_iso(checked_at)
        compounds_uploaded_at_dt = _parse_utc_iso(_safe_json_object(compounds_file).get("uploaded_at"))
        check_is_current_for_compounds = bool(
            checked_at_dt
            and compounds_uploaded_at_dt
            and compounds_uploaded_at_dt <= checked_at_dt
            and checked_database_id == database_id
        )

        if import_compounds and check_is_current_for_compounds:
            reindex_rows = _to_nonneg_int(compound_summary.get("reindex_rows"), 0)
            if reindex_rows <= 0:
                compounds_skipped_reason = "No structural compound delta detected in latest check; skipped compound import."
                logger.info(
                    "Lifecycle apply skipped compounds import: batch=%s db=%s reason=%s",
                    batch_id,
                    database_id,
                    compounds_skipped_reason,
                )
                import_compounds = False
                _mark_progress("preflight", "No structural compound delta; skipped compound import")

        if import_compounds:
            _start_stage("import_compounds", "Importing compounds")
            structures_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=compounds_file)
            if not structures_path or not os.path.exists(structures_path):
                raise ValueError("Compound file not found for batch.")
            compound_cfg = _safe_json_object(compounds_file.get("column_config"))
            compound_ok, compound_error = setup_service.import_compound_batch_with_diagnostics(
                target,
                structures_file=structures_path,
                batch_id=import_batch_id,
                batch_label=import_label,
                batch_notes=import_notes,
                smiles_column=_read_text(compound_cfg.get("smiles_column")),
                id_column=_read_text(compound_cfg.get("id_column")),
                canonicalize_smiles=True,
                **compound_options.to_setup_kwargs(),
                overwrite_existing_batch=True,
            )
            if not compound_ok:
                detail = _read_text(compound_error)
                if not detail:
                    detail = _read_text(
                        setup_service.get_compound_import_failure_diagnostic(
                            target,
                            structures_file=structures_path,
                            output_dir=compound_options.output_dir,
                        )
                    )
                if detail:
                    raise RuntimeError(f"Compound incremental import failed: {detail}")
                raise RuntimeError("Compound incremental import failed")
            _finish_stage("import_compounds")

        experiments_skipped_reason = ""
        if import_experiments:
            _start_stage("prepare_experiments", "Preparing experiment property file")
            mappings = store.list_property_mappings(database_id=database_id)
            source_signature = _compute_experiment_property_import_source_signature(
                batch=batch,
                database_id=database_id,
                mappings=mappings,
            )
            generated_file_meta = _safe_json_object(_safe_json_object(batch.get("files")).get("generated_property_import"))
            cached_generated_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=generated_file_meta)
            cached_signature = _read_text(generated_file_meta.get("source_signature"))
            cached_database_id = _read_text(generated_file_meta.get("database_id"))
            cached_row_count = int(generated_file_meta.get("row_count") or 0)
            cached_property_count = int(generated_file_meta.get("property_count") or 0)

            if (
                cached_generated_path
                and os.path.exists(cached_generated_path)
                and cached_database_id == database_id
                and cached_signature
                and cached_signature == source_signature
                and cached_row_count > 0
                and cached_property_count > 0
            ):
                generated_property_meta = {
                    "path": cached_generated_path,
                    "size": int(os.path.getsize(cached_generated_path)),
                    "row_count": cached_row_count,
                    "property_count": cached_property_count,
                    "database_id": database_id,
                    "source_signature": source_signature,
                }
                generated_path = cached_generated_path
                logger.info(
                    "Lifecycle apply reused cached generated property file: batch=%s db=%s rows=%s properties=%s",
                    batch_id,
                    database_id,
                    cached_row_count,
                    cached_property_count,
                )
                _mark_progress("prepare_experiments", "Reused generated property file")
            else:
                generated_build = _build_property_import_file_from_experiments_fast(
                    logger=logger,
                    store=store,
                    batch=batch,
                    database_id=database_id,
                    mappings=mappings,
                    source_signature=source_signature,
                )
                generated_property_meta = _safe_json_object(generated_build.get("generated_property_file"))
                generated_path = _read_text(generated_property_meta.get("path"))
                store.set_generated_property_import_file(batch_id, generated_path, generated_property_meta)
            _finish_stage("prepare_experiments")

            if not generated_path or not os.path.exists(generated_path):
                raise RuntimeError("Generated property import file is missing")
            if int(generated_property_meta.get("row_count") or 0) <= 0 or int(generated_property_meta.get("property_count") or 0) <= 0:
                experiments_skipped_reason = (
                    "No mapped experiment rows are ready for import; skipped experiment import for this apply."
                )
                logger.info(
                    "Lifecycle apply skipped experiment import: batch=%s db=%s reason=%s",
                    batch_id,
                    database_id,
                    experiments_skipped_reason,
                )
                _mark_progress("prepare_experiments", "No mapped experiment rows; skipped experiment import")
                import_experiments = False

            if import_experiments:
                _start_stage("import_experiments", "Importing experiments")
                property_ok = setup_service.import_property_batch(
                    target,
                    property_file=generated_path,
                    batch_id=import_batch_id,
                    batch_label=import_label,
                    batch_notes=import_notes,
                    smiles_column="smiles",
                    canonicalize_smiles=False,
                    overwrite_existing_batch=True,
                )
                if not property_ok:
                    raise RuntimeError("Property incremental import failed")
                _finish_stage("import_experiments")

        _start_stage("finalize", "Finalizing apply")
        after = verify_service.fetch_dataset_stats(target)
        verify_service.persist_dataset_stats(target, after)
        before_payload = _dataset_stats_payload(before)
        after_payload = _dataset_stats_payload(after)
        delta_payload = _dataset_delta_payload(before, after)
        apply_id = f"apply_{uuid.uuid4().hex[:12]}"
        _finish_stage("finalize")
        timings_s["total"] = sum(value for value in timings_s.values())
        updated_batch = store.append_apply_history(
            batch_id,
            {
                "apply_id": apply_id,
                "database_id": database_id,
                "database_label": _read_text(selected.get("label") or selected.get("schema") or database_id),
                "database_schema": _read_text(selected.get("schema")),
                "import_batch_id": import_batch_id,
                "import_compounds": bool(import_compounds),
                "import_experiments": bool(import_experiments),
                "compound_ok": bool(compound_ok) if compound_ok is not None else None,
                "property_ok": bool(property_ok) if property_ok is not None else None,
                "compounds_skipped_reason": compounds_skipped_reason,
                "experiments_skipped_reason": experiments_skipped_reason,
                "generated_property_file": generated_property_meta,
                "timings_s": timings_s,
                "pending_database_sync": pending_sync_result,
                "before": before_payload,
                "after": after_payload,
                "delta": delta_payload,
            },
        )
        if _read_text(updated_batch.get("selected_database_id")) != database_id:
            updated_batch = store.update_batch(batch_id, {"selected_database_id": database_id})

        return {
            "batch": updated_batch,
            "apply_id": apply_id,
            "check_gate": check_gate,
            "pending_database_sync": pending_sync_result,
            "database": {
                "id": _read_text(selected.get("id") or database_id),
                "label": _read_text(selected.get("label") or selected.get("schema") or database_id),
                "schema": _read_text(selected.get("schema")),
            },
            "import_batch_id": import_batch_id,
            "import_compounds": bool(import_compounds),
            "import_experiments": bool(import_experiments),
            "compounds_skipped_reason": compounds_skipped_reason,
            "experiments_skipped_reason": experiments_skipped_reason,
            "timings_s": timings_s,
            "before": before_payload,
            "after": after_payload,
            "delta": delta_payload,
        }

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/overview', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_overview():
        try:
            catalog = _filter_lifecycle_catalog_databases(
                mmp_database_registry.get_mmp_database_catalog(
                    include_hidden=True,
                    include_stats=True,
                    include_properties=False,
                )
            )
            pending_rows = store.list_pending_database_sync(pending_only=True)
            allowed_database_ids = {
                _read_text(item.get("id"))
                for item in catalog.get("databases", [])
                if isinstance(item, dict) and _read_text(item.get("id"))
            }
            pending_sync_by_database: Dict[str, int] = {}
            for item in pending_rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                if not db_id:
                    continue
                if allowed_database_ids and db_id not in allowed_database_ids:
                    continue
                pending_sync_by_database[db_id] = int(pending_sync_by_database.get(db_id, 0)) + 1
            if allowed_database_ids:
                pending_rows = [
                    _safe_json_object(item)
                    for item in pending_rows
                    if _read_text(_safe_json_object(item).get("database_id")) in allowed_database_ids
                ]
            active_db_locks = store.list_database_operation_locks(active_only=True)
            if allowed_database_ids:
                active_db_locks = [
                    _safe_json_object(item)
                    for item in active_db_locks
                    if _read_text(_safe_json_object(item).get("database_id")) in allowed_database_ids
                ]
            busy_operations_by_database: Dict[str, int] = {}
            for item in active_db_locks:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                if not db_id:
                    continue
                busy_operations_by_database[db_id] = int(busy_operations_by_database.get(db_id, 0)) + 1
            return jsonify(
                {
                    "databases": catalog.get("databases", []),
                    "default_database_id": catalog.get("default_database_id", ""),
                    "methods": store.list_methods(),
                    "batches": store.list_batches(summary=True),
                    "pending_database_sync": pending_rows,
                    "pending_sync_by_database": pending_sync_by_database,
                    "database_operation_locks": active_db_locks,
                    "busy_operations_by_database": busy_operations_by_database,
                    "updated_at": catalog.get("generated_at") if isinstance(catalog, dict) else "",
                }
            )
        except Exception as exc:
            logger.exception('Failed to load MMP lifecycle overview: %s', exc)
            return jsonify({'error': f'Failed to load lifecycle overview: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/database_sync_queue', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_database_sync_queue():
        database_id = _read_text(request.args.get("database_id"))
        include_applied = _to_bool(request.args.get("include_applied"), False)
        try:
            rows = store.list_pending_database_sync(database_id=database_id, pending_only=(not include_applied))
            by_database: Dict[str, int] = {}
            for item in rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                if not db_id:
                    continue
                by_database[db_id] = int(by_database.get(db_id, 0)) + 1
            return jsonify(
                {
                    "database_id": database_id,
                    "pending_only": not include_applied,
                    "rows": rows,
                    "pending_by_database": by_database,
                }
            )
        except Exception as exc:
            logger.exception('Failed to list pending database sync queue: %s', exc)
            return jsonify({'error': f'Failed to list database sync queue: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_list_batches():
        include_full_history = _to_bool(request.args.get("include_full_history"), False)
        try:
            return jsonify({"batches": store.list_batches(summary=(not include_full_history))})
        except Exception as exc:
            logger.exception('Failed to list lifecycle batches: %s', exc)
            return jsonify({'error': f'Failed to list batches: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_create_batch():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        try:
            batch = store.create_batch(payload)
            return jsonify({"batch": batch}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to create lifecycle batch: %s', exc)
            return jsonify({'error': f'Failed to create batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_get_batch(batch_id: str):
        try:
            return jsonify({"batch": store.get_batch(batch_id)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            logger.exception('Failed to get lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to get batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>', methods=['PATCH'])
    @require_api_token
    def mmp_lifecycle_update_batch(batch_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        try:
            batch = store.update_batch(batch_id, payload)
            return jsonify({"batch": batch})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to update lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to update batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/status', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_transition_batch_status(batch_id: str):
        payload = request.get_json(silent=True) or {}
        payload = _safe_json_object(payload)
        action = _read_text(payload.get("action")).lower()
        note = _read_text(payload.get("note"))
        try:
            current_batch = store.get_batch(batch_id)
            database_id = _read_text(payload.get("database_id") or current_batch.get("selected_database_id"))
            gate_payload = None
            if action == "approve":
                files = _safe_json_object(current_batch.get("files"))
                gate_payload = _build_check_gate(
                    batch=current_batch,
                    database_id=database_id,
                    import_compounds=bool(_safe_json_object(files.get("compounds"))),
                    import_experiments=bool(_safe_json_object(files.get("experiments"))),
                    policy={"require_approved_status": False},
                )
                if not bool(gate_payload.get("passed")):
                    return jsonify(
                        {
                            "error": "Batch cannot be approved because check gate failed.",
                            "check_gate": gate_payload,
                        }
                    ), 400
            updated_batch = store.transition_batch_status(batch_id, action=action, note=note)
            return jsonify(
                {
                    "batch": updated_batch,
                    "check_gate": gate_payload,
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to transition lifecycle batch status %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to transition batch status: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>', methods=['DELETE'])
    @require_api_token
    def mmp_lifecycle_delete_batch(batch_id: str):
        payload = _safe_json_object(request.get_json(silent=True) or {})
        lock_id = ""
        try:
            batch = store.get_batch(batch_id)
            current_runtime = _safe_json_object(batch.get("apply_runtime"))
            current_phase = _read_text(current_runtime.get("phase")).lower()
            if current_phase in {"queued", "running", "deleting"}:
                return jsonify({"error": f"Batch has an active task. Current phase: {current_phase}."}), 409

            apply_history = batch.get("apply_history") if isinstance(batch.get("apply_history"), list) else []
            requested_apply_id = _read_text(payload.get("apply_id"))
            requested_database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))

            selected_apply: Optional[Dict[str, Any]] = None
            for item in reversed(apply_history):
                row = _safe_json_object(item)
                apply_id = _read_text(row.get("apply_id"))
                apply_db_id = _read_text(row.get("database_id"))
                if requested_apply_id and apply_id != requested_apply_id:
                    continue
                if requested_database_id and apply_db_id and apply_db_id != requested_database_id:
                    continue
                selected_apply = row
                break

            if requested_apply_id and not selected_apply:
                return jsonify({"error": "Requested apply_id not found in batch apply history."}), 400

            database_id = _read_text(
                payload.get("database_id")
                or (selected_apply or {}).get("database_id")
                or batch.get("selected_database_id")
            )
            import_batch_id = _read_text((selected_apply or {}).get("import_batch_id") or batch_id)
            rollback_compounds = bool(payload.get("rollback_compounds", True)) and bool(
                (selected_apply or {}).get("import_compounds")
            )
            rollback_experiments = bool(payload.get("rollback_experiments", True)) and bool(
                (selected_apply or {}).get("import_experiments")
            )
            cleanup_required = bool(selected_apply) and bool(rollback_compounds or rollback_experiments)

            if cleanup_required:
                if not database_id:
                    return jsonify({"error": "database_id is required for database cleanup delete."}), 400
                _assert_database_ready_for_update(database_id)
                try:
                    lock_row = _acquire_database_update_lock(
                        database_id=database_id,
                        operation="delete_batch",
                        batch_id=batch_id,
                        note="delete_with_db_cleanup",
                    )
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 409
                lock_id = _read_text(lock_row.get("id"))

            task_id = f"delete_task_{uuid.uuid4().hex[:12]}"
            queued_batch = store.mark_delete_queued(
                batch_id,
                task_id=task_id,
                database_id=database_id,
                import_batch_id=import_batch_id,
                rollback_compounds=rollback_compounds,
                rollback_experiments=rollback_experiments,
            )

            worker_payload = dict(payload)
            worker_payload["database_id"] = database_id
            worker_payload["import_batch_id"] = import_batch_id
            worker_payload["rollback_compounds"] = bool(rollback_compounds)
            worker_payload["rollback_experiments"] = bool(rollback_experiments)

            def _run_delete_task() -> None:
                lock_failed = False
                lock_error = ""
                try:
                    store.mark_delete_running(batch_id, task_id=task_id, stage="preflight", message="Deleting batch")
                    if cleanup_required:
                        _, target = _resolve_database_target(database_id)
                        if bool(worker_payload.get("rollback_experiments")):
                            store.mark_delete_running(
                                batch_id,
                                task_id=task_id,
                                stage="delete_experiments",
                                message="Deleting experiment batch",
                            )
                            property_deleted = setup_service.delete_property_batch(
                                target,
                                batch_id=import_batch_id,
                            )
                            if not property_deleted:
                                raise RuntimeError("Property batch cleanup failed while deleting batch.")
                        if bool(worker_payload.get("rollback_compounds")):
                            store.mark_delete_running(
                                batch_id,
                                task_id=task_id,
                                stage="delete_compounds",
                                message="Deleting compound batch",
                            )
                            compound_options = _extract_compound_import_options(worker_payload)
                            compound_deleted = setup_service.delete_compound_batch(
                                target,
                                batch_id=import_batch_id,
                                **compound_options.to_setup_kwargs(),
                            )
                            if not compound_deleted:
                                raise RuntimeError("Compound batch cleanup failed while deleting batch.")
                    store.mark_delete_running(batch_id, task_id=task_id, stage="finalize", message="Finalizing delete")
                    store.delete_batch(batch_id)
                    logger.info("Lifecycle batch deleted: batch=%s task=%s", batch_id, task_id)
                except Exception as exc:
                    err_text = str(exc or "delete failed")
                    lock_failed = True
                    lock_error = err_text
                    logger.exception("Lifecycle batch delete failed: batch=%s task=%s err=%s", batch_id, task_id, err_text)
                    try:
                        store.mark_delete_failed(batch_id, task_id=task_id, error=err_text)
                    except Exception:
                        logger.exception("Failed marking lifecycle batch delete as failed: batch=%s task=%s", batch_id, task_id)
                finally:
                    if lock_id:
                        _release_database_update_lock(lock_id, failed=lock_failed, error=lock_error)

            try:
                apply_executor.submit(_run_delete_task)
            except Exception as exc:
                if lock_id:
                    _release_database_update_lock(lock_id, failed=True, error=str(exc))
                    lock_id = ""
                store.mark_delete_failed(batch_id, task_id=task_id, error=f"Failed to submit delete task: {exc}")
                raise RuntimeError(f"Failed to enqueue delete task: {exc}") from exc

            return jsonify(
                {
                    "queued": True,
                    "task_id": task_id,
                    "database_lock_id": lock_id,
                    "batch": queued_batch,
                    "cleanup_required": cleanup_required,
                    "rollback_compounds": bool(rollback_compounds),
                    "rollback_experiments": bool(rollback_experiments),
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            logger.exception('Failed to delete lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to delete batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/upload_compounds', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_upload_compounds(batch_id: str):
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "file is required"}), 400
        data = file.read()
        if not data:
            return jsonify({"error": "uploaded compound file is empty"}), 400
        column_config = {
            "smiles_column": _read_text(request.form.get("smiles_column")),
            "id_column": _read_text(request.form.get("id_column")),
        }
        try:
            batch = store.attach_batch_file(
                batch_id,
                file_kind="compounds",
                source_filename=file.filename or "compounds.tsv",
                body=data,
                column_config=column_config,
            )
            return jsonify({"batch": batch})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to upload compound file for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to upload compounds: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/upload_experiments', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_upload_experiments(batch_id: str):
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "file is required"}), 400
        data = file.read()
        if not data:
            return jsonify({"error": "uploaded experiment file is empty"}), 400
        raw_activity_columns = _read_text(request.form.get("activity_columns"))
        activity_columns: List[str] = []
        if raw_activity_columns:
            try:
                parsed = json.loads(raw_activity_columns)
                if isinstance(parsed, list):
                    activity_columns = [_read_text(item) for item in parsed if _read_text(item)]
                else:
                    activity_columns = [
                        _read_text(item)
                        for item in str(raw_activity_columns).replace("|", ",").split(",")
                        if _read_text(item)
                    ]
            except Exception:
                activity_columns = [
                    _read_text(item)
                    for item in str(raw_activity_columns).replace("|", ",").split(",")
                    if _read_text(item)
                ]
        raw_activity_method_map = _read_text(request.form.get("activity_method_map"))
        activity_method_map: Dict[str, str] = {}
        if raw_activity_method_map:
            try:
                parsed_map = json.loads(raw_activity_method_map)
                if isinstance(parsed_map, dict):
                    for key, value in parsed_map.items():
                        col = _read_text(key)
                        method_id = _read_text(value)
                        if not col or not method_id:
                            continue
                        activity_method_map[col] = method_id
            except Exception:
                activity_method_map = {}
        raw_activity_transform_map = _read_text(request.form.get("activity_transform_map"))
        activity_transform_map: Dict[str, str] = {}
        if raw_activity_transform_map:
            try:
                parsed_map = json.loads(raw_activity_transform_map)
                if isinstance(parsed_map, dict):
                    for key, value in parsed_map.items():
                        col = _read_text(key)
                        transform = _read_text(value)
                        if not col or not transform:
                            continue
                        activity_transform_map[col] = transform
            except Exception:
                activity_transform_map = {}
        raw_activity_output_property_map = _read_text(request.form.get("activity_output_property_map"))
        activity_output_property_map: Dict[str, str] = {}
        if raw_activity_output_property_map:
            try:
                parsed_map = json.loads(raw_activity_output_property_map)
                if isinstance(parsed_map, dict):
                    for key, value in parsed_map.items():
                        col = _read_text(key)
                        prop_name = _read_text(value)
                        if not col or not prop_name:
                            continue
                        activity_output_property_map[col] = prop_name
            except Exception:
                activity_output_property_map = {}
        column_config = {
            "smiles_column": _read_text(request.form.get("smiles_column")),
            "property_column": _read_text(request.form.get("property_column")),
            "value_column": _read_text(request.form.get("value_column")),
            "method_column": _read_text(request.form.get("method_column")),
            "notes_column": _read_text(request.form.get("notes_column")),
            "activity_columns": activity_columns,
            "activity_method_map": activity_method_map,
            "activity_transform_map": activity_transform_map,
            "activity_output_property_map": activity_output_property_map,
            "assay_method_id": _read_text(request.form.get("assay_method_id")),
            "assay_method_key": _read_text(request.form.get("assay_method_key")),
            "source_notes_column": _read_text(request.form.get("source_notes_column")),
        }
        try:
            batch = store.attach_batch_file(
                batch_id,
                file_kind="experiments",
                source_filename=file.filename or "experiments.tsv",
                body=data,
                column_config=column_config,
            )
            return jsonify({"batch": batch})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to upload experiment file for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to upload experiments: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/clear_experiments', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_clear_experiments(batch_id: str):
        try:
            batch = store.clear_batch_file(batch_id, file_kind="experiments")
            return jsonify({"batch": batch})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to clear experiment file for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to clear experiments: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/materialize_experiments_from_compounds', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_materialize_experiments_from_compounds(batch_id: str):
        try:
            payload = _safe_json_object(request.get_json(silent=True) or {})
            batch = store.get_batch(batch_id)
            files = _safe_json_object(batch.get("files"))
            compounds_meta = _safe_json_object(files.get("compounds"))
            source_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=compounds_meta)
            if not source_path or not os.path.exists(source_path):
                return jsonify({"error": "Saved compound file is missing on server. Please re-upload compounds."}), 400

            compounds_cfg = _safe_json_object(compounds_meta.get("column_config"))
            requested_smiles_col = _read_text(payload.get("smiles_column") or compounds_cfg.get("smiles_column"))
            raw_activity_cols = payload.get("activity_columns")
            activity_columns = [
                _read_text(item)
                for item in (raw_activity_cols if isinstance(raw_activity_cols, list) else [])
                if _read_text(item)
            ]
            raw_method_map = _safe_json_object(payload.get("activity_method_map"))
            raw_transform_map = _safe_json_object(payload.get("activity_transform_map"))
            raw_output_map = _safe_json_object(payload.get("activity_output_property_map"))
            if not activity_columns:
                seeded = list(raw_output_map.keys()) + list(raw_method_map.keys()) + list(raw_transform_map.keys())
                activity_columns = [_read_text(item) for item in seeded if _read_text(item)]
            if not activity_columns:
                return jsonify({"error": "activity_columns is required to materialize experiments."}), 400

            deduped_activity_cols: List[str] = []
            seen_cols: set[str] = set()
            for col in activity_columns:
                key = col.lower()
                if not key or key in seen_cols:
                    continue
                seen_cols.add(key)
                deduped_activity_cols.append(col)

            method_map_by_key: Dict[str, str] = {}
            for raw_key, raw_value in raw_method_map.items():
                source_key = _read_text(raw_key).lower()
                method_id = _read_text(raw_value)
                if source_key and method_id:
                    method_map_by_key[source_key] = method_id
            transform_map_by_key: Dict[str, str] = {}
            for raw_key, raw_value in raw_transform_map.items():
                source_key = _read_text(raw_key).lower()
                if not source_key:
                    continue
                transform_map_by_key[source_key] = _normalize_value_transform(raw_value)
            output_map_by_key: Dict[str, str] = {}
            for raw_key, raw_value in raw_output_map.items():
                source_key = _read_text(raw_key).lower()
                prop_name = _read_text(raw_value)
                if source_key and prop_name:
                    output_map_by_key[source_key] = prop_name

            delimiter = _detect_delimiter(source_path)
            all_headers: List[str] = []
            resolved_smiles_col = ""
            resolved_activity_cols: List[str] = []
            rows_total = 0
            rows_emitted = 0
            rows_invalid_value = 0
            rows_transformed = 0

            content = io.StringIO()
            writer = csv.writer(content, delimiter="\t", lineterminator="\n")
            writer.writerow(["smiles", "source_property", "value"])

            with open(source_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                all_headers = [_read_text(item) for item in list(reader.fieldnames or []) if _read_text(item)]
                if not all_headers:
                    return jsonify({"error": "Saved compound file has no header row."}), 400

                resolved_smiles_col = _pick_column(
                    all_headers,
                    requested_smiles_col,
                    ["smiles", "canonical_smiles", "mol_smiles", "structure", "smi"],
                )
                if not resolved_smiles_col:
                    return jsonify({"error": "Cannot resolve smiles_column from saved compounds file."}), 400

                header_by_key: Dict[str, str] = {}
                for header in all_headers:
                    key = _read_text(header).lower()
                    if key and key not in header_by_key:
                        header_by_key[key] = header
                for col in deduped_activity_cols:
                    resolved = header_by_key.get(col.lower())
                    if resolved and resolved not in resolved_activity_cols:
                        resolved_activity_cols.append(resolved)
                if not resolved_activity_cols:
                    return jsonify({"error": "None of activity_columns exists in saved compounds file headers."}), 400

                for raw_row in reader:
                    row = raw_row or {}
                    smiles_value = _read_text(row.get(resolved_smiles_col))
                    if _is_missing_cell_value(smiles_value):
                        continue
                    for source_col in resolved_activity_cols:
                        raw_value = _read_text(row.get(source_col))
                        if _is_missing_cell_value(raw_value):
                            continue
                        rows_total += 1
                        try:
                            numeric_value = float(raw_value)
                        except Exception:
                            rows_invalid_value += 1
                            continue
                        transform = _normalize_value_transform(transform_map_by_key.get(source_col.lower()))
                        incoming_value = numeric_value
                        if transform != "none":
                            try:
                                incoming_value = _apply_value_transform(numeric_value, transform)
                                rows_transformed += 1
                            except Exception:
                                rows_invalid_value += 1
                                continue
                        writer.writerow([smiles_value, source_col, f"{float(incoming_value):.12g}"])
                        rows_emitted += 1

            activity_method_map = {
                col: _read_text(method_map_by_key.get(col.lower()))
                for col in resolved_activity_cols
                if _read_text(method_map_by_key.get(col.lower()))
            }
            activity_transform_map = {
                col: _normalize_value_transform(transform_map_by_key.get(col.lower()))
                for col in resolved_activity_cols
                if _normalize_value_transform(transform_map_by_key.get(col.lower())) != "none"
            }
            activity_output_property_map = {
                col: _read_text(output_map_by_key.get(col.lower()) or col)
                for col in resolved_activity_cols
            }
            column_config = {
                "smiles_column": "smiles",
                "property_column": "source_property",
                "value_column": "value",
                "method_column": "",
                "notes_column": "",
                "activity_columns": resolved_activity_cols,
                "activity_method_map": activity_method_map,
                "activity_transform_map": activity_transform_map,
                "activity_output_property_map": activity_output_property_map,
                "assay_method_id": "",
                "assay_method_key": "",
                "source_notes_column": "",
            }
            body = content.getvalue().encode("utf-8")
            updated_batch = store.attach_batch_file(
                batch_id,
                file_kind="experiments",
                source_filename="experiments_from_compounds.tsv",
                body=body,
                column_config=column_config,
            )
            return jsonify(
                {
                    "batch": updated_batch,
                    "summary": {
                        "source_headers": all_headers,
                        "smiles_column": resolved_smiles_col,
                        "activity_columns": resolved_activity_cols,
                        "rows_total": rows_total,
                        "rows_emitted": rows_emitted,
                        "rows_invalid_value": rows_invalid_value,
                        "rows_transformed": rows_transformed,
                    },
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to materialize experiments from compounds for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to materialize experiments from compounds: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/preview_compounds', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_preview_compounds(batch_id: str):
        try:
            batch = store.get_batch(batch_id)
            files = _safe_json_object(batch.get("files"))
            compounds_meta = _safe_json_object(files.get("compounds"))
            source_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=compounds_meta)
            if not source_path or not os.path.exists(source_path):
                return jsonify({"error": "Saved compound file is missing on server. Please re-upload compounds."}), 404
            row_limit = max(1, min(2000, int(request.args.get("max_rows") or 500)))
            preview = _build_compounds_preview(source_path, max_rows=row_limit)
            return jsonify(
                {
                    "batch_id": _read_text(batch.get("id") or batch_id),
                    "original_name": _read_text(compounds_meta.get("original_name")),
                    "stored_name": _read_text(compounds_meta.get("stored_name")),
                    **preview,
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to preview compounds file for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to preview compounds file: {exc}'}), 500

    def _find_method_or_raise(method_id: str) -> Dict[str, Any]:
        token = _read_text(method_id)
        if not token:
            raise ValueError("method_id is required")
        for item in store.list_methods():
            row = _safe_json_object(item)
            if _read_text(row.get("id")) == token:
                return row
        raise ValueError(f"Method '{token}' not found")

    def _upsert_method_mapping_for_database(
        *,
        database_id: str,
        method_id: str,
        source_property: str,
        mmp_property: str,
        value_transform: str = "none",
        notes: str = "",
    ) -> List[Dict[str, Any]]:
        db_token = _read_text(database_id)
        method_token = _read_text(method_id)
        source_token = _read_text(source_property)
        mmp_token = _read_text(mmp_property)
        transform_token = _normalize_value_transform(value_transform)
        if not db_token or not method_token or not source_token or not mmp_token:
            return store.list_property_mappings(database_id=db_token)
        rows = store.list_property_mappings(database_id=db_token)
        normalized_source = source_token.lower()
        normalized_rows: List[Dict[str, Any]] = []
        inserted = False
        for item in rows:
            row = _safe_json_object(item)
            src = _read_text(row.get("source_property"))
            method_ref = _read_text(row.get("method_id"))
            if src.lower() == normalized_source or method_ref == method_token:
                if inserted:
                    continue
                normalized_rows.append(
                    {
                        "id": _read_text(row.get("id")) or f"map_{uuid.uuid4().hex[:10]}",
                        "source_property": source_token,
                        "mmp_property": mmp_token,
                        "method_id": method_token,
                        "value_transform": transform_token,
                        "notes": notes or _read_text(row.get("notes")),
                    }
                )
                inserted = True
            else:
                normalized_rows.append(
                    {
                        "id": _read_text(row.get("id")) or f"map_{uuid.uuid4().hex[:10]}",
                        "source_property": src,
                        "mmp_property": _read_text(row.get("mmp_property")),
                        "method_id": method_ref,
                        "value_transform": _normalize_value_transform(row.get("value_transform")),
                        "notes": _read_text(row.get("notes")),
                    }
                )
        if not inserted:
            normalized_rows.append(
                {
                    "id": f"map_{uuid.uuid4().hex[:10]}",
                    "source_property": source_token,
                    "mmp_property": mmp_token,
                    "method_id": method_token,
                    "value_transform": transform_token,
                    "notes": notes or "Method-bound mapping.",
                }
            )
        return store.replace_property_mappings(db_token, normalized_rows)

    def _remove_method_mapping_for_database(*, database_id: str, method_id: str, property_name: str = "") -> int:
        db_token = _read_text(database_id)
        method_token = _read_text(method_id)
        property_token = _read_text(property_name).lower()
        if not db_token or not method_token:
            return 0
        rows = store.list_property_mappings(database_id=db_token)
        kept: List[Dict[str, Any]] = []
        for item in rows:
            row = _safe_json_object(item)
            method_ref = _read_text(row.get("method_id"))
            source_property = _read_text(row.get("source_property")).lower()
            mapped_property = _read_text(row.get("mmp_property")).lower()
            remove = method_ref == method_token
            if property_token and (source_property == property_token or mapped_property == property_token):
                remove = True
            if remove:
                continue
            kept.append(row)
        removed = len(rows) - len(kept)
        if removed <= 0:
            return 0
        payload_rows: List[Dict[str, Any]] = []
        for item in kept:
            row = _safe_json_object(item)
            payload_rows.append(
                {
                    "id": _read_text(row.get("id")) or f"map_{uuid.uuid4().hex[:10]}",
                    "source_property": _read_text(row.get("source_property")),
                    "mmp_property": _read_text(row.get("mmp_property")),
                    "method_id": _read_text(row.get("method_id")),
                    "value_transform": _normalize_value_transform(row.get("value_transform")),
                    "notes": _read_text(row.get("notes")),
                }
            )
        store.replace_property_mappings(db_token, payload_rows)
        return removed

    def _enqueue_property_sync(
        *,
        database_id: str,
        operation: str,
        payload: Dict[str, Any],
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        return store.enqueue_database_sync(
            database_id=_read_text(database_id),
            operation=_read_text(operation),
            payload=_safe_json_object(payload),
            dedupe_key=_read_text(dedupe_key),
        )

    def _apply_pending_property_sync(
        database_id: str,
        target: PostgresTarget,
        *,
        require_lock: bool = False,
        lock_context: str = "",
    ) -> Dict[str, Any]:
        db_token = _read_text(database_id)
        pending_rows = store.list_pending_database_sync(database_id=db_token, pending_only=True)
        if not pending_rows:
            return {"database_id": db_token, "total": 0, "applied": 0, "entries": []}
        lock_id = ""
        if require_lock:
            lock_note = lock_context or "property_sync"
            lock_row = _acquire_database_update_lock(
                database_id=db_token,
                operation="property_sync",
                note=lock_note,
            )
            lock_id = _read_text(lock_row.get("id"))
        applied_entries: List[Dict[str, Any]] = []
        try:
            for item in pending_rows:
                row = _safe_json_object(item)
                entry_id = _read_text(row.get("id"))
                operation = _read_text(row.get("operation")).lower()
                payload = _safe_json_object(row.get("payload"))
                try:
                    if operation == "ensure_property":
                        result = property_admin_service.ensure_property_name(
                            target,
                            property_name=_read_text(payload.get("property_name")),
                        )
                    elif operation == "rename_property":
                        result = property_admin_service.rename_property_name(
                            target,
                            old_name=_read_text(payload.get("old_name")),
                            new_name=_read_text(payload.get("new_name")),
                        )
                    elif operation == "purge_property":
                        result = property_admin_service.purge_property_name(
                            target,
                            property_name=_read_text(payload.get("property_name")),
                        )
                    else:
                        raise ValueError(f"Unsupported pending sync operation: {operation}")
                    store.mark_database_sync_applied(entry_id, result={"operation": operation, **_safe_json_object(result)})
                    applied_entries.append(
                        {
                            "id": entry_id,
                            "operation": operation,
                            "payload": payload,
                            "result": result,
                        }
                    )
                except Exception as exc:
                    store.mark_database_sync_failed(entry_id, error=str(exc))
                    raise RuntimeError(
                        f"Failed to apply pending sync operation '{operation}' for database '{db_token}': {exc}"
                    ) from exc
            return {
                "database_id": db_token,
                "total": len(pending_rows),
                "applied": len(applied_entries),
                "entries": applied_entries,
            }
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_list_methods():
        try:
            catalog = _filter_lifecycle_catalog_databases(
                mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=False)
            )
            _sync_assay_methods_for_catalog(store, catalog)
            return jsonify({"methods": store.list_methods()})
        except Exception as exc:
            logger.exception('Failed to list experiment methods: %s', exc)
            return jsonify({'error': f'Failed to list experiment methods: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/usage', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_method_usage():
        try:
            requested_database_id = _read_text(request.args.get("database_id"))
            catalog = _filter_lifecycle_catalog_databases(
                mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=False)
            )
            _sync_assay_methods_for_catalog(store, catalog)
            database_by_id: Dict[str, Dict[str, Any]] = {}
            for item in catalog.get("databases", []) if isinstance(catalog, dict) else []:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("id"))
                if not db_id:
                    continue
                database_by_id[db_id] = row
            methods = store.list_methods()
            method_by_id: Dict[str, Dict[str, Any]] = {}
            for item in methods:
                row = _safe_json_object(item)
                method_id = _read_text(row.get("id"))
                if method_id:
                    method_by_id[method_id] = row

            usage_rows = store.list_method_usage_rows()
            if requested_database_id:
                usage_rows = [
                    item
                    for item in usage_rows
                    if _read_text(_safe_json_object(item).get("database_id")) == requested_database_id
                ]

            properties_by_database: Dict[str, List[str]] = {}
            for item in usage_rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                method_id = _read_text(row.get("method_id"))
                if not db_id or not method_id:
                    continue
                method_row = _safe_json_object(method_by_id.get(method_id))
                output_property = _read_text(method_row.get("output_property"))
                if not output_property:
                    continue
                bucket = properties_by_database.setdefault(db_id, [])
                if output_property not in bucket:
                    bucket.append(output_property)

            property_counts_by_database: Dict[str, Dict[str, int]] = {}
            counted_databases: set[str] = set()
            if psycopg is not None:
                for db_id, property_names in properties_by_database.items():
                    if not property_names:
                        continue
                    try:
                        _, target = _resolve_database_target(db_id)
                        with psycopg.connect(target.url, autocommit=True) as conn:
                            with conn.cursor() as cursor:
                                cursor.execute(f'SET search_path TO "{target.schema}", public')
                                cursor.execute(
                                    """
                                    SELECT pn.name, COUNT(*)::BIGINT
                                    FROM compound_property cp
                                    INNER JOIN property_name pn ON pn.id = cp.property_name_id
                                    WHERE pn.name = ANY(%s)
                                    GROUP BY pn.name
                                    """,
                                    [property_names],
                                )
                                counts: Dict[str, int] = {}
                                for prop_name, row_count in cursor.fetchall():
                                    counts[_read_text(prop_name)] = int(row_count or 0)
                                property_counts_by_database[db_id] = counts
                                counted_databases.add(db_id)
                    except Exception as exc:
                        logger.warning("Failed counting assay method usage rows for database %s: %s", db_id, exc)

            enriched: List[Dict[str, Any]] = []
            for item in usage_rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                method_id = _read_text(row.get("method_id"))
                method_row = _safe_json_object(method_by_id.get(method_id))
                output_property = _read_text(method_row.get("output_property"))
                data_count = None
                if db_id and output_property and db_id in counted_databases:
                    data_count = int(_safe_json_object(property_counts_by_database.get(db_id)).get(output_property) or 0)
                db = _safe_json_object(database_by_id.get(db_id)) if db_id else {}
                enriched.append(
                    {
                        **row,
                        "database_label": _read_text(db.get("label") or db.get("schema") or db_id),
                        "database_schema": _read_text(db.get("schema")),
                        "data_count": data_count,
                    }
                )
            return jsonify({"usage": enriched})
        except Exception as exc:
            logger.exception('Failed to list experiment method usage: %s', exc)
            return jsonify({'error': f'Failed to list experiment method usage: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_create_method():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        lock_id = ""
        try:
            database_id = _read_text(payload.get("database_id"))
            next_payload = dict(payload)
            if database_id:
                next_payload["reference"] = database_id
                _assert_database_ready_for_update(database_id)
                lock_row = _acquire_database_update_lock(
                    database_id=database_id,
                    operation="method_create",
                    note="method_create_sync",
                )
                lock_id = _read_text(lock_row.get("id"))
            method = store.upsert_method(next_payload)
            mapping_sync: List[Dict[str, Any]] | None = None
            queued_sync: Dict[str, Any] | None = None
            applied_sync: Dict[str, Any] | None = None
            if database_id:
                selected, target = _resolve_database_target(database_id)
                _sync_assay_methods_for_database(store, selected)
                mapping_sync = _upsert_method_mapping_for_database(
                    database_id=database_id,
                    method_id=_read_text(method.get("id")),
                    source_property=_read_text(method.get("key") or method.get("output_property")),
                    mmp_property=_read_text(method.get("output_property")),
                    value_transform=_normalize_value_transform(method.get("display_transform")),
                    notes="Method-bound mapping.",
                )
                prop_name = _read_text(method.get("output_property"))
                discovered_props = _extract_database_property_names(selected)
                alias_rename_source = _pick_family_alias_rename_source(discovered_props, prop_name)
                if alias_rename_source:
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="rename_property",
                        payload={"old_name": alias_rename_source, "new_name": prop_name},
                        dedupe_key=f"rename_property:{alias_rename_source.lower()}->{prop_name.lower()}",
                    )
                else:
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="ensure_property",
                        payload={"property_name": prop_name},
                        dedupe_key=f"ensure_property:{prop_name.lower()}",
                    )
                applied_sync = _apply_pending_property_sync(
                    database_id,
                    target,
                    require_lock=False,
                )
            return jsonify(
                {
                    "method": method,
                    "database_id": database_id,
                    "queued_sync": queued_sync,
                    "applied_sync": applied_sync,
                    "mapping_sync_count": len(mapping_sync or []),
                }
            ), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to create experiment method: %s', exc)
            return jsonify({'error': f'Failed to create experiment method: {exc}'}), 500
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/<method_id>', methods=['PATCH'])
    @require_api_token
    def mmp_lifecycle_update_method(method_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        lock_id = ""
        try:
            previous = _find_method_or_raise(method_id)
            database_id = _read_text(payload.get("database_id") or previous.get("reference"))
            next_payload = dict(payload)
            if database_id:
                next_payload["reference"] = database_id
                _assert_database_ready_for_update(database_id)
                lock_row = _acquire_database_update_lock(
                    database_id=database_id,
                    operation="method_update",
                    note="method_update_sync",
                )
                lock_id = _read_text(lock_row.get("id"))
            method = store.upsert_method(next_payload, method_id=method_id)
            mapping_sync: List[Dict[str, Any]] | None = None
            queued_sync: Dict[str, Any] | None = None
            applied_sync: Dict[str, Any] | None = None
            if database_id:
                selected, target = _resolve_database_target(database_id)
                old_prop = _read_text(previous.get("output_property"))
                new_prop = _read_text(method.get("output_property"))
                discovered_props = _extract_database_property_names(selected)
                direct_old_exists = old_prop and any(old_prop.lower() == item.lower() for item in discovered_props)
                alias_rename_source = _pick_family_alias_rename_source(discovered_props, new_prop)
                if old_prop and new_prop and old_prop.lower() != new_prop.lower():
                    rename_from = old_prop if direct_old_exists else (alias_rename_source or old_prop)
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="rename_property",
                        payload={"old_name": rename_from, "new_name": new_prop},
                        dedupe_key=f"rename_property:{rename_from.lower()}->{new_prop.lower()}",
                    )
                elif alias_rename_source:
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="rename_property",
                        payload={"old_name": alias_rename_source, "new_name": new_prop},
                        dedupe_key=f"rename_property:{alias_rename_source.lower()}->{new_prop.lower()}",
                    )
                else:
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="ensure_property",
                        payload={"property_name": new_prop},
                        dedupe_key=f"ensure_property:{new_prop.lower()}",
                    )
                _sync_assay_methods_for_database(store, selected)
                mapping_sync = _upsert_method_mapping_for_database(
                    database_id=database_id,
                    method_id=_read_text(method.get("id")),
                    source_property=_read_text(method.get("key") or new_prop),
                    mmp_property=new_prop,
                    value_transform=_normalize_value_transform(method.get("display_transform")),
                    notes="Method-bound mapping.",
                )
                applied_sync = _apply_pending_property_sync(
                    database_id,
                    target,
                    require_lock=False,
                )
            return jsonify(
                {
                    "method": method,
                    "database_id": database_id,
                    "queued_sync": queued_sync,
                    "applied_sync": applied_sync,
                    "mapping_sync_count": len(mapping_sync or []),
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to update experiment method %s: %s', method_id, exc)
            return jsonify({'error': f'Failed to update experiment method: {exc}'}), 500
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/<method_id>', methods=['DELETE'])
    @require_api_token
    def mmp_lifecycle_delete_method(method_id: str):
        lock_id = ""
        try:
            method = _find_method_or_raise(method_id)
            payload = request.get_json(silent=True) or {}
            payload = _safe_json_object(payload)
            database_id = _read_text(payload.get("database_id"))
            purge_database_data = _to_bool(payload.get("purge_database_data"), False)
            confirm_output_property = _read_text(payload.get("confirm_output_property"))
            if database_id:
                if not purge_database_data:
                    return jsonify({"error": "purge_database_data=true is required for database-scoped method deletion."}), 400
                _assert_database_ready_for_update(database_id)
                lock_row = _acquire_database_update_lock(
                    database_id=database_id,
                    operation="method_delete",
                    note="method_delete_queue_purge",
                )
                lock_id = _read_text(lock_row.get("id"))
                selected, target = _resolve_database_target(database_id)
                _sync_assay_methods_for_database(store, selected)
                output_property = _read_text(method.get("output_property"))
                if not output_property:
                    return jsonify({"error": "Method output_property is missing; cannot queue purge."}), 400
                if _read_text(confirm_output_property).lower() != output_property.lower():
                    return jsonify(
                        {
                            "error": "confirm_output_property must exactly match method output_property.",
                            "output_property": output_property,
                            "impact_preview": property_admin_service.preview_property_delete_impact(
                                target,
                                property_name=output_property,
                            ),
                        }
                    ), 400
                impact_preview = property_admin_service.preview_property_delete_impact(
                    target,
                    property_name=output_property,
                )
                queued_sync = _enqueue_property_sync(
                    database_id=database_id,
                    operation="purge_property",
                    payload={"property_name": output_property},
                    dedupe_key=f"purge_property:{output_property.lower()}",
                )
                removed_mappings = _remove_method_mapping_for_database(
                    database_id=database_id,
                    method_id=method_id,
                    property_name=output_property,
                )
                all_mappings = store.list_property_mappings()
                still_used = any(_read_text(_safe_json_object(row).get("method_id")) == _read_text(method_id) for row in all_mappings)
                deleted_method = False
                if not still_used:
                    store.delete_method(method_id)
                    deleted_method = True
                return jsonify(
                    {
                        "ok": True,
                        "database_id": database_id,
                        "impact_preview": impact_preview,
                        "queued_sync": queued_sync,
                        "removed_mappings": removed_mappings,
                        "deleted_method": deleted_method,
                    }
                )
            store.delete_method(method_id)
            return jsonify({"ok": True, "deleted_method": True})
        except ValueError as exc:
            message = _read_text(exc).lower()
            if "not found" in message:
                status = 404
            else:
                status = _value_error_http_status(exc)
            return jsonify({"error": str(exc)}), status
        except Exception as exc:
            logger.exception('Failed to delete experiment method %s: %s', method_id, exc)
            return jsonify({'error': f'Failed to delete experiment method: {exc}'}), 500
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/property_mappings', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_list_property_mappings():
        database_id = _read_text(request.args.get("database_id"))
        try:
            if database_id:
                selected, _ = _resolve_database_target(database_id)
                _sync_assay_methods_for_database(store, selected)
            rows = store.list_property_mappings(database_id=database_id)
            return jsonify({"database_id": database_id, "mappings": rows})
        except Exception as exc:
            logger.exception('Failed to list property mappings for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to list property mappings: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/property_mappings/<database_id>', methods=['PUT'])
    @require_api_token
    def mmp_lifecycle_replace_property_mappings(database_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        mappings = payload.get("mappings") if isinstance(payload.get("mappings"), list) else []
        lock_id = ""
        try:
            _assert_database_ready_for_update(database_id)
            lock_row = _acquire_database_update_lock(
                database_id=database_id,
                operation="mapping_update",
                note="mapping_replace",
            )
            lock_id = _read_text(lock_row.get("id"))
            rows = store.replace_property_mappings(database_id, mappings)
            return jsonify({"database_id": database_id, "mappings": rows})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to replace property mappings for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to save property mappings: {exc}'}), 500
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/database_properties/<database_id>', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_database_properties(database_id: str):
        try:
            selected, _ = _resolve_database_target(database_id)
            _sync_assay_methods_for_database(store, selected)
            refreshed, _ = _resolve_database_target(database_id)
            properties = refreshed.get("properties") if isinstance(refreshed.get("properties"), list) else []
            return jsonify({"database_id": database_id, "properties": properties})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to list database properties for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to list database properties: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/check', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_check_batch(batch_id: str):
        payload = request.get_json(silent=True) or {}
        payload = _safe_json_object(payload)
        row_limit = max(1, min(5000, int(payload.get("row_limit") or 400)))

        try:
            batch = store.get_batch(batch_id)
            database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))
            if not database_id:
                return jsonify({"error": "database_id is required."}), 400
            if _read_text(batch.get("selected_database_id")) != database_id:
                batch = store.update_batch(batch_id, {"selected_database_id": database_id})

            selected, target = _resolve_database_target(database_id)
            _sync_assay_methods_for_database(store, selected)
            files = _safe_json_object(batch.get("files"))
            compounds_file = _safe_json_object(files.get("compounds"))
            experiments_file = _safe_json_object(files.get("experiments"))

            compound_check = None
            if compounds_file:
                structures_path = _resolve_batch_file_path(store=store, batch_id=batch_id, file_meta=compounds_file)
                if structures_path and os.path.exists(structures_path):
                    compound_cfg = _safe_json_object(compounds_file.get("column_config"))
                    raw_compound = check_service.preview_compound_import(
                        target,
                        structures_file=structures_path,
                        smiles_column=_read_text(compound_cfg.get("smiles_column")),
                        id_column=_read_text(compound_cfg.get("id_column")),
                        canonicalize_smiles=True,
                    )
                    rows = raw_compound.get("rows") if isinstance(raw_compound.get("rows"), list) else []
                    compound_check = {
                        **raw_compound,
                        "rows": rows[:row_limit],
                        "total_rows": len(rows),
                        "truncated": len(rows) > row_limit,
                    }

            experiment_check = None
            if experiments_file:
                mappings = store.list_property_mappings(database_id=database_id)
                compound_summary = _safe_json_object(_safe_json_object(compound_check).get("summary"))
                # Keep check behavior aligned with apply preflight:
                # when no compound delta exists, apply skips compound import, so
                # experiment rows should only match compounds already in DB.
                allow_batch_compound_match = _to_nonneg_int(compound_summary.get("reindex_rows"), 0) > 0
                experiment_check = _build_property_import_from_experiments(
                    logger=logger,
                    target=target,
                    store=store,
                    batch=batch,
                    database_id=database_id,
                    mappings=mappings,
                    row_limit=row_limit,
                    allow_batch_compound_match=allow_batch_compound_match,
                )
                generated = _safe_json_object(experiment_check.get("generated_property_file"))
                path = _read_text(generated.get("path"))
                if path:
                    store.set_generated_property_import_file(batch_id, path, generated)

            dataset_stats = verify_service.fetch_dataset_stats(target)
            summary_payload = {
                "database": {
                    "id": _read_text(selected.get("id") or database_id),
                    "label": _read_text(selected.get("label") or selected.get("schema") or database_id),
                    "schema": _read_text(selected.get("schema")),
                },
                "dataset_stats": {
                    "compounds": dataset_stats.compounds,
                    "rules": dataset_stats.rules,
                    "pairs": dataset_stats.pairs,
                    "rule_environments": dataset_stats.rule_environments,
                },
                "has_compound_file": bool(compounds_file),
                "has_experiment_file": bool(experiments_file),
                "compound_rows": int((_safe_json_object(compound_check).get("summary") or {}).get("annotated_rows") or 0),
                "experiment_rows": int((_safe_json_object(experiment_check).get("summary") or {}).get("rows_total") or 0),
            }

            check_policy = _normalize_check_policy(_safe_json_object(payload.get("check_policy")))
            check_policy["require_approved_status"] = False
            staged_batch = dict(batch)
            staged_batch["status"] = "checked"
            staged_batch["last_check"] = {
                "database_id": database_id,
                "compound_summary": (_safe_json_object(compound_check).get("summary") or {}),
                "experiment_summary": (_safe_json_object(experiment_check).get("summary") or {}),
                "checked_at": _utc_now_iso(),
            }
            check_gate = _build_check_gate(
                batch=staged_batch,
                database_id=database_id,
                import_compounds=bool(compounds_file),
                import_experiments=bool(experiments_file),
                policy=check_policy,
            )

            updated_batch = store.mark_last_check(
                batch_id,
                {
                    "database_id": database_id,
                    "compound_summary": (_safe_json_object(compound_check).get("summary") or {}),
                    "experiment_summary": (_safe_json_object(experiment_check).get("summary") or {}),
                    "check_policy": check_policy,
                    "check_gate": check_gate,
                },
            )

            return jsonify(
                {
                    "batch": updated_batch,
                    "summary": summary_payload,
                    "compound_check": compound_check,
                    "experiment_check": experiment_check,
                    "check_gate": check_gate,
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to run lifecycle check for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to run batch check: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/apply', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_apply_batch(batch_id: str):
        payload = _safe_json_object(request.get_json(silent=True) or {})
        try:
            async_requested = _to_bool(payload.get("async"), True)
            batch = store.get_batch(batch_id)
            current_runtime = _safe_json_object(batch.get("apply_runtime"))
            current_phase = _read_text(current_runtime.get("phase")).lower()
            if current_phase in {"queued", "running"}:
                return jsonify({"error": f"Batch is already applying. Current phase: {current_phase}."}), 409

            database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))
            if not database_id:
                return jsonify({"error": "database_id is required."}), 400
            _assert_database_ready_for_update(database_id)
            selected, _ = _resolve_database_target(database_id)
            _sync_assay_methods_for_database(store, selected)
            files = _safe_json_object(batch.get("files"))
            compounds_file = _safe_json_object(files.get("compounds"))
            experiments_file = _safe_json_object(files.get("experiments"))
            import_compounds = bool(payload.get("import_compounds", True)) and bool(compounds_file)
            import_experiments = bool(payload.get("import_experiments", True)) and bool(experiments_file)
            if not import_compounds and not import_experiments:
                return jsonify({"error": "Nothing to apply. Upload compounds and/or experiments first."}), 400
            if _read_text(batch.get("selected_database_id")) != database_id:
                batch = store.update_batch(batch_id, {"selected_database_id": database_id})
            import_batch_id = _read_text(payload.get("import_batch_id")) or _read_text(batch.get("id"))

            if not async_requested:
                sync_task_id = f"apply_sync_{uuid.uuid4().hex[:10]}"
                try:
                    lock_row = _acquire_database_update_lock(
                        database_id=database_id,
                        operation="apply",
                        batch_id=batch_id,
                        task_id=sync_task_id,
                        note="sync_apply",
                    )
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 409
                lock_id = _read_text(lock_row.get("id"))
                worker_payload = dict(payload)
                worker_payload["database_id"] = database_id
                worker_payload["import_compounds"] = bool(import_compounds)
                worker_payload["import_experiments"] = bool(import_experiments)
                worker_payload["import_batch_id"] = import_batch_id
                worker_payload["async"] = False
                worker_payload["task_id"] = sync_task_id
                try:
                    return jsonify(_apply_batch_sync(batch_id, worker_payload))
                except Exception as exc:
                    _release_database_update_lock(lock_id, failed=True, error=str(exc))
                    lock_id = ""
                    raise
                finally:
                    if lock_id:
                        _release_database_update_lock(lock_id)

            task_id = f"apply_task_{uuid.uuid4().hex[:12]}"
            try:
                lock_row = _acquire_database_update_lock(
                    database_id=database_id,
                    operation="apply",
                    batch_id=batch_id,
                    task_id=task_id,
                    note="async_apply",
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
            lock_id = _read_text(lock_row.get("id"))
            try:
                queued_batch = store.mark_apply_queued(
                    batch_id,
                    task_id=task_id,
                    database_id=database_id,
                    import_batch_id=import_batch_id,
                    import_compounds=bool(import_compounds),
                    import_experiments=bool(import_experiments),
                )
            except Exception:
                _release_database_update_lock(lock_id, failed=True, error="failed to queue apply task")
                raise

            worker_payload = dict(payload)
            worker_payload["database_id"] = database_id
            worker_payload["import_compounds"] = bool(import_compounds)
            worker_payload["import_experiments"] = bool(import_experiments)
            worker_payload["import_batch_id"] = import_batch_id
            worker_payload["async"] = False
            worker_payload["task_id"] = task_id

            def _run_apply_task() -> None:
                lock_failed = False
                lock_error = ""
                try:
                    store.mark_apply_running(batch_id, task_id=task_id)
                except Exception as exc:
                    logger.warning("Failed marking batch running for task %s: %s", task_id, exc)
                try:
                    _apply_batch_sync(batch_id, worker_payload)
                    logger.info("Lifecycle batch apply finished: batch=%s task=%s", batch_id, task_id)
                except Exception as exc:
                    err_text = str(exc or "apply failed")
                    lock_failed = True
                    lock_error = err_text
                    logger.exception("Lifecycle batch apply failed: batch=%s task=%s err=%s", batch_id, task_id, err_text)
                    try:
                        store.mark_apply_failed(batch_id, task_id=task_id, error=err_text)
                    except Exception:
                        logger.exception("Failed marking lifecycle batch apply task as failed: batch=%s task=%s", batch_id, task_id)
                finally:
                    _release_database_update_lock(lock_id, failed=lock_failed, error=lock_error)

            try:
                apply_executor.submit(_run_apply_task)
            except Exception as exc:
                _release_database_update_lock(lock_id, failed=True, error=str(exc))
                store.mark_apply_failed(batch_id, task_id=task_id, error=f"Failed to submit apply task: {exc}")
                raise RuntimeError(f"Failed to enqueue apply task: {exc}") from exc

            return (
                jsonify(
                    {
                        "queued": True,
                        "task_id": task_id,
                        "database_lock_id": lock_id,
                        "batch": queued_batch,
                    }
                ),
                202,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            logger.exception('Failed to apply lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to apply batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/rollback', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_rollback_batch(batch_id: str):
        payload = _safe_json_object(request.get_json(silent=True) or {})
        lock_id = ""
        try:
            batch = store.get_batch(batch_id)
            current_runtime = _safe_json_object(batch.get("apply_runtime"))
            current_phase = _read_text(current_runtime.get("phase")).lower()
            if current_phase in {"queued", "running"}:
                return jsonify({"error": f"Batch is currently applying. Current phase: {current_phase}."}), 409
            database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))
            if not database_id:
                return jsonify({"error": "database_id is required."}), 400
            _assert_database_ready_for_update(database_id)
            try:
                lock_row = _acquire_database_update_lock(
                    database_id=database_id,
                    operation="rollback",
                    batch_id=batch_id,
                    note="rollback",
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
            lock_id = _read_text(lock_row.get("id"))
            _, target = _resolve_database_target(database_id)
            apply_history = batch.get("apply_history") if isinstance(batch.get("apply_history"), list) else []
            requested_apply_id = _read_text(payload.get("apply_id"))

            selected_apply: Optional[Dict[str, Any]] = None
            for item in reversed(apply_history):
                row = _safe_json_object(item)
                if _read_text(row.get("database_id")) != database_id:
                    continue
                if requested_apply_id and _read_text(row.get("apply_id")) != requested_apply_id:
                    continue
                selected_apply = row
                break

            if not selected_apply:
                return jsonify({"error": "No apply history found for selected database."}), 400

            import_batch_id = _read_text(selected_apply.get("import_batch_id") or batch_id)
            rollback_compounds = bool(payload.get("rollback_compounds", True)) and bool(selected_apply.get("import_compounds"))
            rollback_experiments = bool(payload.get("rollback_experiments", True)) and bool(selected_apply.get("import_experiments"))

            before = verify_service.fetch_dataset_stats(target)
            compound_deleted = None
            property_deleted = None

            if rollback_experiments:
                property_deleted = setup_service.delete_property_batch(target, batch_id=import_batch_id)
                if not property_deleted:
                    raise RuntimeError("Property batch rollback failed")

            if rollback_compounds:
                compound_options = _extract_compound_import_options(payload)
                compound_deleted = setup_service.delete_compound_batch(
                    target,
                    batch_id=import_batch_id,
                    **compound_options.to_setup_kwargs(),
                )
                if not compound_deleted:
                    raise RuntimeError("Compound batch rollback failed")

            after = verify_service.fetch_dataset_stats(target)
            verify_service.persist_dataset_stats(target, after)
            before_payload = _dataset_stats_payload(before)
            after_payload = _dataset_stats_payload(after)
            delta_payload = _dataset_delta_payload(before, after)
            updated_batch = store.append_rollback_history(
                batch_id,
                {
                    "apply_id": _read_text(selected_apply.get("apply_id")),
                    "database_id": database_id,
                    "import_batch_id": import_batch_id,
                    "rollback_compounds": rollback_compounds,
                    "rollback_experiments": rollback_experiments,
                    "compound_deleted": compound_deleted,
                    "property_deleted": property_deleted,
                    "before": before_payload,
                    "after": after_payload,
                    "delta": delta_payload,
                },
            )

            return jsonify(
                {
                    "batch": updated_batch,
                    "apply_id": _read_text(selected_apply.get("apply_id")),
                    "database_id": database_id,
                    "import_batch_id": import_batch_id,
                    "rollback_compounds": rollback_compounds,
                    "rollback_experiments": rollback_experiments,
                    "before": before_payload,
                    "after": after_payload,
                    "delta": delta_payload,
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), _value_error_http_status(exc)
        except Exception as exc:
            if lock_id:
                _release_database_update_lock(lock_id, failed=True, error=str(exc))
                lock_id = ""
            logger.exception('Failed to rollback lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to rollback batch: {exc}'}), 500
        finally:
            if lock_id:
                _release_database_update_lock(lock_id)

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/metrics/<database_id>', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_schema_metrics(database_id: str):
        recent_limit = max(1, min(50, int(request.args.get("recent_limit") or 10)))
        try:
            _, target = _resolve_database_target(database_id)
            metrics = report_service.fetch_schema_metrics(target, recent_limit=recent_limit)
            return jsonify(metrics)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to fetch lifecycle metrics for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to fetch schema metrics: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/metrics/<database_id>/integrity', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_schema_integrity(database_id: str):
        include_duplicate_scan = str(request.args.get("include_duplicate_scan") or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            _, target = _resolve_database_target(database_id)
            issues = verify_service.fetch_incremental_integrity_issues(
                target,
                include_duplicate_pair_scan=include_duplicate_scan,
            )
            issues["database_id"] = database_id
            issues["include_duplicate_scan"] = bool(include_duplicate_scan)
            return jsonify(issues)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to fetch lifecycle integrity metrics for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to fetch lifecycle integrity metrics: {exc}'}), 500


__all__ = ["register_mmp_lifecycle_admin_routes"]
