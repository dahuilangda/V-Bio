from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

try:
    import psycopg
except Exception:
    psycopg = None

from ..models import PostgresTarget


@dataclass(frozen=True)
class DatasetStats:
    compounds: int
    rules: int
    pairs: int
    rule_environments: int


def _ensure_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is required for verify operations (pip install psycopg[binary]).")


def fetch_dataset_stats(target: PostgresTarget) -> DatasetStats:
    _ensure_psycopg()
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute(
                """
                SELECT
                    COALESCE(num_compounds, 0),
                    COALESCE(num_rules, 0),
                    COALESCE(num_pairs, 0),
                    COALESCE(num_rule_environments, 0)
                FROM dataset
                ORDER BY id
                LIMIT 1
                """
            )
            row = cursor.fetchone()
    if not row:
        return DatasetStats(compounds=0, rules=0, pairs=0, rule_environments=0)
    return DatasetStats(
        compounds=int(row[0] or 0),
        rules=int(row[1] or 0),
        pairs=int(row[2] or 0),
        rule_environments=int(row[3] or 0),
    )


def count_pairs_touching_smiles(target: PostgresTarget, smiles: str) -> int:
    _ensure_psycopg()
    token = str(smiles or "").strip()
    if not token:
        return 0
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute("SELECT id FROM compound WHERE clean_smiles = %s ORDER BY id LIMIT 1", [token])
            row = cursor.fetchone()
            if not row:
                return 0
            compound_id = int(row[0])
            cursor.execute(
                "SELECT COUNT(*) FROM pair WHERE compound1_id = %s OR compound2_id = %s",
                [compound_id, compound_id],
            )
            value = cursor.fetchone()
    return int(value[0] or 0) if value else 0


def count_pairs_touching_compound_batch(target: PostgresTarget, batch_id: str) -> int:
    _ensure_psycopg()
    token = str(batch_id or "").strip()
    if not token:
        return 0
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute(
                """
                WITH batch_compounds AS (
                    SELECT c.id AS compound_id
                    FROM compound c
                    INNER JOIN leadopt_compound_batch_rows b
                            ON b.clean_smiles = c.clean_smiles
                    WHERE b.batch_id = %s
                )
                SELECT COUNT(*)
                FROM pair p
                WHERE EXISTS (
                    SELECT 1
                    FROM batch_compounds bc
                    WHERE p.compound1_id = bc.compound_id OR p.compound2_id = bc.compound_id
                )
                """,
                [token],
            )
            row = cursor.fetchone()
    return int(row[0] or 0) if row else 0


def fetch_core_table_counts(target: PostgresTarget) -> Dict[str, int]:
    _ensure_psycopg()
    table_names = [
        "dataset",
        "compound",
        "rule_smiles",
        "constant_smiles",
        "rule",
        "environment_fingerprint",
        "rule_environment",
        "pair",
        "property_name",
        "compound_property",
        "rule_environment_statistics",
    ]
    output: Dict[str, int] = {}
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            for table_name in table_names:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                output[table_name] = int((cursor.fetchone() or [0])[0] or 0)
    return output
