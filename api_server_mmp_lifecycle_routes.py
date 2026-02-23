from __future__ import annotations

import csv
import json
import math
import os
import re
import uuid
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


def _float_equal(left: Any, right: Any, *, eps: float = 1e-12) -> bool:
    try:
        lv = float(left)
        rv = float(right)
    except Exception:
        return False
    return abs(lv - rv) <= eps


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
                bucket[name] = value
                if not value:
                    continue
                has_any = True
                column_non_empty_counts[name] = int(column_non_empty_counts.get(name) or 0) + 1
                try:
                    numeric = float(value)
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


def _resolve_database_target(database_id: str) -> Tuple[Dict[str, Any], PostgresTarget]:
    selected = mmp_database_registry.resolve_mmp_database(str(database_id or "").strip(), include_hidden=True)
    database_url = str(selected.get("database_url") or "").strip()
    schema = str(selected.get("schema") or "").strip()
    target = PostgresTarget.from_inputs(url=database_url, schema=schema)
    return selected, target


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


def _sync_assay_methods_for_database(store: MmpLifecycleAdminStore, database_entry: Dict[str, Any]) -> Dict[str, Any]:
    db_id = _read_text(database_entry.get("id"))
    if not db_id:
        return {"database_id": "", "changed": False}
    property_names = _extract_database_property_names(database_entry)
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


def _extract_compound_import_options(payload: Dict[str, Any]) -> Dict[str, Any]:
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

    return {
        "output_dir": str(payload.get("output_dir") or "lead_optimization/data").strip() or "lead_optimization/data",
        "max_heavy_atoms": _to_int("max_heavy_atoms", 50),
        "skip_attachment_enrichment": _to_bool("skip_attachment_enrichment", False),
        "attachment_force_recompute": _to_bool("attachment_force_recompute", False),
        "fragment_jobs": _to_int("fragment_jobs", 8),
        "index_maintenance_work_mem_mb": _to_int("pg_index_maintenance_work_mem_mb", 1024),
        "index_work_mem_mb": _to_int("pg_index_work_mem_mb", 128),
        "index_parallel_workers": _to_int("pg_index_parallel_workers", 4),
        "index_commit_every_flushes": _to_int("pg_index_commit_every_flushes", 0),
        "incremental_index_shards": _to_int("pg_incremental_index_shards", 1),
        "incremental_index_jobs": _to_int("pg_incremental_index_jobs", 1),
        "build_construct_tables": not _to_bool("pg_skip_construct_tables", False),
        "build_constant_smiles_mol_index": not _to_bool("pg_skip_constant_smiles_mol_index", False),
    }


def _canonicalize_smiles(raw: str) -> str:
    try:
        return str(legacy_engine._canonicalize_smiles_for_lookup(raw, canonicalize=True) or "").strip()
    except Exception:
        return ""


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
    }
    return token if token in allowed else "none"


def _apply_value_transform(value: float, transform: str) -> float:
    op = _normalize_value_transform(transform)
    numeric = float(value)
    if op == "none":
        return numeric
    if numeric <= 0:
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
    return float(-math.log10(numeric))


