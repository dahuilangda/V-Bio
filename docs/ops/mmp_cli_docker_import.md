# MMP Docker CLI Import Guide

This guide uses the current V-Bio layout and Docker deployment to import new MMP databases from command line.

## 1. Start MMP PostgreSQL container

```bash
cd /data/V-Bio/deploy/docker

# First time only:
cp -n DOCKER_CAP_MMP_POSTGRES.env.example DOCKER_CAP_MMP_POSTGRES.env

docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file DOCKER_CAP_MMP_POSTGRES.env \
  up -d --build

docker ps | grep leadopt_mmp_db
```

Default DB DSN used below:

```text
postgresql://leadopt:leadopt@172.17.3.200:54330/leadopt_mmp
```

## 2. Run MMP lifecycle CLI via `docker run` (one-shot)

Use one-shot `docker run` commands (no interactive shell required), with local folder mounted:

```bash
cd /data/V-Bio

# Ensure runtime image exists (first time / after Dockerfile changes)
docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env \
  build cpu-worker

export RUNTIME_IMAGE=vbio-worker-cpu-cpu-worker:latest
export PROJECT_ROOT=/data/V-Bio
export MMP_DSN='postgresql://leadopt:leadopt@172.17.3.200:54330/leadopt_mmp'
```

CLI check:

```bash
docker run --rm --network host \
  -v ${PROJECT_ROOT}:${PROJECT_ROOT} \
  -w ${PROJECT_ROOT} \
  ${RUNTIME_IMAGE} \
  python -m capabilities.lead_optimization.mmp_lifecycle --help
```

## 3. Example A: import `chembl_cyp3a4_herg`

Full build from existing structures + properties + metadata files:

```bash
docker run --rm --network host \
  -v ${PROJECT_ROOT}:${PROJECT_ROOT} \
  -w ${PROJECT_ROOT} \
  ${RUNTIME_IMAGE} \
  python -m capabilities.lead_optimization.mmp_lifecycle db-build \
  --smiles_file capabilities/lead_optimization/data/ChEMBL_CYP3A4_hERG_structures.smi \
  --properties_file capabilities/lead_optimization/data/ChEMBL_CYP3A4_hERG_props.txt \
  --property_metadata_file capabilities/lead_optimization/data/ChEMBL_CYP3A4_hERG_metadata.csv \
  --postgres_url "${MMP_DSN}" \
  --postgres_schema chembl_cyp3a4_herg \
  --output_dir capabilities/lead_optimization/data \
  --max_heavy_atoms 60 \
  --fragment_jobs 32 \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

Verify:

```bash
docker run --rm --network host \
  -v ${PROJECT_ROOT}:${PROJECT_ROOT} \
  -w ${PROJECT_ROOT} \
  ${RUNTIME_IMAGE} \
  python -m capabilities.lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url "${MMP_DSN}" \
  --postgres_schema chembl_cyp3a4_herg
```

## 4. Example B: import `chembl36_full`

Fast path from existing large `.fragdb` cache:

```bash
docker run --rm --network host \
  -v ${PROJECT_ROOT}:${PROJECT_ROOT} \
  -w ${PROJECT_ROOT} \
  ${RUNTIME_IMAGE} \
  python -m capabilities.lead_optimization.mmp_lifecycle db-index-fragdb \
  --fragments_file capabilities/lead_optimization/data/chembl_compounds.fragdb \
  --postgres_url "${MMP_DSN}" \
  --postgres_schema chembl36_full \
  --pg_index_maintenance_work_mem_mb 65536 \
  --pg_index_work_mem_mb 512 \
  --pg_index_parallel_workers 16 \
  --attachment_force_recompute \
  --force
```

Verify:

```bash
docker run --rm --network host \
  -v ${PROJECT_ROOT}:${PROJECT_ROOT} \
  -w ${PROJECT_ROOT} \
  ${RUNTIME_IMAGE} \
  python -m capabilities.lead_optimization.mmp_lifecycle verify-schema \
  --postgres_url "${MMP_DSN}" \
  --postgres_schema chembl36_full
```

## 5. Switch runtime default schema

Edit stack env files (for example `deploy/docker/DOCKER_STACK_CENTRAL.env` and `deploy/docker/DOCKER_STACK_WORKER_CPU.env`):

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@172.17.3.200:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

or:

```env
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@172.17.3.200:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl36_full
```

Then restart central API + CPU worker containers.

## 6. Quick API check

```bash
curl -H "X-API-Token: <YOUR_TOKEN>" \
  http://172.17.3.200:5000/api/lead_optimization/mmp_databases
```

## Notes

- `--force` rebuilds only the target schema and does not drop other schemas.
- Use `db-index-fragdb` for large existing fragment caches to reduce import time.
- All commands above are explicit lifecycle commands; no fallback path is used.
