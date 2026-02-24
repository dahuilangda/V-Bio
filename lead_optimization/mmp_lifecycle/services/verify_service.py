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


def fetch_incremental_integrity_issues(
    target: PostgresTarget,
    *,
    include_duplicate_pair_scan: bool = False,
) -> Dict[str, int]:
    _ensure_psycopg()
    output: Dict[str, int] = {}
    with psycopg.connect(target.url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{target.schema}", public')
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pair p
                LEFT JOIN compound c1 ON c1.id = p.compound1_id
                LEFT JOIN compound c2 ON c2.id = p.compound2_id
                WHERE c1.id IS NULL OR c2.id IS NULL
                """
            )
            output["orphan_pair_compound"] = int((cursor.fetchone() or [0])[0] or 0)

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pair p
                LEFT JOIN rule_environment re ON re.id = p.rule_environment_id
                WHERE re.id IS NULL
                """
            )
            output["orphan_pair_rule_environment"] = int((cursor.fetchone() or [0])[0] or 0)

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM rule_environment_statistics rs
                LEFT JOIN rule_environment re ON re.id = rs.rule_environment_id
                WHERE re.id IS NULL
                """
            )
            output["orphan_rule_environment_statistics"] = int((cursor.fetchone() or [0])[0] or 0)

            cursor.execute("SELECT to_regclass('from_construct')")
            has_from_construct = cursor.fetchone()[0] is not None
            cursor.execute("SELECT to_regclass('to_construct')")
            has_to_construct = cursor.fetchone()[0] is not None
            output["has_from_construct"] = 1 if has_from_construct else 0
            output["has_to_construct"] = 1 if has_to_construct else 0

            if has_from_construct:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM from_construct fc
                    LEFT JOIN pair p ON p.id = fc.pair_id
                    WHERE p.id IS NULL
                    """
                )
                output["orphan_from_construct_pair"] = int((cursor.fetchone() or [0])[0] or 0)
            else:
                output["orphan_from_construct_pair"] = 0

            if has_to_construct:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM to_construct tc
                    LEFT JOIN pair p ON p.id = tc.pair_id
                    WHERE p.id IS NULL
                    """
                )
                output["orphan_to_construct_pair"] = int((cursor.fetchone() or [0])[0] or 0)
            else:
                output["orphan_to_construct_pair"] = 0

            if include_duplicate_pair_scan:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT
                            rule_environment_id,
                            compound1_id,
                            compound2_id,
                            constant_id
                        FROM pair
                        GROUP BY 1, 2, 3, 4
                        HAVING COUNT(*) > 1
                    ) dup
                    """
                )
                output["duplicate_pair_key_groups"] = int((cursor.fetchone() or [0])[0] or 0)
            else:
                output["duplicate_pair_key_groups"] = -1
    return output