def _build_property_import_from_experiments(
    *,
    logger,
    target: PostgresTarget,
    batch: Dict[str, Any],
    database_id: str,
    mappings: List[Dict[str, Any]],
    row_limit: int,
) -> Dict[str, Any]:
    experiments_file = _safe_json_object(_safe_json_object(batch.get("files")).get("experiments"))
    source_path = str(experiments_file.get("path") or "").strip()
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
        for item in mappings:
            key = _read_text(item.get("source_property")).lower()
            if key and key not in mapping_by_source:
                mapping_by_source[key] = item
        raw_upload_transform_map = _safe_json_object(column_config.get("activity_transform_map"))
        upload_transform_by_source: Dict[str, str] = {}
        for key, value in raw_upload_transform_map.items():
            source_key = _read_text(key).lower()
            if not source_key:
                continue
            upload_transform_by_source[source_key] = _normalize_value_transform(value)

        parsed_rows: List[Dict[str, Any]] = []
        duplicate_counts: Dict[str, int] = {}
        latest_line_by_key: Dict[str, int] = {}

        for line_no, row in enumerate(reader, start=2):
            raw_smiles = _read_text((row or {}).get(smiles_col))
            source_property = _read_text((row or {}).get(source_property_col))
            value_raw = _read_text((row or {}).get(value_col))
            method_value = _read_text((row or {}).get(method_col)) if method_col else ""
            notes_value = _read_text((row or {}).get(notes_col)) if notes_col else ""

            clean_smiles = _canonicalize_smiles(raw_smiles) if raw_smiles else ""
            value_parsed: Optional[float] = None
            if value_raw:
                try:
                    value_parsed = float(value_raw)
                except Exception:
                    value_parsed = None

            mapping = mapping_by_source.get(source_property.lower()) if source_property else None
            mapped_property = _read_text((mapping or {}).get("mmp_property"))
            mapping_method_id = _read_text((mapping or {}).get("method_id"))
            mapping_notes = _read_text((mapping or {}).get("notes"))
            upload_transform = upload_transform_by_source.get(source_property.lower(), "none") if source_property else "none"

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
        property_name_exists = False
        existing_value: Optional[float] = None

        if not raw_smiles:
            action = "SKIP_EMPTY_SMILES"
            note = "SMILES is empty."
        elif not clean_smiles:
            action = "SKIP_INVALID_SMILES"
            note = "SMILES cannot be canonicalized by RDKit."
        elif not source_property:
            action = "SKIP_EMPTY_PROPERTY"
            note = "Source property is empty."
        elif not mapped_property:
            action = "SKIP_UNMAPPED_PROPERTY"
            note = "No property mapping found for this source property in selected MMP database."
        elif incoming_value is None:
            action = "SKIP_INVALID_VALUE"
            note = "Numeric value is missing or invalid."
        elif dedupe_key and int(latest_line_by_key.get(dedupe_key) or 0) != line_no:
            action = "SKIP_SHADOWED_DUPLICATE"
            note = "Same (SMILES, mapped_property) appears later; latest row will be applied."
        else:
            in_compound_table = clean_smiles in compound_ids
            property_name_exists = mapped_property in property_name_ids
            existing_value = existing_values.get((clean_smiles, mapped_property))

            if not in_compound_table:
                action = "SKIP_UNMATCHED_COMPOUND"
                note = "No matching compound.clean_smiles in selected schema."
            elif not property_name_exists:
                action = "INSERT_PROPERTY_NAME_AND_COMPOUND_PROPERTY"
                will_import = True
                note = "Property will be created and value inserted for this compound."
            elif existing_value is None:
                action = "INSERT_COMPOUND_PROPERTY"
                will_import = True
                note = "No existing compound_property row; will insert."
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

    generated_dir = Path(str(experiments_file.get("path") or "")).resolve().parent / "generated"
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

    summary = {
        "rows_total": len(output_rows),
        "rows_preview": min(max(1, int(row_limit or 200)), len(output_rows)),
        "rows_mapped": sum(1 for row in output_rows if _read_text(row.get("mapped_property"))),
        "rows_will_import": sum(1 for row in output_rows if bool(row.get("will_import"))),
        "rows_unmapped": sum(1 for row in output_rows if _read_text(row.get("action")) == "SKIP_UNMAPPED_PROPERTY"),
        "rows_unmatched_compound": sum(1 for row in output_rows if _read_text(row.get("action")) == "SKIP_UNMATCHED_COMPOUND"),
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

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/overview', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_overview():
        try:
            catalog = mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=True)
            _sync_assay_methods_for_catalog(store, catalog)
            pending_rows = store.list_pending_database_sync(pending_only=True)
            pending_sync_by_database: Dict[str, int] = {}
            for item in pending_rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                if not db_id:
                    continue
                pending_sync_by_database[db_id] = int(pending_sync_by_database.get(db_id, 0)) + 1
            return jsonify(
                {
                    "databases": catalog.get("databases", []),
                    "default_database_id": catalog.get("default_database_id", ""),
                    "methods": store.list_methods(),
                    "batches": store.list_batches(),
                    "pending_database_sync": pending_rows,
                    "pending_sync_by_database": pending_sync_by_database,
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
        try:
            return jsonify({"batches": store.list_batches()})
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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to transition lifecycle batch status %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to transition batch status: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>', methods=['DELETE'])
    @require_api_token
    def mmp_lifecycle_delete_batch(batch_id: str):
        try:
            store.delete_batch(batch_id)
            return jsonify({"ok": True})
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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to clear experiment file for batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to clear experiments: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/preview_compounds', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_preview_compounds(batch_id: str):
        try:
            batch = store.get_batch(batch_id)
            files = _safe_json_object(batch.get("files"))
            compounds_meta = _safe_json_object(files.get("compounds"))
            source_path = _read_text(compounds_meta.get("path"))
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
            return jsonify({"error": str(exc)}), 400
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

    def _apply_pending_property_sync(database_id: str, target: PostgresTarget) -> Dict[str, Any]:
        db_token = _read_text(database_id)
        pending_rows = store.list_pending_database_sync(database_id=db_token, pending_only=True)
        if not pending_rows:
            return {"database_id": db_token, "total": 0, "applied": 0, "entries": []}
        applied_entries: List[Dict[str, Any]] = []
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

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_list_methods():
        try:
            catalog = mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=False)
            _sync_assay_methods_for_catalog(store, catalog)
            return jsonify({"methods": store.list_methods()})
        except Exception as exc:
            logger.exception('Failed to list experiment methods: %s', exc)
            return jsonify({'error': f'Failed to list experiment methods: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/usage', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_method_usage():
        try:
            catalog = mmp_database_registry.get_mmp_database_catalog(include_hidden=True, include_stats=False)
            _sync_assay_methods_for_catalog(store, catalog)
            database_by_id: Dict[str, Dict[str, Any]] = {}
            for item in catalog.get("databases", []) if isinstance(catalog, dict) else []:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("id"))
                if not db_id:
                    continue
                database_by_id[db_id] = row
            usage_rows = store.list_method_usage_rows()
            enriched: List[Dict[str, Any]] = []
            for item in usage_rows:
                row = _safe_json_object(item)
                db_id = _read_text(row.get("database_id"))
                db = _safe_json_object(database_by_id.get(db_id)) if db_id else {}
                enriched.append(
                    {
                        **row,
                        "database_label": _read_text(db.get("label") or db.get("schema") or db_id),
                        "database_schema": _read_text(db.get("schema")),
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
        try:
            database_id = _read_text(payload.get("database_id"))
            next_payload = dict(payload)
            if database_id:
                next_payload["reference"] = database_id
            method = store.upsert_method(next_payload)
            mapping_sync: List[Dict[str, Any]] | None = None
            queued_sync: Dict[str, Any] | None = None
            if database_id:
                selected, _ = _resolve_database_target(database_id)
                _sync_assay_methods_for_database(store, selected)
                mapping_sync = _upsert_method_mapping_for_database(
                    database_id=database_id,
                    method_id=_read_text(method.get("id")),
                    source_property=_read_text(method.get("output_property")),
                    mmp_property=_read_text(method.get("output_property")),
                    value_transform=_normalize_value_transform(method.get("display_transform")),
                    notes="Method-bound mapping.",
                )
                prop_name = _read_text(method.get("output_property"))
                queued_sync = _enqueue_property_sync(
                    database_id=database_id,
                    operation="ensure_property",
                    payload={"property_name": prop_name},
                    dedupe_key=f"ensure_property:{prop_name.lower()}",
                )
            return jsonify(
                {
                    "method": method,
                    "database_id": database_id,
                    "queued_sync": queued_sync,
                    "mapping_sync_count": len(mapping_sync or []),
                }
            ), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to create experiment method: %s', exc)
            return jsonify({'error': f'Failed to create experiment method: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/<method_id>', methods=['PATCH'])
    @require_api_token
    def mmp_lifecycle_update_method(method_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'payload must be a JSON object.'}), 400
        try:
            previous = _find_method_or_raise(method_id)
            database_id = _read_text(payload.get("database_id") or previous.get("reference"))
            next_payload = dict(payload)
            if database_id:
                next_payload["reference"] = database_id
            method = store.upsert_method(next_payload, method_id=method_id)
            mapping_sync: List[Dict[str, Any]] | None = None
            queued_sync: Dict[str, Any] | None = None
            if database_id:
                selected, _ = _resolve_database_target(database_id)
                old_prop = _read_text(previous.get("output_property"))
                new_prop = _read_text(method.get("output_property"))
                if old_prop and new_prop and old_prop.lower() != new_prop.lower():
                    queued_sync = _enqueue_property_sync(
                        database_id=database_id,
                        operation="rename_property",
                        payload={"old_name": old_prop, "new_name": new_prop},
                        dedupe_key=f"rename_property:{old_prop.lower()}->{new_prop.lower()}",
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
                    source_property=new_prop,
                    mmp_property=new_prop,
                    value_transform=_normalize_value_transform(method.get("display_transform")),
                    notes="Method-bound mapping.",
                )
            return jsonify(
                {
                    "method": method,
                    "database_id": database_id,
                    "queued_sync": queued_sync,
                    "mapping_sync_count": len(mapping_sync or []),
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to update experiment method %s: %s', method_id, exc)
            return jsonify({'error': f'Failed to update experiment method: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/<method_id>', methods=['DELETE'])
    @require_api_token
    def mmp_lifecycle_delete_method(method_id: str):
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
            status = 404 if "not found" in str(exc).lower() else 400
            return jsonify({"error": str(exc)}), status
        except Exception as exc:
            logger.exception('Failed to delete experiment method %s: %s', method_id, exc)
            return jsonify({'error': f'Failed to delete experiment method: {exc}'}), 500

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
        try:
            rows = store.replace_property_mappings(database_id, mappings)
            return jsonify({"database_id": database_id, "mappings": rows})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to replace property mappings for %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to save property mappings: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/database_properties/<database_id>', methods=['GET'])
    @require_api_token
    def mmp_lifecycle_database_properties(database_id: str):
        try:
            selected, _ = _resolve_database_target(database_id)
            _sync_assay_methods_for_database(store, selected)
            properties = selected.get("properties") if isinstance(selected.get("properties"), list) else []
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
                structures_path = _read_text(compounds_file.get("path"))
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
                experiment_check = _build_property_import_from_experiments(
                    logger=logger,
                    target=target,
                    batch=batch,
                    database_id=database_id,
                    mappings=mappings,
                    row_limit=row_limit,
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
            batch = store.get_batch(batch_id)
            database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))
            if not database_id:
                return jsonify({"error": "database_id is required."}), 400

            selected, target = _resolve_database_target(database_id)
            _sync_assay_methods_for_database(store, selected)
            files = _safe_json_object(batch.get("files"))
            compounds_file = _safe_json_object(files.get("compounds"))
            experiments_file = _safe_json_object(files.get("experiments"))
            import_compounds = bool(payload.get("import_compounds", True)) and bool(compounds_file)
            import_experiments = bool(payload.get("import_experiments", True)) and bool(experiments_file)
            status_token = _read_text(batch.get("status")).lower() or "draft"

            if _read_text(batch.get("selected_database_id")) != database_id:
                return jsonify({"error": "Selected database changed. Save batch and rerun check before apply."}), 400
            if status_token != "approved":
                return jsonify({"error": f"Batch status must be approved before apply. Current status: {status_token}."}), 400

            if not import_compounds and not import_experiments:
                return jsonify({"error": "Nothing to apply. Upload compounds and/or experiments first."}), 400

            last_check = _safe_json_object(batch.get("last_check"))
            check_policy = _normalize_check_policy(_safe_json_object(payload.get("check_policy") or last_check.get("check_policy")))
            check_policy["require_approved_status"] = True
            check_gate = _build_check_gate(
                batch=batch,
                database_id=database_id,
                import_compounds=import_compounds,
                import_experiments=import_experiments,
                policy=check_policy,
            )
            if not bool(check_gate.get("passed")):
                return jsonify(
                    {
                        "error": "Check gate failed. Resolve data quality issues and rerun check/approval before apply.",
                        "check_gate": check_gate,
                    }
                ), 400

            pending_sync_result = _apply_pending_property_sync(database_id, target)

            import_batch_id = _read_text(payload.get("import_batch_id")) or _read_text(batch.get("id"))
            import_label = _read_text(payload.get("import_label") or batch.get("name"))
            import_notes = _read_text(payload.get("import_notes") or batch.get("notes"))

            before = verify_service.fetch_dataset_stats(target)
            compound_ok = None
            property_ok = None
            generated_property_meta: Dict[str, Any] = {}

            compound_options = _extract_compound_import_options(payload)

            if import_compounds:
                structures_path = _read_text(compounds_file.get("path"))
                if not structures_path or not os.path.exists(structures_path):
                    return jsonify({"error": "Compound file not found for batch."}), 400
                compound_cfg = _safe_json_object(compounds_file.get("column_config"))
                compound_ok = setup_service.import_compound_batch(
                    target,
                    structures_file=structures_path,
                    batch_id=import_batch_id,
                    batch_label=import_label,
                    batch_notes=import_notes,
                    smiles_column=_read_text(compound_cfg.get("smiles_column")),
                    id_column=_read_text(compound_cfg.get("id_column")),
                    canonicalize_smiles=True,
                    output_dir=compound_options["output_dir"],
                    max_heavy_atoms=compound_options["max_heavy_atoms"],
                    skip_attachment_enrichment=compound_options["skip_attachment_enrichment"],
                    attachment_force_recompute=compound_options["attachment_force_recompute"],
                    fragment_jobs=compound_options["fragment_jobs"],
                    index_maintenance_work_mem_mb=compound_options["index_maintenance_work_mem_mb"],
                    index_work_mem_mb=compound_options["index_work_mem_mb"],
                    index_parallel_workers=compound_options["index_parallel_workers"],
                    index_commit_every_flushes=compound_options["index_commit_every_flushes"],
                    incremental_index_shards=compound_options["incremental_index_shards"],
                    incremental_index_jobs=compound_options["incremental_index_jobs"],
                    build_construct_tables=compound_options["build_construct_tables"],
                    build_constant_smiles_mol_index=compound_options["build_constant_smiles_mol_index"],
                )
                if not compound_ok:
                    raise RuntimeError("Compound incremental import failed")

            if import_experiments:
                mappings = store.list_property_mappings(database_id=database_id)
                experiment_check = _build_property_import_from_experiments(
                    logger=logger,
                    target=target,
                    batch=batch,
                    database_id=database_id,
                    mappings=mappings,
                    row_limit=100,
                )
                generated_property_meta = _safe_json_object(experiment_check.get("generated_property_file"))
                generated_path = _read_text(generated_property_meta.get("path"))
                if not generated_path or not os.path.exists(generated_path):
                    raise RuntimeError("Generated property import file is missing")
                if int(generated_property_meta.get("row_count") or 0) <= 0 or int(generated_property_meta.get("property_count") or 0) <= 0:
                    raise ValueError(
                        "No mapped experiment rows are ready for import. Configure property mappings and rerun check first."
                    )
                store.set_generated_property_import_file(batch_id, generated_path, generated_property_meta)

                property_ok = setup_service.import_property_batch(
                    target,
                    property_file=generated_path,
                    batch_id=import_batch_id,
                    batch_label=import_label,
                    batch_notes=import_notes,
                    smiles_column="smiles",
                    canonicalize_smiles=False,
                )
                if not property_ok:
                    raise RuntimeError("Property incremental import failed")

            after = verify_service.fetch_dataset_stats(target)
            apply_id = f"apply_{uuid.uuid4().hex[:12]}"
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
                    "generated_property_file": generated_property_meta,
                    "pending_database_sync": pending_sync_result,
                    "before": {
                        "compounds": before.compounds,
                        "rules": before.rules,
                        "pairs": before.pairs,
                        "rule_environments": before.rule_environments,
                    },
                    "after": {
                        "compounds": after.compounds,
                        "rules": after.rules,
                        "pairs": after.pairs,
                        "rule_environments": after.rule_environments,
                    },
                    "delta": {
                        "compounds": after.compounds - before.compounds,
                        "rules": after.rules - before.rules,
                        "pairs": after.pairs - before.pairs,
                        "rule_environments": after.rule_environments - before.rule_environments,
                    },
                },
            )
            if _read_text(updated_batch.get("selected_database_id")) != database_id:
                updated_batch = store.update_batch(batch_id, {"selected_database_id": database_id})

            return jsonify(
                {
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
                    "before": {
                        "compounds": before.compounds,
                        "rules": before.rules,
                        "pairs": before.pairs,
                        "rule_environments": before.rule_environments,
                    },
                    "after": {
                        "compounds": after.compounds,
                        "rules": after.rules,
                        "pairs": after.pairs,
                        "rule_environments": after.rule_environments,
                    },
                    "delta": {
                        "compounds": after.compounds - before.compounds,
                        "rules": after.rules - before.rules,
                        "pairs": after.pairs - before.pairs,
                        "rule_environments": after.rule_environments - before.rule_environments,
                    },
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to apply lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to apply batch: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_lifecycle/batches/<batch_id>/rollback', methods=['POST'])
    @require_api_token
    def mmp_lifecycle_rollback_batch(batch_id: str):
        payload = _safe_json_object(request.get_json(silent=True) or {})
        try:
            batch = store.get_batch(batch_id)
            database_id = _read_text(payload.get("database_id") or batch.get("selected_database_id"))
            if not database_id:
                return jsonify({"error": "database_id is required."}), 400
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
                    output_dir=compound_options["output_dir"],
                    max_heavy_atoms=compound_options["max_heavy_atoms"],
                    skip_attachment_enrichment=compound_options["skip_attachment_enrichment"],
                    attachment_force_recompute=compound_options["attachment_force_recompute"],
                    fragment_jobs=compound_options["fragment_jobs"],
                    index_maintenance_work_mem_mb=compound_options["index_maintenance_work_mem_mb"],
                    index_work_mem_mb=compound_options["index_work_mem_mb"],
                    index_parallel_workers=compound_options["index_parallel_workers"],
                    index_commit_every_flushes=compound_options["index_commit_every_flushes"],
                    incremental_index_shards=compound_options["incremental_index_shards"],
                    incremental_index_jobs=compound_options["incremental_index_jobs"],
                    build_construct_tables=compound_options["build_construct_tables"],
                    build_constant_smiles_mol_index=compound_options["build_constant_smiles_mol_index"],
                )
                if not compound_deleted:
                    raise RuntimeError("Compound batch rollback failed")

            after = verify_service.fetch_dataset_stats(target)
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
                    "before": {
                        "compounds": before.compounds,
                        "rules": before.rules,
                        "pairs": before.pairs,
                        "rule_environments": before.rule_environments,
                    },
                    "after": {
                        "compounds": after.compounds,
                        "rules": after.rules,
                        "pairs": after.pairs,
                        "rule_environments": after.rule_environments,
                    },
                    "delta": {
                        "compounds": after.compounds - before.compounds,
                        "rules": after.rules - before.rules,
                        "pairs": after.pairs - before.pairs,
                        "rule_environments": after.rule_environments - before.rule_environments,
                    },
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
                    "before": {
                        "compounds": before.compounds,
                        "rules": before.rules,
                        "pairs": before.pairs,
                        "rule_environments": before.rule_environments,
                    },
                    "after": {
                        "compounds": after.compounds,
                        "rules": after.rules,
                        "pairs": after.pairs,
                        "rule_environments": after.rule_environments,
                    },
                    "delta": {
                        "compounds": after.compounds - before.compounds,
                        "rules": after.rules - before.rules,
                        "pairs": after.pairs - before.pairs,
                        "rule_environments": after.rule_environments - before.rule_environments,
                    },
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to rollback lifecycle batch %s: %s', batch_id, exc)
            return jsonify({'error': f'Failed to rollback batch: {exc}'}), 500

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


__all__ = ["register_mmp_lifecycle_admin_routes"]
