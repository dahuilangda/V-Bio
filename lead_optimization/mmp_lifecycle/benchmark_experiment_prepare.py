from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import api_server_mmp_lifecycle_routes as routes
from lead_optimization.mmp_lifecycle.admin_store import MmpLifecycleAdminStore


def _read_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_state(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid state JSON: {path}")
    return data


def _benchmark_existing_batch(
    *,
    state: Dict[str, Any],
    store: MmpLifecycleAdminStore,
    batch_id: str,
    runs: int,
    logger: logging.Logger,
) -> None:
    batches = state.get("batches") if isinstance(state.get("batches"), list) else []
    batch = next((item for item in batches if _read_text((item or {}).get("id")) == batch_id), None)
    if not isinstance(batch, dict):
        raise ValueError(f"Batch not found in state: {batch_id}")
    database_id = _read_text(batch.get("selected_database_id"))
    mappings = [
        item
        for item in (state.get("property_mappings") or [])
        if isinstance(item, dict) and _read_text(item.get("database_id")) == database_id
    ]

    print(f"[existing] batch={batch_id} db={database_id} runs={runs}")
    out = routes._build_property_import_file_from_experiments_fast(
        logger=logger,
        store=store,
        batch=batch,
        database_id=database_id,
        mappings=mappings,
    )
    print(f"warmup summary={out['summary']}")
    elapsed: List[float] = []
    for idx in range(runs):
        t0 = time.perf_counter()
        out = routes._build_property_import_file_from_experiments_fast(
            logger=logger,
            store=store,
            batch=batch,
            database_id=database_id,
            mappings=mappings,
        )
        dt = time.perf_counter() - t0
        elapsed.append(dt)
        print(f"run{idx + 1}={dt:.4f}s summary={out['summary']}")
    print(f"avg={sum(elapsed)/len(elapsed):.4f}s min={min(elapsed):.4f}s max={max(elapsed):.4f}s")


def _load_smiles_pool(path: str, limit: int) -> List[str]:
    rows: List[str] = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            smiles = _read_text((row or {}).get("smiles"))
            if not smiles:
                continue
            rows.append(smiles)
            if len(rows) >= limit:
                break
    if not rows:
        raise ValueError(f"No smiles found: {path}")
    return rows


def _benchmark_synthetic_mapped(
    *,
    store: MmpLifecycleAdminStore,
    source_compounds_tsv: str,
    rows: int,
    runs: int,
    logger: logging.Logger,
) -> None:
    smiles_pool = _load_smiles_pool(source_compounds_tsv, limit=3000)
    random.seed(7)
    fd, tmp_path = tempfile.mkstemp(prefix="mmp_exp_bench_", suffix=".tsv", dir="/tmp")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["smiles", "source_property", "value"])
            for idx in range(rows):
                smiles = smiles_pool[idx % len(smiles_pool)]
                prop = "CYP3A4 (uM)" if idx % 2 == 0 else "hERG (uM)"
                value = round(random.uniform(0.01, 30.0), 6)
                writer.writerow([smiles, prop, value])

        batch = {
            "id": "benchmark_synthetic_batch",
            "files": {
                "experiments": {
                    "path": tmp_path,
                    "stored_name": Path(tmp_path).name,
                    "size": os.path.getsize(tmp_path),
                    "uploaded_at": "2026-02-23T00:00:00Z",
                    "column_config": {
                        "smiles_column": "smiles",
                        "property_column": "source_property",
                        "value_column": "value",
                        "activity_transform_map": {},
                    },
                }
            },
        }
        mappings = [
            {"source_property": "CYP3A4 (uM)", "mmp_property": "CYP3A4 (uM)"},
            {"source_property": "hERG (uM)", "mmp_property": "hERG (uM)"},
        ]

        print(f"[synthetic] rows={rows} runs={runs} source={source_compounds_tsv}")
        out = routes._build_property_import_file_from_experiments_fast(
            logger=logger,
            store=store,
            batch=batch,
            database_id="benchmark_db",
            mappings=mappings,
        )
        print(f"warmup summary={out['summary']}")
        elapsed: List[float] = []
        for idx in range(runs):
            t0 = time.perf_counter()
            out = routes._build_property_import_file_from_experiments_fast(
                logger=logger,
                store=store,
                batch=batch,
                database_id="benchmark_db",
                mappings=mappings,
            )
            dt = time.perf_counter() - t0
            elapsed.append(dt)
            print(f"run{idx + 1}={dt:.4f}s summary={out['summary']}")
        print(f"avg={sum(elapsed)/len(elapsed):.4f}s min={min(elapsed):.4f}s max={max(elapsed):.4f}s")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark mmp lifecycle experiment prepare path.")
    parser.add_argument(
        "--state",
        default="lead_optimization/data/mmp_lifecycle_admin/state.json",
        help="Path to lifecycle admin state.json",
    )
    parser.add_argument(
        "--batch-id",
        default="batch_20260222_121849_1aa49b58",
        help="Batch id for existing-batch benchmark",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of measured runs per scenario")
    parser.add_argument("--synthetic-rows", type=int, default=120000, help="Synthetic mapped benchmark rows")
    parser.add_argument(
        "--synthetic-compounds-tsv",
        default="lead_optimization/data/mmp_lifecycle_admin/uploads/batch_20260222_121849_1aa49b58/compounds.tsv",
        help="Compounds TSV used as smiles source pool for synthetic benchmark",
    )
    parser.add_argument(
        "--mode",
        choices=["existing", "synthetic", "both"],
        default="both",
        help="Benchmark scenario",
    )
    args = parser.parse_args()

    state = _load_state(args.state)
    store = MmpLifecycleAdminStore()
    logger = logging.getLogger("mmp_lifecycle_benchmark")
    logger.setLevel(logging.ERROR)

    if args.mode in {"existing", "both"}:
        _benchmark_existing_batch(
            state=state,
            store=store,
            batch_id=args.batch_id,
            runs=max(1, int(args.runs)),
            logger=logger,
        )

    if args.mode in {"synthetic", "both"}:
        _benchmark_synthetic_mapped(
            store=store,
            source_compounds_tsv=args.synthetic_compounds_tsv,
            rows=max(1000, int(args.synthetic_rows)),
            runs=max(1, int(args.runs)),
            logger=logger,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
