# Lead Optimization

This directory contains the runtime code that `VBio` currently uses for Lead Optimization.

## Runtime Environment

Set these before running API/Celery:

```bash
export LEAD_OPT_MMP_DB_URL='postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp'
export LEAD_OPT_MMP_DB_SCHEMA='chembl_cyp3a4_herg'
```

Optional:

```bash
export LEAD_OPT_MMP_DB_REGISTRY='/data/Boltz-WebUI/lead_optimization/data/mmp_db_registry.json'
```

## Build / Import MMP Database (Merged Guide)

### 1) Start PostgreSQL (optional local helper)

```bash
cd /data/Boltz-WebUI/lead_optimization/postgres
docker compose up -d
```

### 2) Import a structures/properties dataset into PostgreSQL schema

```bash
cd /data/Boltz-WebUI
python lead_optimization/setup_mmpdb.py \
  --structures_file /path/to/structures.smi \
  --properties_file /path/to/properties.tsv \
  --property_metadata_file /path/to/property_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir lead_optimization/data
```

### 2.1) Full ChEMBL build example (download + build + import)

Use this when you want `setup_mmpdb.py` to download/process ChEMBL and build directly into PostgreSQL in one run:

```bash
cd /data/Boltz-WebUI
python lead_optimization/setup_mmpdb.py \
  --download_chembl \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --force \
  --attachment_force_recompute \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema public
```

Parameter notes:

- `--download_chembl`: fetch/process ChEMBL input automatically.
- `--output_dir`: local build/cache dir (runtime query path is still PostgreSQL).
- `--max_heavy_atoms 60`: pre-filter very large molecules to speed rule generation and reduce noise.
- `--force`: rebuild intermediate outputs even if files already exist.
- `--attachment_force_recompute`: force recompute attachment/fragment metadata for consistency.
- `--fragment_jobs 32`: `mmpdb fragment` parallel workers (CPU scaling happens mainly here).
- `--pg_index_maintenance_work_mem_mb 65536`: PostgreSQL index-build memory budget (`maintenance_work_mem=64GB`).
- `--pg_index_work_mem_mb 512`: per-operation work memory (`work_mem=512MB`).
- `--pg_index_parallel_workers 16`: PostgreSQL index parallelism (`max_parallel_maintenance_workers`).
- `--postgres_url`: PostgreSQL DSN used by Lead Optimization runtime.
- `--postgres_schema`: target schema name. Use a dedicated schema in production (for example `chembl_cyp3a4_herg`) instead of `public`.

Practical tuning:

- For a 512GB host with up to 256GB available for this job, start with:
  - `--pg_index_maintenance_work_mem_mb 65536`
  - `--pg_index_work_mem_mb 512`
  - `--pg_index_parallel_workers 16`
  - `--fragment_jobs 32`
- If memory peaks are still high, reduce `--pg_index_work_mem_mb` first, then `--fragment_jobs`.
- If indexing is stable but slow, increase `--pg_index_parallel_workers` gradually (watch CPU saturation and I/O wait).
- If runtime pipeline does not depend on construct tables, add `--pg_skip_construct_tables` to significantly reduce build time and memory.
- If runtime pipeline does not query `constant_smiles.smiles_mol`, add `--pg_skip_constant_smiles_mol_index`.

Expected completion output includes:

- `PostgreSQL database is ready.`
- exported runtime env hints:
  - `LEAD_OPT_MMP_DB_URL=...`
  - `LEAD_OPT_MMP_DB_SCHEMA=...`

Notes:

- `setup_mmpdb.py` is PostgreSQL-only. No SQLite staging/import path remains.
- Build keeps a temporary `.fragdb` file only during processing; it is removed on success unless `--keep_fragdb` is set.

### 3) Verify catalog via API

```bash
curl -H 'X-API-Token: <TOKEN>' \
  http://127.0.0.1:5000/api/lead_optimization/mmp_databases
```

Admin view:

```bash
curl -H 'X-API-Token: <TOKEN>' \
  http://127.0.0.1:5000/api/admin/lead_optimization/mmp_databases
```

## APIs Used by VBio

- `POST /api/lead_optimization/fragment_preview`
- `POST /api/lead_optimization/reference_preview`
- `POST /api/lead_optimization/mmp_query`
- `GET  /api/lead_optimization/mmp_query_status/<task_id>`
- `GET  /api/lead_optimization/mmp_query_result/<query_id>`
- `GET  /api/lead_optimization/mmp_evidence/<transform_id>`
- `POST /api/lead_optimization/mmp_enumerate`
- `POST /api/lead_optimization/predict_candidate`

Legacy:

- `POST /api/lead_optimization/submit` -> returns `410` (disabled).

## Admin Operations

- List DBs: `GET /api/admin/lead_optimization/mmp_databases`
- Patch visibility/label/default: `PATCH /api/admin/lead_optimization/mmp_databases/<database_id>`
- Delete schema entry: `DELETE /api/admin/lead_optimization/mmp_databases/<database_id>?drop_data=true`
