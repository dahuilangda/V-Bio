from __future__ import annotations

from typing import Any, Dict

try:
    import psycopg
except Exception:
    psycopg = None

from ..models import PostgresTarget


def _ensure_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is required for property admin operations (pip install psycopg[binary]).")


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", [table_name])
    row = cursor.fetchone()
    return bool(row and row[0] is not None)


def ensure_property_name(target: PostgresTarget, *, property_name: str) -> Dict[str, Any]:
    _ensure_psycopg()
    token = str(property_name or "").strip()
    if not token:
        raise ValueError("property_name is required")
    with psycopg.connect(target.url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute("SELECT id FROM property_name WHERE name = %s LIMIT 1", [token])
            row = cursor.fetchone()
            if row:
                conn.commit()
                return {"property_name": token, "property_name_id": int(row[0]), "created": False}
            cursor.execute("INSERT INTO property_name (name) VALUES (%s) RETURNING id", [token])
            created_id = int((cursor.fetchone() or [0])[0] or 0)
            conn.commit()
            return {"property_name": token, "property_name_id": created_id, "created": True}


def preview_property_delete_impact(target: PostgresTarget, *, property_name: str) -> Dict[str, Any]:
    _ensure_psycopg()
    token = str(property_name or "").strip()
    if not token:
        raise ValueError("property_name is required")
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute("SELECT id FROM property_name WHERE name = %s LIMIT 1", [token])
            row = cursor.fetchone()
            prop_id = int(row[0]) if row else 0
            impact = {
                "property_name": token,
                "property_name_id": prop_id or None,
                "compound_property_rows": 0,
                "rule_environment_statistics_rows": 0,
                "property_base_rows": 0,
                "property_batch_rows": 0,
            }
            if prop_id:
                cursor.execute("SELECT COUNT(*) FROM compound_property WHERE property_name_id = %s", [prop_id])
                impact["compound_property_rows"] = int((cursor.fetchone() or [0])[0] or 0)
                cursor.execute("SELECT COUNT(*) FROM rule_environment_statistics WHERE property_name_id = %s", [prop_id])
                impact["rule_environment_statistics_rows"] = int((cursor.fetchone() or [0])[0] or 0)
            if _table_exists(cursor, "leadopt_property_base"):
                cursor.execute("SELECT COUNT(*) FROM leadopt_property_base WHERE property_name = %s", [token])
                impact["property_base_rows"] = int((cursor.fetchone() or [0])[0] or 0)
            if _table_exists(cursor, "leadopt_property_batch_rows"):
                cursor.execute("SELECT COUNT(*) FROM leadopt_property_batch_rows WHERE property_name = %s", [token])
                impact["property_batch_rows"] = int((cursor.fetchone() or [0])[0] or 0)
            return impact


def purge_property_name(target: PostgresTarget, *, property_name: str) -> Dict[str, Any]:
    _ensure_psycopg()
    token = str(property_name or "").strip()
    if not token:
        raise ValueError("property_name is required")
    with psycopg.connect(target.url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute("SELECT id FROM property_name WHERE name = %s LIMIT 1", [token])
            row = cursor.fetchone()
            prop_id = int(row[0]) if row else 0
            deleted_cp = 0
            deleted_stats = 0
            deleted_base = 0
            deleted_batch_rows = 0
            deleted_property_name = 0

            if prop_id:
                cursor.execute("DELETE FROM compound_property WHERE property_name_id = %s", [prop_id])
                deleted_cp = max(0, int(cursor.rowcount or 0))
                cursor.execute("DELETE FROM rule_environment_statistics WHERE property_name_id = %s", [prop_id])
                deleted_stats = max(0, int(cursor.rowcount or 0))

            if _table_exists(cursor, "leadopt_property_base"):
                cursor.execute("DELETE FROM leadopt_property_base WHERE property_name = %s", [token])
                deleted_base = max(0, int(cursor.rowcount or 0))
            if _table_exists(cursor, "leadopt_property_batch_rows"):
                cursor.execute("DELETE FROM leadopt_property_batch_rows WHERE property_name = %s", [token])
                deleted_batch_rows = max(0, int(cursor.rowcount or 0))

            if prop_id:
                cursor.execute("DELETE FROM property_name WHERE id = %s", [prop_id])
                deleted_property_name = max(0, int(cursor.rowcount or 0))

            conn.commit()
            return {
                "property_name": token,
                "property_name_id": prop_id or None,
                "deleted_compound_property_rows": deleted_cp,
                "deleted_rule_environment_statistics_rows": deleted_stats,
                "deleted_property_base_rows": deleted_base,
                "deleted_property_batch_rows": deleted_batch_rows,
                "deleted_property_name_rows": deleted_property_name,
            }


def rename_property_name(target: PostgresTarget, *, old_name: str, new_name: str) -> Dict[str, Any]:
    _ensure_psycopg()
    old_token = str(old_name or "").strip()
    new_token = str(new_name or "").strip()
    if not old_token or not new_token:
        raise ValueError("old_name and new_name are required")
    if old_token == new_token:
        return {"old_name": old_token, "new_name": new_token, "changed": False}
    with psycopg.connect(target.url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute("SELECT id FROM property_name WHERE name = %s LIMIT 1", [old_token])
            old_row = cursor.fetchone()
            old_id = int(old_row[0]) if old_row else 0
            cursor.execute("SELECT id FROM property_name WHERE name = %s LIMIT 1", [new_token])
            new_row = cursor.fetchone()
            new_id = int(new_row[0]) if new_row else 0

            if not old_id and not new_id:
                cursor.execute("INSERT INTO property_name (name) VALUES (%s) RETURNING id", [new_token])
                new_id = int((cursor.fetchone() or [0])[0] or 0)
                conn.commit()
                return {"old_name": old_token, "new_name": new_token, "changed": True, "mode": "created_new"}
            if old_id and not new_id:
                cursor.execute("UPDATE property_name SET name = %s WHERE id = %s", [new_token, old_id])
                if _table_exists(cursor, "leadopt_property_base"):
                    cursor.execute("UPDATE leadopt_property_base SET property_name = %s WHERE property_name = %s", [new_token, old_token])
                if _table_exists(cursor, "leadopt_property_batch_rows"):
                    cursor.execute(
                        "UPDATE leadopt_property_batch_rows SET property_name = %s WHERE property_name = %s",
                        [new_token, old_token],
                    )
                conn.commit()
                return {"old_name": old_token, "new_name": new_token, "changed": True, "mode": "renamed"}
            if old_id and new_id:
                cursor.execute(
                    """
                    DELETE FROM compound_property old_cp
                    USING compound_property new_cp
                    WHERE old_cp.property_name_id = %s
                      AND new_cp.property_name_id = %s
                      AND old_cp.compound_id = new_cp.compound_id
                    """,
                    [old_id, new_id],
                )
                cursor.execute("UPDATE compound_property SET property_name_id = %s WHERE property_name_id = %s", [new_id, old_id])
                cursor.execute(
                    """
                    DELETE FROM rule_environment_statistics old_rs
                    USING rule_environment_statistics new_rs
                    WHERE old_rs.property_name_id = %s
                      AND new_rs.property_name_id = %s
                      AND old_rs.rule_environment_id = new_rs.rule_environment_id
                    """,
                    [old_id, new_id],
                )
                cursor.execute(
                    "UPDATE rule_environment_statistics SET property_name_id = %s WHERE property_name_id = %s",
                    [new_id, old_id],
                )
                if _table_exists(cursor, "leadopt_property_base"):
                    cursor.execute("UPDATE leadopt_property_base SET property_name = %s WHERE property_name = %s", [new_token, old_token])
                if _table_exists(cursor, "leadopt_property_batch_rows"):
                    cursor.execute(
                        "UPDATE leadopt_property_batch_rows SET property_name = %s WHERE property_name = %s",
                        [new_token, old_token],
                    )
                cursor.execute("DELETE FROM property_name WHERE id = %s", [old_id])
                conn.commit()
                return {"old_name": old_token, "new_name": new_token, "changed": True, "mode": "merged"}
            conn.commit()
            return {"old_name": old_token, "new_name": new_token, "changed": False}

