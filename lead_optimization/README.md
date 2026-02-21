# Lead Optimization

This directory contains the runtime code that `VBio` currently uses for Lead Optimization.

## Build Dependencies

`setup_mmpdb.py` PostgreSQL build path requires both:

- `psycopg` (v3, used by finalize/metadata steps)
- `psycopg2` (used by current `mmpdb` PostgreSQL writer backend via peewee)

## Runtime Environment

Recommended: set these in project root `.env` (`/data/Boltz-WebUI/.env`) before running API/Celery:

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

Notes:

- `LEAD_OPT_MMP_DB_*` are backend runtime variables (read by Python services), not browser-side Vite vars.
- `VBio/.env` mainly configures frontend `VITE_*` options; backend DB routing should still be set in root `.env`.
- Temporary shell `export` is still valid, but `.env` is the persistent recommended way.

Optional:

```env
LEAD_OPT_MMP_DB_REGISTRY=/data/Boltz-WebUI/lead_optimization/data/mmp_db_registry.json
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
  --postgres_schema chembl36_full
```

### 2.2) Resume from existing `.fragdb` (skip fragment step)

If fragmenting already finished (for example `lead_optimization/data/chembl_compounds.fragdb`), resume directly:

```bash
cd /data/Boltz-WebUI
python lead_optimization/setup_mmpdb.py \
  --fragments_file lead_optimization/data/chembl_compounds.fragdb \
  --attachment_force_recompute \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full \
  --force
```

### 2.3) If index fails with `UndefinedTable: table "dataset" does not exist`

`setup_mmpdb.py` now applies a schema-safe index patch to support multi-schema coexistence in one PostgreSQL database.

- Existing mmpdb tables in other schemas are allowed.
- If target schema already has mmpdb tables, pass `--force` to rebuild that schema.

### 2.4) Rebuild `chembl_cyp3a4_herg` from local `ChEMBL_CYP3A4_hERG_*` files

Use this when your dataset files are already prepared at:

- `lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi`
- `lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt`
- `lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv`

Step 1: verify files exist

```bash
cd /data/Boltz-WebUI
ls -lh \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv
```

Step 2: rebuild into schema `chembl_cyp3a4_herg`

```bash
cd /data/Boltz-WebUI
python lead_optimization/setup_mmpdb.py \
  --structures_file lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  --properties_file lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  --property_metadata_file lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --attachment_force_recompute \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --force
```

Notes:

- `--force` will rebuild `chembl_cyp3a4_herg` if it already contains mmpdb core tables.
- Other schemas in the same database are kept intact (multi-schema coexistence is supported).
- For lower memory peak, add `--pg_skip_construct_tables`.

Step 3: switch runtime to this schema (edit root `.env`)

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

Step 4: verify data is visible through API

```bash
curl -H 'X-API-Token: <TOKEN>' \
  http://127.0.0.1:5000/api/lead_optimization/mmp_databases
```

### 2.5) Delete / cleanup MMP databases

#### Option A: delete via Admin API (recommended)

Step 1: list current databases and get `database_id`

```bash
curl -H 'X-API-Token: <TOKEN>' \
  http://127.0.0.1:5000/api/admin/lead_optimization/mmp_databases
```

Step 2: delete registry entry only (keep PostgreSQL data)

```bash
curl -X DELETE -H 'X-API-Token: <TOKEN>' \
  "http://127.0.0.1:5000/api/admin/lead_optimization/mmp_databases/<database_id>?drop_data=false"
```

Step 3: delete registry entry and drop schema data

```bash
curl -X DELETE -H 'X-API-Token: <TOKEN>' \
  "http://127.0.0.1:5000/api/admin/lead_optimization/mmp_databases/<database_id>?drop_data=true"
```

#### Option B: drop schema directly in PostgreSQL

Use this when you need hard cleanup at DB level:

```sql
DROP SCHEMA IF EXISTS chembl_cyp3a4_herg CASCADE;
```

If you want to rebuild the same schema immediately:

```sql
CREATE SCHEMA chembl_cyp3a4_herg;
```

Notes:

- `drop_data=true` or `DROP SCHEMA ... CASCADE` is destructive and irreversible.
- In multi-schema coexistence mode, dropping one schema does not affect other MMP schemas.

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
- `--postgres_schema`: target schema name. Use a dedicated schema in production (for example `chembl_cyp3a4_herg`) instead of `chembl36_full`.

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
