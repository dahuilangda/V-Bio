from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from capabilities.lead_optimization.mmp_lifecycle import engine as legacy_engine

_logger = logging.getLogger(__name__)

_SCHEMA_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assert_safe_schema_identifier(schema: Any) -> str:
    """Validate a schema name before interpolating into SET search_path (psycopg does not support
    parameter binding for search_path). Reject anything that is not a plain identifier."""
    token = str(schema or "").strip()
    if not _SCHEMA_IDENTIFIER_RE.match(token):
        raise ValueError(f"Unsafe schema identifier: {schema!r}")
    return token


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


_DEFAULT_LIFECYCLE_OUTPUT_DIR = "capabilities/lead_optimization/data"
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


def _default_lifecycle_output_dir() -> str:
    return _DEFAULT_LIFECYCLE_OUTPUT_DIR


def _read_database_build_progress(item: Dict[str, Any], *, output_dir: str) -> Dict[str, Any]:
    schema = _read_text(item.get("schema"))
    if not schema:
        return {}
    try:
        meta_path = legacy_engine._full_sharded_build_meta_path(output_dir, schema)
        raw_state = legacy_engine._read_json_payload(meta_path)
    except Exception as exc:
        _logger.warning("Failed to read build meta for schema %s: %s", schema, exc)
        return {}
    if _read_text(raw_state.get("mode")) != "full_sharded_index":
        return {}
    try:
        shard_count = int(raw_state.get("shard_count") or 0)
    except (TypeError, ValueError) as exc:
        _logger.warning("Build meta for schema %s has non-integer shard_count: %s", schema, exc)
        return {}
    if shard_count <= 0:
        return {}
    try:
        state = legacy_engine._read_full_sharded_build_state(
            output_dir=output_dir,
            schema=schema,
            shard_count=shard_count,
        )
    except Exception as exc:
        _logger.warning("Failed to read full sharded build state for schema %s: %s", schema, exc)
        return {}
    merged_raw = state.get("merged_shards")
    merged_shards = [int(item) for item in merged_raw] if isinstance(merged_raw, list) else []
    fragment_file = _safe_json_object(state.get("fragment_file"))
    fragment_path = _read_text(fragment_file.get("path"))
    progress = {
        "mode": "full_sharded_index",
        "schema": schema,
        "shard_count": shard_count,
        "merged_shards": merged_shards,
        "merged_shard_count": len(merged_shards),
        "merge_completed": bool(state.get("merge_completed")),
    }
    if fragment_path:
        progress["fragment_file"] = {
            "path": os.path.basename(fragment_path),
            "size": fragment_file.get("size"),
            "mtime_ns": fragment_file.get("mtime_ns"),
        }
    return progress


def _enrich_lifecycle_catalog_databases(
    databases: List[Dict[str, Any]],
    *,
    output_dir: str,
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for item in databases:
        row = dict(_safe_json_object(item))
        if not row:
            continue
        build_progress = _read_database_build_progress(row, output_dir=output_dir)
        if build_progress:
            row["build_progress"] = build_progress
        enriched.append(row)
    return enriched


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
    except (TypeError, ValueError):
        return None


def _to_nonneg_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
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
    except Exception as exc:
        _logger.warning("Delimiter detection failed for %s; falling back to extension guess: %s", path, exc)
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
        output_dir=str(payload.get("output_dir") or _default_lifecycle_output_dir()).strip() or _default_lifecycle_output_dir(),
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


def _autotune_compound_import_options(
    options: CompoundImportOptions,
    *,
    payload: Dict[str, Any],
    compound_summary: Dict[str, Any],
) -> CompoundImportOptions:
    reindex_rows = _to_nonneg_int(compound_summary.get("reindex_rows"), 0)
    if reindex_rows <= 0:
        return options

    cpu_count = max(1, int(os.cpu_count() or 1))
    tuned_fragment_jobs = options.fragment_jobs
    tuned_parallel_workers = options.index_parallel_workers
    tuned_shards = options.incremental_index_shards
    tuned_jobs = options.incremental_index_jobs

    if reindex_rows >= 100_000:
        tuned_fragment_jobs = min(32, cpu_count)
        tuned_parallel_workers = min(8, max(2, cpu_count // 4))
        tuned_shards = min(16, max(4, cpu_count // 2))
        tuned_jobs = min(tuned_shards, max(2, cpu_count // 4))
    elif reindex_rows >= 25_000:
        tuned_fragment_jobs = min(24, cpu_count)
        tuned_parallel_workers = min(6, max(2, cpu_count // 5))
        tuned_shards = min(8, max(4, cpu_count // 3))
        tuned_jobs = min(tuned_shards, max(2, cpu_count // 6))
    elif reindex_rows >= 5_000:
        tuned_fragment_jobs = min(16, cpu_count)
        tuned_parallel_workers = min(4, max(2, cpu_count // 6))
        tuned_shards = min(4, max(2, cpu_count // 4))
        tuned_jobs = min(tuned_shards, max(1, cpu_count // 8))

    explicit_int_keys = {
        key
        for key in (
            "fragment_jobs",
            "pg_index_parallel_workers",
            "pg_incremental_index_shards",
            "pg_incremental_index_jobs",
        )
        if key in payload
    }

    next_options = replace(
        options,
        fragment_jobs=options.fragment_jobs if "fragment_jobs" in explicit_int_keys else max(options.fragment_jobs, tuned_fragment_jobs),
        index_parallel_workers=(
            options.index_parallel_workers
            if "pg_index_parallel_workers" in explicit_int_keys
            else max(options.index_parallel_workers, tuned_parallel_workers)
        ),
        incremental_index_shards=(
            options.incremental_index_shards
            if "pg_incremental_index_shards" in explicit_int_keys
            else max(options.incremental_index_shards, tuned_shards)
        ),
        incremental_index_jobs=(
            options.incremental_index_jobs
            if "pg_incremental_index_jobs" in explicit_int_keys
            else max(options.incremental_index_jobs, tuned_jobs)
        ),
    )
    if next_options.incremental_index_jobs > next_options.incremental_index_shards:
        next_options = replace(
            next_options,
            incremental_index_jobs=next_options.incremental_index_shards,
        )
    return next_options


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
