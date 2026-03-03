from __future__ import annotations

import argparse
import csv
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import List, Tuple

import psycopg

from . import engine


CORE_TABLES = (
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
)


def _read_source_rows(source_file: str, *, batch_size: int, batch_count: int) -> List[Tuple[str, str]]:
    limit = max(1, int(batch_size)) * max(1, int(batch_count))
    rows: List[Tuple[str, str]] = []
    with open(source_file, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            smiles = str((row or {}).get("SMILES") or "").strip()
            cid = str((row or {}).get("CMPD_CHEMBLID") or "").strip()
            if not smiles:
                continue
            rows.append((smiles, cid))
            if len(rows) >= limit:
                break
    return rows


def _create_temp_schema(url: str, *, schema: str, template_schema: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{schema}"')
            for table in CORE_TABLES:
                cur.execute(
                    f'CREATE TABLE "{schema}"."{table}" '
                    f'(LIKE "{template_schema}"."{table}" INCLUDING DEFAULTS INCLUDING IDENTITY)'
                )


def _drop_schema(url: str, *, schema: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark incremental compound batch import wall time.")
    parser.add_argument("--postgres_url", required=True, type=str)
    parser.add_argument("--template_schema", required=True, type=str)
    parser.add_argument("--source_file", required=True, type=str)
    parser.add_argument("--batch_size", type=int, default=1000)
    parser.add_argument("--batch_count", type=int, default=6)
    parser.add_argument("--fragment_jobs", type=int, default=8)
    parser.add_argument("--incremental_index_shards", type=int, default=4)
    parser.add_argument("--incremental_index_jobs", type=int, default=2)
    parser.add_argument("--index_parallel_workers", type=int, default=8)
    parser.add_argument("--output_dir", type=str, default="/tmp")
    parser.add_argument("--keep_schema", action="store_true")
    args = parser.parse_args()

    source_rows = _read_source_rows(
        args.source_file,
        batch_size=args.batch_size,
        batch_count=args.batch_count,
    )
    required_rows = max(1, int(args.batch_size)) * max(1, int(args.batch_count))
    if len(source_rows) < required_rows:
        raise RuntimeError(
            f"Not enough source rows for benchmark: required={required_rows} got={len(source_rows)}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bench_schema = f"tmp_inc_bench_{stamp}"
    _create_temp_schema(args.postgres_url, schema=bench_schema, template_schema=args.template_schema)
    print(f"schema={bench_schema}")

    try:
        print("batch,ok,seconds,compound_count,pair_count")
        for i in range(max(1, int(args.batch_count))):
            chunk = source_rows[i * args.batch_size : (i + 1) * args.batch_size]
            fd, temp_file = tempfile.mkstemp(suffix=".tsv", prefix=f"inc_bench_{i+1:02d}_")
            os.close(fd)
            with open(temp_file, "w", encoding="utf-8") as handle:
                handle.write("smiles\tcompound_id\n")
                for smiles, cid in chunk:
                    handle.write(f"{smiles}\t{cid}\n")

            batch_id = f"inc_bench_batch_{i+1:02d}"
            t0 = time.time()
            ok = engine.import_compound_batch_postgres(
                args.postgres_url,
                schema=bench_schema,
                structures_file=temp_file,
                batch_id=batch_id,
                smiles_column="smiles",
                id_column="compound_id",
                output_dir=args.output_dir,
                fragment_jobs=max(1, int(args.fragment_jobs)),
                incremental_index_shards=max(1, int(args.incremental_index_shards)),
                incremental_index_jobs=max(1, int(args.incremental_index_jobs)),
                index_parallel_workers=max(1, int(args.index_parallel_workers)),
                skip_incremental_analyze=True,
                overwrite_existing_batch=True,
            )
            elapsed = time.time() - t0
            with psycopg.connect(args.postgres_url, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SELECT COUNT(*) FROM "{bench_schema}".compound')
                    compounds = int(cur.fetchone()[0] or 0)
                    cur.execute(f'SELECT COUNT(*) FROM "{bench_schema}".pair')
                    pairs = int(cur.fetchone()[0] or 0)
            print(f"{i+1},{int(bool(ok))},{elapsed:.3f},{compounds},{pairs}")
            os.remove(temp_file)
            if not ok:
                return 2
        return 0
    finally:
        if not bool(args.keep_schema):
            _drop_schema(args.postgres_url, schema=bench_schema)


if __name__ == "__main__":
    raise SystemExit(main())
