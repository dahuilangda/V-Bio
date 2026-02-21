# Lead Optimization

This directory contains the runtime code that `VBio` currently uses for Lead Optimization.

## MMP Lifecycle Toolkit

An engineered tool package now lives at `lead_optimization/mmp_lifecycle` with
layered modules (`services`, `models`, unified CLI) for:

- database build/index
- compound/property batch import/delete/list
- registry operations
- verification (`dataset` counts, pair-touch checks)

Entry point:

```bash
python -m lead_optimization.mmp_lifecycle --help
```

Quick verify example:

```bash
python -m lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220
```

Detailed examples are documented in `lead_optimization/mmp_lifecycle/README.md`.
The complete `ChEMBL_CYP3A4_hERG_*` rebuild case (including `props` import via
`--properties_file`) is in section `3.0`.
The two-step flow (compounds first, then props) is in section `3.0B`.

## Build Dependencies

`lead_optimization.mmp_lifecycle.engine` PostgreSQL build path requires both:

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
python -m lead_optimization.mmp_lifecycle.engine \
  --structures_file /path/to/structures.smi \
  --properties_file /path/to/properties.tsv \
  --property_metadata_file /path/to/property_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir lead_optimization/data
```

### 2.1) Full ChEMBL build example (download + build + import)

Use this when you want `lead_optimization.mmp_lifecycle.engine` to download/process ChEMBL and build directly into PostgreSQL in one run:

```bash
cd /data/Boltz-WebUI
python -m lead_optimization.mmp_lifecycle.engine \
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
python -m lead_optimization.mmp_lifecycle.engine \
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

`lead_optimization.mmp_lifecycle.engine` now applies a schema-safe index patch to support multi-schema coexistence in one PostgreSQL database.

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
python -m lead_optimization.mmp_lifecycle.engine \
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

### 2.6) Incremental property batches (SMILES + property columns)

Use this for lifecycle-friendly property updates on an existing MMP schema:

- Upload one batch at a time.
- Query uses refreshed `compound_property` immediately.
- Delete by `batch_id` with automatic rollback to previous value (or original baseline value).
- New properties become query-resolvable without restarting service.

Supported file format:

- CSV or TSV with header.
- Must contain a SMILES column (`smiles` / `canonical_smiles`, case-insensitive).
- One or more numeric property columns (for example `CYP3A4`, `hERG_pIC50`).
- Missing tokens are ignored: `*`, `NA`, `N/A`, `NULL`, empty.

`property_batch.tsv` detailed spec:

1. File encoding and delimiter
- UTF-8 text.
- Recommended: TSV (`\t`); CSV is also supported.
- First line must be header.

2. Required column(s)
- Exactly one SMILES source column is required.
- Auto-detected SMILES column names (case-insensitive):
  - `smiles`
  - `canonical_smiles`
  - `mol_smiles`
  - `molecule_smiles`
  - `query_smiles`
- If your SMILES header is different, pass `--property_batch_smiles_column <your_column_name>`.

3. Property columns
- Any non-ID column except the SMILES column is treated as a property column.
- Ignored ID/name-like columns (case-insensitive):
  - `id`, `compound_id`, `cmpd_chemblid`, `chembl_id`, `molecule_id`, `mol_id`, `public_id`, `name`
- Property column names are written into `property_name.name` if not already present.

4. Value parsing rules
- Numeric values: parsed as `float`.
- Ignored as missing:
  - empty string
  - `*`
  - `NA`, `N/A`, `NaN`, `NULL`, `NONE`, `-` (case-insensitive)
- Non-numeric text is ignored for that property cell.

5. Duplicate row behavior
- Dedup key: `(SMILES, property_name)`.
- If the same key appears multiple times in one file, the last occurrence wins.

6. SMILES matching behavior
- Default behavior: RDKit canonicalization is applied before matching DB `compound.clean_smiles`.
- Disable canonicalization only if your file is already canonical and exactly aligned:
  - `--property_batch_no_canonicalize_smiles`
- Rows with invalid SMILES or unmatched SMILES are skipped and counted in import stats.

7. Import effect on runtime query
- Imported properties are upserted into `compound_property` for matched compounds.
- New property names become query-resolvable immediately (no service restart required).
- Batch deletion (`--delete_properties_batch`) rolls values back to:
  - latest remaining batch value, or
  - baseline snapshot (first incremental import time), if no later batch remains.

Minimal TSV example:

```tsv
smiles	CYP3A4	hERG_pIC50
CCOc1ccc(cc1)C(=O)N	5.62	6.11
CCN(CC)CCOc1ccccc1	*	5.40
O=C(Nc1ccccc1)C2CCNCC2	4.98	NA
```

Recommended practical template:

