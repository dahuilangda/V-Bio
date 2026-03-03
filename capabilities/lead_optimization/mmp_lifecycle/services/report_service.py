from __future__ import annotations

import json
import os
from typing import Any, Dict, List

try:
    import psycopg
except Exception:
    psycopg = None

from ..models import PostgresTarget
from .verify_service import fetch_core_table_counts, fetch_dataset_stats


COMPOUND_BATCH_HEADER_TABLE = "leadopt_compound_batches"
PROPERTY_BATCH_HEADER_TABLE = "leadopt_property_batches"


def _ensure_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is required for report operations (pip install psycopg[binary]).")


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", [table_name])
    row = cursor.fetchone()
    return bool(row and row[0] is not None)


def _table_columns(cursor: Any, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        [table_name],
    )
    return {str(row[0] or "").strip() for row in cursor.fetchall() if row and row[0]}


def _fetch_recent_compound_batches(cursor: Any, *, limit: int) -> List[Dict[str, Any]]:
    if not _table_exists(cursor, COMPOUND_BATCH_HEADER_TABLE):
        return []
    columns = _table_columns(cursor, COMPOUND_BATCH_HEADER_TABLE)
    numeric_expr = lambda name: f"COALESCE({name}, 0)" if name in columns else "0"
    text_expr = lambda name: f"COALESCE({name}, '')" if name in columns else "''"
    seq_order = "batch_seq" if "batch_seq" in columns else "imported_at"
    cursor.execute(
        f"""
        SELECT
            batch_id,
            {numeric_expr("batch_seq")} AS batch_seq,
            imported_at,
            {numeric_expr("total_rows")} AS total_rows,
            {numeric_expr("valid_rows")} AS valid_rows,
            {numeric_expr("dedup_rows")} AS dedup_rows,
            {numeric_expr("new_unique_rows")} AS new_unique_rows,
            {text_expr("label")} AS label,
            {text_expr("source_file")} AS source_file
        FROM {COMPOUND_BATCH_HEADER_TABLE}
        ORDER BY {seq_order} DESC
        LIMIT %s
        """,
        [max(1, int(limit or 1))],
    )
    output: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        output.append(
            {
                "batch_id": str(row[0] or "").strip(),
                "batch_seq": int(row[1] or 0),
                "imported_at": str(row[2] or "").strip(),
                "total_rows": int(row[3] or 0),
                "valid_rows": int(row[4] or 0),
                "dedup_rows": int(row[5] or 0),
                "new_unique_rows": int(row[6] or 0),
                "label": str(row[7] or "").strip(),
                "source_file": str(row[8] or "").strip(),
            }
        )
    return output


def _fetch_recent_property_batches(cursor: Any, *, limit: int) -> List[Dict[str, Any]]:
    if not _table_exists(cursor, PROPERTY_BATCH_HEADER_TABLE):
        return []
    columns = _table_columns(cursor, PROPERTY_BATCH_HEADER_TABLE)
    numeric_expr = lambda name: f"COALESCE({name}, 0)" if name in columns else "0"
    text_expr = lambda name: f"COALESCE({name}, '')" if name in columns else "''"
    if "valid_rows" in columns:
        valid_rows_expr = "COALESCE(valid_rows, 0)"
    elif "matched_rows" in columns:
        valid_rows_expr = "COALESCE(matched_rows, 0)"
    else:
        valid_rows_expr = "0"
    if "unmatched_rows" in columns:
        unmatched_rows_expr = "COALESCE(unmatched_rows, 0)"
    elif ("total_rows" in columns) and ("matched_rows" in columns):
        unmatched_rows_expr = "GREATEST(COALESCE(total_rows, 0) - COALESCE(matched_rows, 0), 0)"
    else:
        unmatched_rows_expr = "0"
    cursor.execute(
        f"""
        SELECT
            batch_id,
            imported_at,
            {numeric_expr("total_rows")} AS total_rows,
            {valid_rows_expr} AS valid_rows,
            {numeric_expr("matched_rows")} AS matched_rows,
            {unmatched_rows_expr} AS unmatched_rows,
            {numeric_expr("distinct_smiles")} AS distinct_smiles,
            {numeric_expr("distinct_pairs")} AS distinct_pairs,
            {text_expr("label")} AS label,
            {text_expr("source_file")} AS source_file
        FROM {PROPERTY_BATCH_HEADER_TABLE}
        ORDER BY imported_at DESC
        LIMIT %s
        """,
        [max(1, int(limit or 1))],
    )
    output: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        output.append(
            {
                "batch_id": str(row[0] or "").strip(),
                "imported_at": str(row[1] or "").strip(),
                "total_rows": int(row[2] or 0),
                "valid_rows": int(row[3] or 0),
                "matched_rows": int(row[4] or 0),
                "unmatched_rows": int(row[5] or 0),
                "distinct_smiles": int(row[6] or 0),
                "distinct_pairs": int(row[7] or 0),
                "label": str(row[8] or "").strip(),
                "source_file": str(row[9] or "").strip(),
            }
        )
    return output


def fetch_schema_metrics(target: PostgresTarget, *, recent_limit: int = 10) -> Dict[str, Any]:
    _ensure_psycopg()
    dataset = fetch_dataset_stats(target)
    core = fetch_core_table_counts(target)
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            recent_compounds = _fetch_recent_compound_batches(cursor, limit=recent_limit)
            recent_properties = _fetch_recent_property_batches(cursor, limit=recent_limit)
    return {
        "schema": target.schema,
        "dataset": {
            "compounds": dataset.compounds,
            "rules": dataset.rules,
            "pairs": dataset.pairs,
            "rule_environments": dataset.rule_environments,
        },
        "core_table_counts": core,
        "recent_compound_batches": recent_compounds,
        "recent_property_batches": recent_properties,
    }


def write_metrics_json(output_file: str, payload: Dict[str, Any]) -> str:
    path = str(output_file or "").strip()
    if not path:
        raise ValueError("output_file is required")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path
