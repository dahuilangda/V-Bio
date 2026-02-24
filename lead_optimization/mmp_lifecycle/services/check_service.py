from __future__ import annotations

import csv
import math
from typing import Any, Dict, List

try:
    import psycopg
except Exception:
    psycopg = None

from lead_optimization.mmp_lifecycle import engine as legacy

from ..models import PostgresTarget


def _ensure_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is required for check operations (pip install psycopg[binary]).")


def _float_equal(left: Any, right: Any, *, eps: float = 1e-6) -> bool:
    try:
        lv = float(left)
        rv = float(right)
    except Exception:
        return False
    return math.isclose(lv, rv, rel_tol=1e-9, abs_tol=eps)


def _count_actions(rows: List[Dict[str, Any]], key: str = "action") -> Dict[str, int]:
    output: Dict[str, int] = {}
    for row in rows:
        action = str(row.get(key, "") or "").strip() or "UNKNOWN"
        output[action] = output.get(action, 0) + 1
    return output


def preview_compound_import(
    target: PostgresTarget,
    *,
    structures_file: str,
    smiles_column: str = "",
    id_column: str = "",
    canonicalize_smiles: bool = True,
) -> Dict[str, Any]:
    _ensure_psycopg()
    result_rows: List[Dict[str, Any]] = []
    with psycopg.connect(target.url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            legacy._pg_set_search_path(cursor, target.schema)
            legacy._ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
            parse_stats = legacy._load_compound_batch_temp_table(
                cursor,
                structures_file=structures_file,
                smiles_column=smiles_column,
                id_column=id_column,
                canonicalize_smiles=canonicalize_smiles,
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_compound_upload_ranked")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_compound_upload_ranked AS
                SELECT
                    line_no,
                    input_smiles,
                    clean_smiles,
                    COALESCE(NULLIF(public_id, ''), 'CMPD_' || SUBSTRING(md5(clean_smiles), 1, 16)) AS public_id,
                    ROW_NUMBER() OVER (PARTITION BY clean_smiles ORDER BY line_no DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY clean_smiles) AS duplicate_count
                FROM tmp_compound_upload
                """
            )
            cursor.execute(
                f"""
                SELECT
                    r.line_no,
                    r.clean_smiles,
                    r.public_id,
                    r.input_smiles,
                    r.duplicate_count,
                    EXISTS(
                        SELECT 1 FROM compound c
                        WHERE c.clean_smiles = r.clean_smiles
                    ) AS in_compound,
                    EXISTS(
                        SELECT 1 FROM {legacy.COMPOUND_BATCH_BASE_TABLE} b
                        WHERE b.clean_smiles = r.clean_smiles
                    ) AS in_baseline,
                    EXISTS(
                        SELECT 1 FROM {legacy.COMPOUND_BATCH_ROWS_TABLE} br
                        WHERE br.clean_smiles = r.clean_smiles
                    ) AS in_incremental_rows,
                    COALESCE((
                        SELECT c.public_id
                        FROM compound c
                        WHERE c.clean_smiles = r.clean_smiles
                        ORDER BY c.id
                        LIMIT 1
                    ), '') AS existing_public_id
                FROM tmp_compound_upload_ranked r
                WHERE r.rn = 1
                ORDER BY r.line_no ASC
                """
            )
            for row in cursor.fetchall():
                line_no = int(row[0] or 0)
                clean_smiles = str(row[1] or "").strip()
                public_id = str(row[2] or "").strip()
                input_smiles = str(row[3] or "").strip()
                duplicate_count = int(row[4] or 0)
                in_compound = bool(row[5])
                in_baseline = bool(row[6])
                in_incremental_rows = bool(row[7])
                existing_public_id = str(row[8] or "").strip()

                if not in_baseline and not in_incremental_rows:
                    action = "UPSERT_BATCH_AND_REINDEX_STRUCTURE"
                    will_reindex = True
                    note = "Will introduce a new structural SMILES into active lifecycle state."
                elif in_baseline:
                    action = "UPSERT_BATCH_ONLY_BASELINE_EXISTS"
                    will_reindex = False
                    note = "SMILES already exists in baseline state; only batch lineage is updated."
                else:
                    action = "UPSERT_BATCH_ONLY_ALREADY_TRACKED"
                    will_reindex = False
                    note = "SMILES already exists in prior incremental rows; only latest batch lineage is updated."

                if duplicate_count > 1:
                    note = f"{note} Source has {duplicate_count} duplicate rows; latest line wins."

                result_rows.append(
                    {
                        "line_no": line_no,
                        "clean_smiles": clean_smiles,
                        "public_id": public_id,
                        "input_smiles": input_smiles,
                        "action": action,
                        "will_reindex": will_reindex,
                        "in_compound_table": in_compound,
                        "in_baseline": in_baseline,
                        "in_incremental_rows": in_incremental_rows,
                        "existing_public_id": existing_public_id,
                        "duplicate_count": duplicate_count,
                        "note": note,
                    }
                )
        conn.rollback()

    action_counts = _count_actions(result_rows)
    return {
        "summary": {
            **parse_stats,
            "annotated_rows": len(result_rows),
            "reindex_rows": sum(1 for row in result_rows if bool(row.get("will_reindex"))),
            "action_counts": action_counts,
        },
        "columns": [
            "line_no",
            "clean_smiles",
            "public_id",
            "input_smiles",
            "action",
            "will_reindex",
            "in_compound_table",
            "in_baseline",
            "in_incremental_rows",
            "existing_public_id",
            "duplicate_count",
            "note",
        ],
        "rows": result_rows,
    }


def preview_property_import(
    target: PostgresTarget,
    *,
    property_file: str,
    smiles_column: str = "",
    canonicalize_smiles: bool = True,
) -> Dict[str, Any]:
    _ensure_psycopg()
    result_rows: List[Dict[str, Any]] = []
    with psycopg.connect(target.url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            legacy._pg_set_search_path(cursor, target.schema)
            legacy._ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
            parse_stats = legacy._load_property_batch_temp_table(
                cursor,
                property_file=property_file,
                smiles_column=smiles_column,
                canonicalize_smiles=canonicalize_smiles,
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_property_upload_ranked")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_property_upload_ranked AS
                SELECT
                    line_no,
                    clean_smiles,
                    property_name,
                    value,
                    ROW_NUMBER() OVER (PARTITION BY clean_smiles, property_name ORDER BY line_no DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY clean_smiles, property_name) AS duplicate_count
                FROM tmp_property_upload
                """
            )
            cursor.execute(
                """
                SELECT
                    r.line_no,
                    r.clean_smiles,
                    r.property_name,
                    r.value,
                    r.duplicate_count,
                    c.id AS compound_id,
                    pn.id AS property_name_id,
                    cp.value AS existing_value
                FROM tmp_property_upload_ranked r
                LEFT JOIN compound c
                       ON c.clean_smiles = r.clean_smiles
                LEFT JOIN property_name pn
                       ON pn.name = r.property_name
                LEFT JOIN compound_property cp
                       ON cp.compound_id = c.id
                      AND cp.property_name_id = pn.id
                WHERE r.rn = 1
                ORDER BY r.line_no ASC
                """
            )
            for row in cursor.fetchall():
                line_no = int(row[0] or 0)
                clean_smiles = str(row[1] or "").strip()
                property_name = str(row[2] or "").strip()
                incoming_value = float(row[3])
                duplicate_count = int(row[4] or 0)
                compound_id = int(row[5]) if row[5] is not None else None
                property_name_id = int(row[6]) if row[6] is not None else None
                existing_value = float(row[7]) if row[7] is not None else None

                if compound_id is None:
                    action = "SKIP_UNMATCHED_COMPOUND"
                    will_insert_batch_row = False
                    will_touch_compound_property = False
                    note = "No matching compound.clean_smiles in current schema; row will not be imported."
                elif property_name_id is None:
                    action = "INSERT_PROPERTY_NAME_AND_COMPOUND_PROPERTY"
                    will_insert_batch_row = True
                    will_touch_compound_property = True
                    note = "Property name does not exist yet; property_name + compound_property will be inserted."
                elif existing_value is None:
                    action = "INSERT_COMPOUND_PROPERTY"
                    will_insert_batch_row = True
                    will_touch_compound_property = True
                    note = "Compound/property key does not exist; a new compound_property row will be inserted."
                elif _float_equal(existing_value, incoming_value):
                    action = "NOOP_VALUE_UNCHANGED"
                    will_insert_batch_row = True
                    will_touch_compound_property = False
                    note = "Incoming value matches existing compound_property value."
                else:
                    action = "UPDATE_COMPOUND_PROPERTY"
                    will_insert_batch_row = True
                    will_touch_compound_property = True
                    note = "Incoming value differs; compound_property value will be updated."

                if duplicate_count > 1:
                    note = f"{note} Source has {duplicate_count} duplicate rows for this key; latest line wins."

                result_rows.append(
                    {
                        "line_no": line_no,
                        "clean_smiles": clean_smiles,
                        "property_name": property_name,
                        "incoming_value": incoming_value,
                        "action": action,
                        "will_insert_batch_row": will_insert_batch_row,
                        "will_touch_compound_property": will_touch_compound_property,
                        "compound_id": compound_id,
                        "property_name_id": property_name_id,
                        "existing_value": existing_value,
                        "duplicate_count": duplicate_count,
                        "note": note,
                    }
                )
        conn.rollback()

    action_counts = _count_actions(result_rows)
    return {
        "summary": {
            **parse_stats,
            "annotated_rows": len(result_rows),
            "matched_rows": sum(1 for row in result_rows if row.get("compound_id") is not None),
            "action_counts": action_counts,
        },
        "columns": [
            "line_no",
            "clean_smiles",
            "property_name",
            "incoming_value",
            "action",
            "will_insert_batch_row",
            "will_touch_compound_property",
            "compound_id",
            "property_name_id",
            "existing_value",
            "duplicate_count",
            "note",
        ],
        "rows": result_rows,
    }


def write_annotated_table_tsv(path: str, columns: List[str], rows: List[Dict[str, Any]]) -> str:
    output_path = str(path or "").strip()
    if not output_path:
        raise ValueError("output path is required")
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = {
                key: (
                    "1"
                    if row.get(key) is True
                    else "0"
                    if row.get(key) is False
                    else ""
                    if row.get(key) is None
                    else row.get(key)
                )
                for key in columns
            }
            writer.writerow(payload)
    return output_path