```tsv
smiles	CYP3A4	hERG_pIC50
<SMILES_1>	6.13	5.82
<SMILES_2>	*	4.91
<SMILES_3>	5.07	*
```

Example import:

```bash
cd /data/Boltz-WebUI
python -m lead_optimization.mmp_lifecycle.engine \
  --import_properties_batch /path/to/property_batch.tsv \
  --property_batch_label "assay_2026w08" \
  --property_batch_notes "week-8 screening results" \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

Optional:

- `--property_batch_id <id>`: fixed batch id instead of auto-generated `batch_YYYYMMDD_HHMMSS`.
- `--property_batch_smiles_column <col>`: explicit smiles column name.
- `--property_batch_no_canonicalize_smiles`: skip RDKit canonicalization (only when input smiles already matches `compound.clean_smiles`).
- `--property_metadata_file <file>`: apply metadata update to `property_name` in the same run.

List existing batches:

```bash
python -m lead_optimization.mmp_lifecycle.engine \
  --list_properties_batches \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

Delete one batch and rollback:

```bash
python -m lead_optimization.mmp_lifecycle.engine \
  --delete_properties_batch batch_20260221_101500 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

Implementation note:

- The first incremental import seeds a baseline snapshot from current `compound_property`.
- Later imports write per-batch rows and recompute touched keys.
- Deleting a batch restores each touched key to the latest remaining batch value, or baseline if no batch remains.

### 2.7) Incremental compound batches (without full index rebuild)

`mmpdb` PostgreSQL backend does not provide native in-place append/delete for full pair graph.
This project now uses a constant-scoped incremental strategy:

- Record each compound batch in lifecycle tables.
- Detect structural delta (`new unique` for import / truly removed compounds for delete).
- Fragment only delta compounds, extract affected `constant_smiles`.
- Re-index only affected constants into a temporary schema.
- Merge temporary results back into target schema, then refresh `compound_property`.
- Keep query functionality unchanged (same runtime tables/query path).
- In shard mode, constants are weight-balanced by estimated fragment load (not naive count-split), reducing long-tail shard time.
- Temporary shard schemas skip mmpdb post-index finalize maintenance (index/analyze/stats) and rely on final target-side maintenance, which cuts incremental latency and avoids parallel deadlocks.
- Incremental add now runs an exact `pair` potential probe on affected keys `(constant_smiles, num_cuts, attachment_order)` (delta vs active compounds).
- Scope pruning now happens at both levels:
  - constants with zero pair potential are pruned;
  - delta fragment keys with zero pair potential are also pruned before index.
- Candidate scope is now built from `delta + real partner smiles` (same constant/num_cuts/attachment_order), not all compounds under affected constants.
- Filtered fragdb now applies the same fragment key (`constant_smiles + num_cuts + attachment_order`) instead of constant-only filtering, further shrinking index input.
- Candidate re-scope after pair probe is now in-memory (reuse already loaded active rows), removing an extra sqlite scan + PostgreSQL reload step.
- Finalize/property refresh scope is bounded to `delta_smiles` (not full candidate scope), reducing merge-phase cost on large catalogs.
- Incremental shard indexing applies a runtime `mmpdb` delta-only pair filter:
  - only pairs touching current delta IDs are materialized for temporary shard schemas;
  - candidate expansion is reduced from dense `N^2` pair enumeration to delta-driven `D*N` style expansion at constant-match level.
- Shard cache filtering now emits shard-local delta record IDs directly, so each shard no longer needs a second pass to re-scan filtered fragdb for delta IDs.
- When a shard uses delta-only pair materialization, merge runs in append mode for that shard (do not wipe existing constant pairs), preserving historical partner-partner pairs.
- Incremental merge now maps/inserts constants from temporary shard `pair` output (not only pre-probe constant list), so valid delta pairs with transformed constants are no longer dropped.
- If probe finds zero pair potential for the whole delta, setup enters a fast-path (no mmpdb re-index), only upserts `compound` + refreshes touched `compound_property`, and keeps `pair` unchanged by design.

Compound batch file format:

1. File encoding and delimiter
- UTF-8 text.
- TSV recommended (CSV also supported).
- Header required.

2. Required column
- A SMILES column is required.
- Auto-detected names: `smiles`, `canonical_smiles`, `mol_smiles`, `molecule_smiles`, `query_smiles`.
- Override with `--compound_batch_smiles_column`.

3. Optional ID column
- Auto-detected names: `id`, `public_id`, `cmpd_chemblid`, `chembl_id`, `compound_id`, `name`.
- Override with `--compound_batch_id_column`.
- If ID is empty/missing, a stable ID is auto-generated from canonical SMILES.

4. Dedup and matching
- Dedup key: canonical SMILES (last row wins in one batch file).
- Default canonicalization is ON; disable only if already aligned:
  - `--compound_batch_no_canonicalize_smiles`

Minimal compound batch TSV example:

```tsv
smiles	CMPD_CHEMBLID
CCOc1ccc(cc1)C(=O)N	CHEMBL_NEW_0001
CCN(CC)CCOc1ccccc1	CHEMBL_NEW_0002
O=C(Nc1ccccc1)C2CCNCC2	
```

Import compound batch (incremental index):

```bash
python -m lead_optimization.mmp_lifecycle.engine \
  --import_compounds_batch /path/to/compound_batch.tsv \
  --compound_batch_label "wave_03" \
  --pg_incremental_index_shards 4 \
  --pg_incremental_index_jobs 2 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full \
  --output_dir lead_optimization/data
