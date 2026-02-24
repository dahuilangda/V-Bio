# Lead Optimization MMP Lifecycle Toolkit

This package provides an engineered toolkit for MMP data lifecycle management on PostgreSQL:

- schema build and index
- compound batch add/delete (default incremental re-index path)
- incremental property batch add/delete
- registry management
- verification and metrics collection
- preflight check with row-level operation annotations for compound/property batch imports

Behavior note:
- `compound-import` / `compound-delete` default to incremental MMP re-index (no fallback to full rebuild).
- You can tune shard count and concurrency with `--pg_incremental_index_shards` and `--pg_incremental_index_jobs`.
- `--pg_index_commit_every_flushes` defaults to `1` (safer commit cadence on large runs); set `<=0` to use adaptive mode.
- Incremental add computes affected constants from delta `.fragdb` and rebuilds candidate pairs via `fragdb_partition`, so newly added compounds can correctly grow pair counts.
- Lifecycle state is deduplicated by `clean_smiles`; incremental validation should be compared against a rebuild of lifecycle state (not raw duplicated input rows).
- `--pg_incremental_index_jobs` now runs shard prepare/index steps concurrently (merge remains transaction-serialized for correctness).
- Incremental temp-shard indexing skips global core-table discovery scans to reduce metadata load on large multi-schema PostgreSQL instances.

It wraps proven logic from `lead_optimization/mmp_lifecycle/engine.py` and
`lead_optimization/mmp_database_registry.py` into a clean command surface.

## 1) Entry Points

Module entry:

```bash
python -m lead_optimization.mmp_lifecycle --help
```

Script-style entry (both direct script and module style are supported):

```bash
python lead_optimization/mmp_lifecycle/run_mmp_lifecycle.py --help
python lead_optimization/mmp_lifecycle/prepare_compound_batch_template.py --help
python lead_optimization/mmp_lifecycle/prepare_property_batch_template.py --help
python lead_optimization/mmp_lifecycle/collect_mmp_lifecycle_metrics.py --help

python -m lead_optimization.mmp_lifecycle.run_mmp_lifecycle --help
python -m lead_optimization.mmp_lifecycle.prepare_compound_batch_template --help
python -m lead_optimization.mmp_lifecycle.prepare_property_batch_template --help
python -m lead_optimization.mmp_lifecycle.collect_mmp_lifecycle_metrics --help
```

Preflight check examples (annotated operation table):

```bash
python -m lead_optimization.mmp_lifecycle check-compound-import \
  --file lead_optimization/data/compound_batch.tsv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_tsv lead_optimization/data/compound_batch_check.tsv

python -m lead_optimization.mmp_lifecycle check-property-import \
  --file lead_optimization/data/property_batch.tsv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_tsv lead_optimization/data/property_batch_check.tsv
```

## 2) Runtime Environment

Set backend MMP routing in project root `.env` (`/data/Boltz-WebUI/.env`):

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

Notes:

- This is backend env, not frontend `VITE_*`.
- `VBio/.env` should not be used for backend MMP DB routing.

## 3) Full Lifecycle Cookbook

### 3.0 Complete example: `ChEMBL_CYP3A4_hERG_*` (structures + props + metadata)

This is the recommended full import path when you already have:

- `lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi`
- `lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt`
- `lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv`

Step 1: verify files

```bash
ls -lh \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv
```

Step 2: one-shot rebuild and import structures + props + metadata

```bash
python -m lead_optimization.mmp_lifecycle db-build \
  --smiles_file lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  --properties_file lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  --property_metadata_file lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

Step 3: verify dataset and property counts

```bash
python -m lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg
```

Expected: `property_name` and `compound_property` should be non-zero.

Step 4: set runtime schema in root `.env`

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

If you already have `.fragdb` and want rebuild + import props in one pass:

```bash
python -m lead_optimization.mmp_lifecycle db-index-fragdb \
  --fragments_file lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.fragdb \
  --properties_file lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  --property_metadata_file lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --force
```

Note:

- `--properties_file` is the correct way to import this `ChEMBL_CYP3A4_hERG_props.txt` baseline property file.
- `property-import` is for incremental SMILES-based property batches (`property_batch.tsv`), not for this baseline props format.

### 3.0B Two-step import: compounds first, then props

Use this when you want to build structure index first, then import properties in a separate step.

Step 1: build compounds only (no baseline props yet)

```bash
python -m lead_optimization.mmp_lifecycle db-build \
  --smiles_file lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

