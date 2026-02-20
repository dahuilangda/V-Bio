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

Use this when you want `setup_mmpdb.py` to download/process ChEMBL and import into PostgreSQL in one run:

```bash
cd /data/Boltz-WebUI
python lead_optimization/setup_mmpdb.py \
  --download_chembl \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --force \
  --attachment_force_recompute \
  --pg_copy_workers 32 \
  --pg_copy_batch_size 10000 \
  --pg_copy_flush_rows 2000 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema public
```

Parameter notes:

- `--download_chembl`: fetch/process ChEMBL input automatically.
- `--output_dir`: local build/cache dir (runtime query path is still PostgreSQL).
- `--max_heavy_atoms 60`: pre-filter very large molecules to speed rule generation and reduce noise.
- `--force`: rebuild intermediate outputs even if files already exist.
- `--attachment_force_recompute`: force recompute attachment/fragment metadata for consistency.
- `--pg_copy_workers 32`: parallel PostgreSQL COPY workers; tune by CPU/IO capacity.
- `--pg_copy_batch_size 10000`: rows pulled per batch from staging tables.
- `--pg_copy_flush_rows 2000`: flush size for COPY writes; lower can reduce memory spikes.
- `--postgres_url`: PostgreSQL DSN used by Lead Optimization runtime.
- `--postgres_schema`: target schema name. Use a dedicated schema in production (for example `chembl_cyp3a4_herg`) instead of `public`.

Practical tuning:

- If host memory/IO is limited, start from `--pg_copy_workers 8` and increase gradually.
- If import is slow but stable, increase `--pg_copy_batch_size` first.
- If memory peaks are high, reduce `--pg_copy_flush_rows`.

Expected completion output includes:

- `PostgreSQL database is ready.`
- exported runtime env hints:
  - `LEAD_OPT_MMP_DB_URL=...`
  - `LEAD_OPT_MMP_DB_SCHEMA=...`

Notes:

- `setup_mmpdb.py` may use a staging SQLite file during build, but runtime query path is PostgreSQL-only.
- On success, staging DB is removed by default (unless `--keep_staging_db`).

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