```

List compound batches:

```bash
python -m lead_optimization.mmp_lifecycle.engine \
  --list_compounds_batches \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

Delete compound batch (incremental index):

```bash
python -m lead_optimization.mmp_lifecycle.engine \
  --delete_compounds_batch compound_batch_20260221_101500 \
  --pg_incremental_index_shards 4 \
  --pg_incremental_index_jobs 2 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full \
  --output_dir lead_optimization/data
```

Operational notes:

- This path avoids full `mmpdb index` on the whole schema.
- First incremental run will create a schema-level fragment cache if missing:
  - `<schema>_compound_state.cache.fragdb`
  This one-time seed can still take time on very large schemas.
- Cache fragdb is now auto-indexed for incremental lookups (`record(normalized_smiles)` and `fragmentation(constant_smiles, num_cuts, attachment_order, record_id)`), improving repeated delta runs.
- Subsequent compound batch updates reuse cache and only process affected constants.
- During incremental merge, avoid concurrent heavy writes on the same schema.
- `lead_optimization.mmp_lifecycle.engine` now logs phase timing for incremental run:
  - `cache_ready`, `delta_fragment`, `cache_merge`, `candidate_resolve`,
    `pair_probe`, `cache_filter`, `shard_prepare` (when enabled), `temp_schema_index`, `merge_finalize`, `total`.
  This helps you identify whether bottleneck is fragment/index or PostgreSQL merge.
- `candidate_resolve` now reports `scope_smiles`, which should be much smaller than full constant-hit scope on large databases.

Why `pair` may not increase after adding compounds:

- New compounds can be validly added into `compound` while contributing zero new matched pairs under current fragmentation/constant constraints.
- In that case logs will show `constants_with_pair_potential=0/N` and `Incremental add fast-path ... pair unchanged by design`.
- This is expected behavior, not data loss.

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
- `--pg_incremental_index_shards 1`: split affected constants into N shards for incremental index.
- `--pg_incremental_index_jobs 1`: run shard index tasks in parallel (recommended to keep `jobs <= shards`).
  Internally each shard is load-balanced by fragment weight to reduce tail latency.
- `--postgres_url`: PostgreSQL DSN used by Lead Optimization runtime.
- `--postgres_schema`: target schema name. Use a dedicated schema in production (for example `chembl_cyp3a4_herg`) instead of `chembl36_full`.

Practical tuning:

- For a 512GB host with up to 256GB available for this job, start with:
  - `--pg_index_maintenance_work_mem_mb 65536`
  - `--pg_index_work_mem_mb 512`
  - `--pg_index_parallel_workers 16`
  - `--fragment_jobs 32`
- For heavy incremental updates, enable shard mode first:
  - `--pg_incremental_index_shards 4`
  - `--pg_incremental_index_jobs 2`
  This typically reduces single-shot `temp_schema_index` tail latency and improves throughput.
- With shard mode, per-job memory is auto-scaled by concurrent jobs (maintenance/work/parallel workers are divided).
- If memory peaks are still high, reduce `--pg_index_work_mem_mb` first, then `--fragment_jobs`.
- If shard mode is enabled and wall time is still high, increase `--pg_incremental_index_shards` first, then `--pg_incremental_index_jobs`.
- If indexing is stable but slow, increase `--pg_index_parallel_workers` gradually (watch CPU saturation and I/O wait).
- If runtime pipeline does not depend on construct tables, add `--pg_skip_construct_tables` to significantly reduce build time and memory.
- If runtime pipeline does not query `constant_smiles.smiles_mol`, add `--pg_skip_constant_smiles_mol_index`.

Expected completion output includes:

- `PostgreSQL database is ready.`
- exported runtime env hints:
  - `LEAD_OPT_MMP_DB_URL=...`
  - `LEAD_OPT_MMP_DB_SCHEMA=...`

Notes:

- `lead_optimization.mmp_lifecycle.engine` is PostgreSQL-only. No SQLite staging/import path remains.
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