Step 2: convert `ChEMBL_CYP3A4_hERG_props.txt` (ID keyed) to SMILES-keyed `property_batch.tsv`

```bash
python - <<'PY'
import csv
from pathlib import Path

structures = Path("lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi")
props = Path("lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt")
out = Path("lead_optimization/data/ChEMBL_CYP3A4_hERG_property_batch.tsv")

# structures.smi: SMILES<TAB>ID (header allowed)
id_to_smiles = {}
with structures.open("r", encoding="utf-8") as fh:
    for line_no, raw in enumerate(fh, 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        smi, cid = parts[0].strip(), parts[1].strip()
        if line_no == 1 and smi.lower() == "smiles":
            continue
        if smi and cid:
            id_to_smiles[cid] = smi

with props.open("r", encoding="utf-8") as fin, out.open("w", encoding="utf-8", newline="") as fout:
    reader = csv.reader(fin, delimiter="\t")
    writer = csv.writer(fout, delimiter="\t")
    header = next(reader)
    writer.writerow(["smiles", *header[1:]])
    for row in reader:
        if not row:
            continue
        cid = row[0].strip()
        smi = id_to_smiles.get(cid)
        if not smi:
            continue
        writer.writerow([smi, *row[1:]])

print(out)
PY
```

Step 3: import converted property batch

```bash
python -m lead_optimization.mmp_lifecycle property-import \
  --file lead_optimization/data/ChEMBL_CYP3A4_hERG_property_batch.tsv \
  --batch_label chembl_cyp3a4_herg_baseline_props \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg
```

Step 4: apply property metadata

```bash
python -m lead_optimization.mmp_lifecycle metadata-apply \
  --file lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg
```

Step 5: verify

```bash
python -m lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg

python -m lead_optimization.mmp_lifecycle property-list \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg
```

Notes:

- This two-step path avoids rebuilding the structure index when only properties need refresh.
- If your props file is already a SMILES-keyed `property_batch.tsv`, skip Step 2 and run `property-import` directly.

### 3.1 Create or rebuild a schema from structures

```bash
python -m lead_optimization.mmp_lifecycle db-build \
  --smiles_file lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  --properties_file lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  --property_metadata_file lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir lead_optimization/data \
  --max_heavy_atoms 60 \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

### 3.2 Index from existing `.fragdb`

```bash
python -m lead_optimization.mmp_lifecycle db-index-fragdb \
  --fragments_file lead_optimization/data/chembl_compounds.fragdb \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

### 3.3 Incremental compound add

```bash
python -m lead_optimization.mmp_lifecycle compound-import \
  --file lead_optimization/data/bench_compound_batch_0020_opt.tsv \
  --batch_label bench_wave \
  --pg_incremental_index_shards 4 \
  --pg_incremental_index_jobs 2 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220
```

### 3.4 Incremental compound delete

```bash
python -m lead_optimization.mmp_lifecycle compound-delete \
  --batch_id compound_batch_20260221_094933 \
  --pg_incremental_index_shards 4 \
  --pg_incremental_index_jobs 2 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220
```

### 3.5 Incremental property add

```bash
python -m lead_optimization.mmp_lifecycle property-import \
  --file /path/to/property_batch.tsv \
  --batch_label assay_week08 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

### 3.6 Incremental property delete

```bash
python -m lead_optimization.mmp_lifecycle property-delete \
  --batch_id batch_20260221_101500 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

### 3.7 List lifecycle batches

```bash
python -m lead_optimization.mmp_lifecycle compound-list \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full

python -m lead_optimization.mmp_lifecycle property-list \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema chembl36_full
```

### 3.8 Verify counts and pair touch

```bash
python -m lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220

python -m lead_optimization.mmp_lifecycle verify-pair-smiles \
  --smiles 'CC(C)(S)C(N)C(=O)O' \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220

python -m lead_optimization.mmp_lifecycle verify-pair-batch \
  --batch_id compound_batch_20260221_094933 \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220
```

### 3.9 Collect metrics report

```bash
python -m lead_optimization.mmp_lifecycle report-metrics \
  --postgres_url 'postgresql://leadopt:leadopt@127.0.0.1:54330/leadopt_mmp' \
  --postgres_schema bench_herg_inc_0221_043220 \
  --recent_limit 20 \
  --output_json /tmp/bench_herg_metrics.json
```

### 3.10 Registry operations

```bash
python -m lead_optimization.mmp_lifecycle registry-list

python -m lead_optimization.mmp_lifecycle registry-set \
  --database_id pg:127.0.0.1:54330:leadopt_mmp:chembl36_full \
  --visible true

python -m lead_optimization.mmp_lifecycle registry-delete \
  --database_id pg:127.0.0.1:54330:leadopt_mmp:chembl36_full \
  --drop_data
```

## 4) Batch File Specs

### 4.1 `compound_batch.tsv` specification

Required:

- header row
- one SMILES column

SMILES auto-detection (case-insensitive):

- `smiles`
- `canonical_smiles`
- `mol_smiles`
- `molecule_smiles`
- `query_smiles`

Optional ID column auto-detection:

- `id`
- `public_id`
- `cmpd_chemblid`
- `chembl_id`
- `compound_id`
- `name`

Behavior:

- canonicalization is enabled by default
- dedup key is canonical SMILES (last row wins)
- empty ID is allowed (stable ID generated)

Minimal example:

```tsv
smiles	cmpd_chemblid
CCOc1ccc(cc1)C(=O)N	CHEMBL_NEW_0001
CCN(CC)CCOc1ccccc1	CHEMBL_NEW_0002
O=C(Nc1ccccc1)C2CCNCC2	
```

Template generator:

```bash
python -m lead_optimization.mmp_lifecycle template-compound \
  --output /tmp/compound_batch_template.tsv --rows 5
```

### 4.2 `property_batch.tsv` specification

Required:

- header row
- one SMILES column
- one or more numeric property columns

SMILES auto-detection (case-insensitive):

- `smiles`
- `canonical_smiles`
- `mol_smiles`
- `molecule_smiles`
- `query_smiles`

ID-like columns ignored as properties (case-insensitive):

- `id`
- `compound_id`
- `cmpd_chemblid`
- `chembl_id`
- `molecule_id`
- `mol_id`
- `public_id`
- `name`

Value parsing:

- numeric cell: parsed as `float`
- missing tokens ignored: empty, `*`, `NA`, `N/A`, `NaN`, `NULL`, `NONE`, `-`
- non-numeric text ignored for that property cell
- duplicate `(smiles, property)` in one file: last row wins

Matching behavior:

- canonicalization enabled by default
- unmatched or invalid SMILES rows are skipped and counted

Minimal example:

```tsv
smiles	CYP3A4	hERG_pIC50
CCOc1ccc(cc1)C(=O)N	5.62	6.11
CCN(CC)CCOc1ccccc1	*	5.40
O=C(Nc1ccccc1)C2CCNCC2	4.98	NA
```

Template generator:

```bash
python -m lead_optimization.mmp_lifecycle template-property \
  --output /tmp/property_batch_template.tsv \
  --properties CYP3A4,hERG_pIC50 \
  --rows 5
```

## 5) Database Cleanup

Drop one schema directly in PostgreSQL:

```sql
DROP SCHEMA IF EXISTS chembl_cyp3a4_herg CASCADE;
```

Recreate empty schema:

```sql
CREATE SCHEMA chembl_cyp3a4_herg;
```

Use `registry-delete --drop_data` when you want registry + schema data removed together.

## 6) Notes on Performance and Correctness

- Compound lifecycle defaults to incremental re-index path (no fallback to full rebuild).
- Incremental compound updates use constant-scoped re-indexing and pair merge.
- Recent fix ensures transformed constants from temp shard pairs are mapped/inserted,
  so valid new pairs are not dropped during merge.

## 7) Existing / Missing Data Handling

### 7.1 Compound batch contains molecules already in this schema

- No error.
- Batch file is still recorded in `leadopt_compound_batches` / `leadopt_compound_batch_rows`.
- Structural re-index only uses `new_unique_rows` (truly new clean SMILES).
- Existing molecules are treated as no structural delta (pair graph unchanged by design).

### 7.2 Property batch updates properties that already exist

- No error.
- For the same `(clean_smiles, property_name)`, latest batch wins.
- If one file contains duplicate `(smiles, property)` rows, last row wins.
- Deleting one property batch rolls touched keys back to:
  - the latest remaining batch value, or
  - baseline snapshot (`leadopt_property_base`) if no later batch remains.

### 7.3 Property batch contains molecules not in this schema

- No error.
- These rows are counted as `unmatched_rows` and skipped from effective write.
- They are not query-active until those molecules exist in `compound`.
- Correct lifecycle strategy:
  1. import compounds first (or add missing compounds incrementally)
  2. re-import property batch for those molecules
