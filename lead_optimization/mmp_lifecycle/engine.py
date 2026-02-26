#!/usr/bin/env python3
# /data/Boltz-WebUI/lead_optimization/mmp_lifecycle/engine.py

"""
Setup script for initializing mmpdb database for lead optimization
Enhanced with ChEMBL data download capabilities
"""

import argparse
import csv
import glob
import json
import math
import os
import sys
import logging
import subprocess
import shutil
import re
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors
    import requests
    import gzip
    HAS_RDKIT_MMPDB = True
except ImportError:
    HAS_RDKIT_MMPDB = False
    Chem = None
    Descriptors = None
    rdMolDescriptors = None
    requests = None
    gzip = None

try:
    import psycopg
    from psycopg.sql import Identifier, SQL
    HAS_PSYCOPG = True
except ImportError:
    psycopg = None
    Identifier = None
    SQL = None
    HAS_PSYCOPG = False

try:
    import psycopg2  # Required by mmpdblib/peewee PostgreSQL writer.
    HAS_PSYCOPG2 = True
except ImportError:
    psycopg2 = None
    HAS_PSYCOPG2 = False


PG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MMPDB_CORE_TABLE_NAMES = (
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
PROPERTY_BATCH_BASE_TABLE = "leadopt_property_base"
PROPERTY_BATCH_HEADER_TABLE = "leadopt_property_batches"
PROPERTY_BATCH_ROWS_TABLE = "leadopt_property_batch_rows"
PROPERTY_BATCH_MISSING_TOKENS = {"", "*", "na", "n/a", "nan", "null", "none", "-"}
COMPOUND_BATCH_BASE_TABLE = "leadopt_compound_base"
COMPOUND_BATCH_HEADER_TABLE = "leadopt_compound_batches"
COMPOUND_BATCH_ROWS_TABLE = "leadopt_compound_batch_rows"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "t", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _registry_begin_status(postgres_url: str, schema: str, operation: str) -> str:
    try:
        from lead_optimization import mmp_database_registry as registry
    except Exception:
        return ""
    try:
        return registry.begin_mmp_database_operation(
            database_url=str(postgres_url or "").strip(),
            schema=str(schema or "").strip(),
            message=str(operation or "").strip() or "operation",
        )
    except Exception:
        return ""


def _registry_finish_status(
    postgres_url: str,
    schema: str,
    *,
    token: str,
    success: bool,
    message: str = "",
) -> None:
    try:
        from lead_optimization import mmp_database_registry as registry
    except Exception:
        return
    try:
        registry.finish_mmp_database_operation(
            database_url=str(postgres_url or "").strip(),
            schema=str(schema or "").strip(),
            token=str(token or "").strip(),
            success=bool(success),
            message=str(message or "").strip(),
        )
    except Exception:
        return


def _run_with_registry_status(postgres_url: str, schema: str, operation: str, fn):
    token = _registry_begin_status(postgres_url, schema, operation)
    ok = False
    error_text = ""
    try:
        ok = bool(fn())
        return ok
    except Exception as exc:
        error_text = str(exc or "")
        raise
    finally:
        _registry_finish_status(
            postgres_url,
            schema,
            token=token,
            success=ok,
            message="" if ok else (error_text or f"{operation} failed"),
        )


def _guard_large_index_auto_build(*, pair_rows_est: int, missing_indexes: Sequence[str], guard_label: str) -> None:
    if not missing_indexes:
        return
    allow_large_auto_build = _env_bool("MMP_LIFECYCLE_ALLOW_LARGE_INDEX_AUTO_BUILD", default=False)
    large_pair_threshold = max(1, _env_int("MMP_LIFECYCLE_LARGE_PAIR_THRESHOLD", 100_000_000))
    if pair_rows_est >= large_pair_threshold and not allow_large_auto_build:
        missing_text = ", ".join(sorted({str(i) for i in missing_indexes if str(i)}))
        raise RuntimeError(
            f"{guard_label}: missing lookup indexes on large pair table (rows~{pair_rows_est}): {missing_text}. "
            "Set MMP_LIFECYCLE_ALLOW_LARGE_INDEX_AUTO_BUILD=1 for one-time online build, "
            "or run offline index maintenance first."
        )


def _current_schema_index_names(cursor) -> set[str]:
    cursor.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = current_schema()
        """
    )
    return {str(row[0] or "").strip() for row in cursor.fetchall() if str(row[0] or "").strip()}


def _estimate_current_schema_table_rows(cursor, table_name: str) -> int:
    token = str(table_name or "").strip()
    if not token:
        return 0
    cursor.execute(
        """
        SELECT COALESCE(c.reltuples, 0)::BIGINT
        FROM pg_class c
        WHERE c.oid = to_regclass(%s)
        """,
        [token],
    )
    row = cursor.fetchone()
    return max(0, int((row or [0])[0] or 0))


def _ensure_named_indexes(cursor, named_statements: Sequence[Tuple[str, str]], *, existing_indexes: Optional[set[str]] = None) -> set[str]:
    names = set(existing_indexes or set())
    if not names:
        names = _current_schema_index_names(cursor)
    for index_name, statement in named_statements:
        token = str(index_name or "").strip()
        if not token or token in names:
            continue
        cursor.execute(statement)
        names.add(token)
    return names


def _copy_rows(cursor, copy_sql: str, rows: Sequence[Tuple[object, ...]]) -> None:
    if not rows:
        return
    with cursor.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row(row)


def _is_postgres_dsn(value: str) -> bool:
    token = str(value or "").strip().lower()
    return token.startswith("postgres://") or token.startswith("postgresql://")

def setup_logging():
    """Setup logging"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def download_chembl_data(output_dir: str, subset_size: Optional[int] = None,
                        min_mw: float = 100, max_mw: float = 800) -> Optional[str]:
    """Download ChEMBL compound data and create SMILES file"""
    logger = logging.getLogger(__name__)
    
    # ChEMBL download URLs (prefer explicit release first)
    chembl_urls = {
        'compounds': 'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_36/chembl_36_chemreps.txt.gz',
        'activities': 'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_36/chembl_36_activities.txt.gz'
    }
    
    # Alternative URLs if main site is down
    alternative_urls = {
        'compounds': [
            'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/chembl_36_chemreps.txt.gz',
            'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_35/chembl_35_chemreps.txt.gz',
            'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_34/chembl_34_chemreps.txt.gz'
        ]
    }
    
    smiles_file = os.path.join(output_dir, "chembl_compounds.smi")
    chembl_raw_file = os.path.join(output_dir, "chembl_raw_data.txt")
    chembl_gz_file = os.path.join(output_dir, "chembl_raw_data.txt.gz")
    
    # Check if files already exist
    if os.path.exists(smiles_file):
        logger.info(f"SMILES file already exists: {smiles_file}")
        return smiles_file
    
    if os.path.exists(chembl_raw_file):
        logger.info(f"Using existing ChEMBL raw data: {chembl_raw_file}")
    else:
        # Check if we have compressed file
        if os.path.exists(chembl_gz_file):
            logger.info(f"Extracting existing compressed ChEMBL data: {chembl_gz_file}")
            with gzip.open(chembl_gz_file, 'rb') as f_in:
                with open(chembl_raw_file, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
        else:
            # Need to download the data
            try:
                # Download ChEMBL compounds file
                logger.info("Downloading ChEMBL compound representations...")
                logger.info("Preferred dataset release: ChEMBL 36")
                logger.info("This may take several minutes depending on your connection speed...")
                
                compounds_url = chembl_urls['compounds']
                response = None
                
                # Try main URL first, then alternatives
                urls_to_try = [compounds_url] + alternative_urls.get('compounds', [])
                
                for i, url in enumerate(urls_to_try):
                    try:
                        url_type = "primary" if i == 0 else f"alternative_{i}"
                        logger.info(f"Trying {url_type} URL: {url}")
                        response = requests.get(url, stream=True, timeout=600)
                        response.raise_for_status()
                        break
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"Failed to download from {url_type} URL: {e}")
                        if i < len(urls_to_try) - 1:
                            continue
                        else:
                            raise Exception("All download URLs failed")
                
                if not response:
                    raise Exception("All download URLs failed")
                
                # Save compressed data first (to keep as backup)
                logger.info("Saving compressed data...")
                with open(chembl_gz_file, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                # Extract the data
                logger.info("Extracting compressed data...")
                with gzip.open(chembl_gz_file, 'rb') as f_in:
                    with open(chembl_raw_file, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                        
            except Exception as e:
                logger.error(f"Failed to download ChEMBL data: {e}")
                return None
    
    try:
        # Stream processing to avoid loading full ChEMBL file in memory.
        logger.info("Processing ChEMBL data with streaming parser...")

        with open(chembl_raw_file, 'r', encoding='utf-8') as src, open(smiles_file, 'w', encoding='utf-8') as dst:
            header_line = src.readline()
            if not header_line:
                logger.error("ChEMBL raw file is empty: %s", chembl_raw_file)
                return None
            header = header_line.strip().split('\t')

            try:
                chembl_id_idx = header.index('chembl_id')
                smiles_idx = header.index('canonical_smiles')
                mw_idx = None
                for mw_col in ['mw_freebase', 'full_mwt', 'molecular_weight']:
                    if mw_col in header:
                        mw_idx = header.index(mw_col)
                        break
                if mw_idx is None:
                    logger.warning("No molecular weight column found, using RDKit calculation")
            except ValueError as e:
                logger.error(f"Required column not found in ChEMBL data: {e}")
                logger.info(f"Available columns: {header[:10]}...")
                return None

            processed_count = 0
            valid_count = 0
            skipped_count = 0
            dst.write("SMILES\tID\n")

            for line in src:
                processed_count += 1
                if processed_count % 10000 == 0:
                    logger.info("Processed %s compounds, kept %s", processed_count, valid_count)

                if subset_size and valid_count >= subset_size:
                    logger.info("Reached target subset size of %s", subset_size)
                    break

                try:
                    parts = line.strip().split('\t')
                    if len(parts) <= max(chembl_id_idx, smiles_idx):
                        skipped_count += 1
                        continue

                    chembl_id = parts[chembl_id_idx].strip()
                    smiles = parts[smiles_idx].strip()
                    if not chembl_id or not smiles or smiles == 'NULL':
                        skipped_count += 1
                        continue

                    mol = Chem.MolFromSmiles(smiles)
                    if not mol:
                        skipped_count += 1
                        continue

                    if mw_idx is not None and len(parts) > mw_idx:
                        try:
                            mw = float(parts[mw_idx].strip())
                        except (ValueError, TypeError):
                            mw = Descriptors.MolWt(mol)
                    else:
                        mw = Descriptors.MolWt(mol)

                    if not (min_mw <= mw <= max_mw):
                        skipped_count += 1
                        continue
                    if not is_drug_like(mol):
                        skipped_count += 1
                        continue

                    dst.write(f"{smiles}\t{chembl_id}\n")
                    valid_count += 1
                except Exception:
                    skipped_count += 1
                    continue

        logger.info("ChEMBL data processing completed:")
        logger.info("  - Total processed: %s", processed_count)
        logger.info("  - Valid compounds: %s", valid_count)
        logger.info("  - Skipped: %s", skipped_count)
        if processed_count > 0:
            logger.info("  - Filter efficiency: %.1f%%", (valid_count / processed_count) * 100.0)
        logger.info("  - Output file: %s", smiles_file)

        if valid_count == 0:
            logger.error("No valid compounds found in ChEMBL data")
            return None

        return smiles_file
        
    except Exception as e:
        logger.error(f"Failed to download ChEMBL data: {e}")
        return None

def is_drug_like(mol) -> bool:
    """Apply basic drug-likeness filters (Lipinski's rule of five)"""
    try:
        mw = Descriptors.MolWt(mol)
        logp = Descriptors.MolLogP(mol)
        hbd = rdMolDescriptors.CalcNumHBD(mol)
        hba = rdMolDescriptors.CalcNumHBA(mol)
        
        # Lipinski's rule of five
        if mw > 500:
            return False
        if logp > 5:
            return False
        if hbd > 5:
            return False
        if hba > 10:
            return False
        
        # Additional filters for drug-likeness
        rotatable_bonds = rdMolDescriptors.CalcNumRotatableBonds(mol)
        if rotatable_bonds > 15:
            return False
        
        tpsa = rdMolDescriptors.CalcTPSA(mol)
        if tpsa > 200:
            return False
        
        # Filter out very simple molecules
        if mol.GetNumAtoms() < 5:
            return False
        
        # Filter out molecules with too many aromatic rings
        aromatic_rings = rdMolDescriptors.CalcNumAromaticRings(mol)
        if aromatic_rings > 5:
            return False
        
        return True
        
    except Exception:
        return True  # If calculation fails, assume it's OK

def validate_smiles_file(smiles_file: str) -> bool:
    """Validate SMILES file format and content"""
    logger = logging.getLogger(__name__)
    
    if not os.path.exists(smiles_file):
        logger.error(f"SMILES file not found: {smiles_file}")
        return False
    
    valid_count = 0
    total_count = 0
    
    try:
        with open(smiles_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                # Parse line (SMILES + optional ID)
                parts = line.split('\t')
                if len(parts) < 1:
                    logger.warning(f"Line {line_num}: Invalid format")
                    continue

                smiles = parts[0].strip()
                if line_num == 1 and smiles.upper() == "SMILES":
                    # Header row (e.g. "SMILES<TAB>ID"), skip validation.
                    continue

                total_count += 1
                
                # Validate SMILES
                if HAS_RDKIT_MMPDB:
                    mol = Chem.MolFromSmiles(smiles)
                    if mol:
                        valid_count += 1
                    else:
                        logger.warning(f"Line {line_num}: Invalid SMILES: {smiles}")
                else:
                    valid_count += 1  # Assume valid if RDKit not available
        
        logger.info(f"SMILES file validation: {valid_count}/{total_count} valid compounds")
        
        if valid_count == 0:
            logger.error("No valid compounds found")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error reading SMILES file: {e}")
        return False


def _ensure_postgres_schema_exists(postgres_url: str, schema: str) -> None:
    if not HAS_PSYCOPG:
        return
    normalized_schema = _validate_pg_schema(schema)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(normalized_schema))
            )


def _run_mmpdb_index_with_schema_safe_patch(
    index_args: List[str],
    env: dict[str, str],
    *,
    skip_post_index_finalize: bool = False,
    skip_post_index_analyze_only: bool = False,
    commit_every_flushes: int = 0,
) -> subprocess.CompletedProcess:
    patch_runner_code = r"""
import sys
import os
from mmpdblib import cli, index_writers, schema as mmp_schema, index_algorithm

_SKIP_FINALIZE = __SKIP_FINALIZE__
_SKIP_ANALYZE = __SKIP_ANALYZE__
_COMMIT_EVERY_FLUSHES = max(0, int(__COMMIT_EVERY_FLUSHES__))
_DELTA_PAIR_IDS_FILE = os.environ.get("LEADOPT_MMP_DELTA_PAIR_IDS_FILE", "").strip()
_DELTA_PAIR_IDS = set()
if _DELTA_PAIR_IDS_FILE:
    try:
        with open(_DELTA_PAIR_IDS_FILE, "r", encoding="utf-8") as _handle:
            for _line in _handle:
                _token = _line.strip()
                if _token:
                    _DELTA_PAIR_IDS.add(_token)
    except Exception:
        _DELTA_PAIR_IDS = set()

def _q_ident(value):
    return '"' + str(value).replace('"', '""') + '"'

def _create_postgres_bigint_schema(db):
    schema_sql = mmp_schema.get_schema_for_database(mmp_schema.PostgresConfig)
    # mmpdblib default Postgres schema uses SERIAL/INTEGER (int4), which can overflow
    # on large datasets (for example full ChEMBL pair table). Promote to int8 at create time.
    schema_sql = schema_sql.replace("SERIAL PRIMARY KEY", "BIGSERIAL PRIMARY KEY")
    schema_sql = schema_sql.replace(" INTEGER", " BIGINT")
    c = db.cursor()
    mmp_schema._execute_sql(c, schema_sql)

def _patched_create_schema(self):
    c = self.conn
    c.execute("SELECT current_schema()")
    row = c.fetchone()
    current_schema = (row[0] if row and row[0] else "public")
    c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (current_schema,),
    )
    table_names = set(r[0] for r in c)
    existing = [t for t in mmp_schema.SCHEMA_TABLE_NAMES if t in table_names]
    for table_name in existing:
        c.execute(
            "DROP TABLE {}.{} CASCADE".format(
                _q_ident(current_schema),
                _q_ident(table_name),
            )
        )
    _create_postgres_bigint_schema(self.db)

def _patched_end_skip_finalize(self, reporter):
    self.flush()
    reporter.update("")

def _patched_end_skip_analyze(self, reporter):
    self.flush()
    reporter.update("Building index ...")
    mmp_schema.create_index(self.conn)
    reporter.update("Computing sizes ...")
    sql = self.prepare_single(index_writers.UPDATE_DATASET_SQL)
    self.conn.execute(sql)
    sql = self.prepare_single(index_writers.UPDATE_RULE_ENV_NUM_PAIRS)
    self.conn.execute(sql)
    reporter.update("")

index_writers.PostgresIndexWriter.create_schema = _patched_create_schema

_orig_pg_start = index_writers.PostgresIndexWriter.start
def _patched_pg_start(self, fragment_options, index_options):
    self._leadopt_flush_commit_counter = 0
    return _orig_pg_start(self, fragment_options, index_options)

_orig_pg_flush = index_writers.PostgresIndexWriter.flush
def _patched_pg_flush(self):
    _orig_pg_flush(self)
    if _COMMIT_EVERY_FLUSHES <= 0:
        return
    self._leadopt_flush_commit_counter = int(getattr(self, "_leadopt_flush_commit_counter", 0)) + 1
    if self._leadopt_flush_commit_counter % _COMMIT_EVERY_FLUSHES != 0:
        return
    # Reset PostgreSQL transaction command counter periodically to avoid:
    # "cannot have more than 2^32-2 commands in a transaction"
    self.conn.execute("COMMIT")
    self.conn.execute("BEGIN TRANSACTION")

index_writers.PostgresIndexWriter.start = _patched_pg_start
index_writers.PostgresIndexWriter.flush = _patched_pg_flush

if _SKIP_FINALIZE:
    index_writers.PostgresIndexWriter.end = _patched_end_skip_finalize
elif _SKIP_ANALYZE:
    index_writers.PostgresIndexWriter.end = _patched_end_skip_analyze
if _DELTA_PAIR_IDS:
    _orig_find_mmp = index_algorithm.find_matched_molecular_pairs
    def _patched_find_matched_molecular_pairs(index, *args, **kwargs):
        _orig_iter_constant_matches = getattr(index, "iter_constant_matches", None)
        if _orig_iter_constant_matches is None:
            for _pair in _orig_find_mmp(index, *args, **kwargs):
                if (_pair.id1 in _DELTA_PAIR_IDS) or (_pair.id2 in _DELTA_PAIR_IDS):
                    yield _pair
            return

        def _filtered_iter_constant_matches():
            for _num_cuts, _constant_smiles, _constant_symmetry_class, _matches in _orig_iter_constant_matches():
                _delta_positions = [
                    _idx for _idx, _entry in enumerate(_matches)
                    if _entry[0] in _DELTA_PAIR_IDS
                ]
                if not _delta_positions:
                    continue
                _delta_set = set(_delta_positions)
                _n = len(_matches)

                for _i in _delta_positions:
                    _entry_i = _matches[_i]
                    for _j in range(_n):
                        if _j == _i:
                            continue
                        if _j in _delta_set and _j < _i:
                            continue
                        if _j in _delta_set:
                            # Keep delta-delta once (i<j), and skip i==j above.
                            _entry_j = _matches[_j]
                            yield (_num_cuts, _constant_smiles, _constant_symmetry_class, [_entry_i, _entry_j])
                        else:
                            _entry_j = _matches[_j]
                            yield (_num_cuts, _constant_smiles, _constant_symmetry_class, [_entry_i, _entry_j])

        index.iter_constant_matches = _filtered_iter_constant_matches
        try:
            for _pair in _orig_find_mmp(index, *args, **kwargs):
                if (_pair.id1 in _DELTA_PAIR_IDS) or (_pair.id2 in _DELTA_PAIR_IDS):
                    yield _pair
        finally:
            index.iter_constant_matches = _orig_iter_constant_matches
    index_algorithm.find_matched_molecular_pairs = _patched_find_matched_molecular_pairs
sys.argv = ["mmpdb"] + sys.argv[1:]
cli.main()
"""
    patch_runner_code = patch_runner_code.replace(
        "__SKIP_FINALIZE__",
        "1" if skip_post_index_finalize else "0",
    ).replace(
        "__SKIP_ANALYZE__",
        "1" if skip_post_index_analyze_only else "0",
    ).replace(
        "__COMMIT_EVERY_FLUSHES__",
        str(max(0, int(commit_every_flushes or 0))),
    )
    cmd = [sys.executable, "-c", patch_runner_code, *index_args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _summarize_core_tables(rows: List[tuple[str, str]]) -> str:
    by_schema: dict[str, List[str]] = {}
    for table_schema, table_name in rows:
        by_schema.setdefault(table_schema, []).append(table_name)
    return "; ".join(
        f"{table_schema}: {', '.join(sorted(set(table_names)))}"
        for table_schema, table_names in sorted(by_schema.items())
    )


def _resolve_index_commit_every_flushes(
    requested_flushes: int,
    *,
    fragments_file: str = "",
) -> tuple[int, bool]:
    explicit = int(requested_flushes or 0)
    if explicit > 0:
        return explicit, False
    size_bytes = 0
    try:
        token = str(fragments_file or "").strip()
        if token and os.path.exists(token):
            size_bytes = int(os.path.getsize(token) or 0)
    except Exception:
        size_bytes = 0
    size_gib = max(0.0, float(size_bytes) / float(1024 ** 3))
    # Larger fragdb should commit *more frequently* to keep transactions/WAL bounded.
    if size_gib >= 8.0:
        auto_flushes = 1
    elif size_gib >= 2.0:
        auto_flushes = 2
    elif size_gib >= 0.5:
        auto_flushes = 4
    else:
        auto_flushes = 8
    return auto_flushes, True


def _normalize_index_runtime_tuning(
    *,
    maintenance_work_mem_mb: int,
    work_mem_mb: int,
    parallel_workers: int,
) -> tuple[int, int, int]:
    cpu_cap = max(1, int(os.cpu_count() or 8))
    max_maintenance_mb = max(128, _env_int("MMP_LIFECYCLE_MAX_INDEX_MAINTENANCE_WORK_MEM_MB", 8192))
    max_work_mb = max(16, _env_int("MMP_LIFECYCLE_MAX_INDEX_WORK_MEM_MB", 512))
    max_parallel = max(1, _env_int("MMP_LIFECYCLE_MAX_INDEX_PARALLEL_WORKERS", min(8, cpu_cap)))
    norm_maintenance = max(0, min(int(maintenance_work_mem_mb or 0), max_maintenance_mb))
    norm_work = max(0, min(int(work_mem_mb or 0), max_work_mb))
    norm_parallel = max(0, min(int(parallel_workers or 0), max_parallel))
    return norm_maintenance, norm_work, norm_parallel


def _index_fragments_to_postgres(
    fragments_file: str,
    postgres_url: str,
    *,
    postgres_schema: str = "public",
    force_rebuild_schema: bool = False,
    properties_file: Optional[str] = None,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    index_maintenance_work_mem_mb: int = 0,
    index_work_mem_mb: int = 0,
    index_parallel_workers: int = 0,
    index_commit_every_flushes: int = 0,
    skip_mmpdb_post_index_finalize: bool = False,
    skip_mmpdb_list_verify: bool = False,
    delta_pair_record_ids: Optional[Sequence[str]] = None,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    detect_existing_core_tables: bool = True,
) -> bool:
    """Index an existing .fragdb file into PostgreSQL and run finalize steps."""
    logger = logging.getLogger(__name__)
    normalized_schema = _validate_pg_schema(postgres_schema)
    try:
        if not _is_postgres_dsn(str(postgres_url or "").strip()):
            logger.error("PostgreSQL DSN is required. Received: %s", postgres_url)
            return False

        _ensure_postgres_schema_exists(postgres_url, normalized_schema)
        if detect_existing_core_tables:
            existing_core_tables = _find_mmpdb_core_tables(postgres_url)
            if existing_core_tables:
                logger.warning(
                    "Detected existing mmpdb core tables in database: %s",
                    _summarize_core_tables(existing_core_tables),
                )
                logger.warning(
                    "Applying schema-safe mmpdb index patch; only current schema '%s' tables will be replaced.",
                    normalized_schema,
                )
        current_schema_tables = _find_mmpdb_core_tables_in_schema(postgres_url, normalized_schema)
        if current_schema_tables and not force_rebuild_schema:
            logger.error(
                "Target schema '%s' already contains mmpdb core tables: %s. "
                "Use --force to rebuild this schema, or choose another --postgres_schema.",
                normalized_schema,
                ", ".join(current_schema_tables),
            )
            return False
        if current_schema_tables and force_rebuild_schema:
            logger.warning(
                "Target schema '%s' contains existing core tables and will be rebuilt due to --force: %s",
                normalized_schema,
                ", ".join(current_schema_tables),
            )

        # Check fragments file
        if not os.path.exists(fragments_file):
            logger.error("Fragments file not found: %s", fragments_file)
            return False

        # Step 2: Create index directly in PostgreSQL
        logger.info("Step 2: Creating MMP database index in PostgreSQL (this may take a while)...")
        index_args = [
            "index",
            fragments_file,
            "-o", postgres_url
        ]
        if properties_file:
            index_args.extend(["--properties", properties_file])

        # Let mmpdb index session inherit memory/parallel/search_path tuning through libpq.
        tuned_maintenance_mb, tuned_work_mb, tuned_parallel_workers = _normalize_index_runtime_tuning(
            maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            work_mem_mb=index_work_mem_mb,
            parallel_workers=index_parallel_workers,
        )
        if tuned_maintenance_mb != int(index_maintenance_work_mem_mb or 0):
            logger.warning(
                "Clamped maintenance_work_mem for mmpdb index: requested=%sMB effective=%sMB",
                int(index_maintenance_work_mem_mb or 0),
                tuned_maintenance_mb,
            )
        if tuned_work_mb != int(index_work_mem_mb or 0):
            logger.warning(
                "Clamped work_mem for mmpdb index: requested=%sMB effective=%sMB",
                int(index_work_mem_mb or 0),
                tuned_work_mb,
            )
        if tuned_parallel_workers != int(index_parallel_workers or 0):
            logger.warning(
                "Clamped max_parallel_maintenance_workers for mmpdb index: requested=%s effective=%s",
                int(index_parallel_workers or 0),
                tuned_parallel_workers,
            )
        index_env = os.environ.copy()
        index_pgoptions: List[str] = [f"-c search_path={normalized_schema},public"]
        if tuned_maintenance_mb > 0:
            index_pgoptions.append(f"-c maintenance_work_mem={tuned_maintenance_mb}MB")
        if tuned_work_mb > 0:
            index_pgoptions.append(f"-c work_mem={tuned_work_mb}MB")
        if tuned_parallel_workers > 0:
            index_pgoptions.append(f"-c max_parallel_maintenance_workers={tuned_parallel_workers}")
        if index_pgoptions:
            existing_pgoptions = str(index_env.get("PGOPTIONS", "") or "").strip()
            merged_pgoptions = " ".join(index_pgoptions)
            index_env["PGOPTIONS"] = (
                f"{existing_pgoptions} {merged_pgoptions}".strip()
                if existing_pgoptions else merged_pgoptions
            )
            logger.info("PGOPTIONS for mmpdb index: %s", index_env["PGOPTIONS"])

        logger.info(
            "Running mmpdb index with schema-safe patch: %s",
            " ".join([sys.executable, "-m", "mmpdblib", *index_args]),
        )
        delta_id_file = ""
        delta_ids = sorted({str(item or "").strip() for item in (delta_pair_record_ids or []) if str(item or "").strip()})
        if delta_ids:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", prefix="mmpdb_delta_ids_", suffix=".txt") as handle:
                for token in delta_ids:
                    handle.write(token + "\n")
                delta_id_file = handle.name
            index_env["LEADOPT_MMP_DELTA_PAIR_IDS_FILE"] = delta_id_file
            logger.info("Applying delta-only pair filter for mmpdb index: delta_ids=%s", len(delta_ids))
        try:
            safe_commit_every_flushes, is_auto_commit_flushes = _resolve_index_commit_every_flushes(
                index_commit_every_flushes,
                fragments_file=fragments_file,
            )
            logger.info(
                "mmpdb index commit cadence: every %s flush(es)%s",
                safe_commit_every_flushes,
                " [auto]" if is_auto_commit_flushes else "",
            )
            result = _run_mmpdb_index_with_schema_safe_patch(
                index_args,
                index_env,
                skip_post_index_finalize=skip_mmpdb_post_index_finalize,
                commit_every_flushes=safe_commit_every_flushes,
            )
        finally:
            if delta_id_file:
                index_env.pop("LEADOPT_MMP_DELTA_PAIR_IDS_FILE", None)
                try:
                    os.remove(delta_id_file)
                except Exception:
                    pass

        if result.returncode != 0:
            logger.error(f"Index creation failed: {result.stderr}")
            logger.error(f"Index stdout: {result.stdout}")
            stderr_text = str(result.stderr or "")
            if "server closed the connection unexpectedly" in stderr_text.lower():
                logger.error(
                    "mmpdb index lost PostgreSQL connection unexpectedly. "
                    "Likely backend restart/termination under heavy write pressure. "
                    "Try lower pg_index_* settings, keep concurrent batch jobs minimal, "
                    "and increase PostgreSQL WAL capacity (max_wal_size)."
                )
            logger.info(f"Fragments file kept for debugging: {fragments_file}")
            return False

        logger.info("PostgreSQL indexing completed successfully")
        if result.stdout:
            logger.info(f"Index output: {result.stdout[-500:]}")  # Last 500 chars

        if not skip_mmpdb_list_verify:
            list_cmd = [sys.executable, "-m", "mmpdblib", "list", postgres_url]
            list_result = subprocess.run(list_cmd, capture_output=True, text=True, env=index_env)
            if list_result.returncode != 0:
                logger.error("PostgreSQL database verification failed: %s", list_result.stderr)
                return False
            logger.info("PostgreSQL dataset summary:\n%s", list_result.stdout.strip())

        if not skip_attachment_enrichment:
            enrich_ok = finalize_postgres_database(
                postgres_url,
                schema=normalized_schema,
                force_recompute_num_frags=attachment_force_recompute,
                index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
                index_work_mem_mb=index_work_mem_mb,
                index_parallel_workers=index_parallel_workers,
                build_construct_tables=build_construct_tables,
                build_constant_smiles_mol_index=build_constant_smiles_mol_index,
            )
            if not enrich_ok:
                logger.warning(
                    "Attachment/index enrichment failed; base MMP data exists but "
                    "lead-opt query quality/performance may degrade."
                )
                return False

        return True
    except Exception as e:
        logger.error(f"Database creation failed: {e}")
        return False


def create_mmp_database(
    smiles_file: str,
    postgres_url: str,
    max_heavy_atoms: int = 50,
    *,
    output_dir: str,
    fragment_cache_file: Optional[str] = None,
    postgres_schema: str = "public",
    properties_file: Optional[str] = None,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    force_rebuild_schema: bool = False,
    keep_fragments: bool = False,
    fragment_jobs: int = 0,
    index_maintenance_work_mem_mb: int = 0,
    index_work_mem_mb: int = 0,
    index_parallel_workers: int = 0,
    index_commit_every_flushes: int = 0,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
) -> bool:
    """Create MMP database directly in PostgreSQL using mmpdb."""
    logger = logging.getLogger(__name__)

    try:
        if not _is_postgres_dsn(str(postgres_url or "").strip()):
            logger.error("PostgreSQL DSN is required. Received: %s", postgres_url)
            return False
        os.makedirs(output_dir, exist_ok=True)

        base_name = os.path.splitext(os.path.basename(str(smiles_file or "").strip()))[0] or "dataset"
        fragments_file = os.path.join(output_dir, f"{base_name}.fragdb")

        # Step 1: Fragment compounds
        logger.info("Step 1: Fragmenting compounds (this may take a while for large datasets)...")
        fragment_cmd = [
            sys.executable, "-m", "mmpdblib", "fragment",
            smiles_file,
            "-o", fragments_file,
            "--max-heavies", str(max_heavy_atoms),
            "--has-header"  # Our SMILES file has a header
        ]
        cache_file = str(fragment_cache_file or "").strip()
        if cache_file and os.path.exists(cache_file):
            fragment_cmd.extend(["--cache", cache_file])
            logger.info("Using fragment cache: %s", cache_file)
        if int(fragment_jobs or 0) > 0:
            fragment_cmd.extend(["-j", str(int(fragment_jobs))])

        logger.info(f"Running: {' '.join(fragment_cmd)}")
        result = subprocess.run(fragment_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Fragmentation failed: {result.stderr}")
            logger.error(f"Fragmentation stdout: {result.stdout}")
            return False

        logger.info("Fragmentation completed successfully")
        if result.stdout:
            logger.info(f"Fragment output: {result.stdout[-500:]}")  # Last 500 chars

        index_ok = _index_fragments_to_postgres(
            fragments_file,
            postgres_url,
            postgres_schema=postgres_schema,
            force_rebuild_schema=force_rebuild_schema,
            properties_file=properties_file,
            skip_attachment_enrichment=skip_attachment_enrichment,
            attachment_force_recompute=attachment_force_recompute,
            index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            index_work_mem_mb=index_work_mem_mb,
            index_parallel_workers=index_parallel_workers,
            index_commit_every_flushes=index_commit_every_flushes,
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
        )
        if not index_ok:
            logger.info("Fragments file kept for debugging: %s", fragments_file)
            return False

        if cache_file:
            try:
                cache_dir = os.path.dirname(cache_file)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                if os.path.abspath(cache_file) != os.path.abspath(fragments_file):
                    shutil.copy2(fragments_file, cache_file)
                logger.info("Updated fragment cache: %s", cache_file)
            except Exception as exc:
                logger.warning("Failed to update fragment cache %s: %s", cache_file, exc)

        if keep_fragments:
            logger.info("Fragments file preserved: %s", fragments_file)
        else:
            try:
                os.remove(fragments_file)
                logger.info("Removed fragments file: %s", fragments_file)
            except Exception as exc:
                logger.warning("Failed to remove fragments file %s: %s", fragments_file, exc)

        return True
    except Exception as e:
        logger.error(f"Database creation failed: {e}")
        return False

def _validate_pg_schema(schema: str) -> str:
    token = str(schema or "public").strip().lower()
    if not token:
        return "public"
    if not PG_IDENTIFIER_RE.match(token):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema}")
    return token


def _find_mmpdb_core_tables(postgres_url: str) -> List[tuple[str, str]]:
    if not HAS_PSYCOPG:
        return []
    placeholders = ", ".join(["%s"] * len(MMPDB_CORE_TABLE_NAMES))
    query = f"""
        SELECT table_schema, table_name
          FROM information_schema.tables
         WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
           AND table_name IN ({placeholders})
         ORDER BY table_schema, table_name
    """
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, list(MMPDB_CORE_TABLE_NAMES))
            return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]


def _find_mmpdb_core_tables_in_schema(postgres_url: str, schema: str) -> List[str]:
    if not HAS_PSYCOPG:
        return []
    normalized_schema = _validate_pg_schema(schema)
    placeholders = ", ".join(["%s"] * len(MMPDB_CORE_TABLE_NAMES))
    query = f"""
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = %s
           AND table_name IN ({placeholders})
         ORDER BY table_name
    """
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, [normalized_schema, *list(MMPDB_CORE_TABLE_NAMES)])
            return [str(row[0]) for row in cursor.fetchall()]


def _pg_set_search_path(cursor, schema: str) -> None:
    if schema == "public":
        cursor.execute("SET search_path TO public")
    else:
        cursor.execute(SQL("SET search_path TO {}, public").format(Identifier(schema)))


def _pg_column_exists(cursor, schema: str, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (schema, table_name, column_name),
    )
    return cursor.fetchone() is not None


def _pg_align_id_sequence(cursor, table_name: str) -> None:
    token = str(table_name or "").strip()
    if not token:
        return
    cursor.execute("SELECT to_regclass(%s)", [token])
    if cursor.fetchone()[0] is None:
        return
    cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [token])
    seq_name = cursor.fetchone()[0]
    if not seq_name:
        return
    cursor.execute(f"SELECT id FROM {token} ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    max_id = int((row or [0])[0] or 0)
    if max_id > 0:
        cursor.execute("SELECT setval(%s, %s, true)", [seq_name, max_id])
    else:
        cursor.execute("SELECT setval(%s, 1, false)", [seq_name])


def _ensure_postgres_attachment_columns(cursor, schema: str) -> None:
    """Backfill columns expected by attachment-aware workflows."""
    required_columns = [
        ("rule_smiles", "num_heavies", "INTEGER"),
        ("rule_smiles", "num_frags", "INTEGER"),
        ("constant_smiles", "num_frags", "INTEGER"),
    ]
    for table_name, column_name, column_type in required_columns:
        if not _pg_column_exists(cursor, schema, table_name, column_name):
            logging.getLogger(__name__).info(
                "PostgreSQL schema patch: adding %s.%s (%s)",
                table_name,
                column_name,
                column_type,
            )
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _assert_postgres_attachment_columns_ready(cursor, schema: str) -> None:
    required_columns = [
        ("rule_smiles", "num_heavies"),
        ("rule_smiles", "num_frags"),
        ("constant_smiles", "num_frags"),
    ]
    missing: List[str] = []
    for table_name, column_name in required_columns:
        if not _pg_column_exists(cursor, schema, table_name, column_name):
            missing.append(f"{table_name}.{column_name}")
    if missing:
        raise RuntimeError(f"PostgreSQL schema missing required attachment columns: {', '.join(missing)}")
    logging.getLogger(__name__).info(
        "PostgreSQL attachment columns verified: %s",
        ", ".join(f"{table}.{column}" for table, column in required_columns),
    )


def _create_postgres_core_indexes(
    cursor,
    schema: str,
    *,
    enable_constant_smiles_mol_index: bool = True,
) -> None:
    index_statements: List[Tuple[str, str]] = [
        ("idx_compound_public_id", "CREATE INDEX IF NOT EXISTS idx_compound_public_id ON compound(public_id)"),
        ("idx_compound_clean_smiles", "CREATE INDEX IF NOT EXISTS idx_compound_clean_smiles ON compound(clean_smiles)"),
        ("idx_constant_smiles_smiles", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles ON constant_smiles(smiles)"),
        ("idx_rule_from_to", "CREATE INDEX IF NOT EXISTS idx_rule_from_to ON rule(from_smiles_id, to_smiles_id)"),
        ("idx_rule_environment_rule_radius", "CREATE INDEX IF NOT EXISTS idx_rule_environment_rule_radius ON rule_environment(rule_id, radius)"),
        ("idx_rule_environment_lookup", "CREATE INDEX IF NOT EXISTS idx_rule_environment_lookup ON rule_environment(rule_id, environment_fingerprint_id, radius)"),
        ("idx_env_fp_lookup", "CREATE INDEX IF NOT EXISTS idx_env_fp_lookup ON environment_fingerprint(smarts, pseudosmiles, parent_smarts)"),
        ("idx_pair_rule_env", "CREATE INDEX IF NOT EXISTS idx_pair_rule_env ON pair(rule_environment_id)"),
        ("idx_pair_constant", "CREATE INDEX IF NOT EXISTS idx_pair_constant ON pair(constant_id)"),
        ("idx_pair_compound1", "CREATE INDEX IF NOT EXISTS idx_pair_compound1 ON pair(compound1_id)"),
        ("idx_pair_compound2", "CREATE INDEX IF NOT EXISTS idx_pair_compound2 ON pair(compound2_id)"),
        ("idx_pair_lookup_exact", "CREATE INDEX IF NOT EXISTS idx_pair_lookup_exact ON pair(rule_environment_id, compound1_id, compound2_id, constant_id)"),
        ("idx_prop_name_name", "CREATE INDEX IF NOT EXISTS idx_prop_name_name ON property_name(name)"),
        ("idx_compound_prop_compound_prop", "CREATE INDEX IF NOT EXISTS idx_compound_prop_compound_prop ON compound_property(compound_id, property_name_id)"),
        ("idx_rule_env_stats_prop", "CREATE INDEX IF NOT EXISTS idx_rule_env_stats_prop ON rule_environment_statistics(rule_environment_id, property_name_id)"),
    ]
    existing = _ensure_named_indexes(cursor, index_statements)

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        existing = _ensure_named_indexes(
            cursor,
            [("idx_rule_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")],
            existing_indexes=existing,
        )
    if enable_constant_smiles_mol_index and _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
        _ensure_named_indexes(
            cursor,
            [("idx_constant_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")],
            existing_indexes=existing,
        )


def _apply_postgres_build_tuning(
    cursor,
    *,
    maintenance_work_mem_mb: int = 0,
    work_mem_mb: int = 0,
    parallel_maintenance_workers: int = 0,
) -> None:
    if maintenance_work_mem_mb and maintenance_work_mem_mb > 0:
        cursor.execute(f"SET maintenance_work_mem TO '{int(maintenance_work_mem_mb)}MB'")
    if work_mem_mb and work_mem_mb > 0:
        cursor.execute(f"SET work_mem TO '{int(work_mem_mb)}MB'")
    if parallel_maintenance_workers and parallel_maintenance_workers > 0:
        cursor.execute(f"SET max_parallel_maintenance_workers TO {int(parallel_maintenance_workers)}")


def _enrich_attachment_schema_postgres(
    cursor,
    schema: str,
    force_recompute: bool = False,
    *,
    enable_constant_smiles_mol_index: bool = True,
) -> None:
    if not _pg_column_exists(cursor, schema, "rule_smiles", "num_frags"):
        cursor.execute("ALTER TABLE rule_smiles ADD COLUMN num_frags INTEGER")
    if not _pg_column_exists(cursor, schema, "constant_smiles", "num_frags"):
        cursor.execute("ALTER TABLE constant_smiles ADD COLUMN num_frags INTEGER")

    if force_recompute:
        rule_where = ""
        constant_where = ""
    else:
        rule_where = "WHERE num_frags IS NULL OR num_frags < 1 OR num_frags > 3"
        constant_where = "WHERE num_frags IS NULL OR num_frags < 0 OR num_frags > 3"

    cursor.execute(
        f"""
        UPDATE rule_smiles
        SET num_frags = CASE
            WHEN POSITION('[*:3]' IN smiles) > 0 THEN 3
            WHEN POSITION('[*:2]' IN smiles) > 0 THEN 2
            WHEN POSITION('[*:1]' IN smiles) > 0 THEN 1
            WHEN POSITION('*' IN smiles) > 0 THEN CASE
                WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 3 THEN 3
                WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 2 THEN 2
                ELSE 1
            END
            ELSE 1
        END
        {rule_where}
        """
    )
    cursor.execute(
        f"""
        UPDATE constant_smiles
        SET num_frags = CASE
            WHEN POSITION('[*:3]' IN smiles) > 0 THEN 3
            WHEN POSITION('[*:2]' IN smiles) > 0 THEN 2
            WHEN POSITION('[*:1]' IN smiles) > 0 THEN 1
            WHEN POSITION('*' IN smiles) > 0 THEN CASE
                WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 3 THEN 3
                WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 2 THEN 2
                ELSE 1
            END
            ELSE 0
        END
        {constant_where}
        """
    )

    existing_indexes = _ensure_named_indexes(
        cursor,
        [
            ("idx_rule_smiles_num_frags", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags ON rule_smiles(num_frags)"),
            ("idx_rule_smiles_num_frags_heavies", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags_heavies ON rule_smiles(num_frags, num_heavies)"),
            ("idx_constant_smiles_num_frags", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_num_frags ON constant_smiles(num_frags)"),
            ("idx_pair_rule_env_constant", "CREATE INDEX IF NOT EXISTS idx_pair_rule_env_constant ON pair(rule_environment_id, constant_id)"),
        ],
    )

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        cursor.execute("UPDATE rule_smiles SET smiles_mol = mol_from_smiles(smiles) WHERE smiles_mol IS NULL")
    else:
        cursor.execute("ALTER TABLE rule_smiles ADD COLUMN smiles_mol mol")
        cursor.execute("UPDATE rule_smiles SET smiles_mol = mol_from_smiles(smiles)")

    existing_indexes = _ensure_named_indexes(
        cursor,
        [("idx_rule_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")],
        existing_indexes=existing_indexes,
    )
    if enable_constant_smiles_mol_index:
        if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
            cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles) WHERE smiles_mol IS NULL")
        else:
            cursor.execute("ALTER TABLE constant_smiles ADD COLUMN smiles_mol mol")
            cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles)")
        _ensure_named_indexes(
            cursor,
            [("idx_constant_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")],
            existing_indexes=existing_indexes,
        )


def _enrich_attachment_schema_postgres_incremental(
    cursor,
    schema: str,
    force_recompute: bool = False,
    *,
    affected_constants: Optional[Sequence[str]] = None,
    enable_constant_smiles_mol_index: bool = True,
) -> None:
    constants = sorted({str(item or "").strip() for item in (affected_constants or []) if str(item or "").strip()})
    if not constants:
        _enrich_attachment_schema_postgres(
            cursor,
            schema,
            force_recompute=force_recompute,
            enable_constant_smiles_mol_index=enable_constant_smiles_mol_index,
        )
        return

    if not _pg_column_exists(cursor, schema, "rule_smiles", "num_frags"):
        cursor.execute("ALTER TABLE rule_smiles ADD COLUMN num_frags INTEGER")
    if not _pg_column_exists(cursor, schema, "constant_smiles", "num_frags"):
        cursor.execute("ALTER TABLE constant_smiles ADD COLUMN num_frags INTEGER")

    existing_indexes = _ensure_named_indexes(
        cursor,
        [
            ("idx_rule_smiles_num_frags", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags ON rule_smiles(num_frags)"),
            ("idx_rule_smiles_num_frags_heavies", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags_heavies ON rule_smiles(num_frags, num_heavies)"),
            ("idx_constant_smiles_num_frags", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_num_frags ON constant_smiles(num_frags)"),
            ("idx_pair_rule_env_constant", "CREATE INDEX IF NOT EXISTS idx_pair_rule_env_constant ON pair(rule_environment_id, constant_id)"),
        ],
    )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_attach_constants")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_attach_constants (
            smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    _copy_rows(
        cursor,
        "COPY tmp_inc_attach_constants (smiles) FROM STDIN",
        [(value,) for value in constants],
    )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_attach_rule_smiles")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_attach_rule_smiles AS
        SELECT DISTINCT r.from_smiles_id AS id
        FROM pair p
        INNER JOIN constant_smiles cs
                ON cs.id = p.constant_id
        INNER JOIN tmp_inc_attach_constants tc
                ON tc.smiles = cs.smiles
        INNER JOIN rule_environment re
                ON re.id = p.rule_environment_id
        INNER JOIN rule r
                ON r.id = re.rule_id
        UNION
        SELECT DISTINCT r.to_smiles_id AS id
        FROM pair p
        INNER JOIN constant_smiles cs
                ON cs.id = p.constant_id
        INNER JOIN tmp_inc_attach_constants tc
                ON tc.smiles = cs.smiles
        INNER JOIN rule_environment re
                ON re.id = p.rule_environment_id
        INNER JOIN rule r
                ON r.id = re.rule_id
        """
    )

    rule_predicate = (
        ""
        if force_recompute
        else "AND (rs.num_frags IS NULL OR rs.num_frags < 1 OR rs.num_frags > 3)"
    )
    cursor.execute(
        f"""
        UPDATE rule_smiles rs
           SET num_frags = CASE
               WHEN POSITION('[*:3]' IN rs.smiles) > 0 THEN 3
               WHEN POSITION('[*:2]' IN rs.smiles) > 0 THEN 2
               WHEN POSITION('[*:1]' IN rs.smiles) > 0 THEN 1
               WHEN POSITION('*' IN rs.smiles) > 0 THEN CASE
                   WHEN (LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', ''))) >= 3 THEN 3
                   WHEN (LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', ''))) >= 2 THEN 2
                   ELSE 1
               END
               ELSE 1
           END
         WHERE rs.id IN (SELECT id FROM tmp_inc_attach_rule_smiles)
           {rule_predicate}
        """
    )

    constant_predicate = (
        ""
        if force_recompute
        else "AND (cs.num_frags IS NULL OR cs.num_frags < 0 OR cs.num_frags > 3)"
    )
    cursor.execute(
        f"""
        UPDATE constant_smiles cs
           SET num_frags = CASE
               WHEN POSITION('[*:3]' IN cs.smiles) > 0 THEN 3
               WHEN POSITION('[*:2]' IN cs.smiles) > 0 THEN 2
               WHEN POSITION('[*:1]' IN cs.smiles) > 0 THEN 1
               WHEN POSITION('*' IN cs.smiles) > 0 THEN CASE
                   WHEN (LENGTH(cs.smiles) - LENGTH(REPLACE(cs.smiles, '*', ''))) >= 3 THEN 3
                   WHEN (LENGTH(cs.smiles) - LENGTH(REPLACE(cs.smiles, '*', ''))) >= 2 THEN 2
                   ELSE 1
               END
               ELSE 0
           END
         WHERE cs.smiles IN (SELECT smiles FROM tmp_inc_attach_constants)
           {constant_predicate}
        """
    )

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        cursor.execute(
            """
            UPDATE rule_smiles rs
               SET smiles_mol = mol_from_smiles(rs.smiles)
             WHERE rs.id IN (SELECT id FROM tmp_inc_attach_rule_smiles)
               AND rs.smiles_mol IS NULL
            """
        )
    else:
        cursor.execute("ALTER TABLE rule_smiles ADD COLUMN smiles_mol mol")
        cursor.execute(
            """
            UPDATE rule_smiles rs
               SET smiles_mol = mol_from_smiles(rs.smiles)
             WHERE rs.id IN (SELECT id FROM tmp_inc_attach_rule_smiles)
            """
        )
    existing_indexes = _ensure_named_indexes(
        cursor,
        [("idx_rule_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")],
        existing_indexes=existing_indexes,
    )

    if enable_constant_smiles_mol_index:
        if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
            cursor.execute(
                """
                UPDATE constant_smiles cs
                   SET smiles_mol = mol_from_smiles(cs.smiles)
                 WHERE cs.smiles IN (SELECT smiles FROM tmp_inc_attach_constants)
                   AND cs.smiles_mol IS NULL
                """
            )
        else:
            cursor.execute("ALTER TABLE constant_smiles ADD COLUMN smiles_mol mol")
            cursor.execute(
                """
                UPDATE constant_smiles cs
                   SET smiles_mol = mol_from_smiles(cs.smiles)
                 WHERE cs.smiles IN (SELECT smiles FROM tmp_inc_attach_constants)
                """
            )
        _ensure_named_indexes(
            cursor,
            [("idx_constant_smiles_smiles_mol", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")],
            existing_indexes=existing_indexes,
        )


def _rebuild_construct_tables_postgres(cursor) -> None:
    cursor.execute("DROP TABLE IF EXISTS from_construct")
    cursor.execute("DROP TABLE IF EXISTS to_construct")
    cursor.execute(
        """
        CREATE TABLE from_construct AS
        SELECT
            ROW_NUMBER() OVER (ORDER BY p.id) AS id,
            p.id AS pair_id,
            p.rule_environment_id AS rule_environment_id,
            COALESCE(p.constant_id, -1) AS constant_id,
            r.from_smiles_id AS rule_smiles_id,
            p.compound1_id AS compound_id,
            COALESCE(rs.num_frags, GREATEST(1, LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', '')))) AS num_frags,
            COALESCE(rs.num_heavies, 0) AS rule_smiles_num_heavies,
            COALESCE(c.clean_num_heavies, 0) AS compound_num_heavies
        FROM pair p
        INNER JOIN rule_environment re ON re.id = p.rule_environment_id
        INNER JOIN rule r ON r.id = re.rule_id
        INNER JOIN rule_smiles rs ON rs.id = r.from_smiles_id
        INNER JOIN compound c ON c.id = p.compound1_id
        """
    )
    cursor.execute(
        """
        CREATE TABLE to_construct AS
        SELECT
            ROW_NUMBER() OVER (ORDER BY p.id) AS id,
            p.id AS pair_id,
            p.rule_environment_id AS rule_environment_id,
            COALESCE(p.constant_id, -1) AS constant_id,
            r.to_smiles_id AS rule_smiles_id,
            p.compound2_id AS compound_id,
            COALESCE(rs.num_frags, GREATEST(1, LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', '')))) AS num_frags,
            COALESCE(rs.num_heavies, 0) AS rule_smiles_num_heavies,
            COALESCE(c.clean_num_heavies, 0) AS compound_num_heavies
        FROM pair p
        INNER JOIN rule_environment re ON re.id = p.rule_environment_id
        INNER JOIN rule r ON r.id = re.rule_id
        INNER JOIN rule_smiles rs ON rs.id = r.to_smiles_id
        INNER JOIN compound c ON c.id = p.compound2_id
        """
    )
    cursor.execute("ALTER TABLE from_construct ADD PRIMARY KEY (id)")
    cursor.execute("ALTER TABLE to_construct ADD PRIMARY KEY (id)")
    cursor.execute("CREATE INDEX idx_from_construct_lookup ON from_construct(num_frags, constant_id, rule_smiles_id)")
    cursor.execute("CREATE INDEX idx_to_construct_lookup ON to_construct(num_frags, constant_id, rule_smiles_id)")
    cursor.execute("CREATE INDEX idx_from_construct_pair ON from_construct(pair_id)")
    cursor.execute("CREATE INDEX idx_to_construct_pair ON to_construct(pair_id)")
    cursor.execute("CREATE INDEX idx_from_construct_compound ON from_construct(compound_id)")
    cursor.execute("CREATE INDEX idx_to_construct_compound ON to_construct(compound_id)")


def _append_construct_tables_postgres_for_inserted_pairs(cursor) -> None:
    cursor.execute("SELECT to_regclass('tmp_inc_inserted_pair_ids_all')")
    if cursor.fetchone()[0] is None:
        return

    cursor.execute("SELECT to_regclass('from_construct')")
    has_from_construct = cursor.fetchone()[0] is not None
    cursor.execute("SELECT to_regclass('to_construct')")
    has_to_construct = cursor.fetchone()[0] is not None
    if not has_from_construct or not has_to_construct:
        _rebuild_construct_tables_postgres(cursor)
        return

    cursor.execute(
        """
        WITH base AS (
            SELECT COALESCE(MAX(id), 0) AS max_id
            FROM from_construct
        )
        INSERT INTO from_construct (
            id,
            pair_id,
            rule_environment_id,
            constant_id,
            rule_smiles_id,
            compound_id,
            num_frags,
            rule_smiles_num_heavies,
            compound_num_heavies
        )
        SELECT
            base.max_id + ROW_NUMBER() OVER (ORDER BY p.id) AS id,
            p.id AS pair_id,
            p.rule_environment_id AS rule_environment_id,
            COALESCE(p.constant_id, -1) AS constant_id,
            r.from_smiles_id AS rule_smiles_id,
            p.compound1_id AS compound_id,
            COALESCE(rs.num_frags, GREATEST(1, LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', '')))) AS num_frags,
            COALESCE(rs.num_heavies, 0) AS rule_smiles_num_heavies,
            COALESCE(c.clean_num_heavies, 0) AS compound_num_heavies
        FROM pair p
        INNER JOIN tmp_inc_inserted_pair_ids_all pi
                ON pi.pair_id = p.id
        INNER JOIN rule_environment re
                ON re.id = p.rule_environment_id
        INNER JOIN rule r
                ON r.id = re.rule_id
        INNER JOIN rule_smiles rs
                ON rs.id = r.from_smiles_id
        INNER JOIN compound c
                ON c.id = p.compound1_id
        LEFT JOIN from_construct existing
               ON existing.pair_id = p.id
        CROSS JOIN base
        WHERE existing.pair_id IS NULL
        """
    )
    cursor.execute(
        """
        WITH base AS (
            SELECT COALESCE(MAX(id), 0) AS max_id
            FROM to_construct
        )
        INSERT INTO to_construct (
            id,
            pair_id,
            rule_environment_id,
            constant_id,
            rule_smiles_id,
            compound_id,
            num_frags,
            rule_smiles_num_heavies,
            compound_num_heavies
        )
        SELECT
            base.max_id + ROW_NUMBER() OVER (ORDER BY p.id) AS id,
            p.id AS pair_id,
            p.rule_environment_id AS rule_environment_id,
            COALESCE(p.constant_id, -1) AS constant_id,
            r.to_smiles_id AS rule_smiles_id,
            p.compound2_id AS compound_id,
            COALESCE(rs.num_frags, GREATEST(1, LENGTH(rs.smiles) - LENGTH(REPLACE(rs.smiles, '*', '')))) AS num_frags,
            COALESCE(rs.num_heavies, 0) AS rule_smiles_num_heavies,
            COALESCE(c.clean_num_heavies, 0) AS compound_num_heavies
        FROM pair p
        INNER JOIN tmp_inc_inserted_pair_ids_all pi
                ON pi.pair_id = p.id
        INNER JOIN rule_environment re
                ON re.id = p.rule_environment_id
        INNER JOIN rule r
                ON r.id = re.rule_id
        INNER JOIN rule_smiles rs
                ON rs.id = r.to_smiles_id
        INNER JOIN compound c
                ON c.id = p.compound2_id
        LEFT JOIN to_construct existing
               ON existing.pair_id = p.id
        CROSS JOIN base
        WHERE existing.pair_id IS NULL
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_from_construct_pair ON from_construct(pair_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_to_construct_pair ON to_construct(pair_id)")


def _normalize_property_batch_id(value: str) -> str:
    token = str(value or "").strip()
    if token:
        return token
    return f"batch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


def _parse_property_numeric(value: object) -> Optional[float]:
    token = str(value or "").strip()
    if token.lower() in PROPERTY_BATCH_MISSING_TOKENS:
        return None
    try:
        parsed = float(token)
    except Exception:
        return None
    if parsed != parsed:  # NaN
        return None
    if parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _canonicalize_smiles_for_lookup(smiles: str, *, canonicalize: bool) -> str:
    text = str(smiles or "").strip()
    if not text:
        return ""
    if not canonicalize or not HAS_RDKIT_MMPDB or Chem is None:
        return text
    try:
        mol = Chem.MolFromSmiles(text)
        if mol is None:
            return ""
        return str(Chem.MolToSmiles(mol, canonical=True) or "").strip()
    except Exception:
        return ""


def _detect_table_delimiter(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8") as handle:
        first_line = handle.readline()
    return "\t" if "\t" in first_line else ","


def _resolve_smiles_column(headers: List[str], preferred_column: str = "") -> str:
    normalized_headers = {str(h or "").strip().lower(): str(h or "").strip() for h in headers}
    preferred = str(preferred_column or "").strip()
    if preferred:
        direct = normalized_headers.get(preferred.lower())
        if direct:
            return direct
        raise ValueError(f"Requested smiles column '{preferred}' not found in file headers.")

    for candidate in ("smiles", "canonical_smiles", "mol_smiles", "molecule_smiles", "query_smiles"):
        resolved = normalized_headers.get(candidate)
        if resolved:
            return resolved
    raise ValueError("No SMILES column found. Expected one of: smiles/canonical_smiles/mol_smiles.")


def _resolve_property_columns(headers: List[str], smiles_column: str) -> List[str]:
    ignored = {
        str(smiles_column or "").strip().lower(),
        "id",
        "compound_id",
        "cmpd_chemblid",
        "chembl_id",
        "molecule_id",
        "mol_id",
        "public_id",
        "name",
    }
    properties: List[str] = []
    for header in headers:
        token = str(header or "").strip()
        if not token:
            continue
        if token.lower() in ignored:
            continue
        properties.append(token)
    return properties


def _resolve_identifier_column(headers: List[str], preferred_column: str = "") -> str:
    normalized_headers = {str(h or "").strip().lower(): str(h or "").strip() for h in headers}
    preferred = str(preferred_column or "").strip()
    if preferred:
        direct = normalized_headers.get(preferred.lower())
        if direct:
            return direct
        raise ValueError(f"Requested identifier column '{preferred}' not found in file headers.")
    for candidate in ("id", "public_id", "cmpd_chemblid", "chembl_id", "compound_id", "name"):
        resolved = normalized_headers.get(candidate)
        if resolved:
            return resolved
    return ""


def _current_schema_table_columns(cursor, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        [str(table_name or "").strip()],
    )
    return {str(row[0] or "").strip() for row in cursor.fetchall()}


def _current_schema_primary_key_columns(cursor, table_name: str) -> List[str]:
    cursor.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.table_schema = current_schema()
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        [str(table_name or "").strip()],
    )
    return [str(row[0] or "").strip() for row in cursor.fetchall()]


def _ensure_property_batch_tables(cursor, *, seed_base_from_compound_property: bool = False) -> None:
    logger = logging.getLogger(__name__)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROPERTY_BATCH_BASE_TABLE} (
            clean_smiles TEXT NOT NULL,
            property_name TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (clean_smiles, property_name)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROPERTY_BATCH_HEADER_TABLE} (
            batch_id TEXT PRIMARY KEY,
            batch_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            total_rows INTEGER NOT NULL DEFAULT 0,
            matched_rows INTEGER NOT NULL DEFAULT 0,
            unmatched_rows INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROPERTY_BATCH_ROWS_TABLE} (
            batch_id TEXT NOT NULL REFERENCES {PROPERTY_BATCH_HEADER_TABLE}(batch_id) ON DELETE CASCADE,
            batch_seq BIGINT NOT NULL,
            clean_smiles TEXT NOT NULL,
            property_name TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (batch_id, clean_smiles, property_name)
        )
        """
    )
    base_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_BASE_TABLE)
    rows_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_ROWS_TABLE)
    base_pk_columns = _current_schema_primary_key_columns(cursor, PROPERTY_BATCH_BASE_TABLE)
    rows_pk_columns = _current_schema_primary_key_columns(cursor, PROPERTY_BATCH_ROWS_TABLE)
    legacy_base_layout = (
        {"compound_id", "property_name_id"}.issubset(base_columns)
        and (not {"clean_smiles", "property_name"}.issubset(base_columns)
             or base_pk_columns != ["clean_smiles", "property_name"])
    )
    legacy_rows_layout = (
        {"compound_id", "property_name_id"}.issubset(rows_columns)
        and (not {"clean_smiles", "property_name"}.issubset(rows_columns)
             or rows_pk_columns != ["batch_id", "clean_smiles", "property_name"])
    )
    if legacy_base_layout:
        logger.info("Migrating legacy table %s to smiles/name-based layout.", PROPERTY_BATCH_BASE_TABLE)
        cursor.execute(f"DROP TABLE IF EXISTS {PROPERTY_BATCH_BASE_TABLE}_new")
        cursor.execute(
            f"""
            CREATE TABLE {PROPERTY_BATCH_BASE_TABLE}_new (
                clean_smiles TEXT NOT NULL,
                property_name TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (clean_smiles, property_name)
            )
            """
        )
        cursor.execute(
            f"""
            INSERT INTO {PROPERTY_BATCH_BASE_TABLE}_new (clean_smiles, property_name, value)
            SELECT c.clean_smiles, pn.name, b.value
            FROM {PROPERTY_BATCH_BASE_TABLE} b
            INNER JOIN compound c ON c.id = b.compound_id
            INNER JOIN property_name pn ON pn.id = b.property_name_id
            WHERE c.clean_smiles IS NOT NULL
              AND c.clean_smiles <> ''
              AND pn.name IS NOT NULL
              AND pn.name <> ''
            ON CONFLICT (clean_smiles, property_name) DO UPDATE
                SET value = EXCLUDED.value
            """
        )
        cursor.execute(f"DROP TABLE {PROPERTY_BATCH_BASE_TABLE}")
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_BASE_TABLE}_new RENAME TO {PROPERTY_BATCH_BASE_TABLE}")
    if legacy_rows_layout:
        logger.info("Migrating legacy table %s to smiles/name-based layout.", PROPERTY_BATCH_ROWS_TABLE)
        cursor.execute(f"DROP TABLE IF EXISTS {PROPERTY_BATCH_ROWS_TABLE}_new")
        cursor.execute(
            f"""
            CREATE TABLE {PROPERTY_BATCH_ROWS_TABLE}_new (
                batch_id TEXT NOT NULL REFERENCES {PROPERTY_BATCH_HEADER_TABLE}(batch_id) ON DELETE CASCADE,
                batch_seq BIGINT NOT NULL,
                clean_smiles TEXT NOT NULL,
                property_name TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (batch_id, clean_smiles, property_name)
            )
            """
        )
        cursor.execute(
            f"""
            INSERT INTO {PROPERTY_BATCH_ROWS_TABLE}_new (batch_id, batch_seq, clean_smiles, property_name, value)
            SELECT r.batch_id, r.batch_seq, c.clean_smiles, pn.name, r.value
            FROM {PROPERTY_BATCH_ROWS_TABLE} r
            INNER JOIN compound c ON c.id = r.compound_id
            INNER JOIN property_name pn ON pn.id = r.property_name_id
            WHERE c.clean_smiles IS NOT NULL
              AND c.clean_smiles <> ''
              AND pn.name IS NOT NULL
              AND pn.name <> ''
            ON CONFLICT (batch_id, clean_smiles, property_name) DO UPDATE
                SET value = EXCLUDED.value
            """
        )
        cursor.execute(f"DROP TABLE {PROPERTY_BATCH_ROWS_TABLE}")
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_ROWS_TABLE}_new RENAME TO {PROPERTY_BATCH_ROWS_TABLE}")

    base_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_BASE_TABLE)
    if "clean_smiles" not in base_columns:
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_BASE_TABLE} ADD COLUMN clean_smiles TEXT")
    if "property_name" not in base_columns:
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_BASE_TABLE} ADD COLUMN property_name TEXT")
    base_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_BASE_TABLE)
    if {"compound_id", "property_name_id", "clean_smiles", "property_name"}.issubset(base_columns):
        cursor.execute(
            f"""
            UPDATE {PROPERTY_BATCH_BASE_TABLE} b
            SET clean_smiles = c.clean_smiles,
                property_name = pn.name
            FROM compound c
            INNER JOIN property_name pn ON pn.id = b.property_name_id
            WHERE (b.clean_smiles IS NULL OR b.clean_smiles = '' OR b.property_name IS NULL OR b.property_name = '')
              AND c.id = b.compound_id
            """
        )

    rows_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_ROWS_TABLE)
    if "clean_smiles" not in rows_columns:
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_ROWS_TABLE} ADD COLUMN clean_smiles TEXT")
    if "property_name" not in rows_columns:
        cursor.execute(f"ALTER TABLE {PROPERTY_BATCH_ROWS_TABLE} ADD COLUMN property_name TEXT")
    rows_columns = _current_schema_table_columns(cursor, PROPERTY_BATCH_ROWS_TABLE)
    if {"compound_id", "property_name_id", "clean_smiles", "property_name"}.issubset(rows_columns):
        cursor.execute(
            f"""
            UPDATE {PROPERTY_BATCH_ROWS_TABLE} r
            SET clean_smiles = c.clean_smiles,
                property_name = pn.name
            FROM compound c
            INNER JOIN property_name pn ON pn.id = r.property_name_id
            WHERE (r.clean_smiles IS NULL OR r.clean_smiles = '' OR r.property_name IS NULL OR r.property_name = '')
              AND c.id = r.compound_id
            """
        )

    cursor.execute(
        f"""
        DELETE FROM {PROPERTY_BATCH_BASE_TABLE}
        WHERE clean_smiles IS NULL OR clean_smiles = '' OR property_name IS NULL OR property_name = ''
        """
    )
    cursor.execute(
        f"""
        DELETE FROM {PROPERTY_BATCH_ROWS_TABLE}
        WHERE clean_smiles IS NULL OR clean_smiles = '' OR property_name IS NULL OR property_name = ''
        """
    )
    cursor.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lopbase_smiles_prop
            ON {PROPERTY_BATCH_BASE_TABLE}(clean_smiles, property_name)
        """
    )
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lopbr_lookup
            ON {PROPERTY_BATCH_ROWS_TABLE}(clean_smiles, property_name, batch_seq DESC)
        """
    )
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lopbr_batch
            ON {PROPERTY_BATCH_ROWS_TABLE}(batch_id)
        """
    )

    if not seed_base_from_compound_property:
        return
    cursor.execute(f"SELECT EXISTS(SELECT 1 FROM {PROPERTY_BATCH_BASE_TABLE} LIMIT 1)")
    has_seed = bool(cursor.fetchone()[0])
    if has_seed:
        return
    cursor.execute(
        f"""
        INSERT INTO {PROPERTY_BATCH_BASE_TABLE} (clean_smiles, property_name, value)
        SELECT picked.clean_smiles, picked.property_name, picked.value
        FROM (
            SELECT
                c.clean_smiles AS clean_smiles,
                pn.name AS property_name,
                cp.value AS value,
                ROW_NUMBER() OVER (
                    PARTITION BY c.clean_smiles, pn.name
                    ORDER BY cp.id DESC
                ) AS rn
            FROM compound_property cp
            INNER JOIN compound c ON c.id = cp.compound_id
            INNER JOIN property_name pn ON pn.id = cp.property_name_id
            WHERE c.clean_smiles IS NOT NULL
              AND c.clean_smiles <> ''
              AND pn.name IS NOT NULL
              AND pn.name <> ''
        ) picked
        WHERE picked.rn = 1
        ON CONFLICT (clean_smiles, property_name) DO UPDATE
            SET value = EXCLUDED.value
        """
    )
    logger.info(
        "Seeded %s from current compound_property for batch rollback baseline.",
        PROPERTY_BATCH_BASE_TABLE,
    )


def _load_property_batch_temp_table(
    cursor,
    *,
    property_file: str,
    smiles_column: str = "",
    canonicalize_smiles: bool = True,
    insert_batch_size: int = 5000,
) -> Dict[str, int]:
    logger = logging.getLogger(__name__)
    delimiter = _detect_table_delimiter(property_file)
    cursor.execute("DROP TABLE IF EXISTS tmp_property_upload")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_property_upload (
            line_no BIGINT NOT NULL,
            clean_smiles TEXT NOT NULL,
            property_name TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL
        ) ON COMMIT DROP
        """
    )

    total_rows = 0
    parsed_rows = 0
    invalid_smiles_rows = 0
    value_rows = 0
    buffer: List[tuple[int, str, str, float]] = []
    def _flush_property_rows(rows: List[tuple[int, str, str, float]]) -> None:
        if not rows:
            return
        with cursor.copy(
            "COPY tmp_property_upload (line_no, clean_smiles, property_name, value) FROM STDIN"
        ) as copy:
            for item in rows:
                copy.write_row(item)

    with open(property_file, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [str(h or "").strip() for h in (reader.fieldnames or [])]
        if not headers:
            raise ValueError(f"Property batch file has no header: {property_file}")
        smiles_col = _resolve_smiles_column(headers, preferred_column=smiles_column)
        property_columns = _resolve_property_columns(headers, smiles_col)
        if not property_columns:
            raise ValueError(
                "No property columns found in batch file. Keep one SMILES column plus at least one numeric property column."
            )
        logger.info(
            "Property batch parser: smiles_column=%s properties=%s delimiter=%s",
            smiles_col,
            ", ".join(property_columns[:12]),
            "TAB" if delimiter == "\t" else "CSV",
        )

        for line_no, row in enumerate(reader, start=2):
            total_rows += 1
            smiles_raw = str((row or {}).get(smiles_col) or "").strip()
            clean_smiles = _canonicalize_smiles_for_lookup(smiles_raw, canonicalize=canonicalize_smiles)
            if not clean_smiles:
                invalid_smiles_rows += 1
                continue
            parsed_rows += 1
            for property_name in property_columns:
                value = _parse_property_numeric((row or {}).get(property_name))
                if value is None:
                    continue
                buffer.append((line_no, clean_smiles, property_name, value))
                value_rows += 1
            if len(buffer) >= max(100, int(insert_batch_size)):
                _flush_property_rows(buffer)
                buffer = []
        if buffer:
            _flush_property_rows(buffer)

    staged_values = int(value_rows)
    cursor.execute("DROP TABLE IF EXISTS tmp_property_upload_dedup")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_property_upload_dedup AS
        SELECT clean_smiles, property_name, value
        FROM (
            SELECT
                line_no,
                clean_smiles,
                property_name,
                value,
                ROW_NUMBER() OVER (
                    PARTITION BY clean_smiles, property_name
                    ORDER BY line_no DESC
                ) AS rn
            FROM tmp_property_upload
        ) ranked
        WHERE rn = 1
        """
    )
    dedup_rows = int(cursor.rowcount or 0)
    if dedup_rows <= 0:
        cursor.execute("SELECT COUNT(*) FROM tmp_property_upload_dedup")
        dedup_rows = int(cursor.fetchone()[0])
    return {
        "total_rows": total_rows,
        "parsed_rows": parsed_rows,
        "invalid_smiles_rows": invalid_smiles_rows,
        "value_rows": value_rows,
        "staged_rows": staged_values,
        "dedup_rows": dedup_rows,
    }


def _refresh_compound_property_for_touched_keys(cursor) -> Dict[str, int]:
    cursor.execute("DROP TABLE IF EXISTS tmp_property_latest_batch")
    cursor.execute(
        f"""
        CREATE TEMP TABLE tmp_property_latest_batch AS
        SELECT DISTINCT ON (r.clean_smiles, r.property_name)
            r.clean_smiles,
            r.property_name,
            r.value
        FROM {PROPERTY_BATCH_ROWS_TABLE} r
        INNER JOIN tmp_property_touched t
            ON t.clean_smiles = r.clean_smiles
           AND t.property_name = r.property_name
        ORDER BY r.clean_smiles, r.property_name, r.batch_seq DESC, r.batch_id DESC
        """
    )
    cursor.execute(
        """
        INSERT INTO property_name (name)
        SELECT DISTINCT t.property_name
        FROM tmp_property_touched t
        LEFT JOIN property_name pn ON pn.name = t.property_name
        WHERE pn.id IS NULL
          AND t.property_name IS NOT NULL
          AND t.property_name <> ''
        """
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_property_resolved")
    cursor.execute(
        f"""
        CREATE TEMP TABLE tmp_property_resolved AS
        SELECT
            t.clean_smiles,
            t.property_name,
            COALESCE(lb.value, pb.value) AS resolved_value
        FROM tmp_property_touched t
        LEFT JOIN tmp_property_latest_batch lb
            ON lb.clean_smiles = t.clean_smiles
           AND lb.property_name = t.property_name
        LEFT JOIN {PROPERTY_BATCH_BASE_TABLE} pb
            ON pb.clean_smiles = t.clean_smiles
           AND pb.property_name = t.property_name
        """
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_property_resolved_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_property_resolved_ids AS
        SELECT
            c.id AS compound_id,
            pn.id AS property_name_id,
            r.resolved_value
        FROM tmp_property_resolved r
        INNER JOIN compound c ON c.clean_smiles = r.clean_smiles
        INNER JOIN property_name pn ON pn.name = r.property_name
        """
    )
    cursor.execute(
        """
        UPDATE compound_property cp
           SET value = r.resolved_value
          FROM tmp_property_resolved_ids r
         WHERE cp.compound_id = r.compound_id
           AND cp.property_name_id = r.property_name_id
           AND r.resolved_value IS NOT NULL
           AND cp.value IS DISTINCT FROM r.resolved_value
        """
    )
    updated = int(cursor.rowcount or 0)
    cursor.execute(
        """
        INSERT INTO compound_property (compound_id, property_name_id, value)
        SELECT r.compound_id, r.property_name_id, r.resolved_value
          FROM tmp_property_resolved_ids r
         WHERE r.resolved_value IS NOT NULL
           AND NOT EXISTS (
                SELECT 1
                FROM compound_property cp
                WHERE cp.compound_id = r.compound_id
                  AND cp.property_name_id = r.property_name_id
            )
        """
    )
    inserted = int(cursor.rowcount or 0)
    cursor.execute(
        """
        DELETE FROM compound_property cp
        USING tmp_property_resolved_ids r
        WHERE cp.compound_id = r.compound_id
          AND cp.property_name_id = r.property_name_id
          AND r.resolved_value IS NULL
        """
    )
    deleted = int(cursor.rowcount or 0)
    return {"updated": updated, "inserted": inserted, "deleted": deleted}


def _refresh_compound_property_for_all_keys(cursor) -> Dict[str, int]:
    cursor.execute("DROP TABLE IF EXISTS tmp_property_touched")
    cursor.execute(
        f"""
        CREATE TEMP TABLE tmp_property_touched AS
        SELECT clean_smiles, property_name FROM {PROPERTY_BATCH_BASE_TABLE}
        UNION
        SELECT clean_smiles, property_name FROM {PROPERTY_BATCH_ROWS_TABLE}
        """
    )
    return _refresh_compound_property_for_touched_keys(cursor)


def _refresh_compound_property_for_smiles(cursor, smiles_values: Sequence[str]) -> Dict[str, int]:
    normalized_smiles = sorted({str(item or "").strip() for item in smiles_values if str(item or "").strip()})
    if not normalized_smiles:
        return {"updated": 0, "inserted": 0, "deleted": 0}
    cursor.execute("DROP TABLE IF EXISTS tmp_property_smiles_filter")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_property_smiles_filter (
            clean_smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    _copy_rows(
        cursor,
        "COPY tmp_property_smiles_filter (clean_smiles) FROM STDIN",
        [(value,) for value in normalized_smiles],
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_property_touched")
    cursor.execute(
        f"""
        CREATE TEMP TABLE tmp_property_touched AS
        SELECT b.clean_smiles, b.property_name
        FROM {PROPERTY_BATCH_BASE_TABLE} b
        INNER JOIN tmp_property_smiles_filter s
                ON s.clean_smiles = b.clean_smiles
        UNION
        SELECT r.clean_smiles, r.property_name
        FROM {PROPERTY_BATCH_ROWS_TABLE} r
        INNER JOIN tmp_property_smiles_filter s
                ON s.clean_smiles = r.clean_smiles
        """
    )
    return _refresh_compound_property_for_touched_keys(cursor)


def import_property_batch_postgres(
    postgres_url: str,
    *,
    schema: str,
    property_file: str,
    batch_id: str = "",
    batch_label: str = "",
    batch_notes: str = "",
    smiles_column: str = "",
    canonicalize_smiles: bool = True,
    insert_batch_size: int = 5000,
    overwrite_existing_batch: bool = False,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL property batch import.")
        return False
    source_file = str(property_file or "").strip()
    if not source_file or not os.path.exists(source_file):
        logger.error("Property batch file not found: %s", source_file)
        return False
    normalized_schema = _validate_pg_schema(schema)
    normalized_batch_id = _normalize_property_batch_id(batch_id)

    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)

                cursor.execute(
                    f"SELECT 1 FROM {PROPERTY_BATCH_HEADER_TABLE} WHERE batch_id = %s LIMIT 1",
                    [normalized_batch_id],
                )
                if cursor.fetchone() is not None:
                    if not overwrite_existing_batch:
                        logger.error(
                            "Batch id '%s' already exists in schema '%s'. Please use a new --property_batch_id.",
                            normalized_batch_id,
                            normalized_schema,
                        )
                        conn.rollback()
                        return False
                    logger.warning(
                        "Property batch id '%s' already exists in schema '%s'; replacing existing rows for retry.",
                        normalized_batch_id,
                        normalized_schema,
                    )
                    cursor.execute(
                        f"DELETE FROM {PROPERTY_BATCH_ROWS_TABLE} WHERE batch_id = %s",
                        [normalized_batch_id],
                    )
                    cursor.execute(
                        f"DELETE FROM {PROPERTY_BATCH_HEADER_TABLE} WHERE batch_id = %s",
                        [normalized_batch_id],
                    )

                parse_stats = _load_property_batch_temp_table(
                    cursor,
                    property_file=source_file,
                    smiles_column=smiles_column,
                    canonicalize_smiles=canonicalize_smiles,
                    insert_batch_size=insert_batch_size,
                )
                if parse_stats["dedup_rows"] <= 0:
                    logger.error("No numeric property rows were parsed from: %s", source_file)
                    conn.rollback()
                    return False

                cursor.execute(
                    """
                    INSERT INTO property_name (name)
                    SELECT DISTINCT d.property_name
                    FROM tmp_property_upload_dedup d
                    LEFT JOIN property_name pn ON pn.name = d.property_name
                    WHERE pn.id IS NULL
                    """
                )

                cursor.execute(
                    f"""
                    INSERT INTO {PROPERTY_BATCH_HEADER_TABLE}
                        (batch_id, label, source_file, notes, total_rows, matched_rows, unmatched_rows)
                    VALUES (%s, %s, %s, %s, 0, 0, 0)
                    RETURNING batch_seq
                    """,
                    [
                        normalized_batch_id,
                        str(batch_label or "").strip(),
                        source_file,
                        str(batch_notes or "").strip(),
                    ],
                )
                batch_seq = int(cursor.fetchone()[0])

                total_rows = int(parse_stats["dedup_rows"])

                cursor.execute(
                    f"""
                    INSERT INTO {PROPERTY_BATCH_ROWS_TABLE}
                        (batch_id, batch_seq, clean_smiles, property_name, value)
                    SELECT
                        %s AS batch_id,
                        %s AS batch_seq,
                        d.clean_smiles,
                        d.property_name,
                        d.value AS value
                    FROM tmp_property_upload_dedup d
                    WHERE EXISTS (
                        SELECT 1
                        FROM compound c
                        WHERE c.clean_smiles = d.clean_smiles
                    )
                    """,
                    [normalized_batch_id, batch_seq],
                )
                inserted_batch_rows = int(cursor.rowcount or 0)
                matched_rows = inserted_batch_rows
                unmatched_rows = max(0, total_rows - matched_rows)

                cursor.execute("DROP TABLE IF EXISTS tmp_property_touched")
                cursor.execute(
                    f"""
                    CREATE TEMP TABLE tmp_property_touched AS
                    SELECT DISTINCT clean_smiles, property_name
                    FROM {PROPERTY_BATCH_ROWS_TABLE}
                    WHERE batch_id = %s
                    """,
                    [normalized_batch_id],
                )
                touched_pairs = max(0, int(cursor.rowcount or 0))
                refresh_stats = _refresh_compound_property_for_touched_keys(cursor)

                cursor.execute(
                    f"""
                    UPDATE {PROPERTY_BATCH_HEADER_TABLE}
                    SET total_rows = %s,
                        matched_rows = %s,
                        unmatched_rows = %s
                    WHERE batch_id = %s
                    """,
                    [total_rows, matched_rows, unmatched_rows, normalized_batch_id],
                )
                conn.commit()

        logger.info(
            "Imported property batch '%s' into schema '%s': parsed_rows=%s dedup_rows=%s matched=%s unmatched=%s batch_rows=%s touched_pairs=%s cp(updated=%s inserted=%s deleted=%s)",
            normalized_batch_id,
            normalized_schema,
            parse_stats["parsed_rows"],
            parse_stats["dedup_rows"],
            matched_rows,
            unmatched_rows,
            inserted_batch_rows,
            touched_pairs,
            refresh_stats["updated"],
            refresh_stats["inserted"],
            refresh_stats["deleted"],
        )
        return True
    except Exception as exc:
        logger.error("Failed importing property batch: %s", exc, exc_info=True)
        return False


def delete_property_batch_postgres(
    postgres_url: str,
    *,
    schema: str,
    batch_id: str,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL property batch delete.")
        return False
    normalized_schema = _validate_pg_schema(schema)
    normalized_batch_id = str(batch_id or "").strip()
    if not normalized_batch_id:
        logger.error("--delete_properties_batch requires a non-empty batch id.")
        return False

    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=False)
                cursor.execute(
                    f"""
                    SELECT batch_id, batch_seq, total_rows, matched_rows, unmatched_rows
                    FROM {PROPERTY_BATCH_HEADER_TABLE}
                    WHERE batch_id = %s
                    LIMIT 1
                    """,
                    [normalized_batch_id],
                )
                row = cursor.fetchone()
                if row is None:
                    logger.error(
                        "Batch '%s' not found in schema '%s'.",
                        normalized_batch_id,
                        normalized_schema,
                    )
                    conn.rollback()
                    return False
                batch_seq = int(row[1] or 0)
                total_rows = int(row[2] or 0)
                matched_rows = int(row[3] or 0)
                unmatched_rows = int(row[4] or 0)

                cursor.execute("DROP TABLE IF EXISTS tmp_property_touched")
                cursor.execute(
                    f"""
                    CREATE TEMP TABLE tmp_property_touched AS
                    SELECT DISTINCT clean_smiles, property_name
                    FROM {PROPERTY_BATCH_ROWS_TABLE}
                    WHERE batch_id = %s
                    """,
                    [normalized_batch_id],
                )
                touched_pairs = max(0, int(cursor.rowcount or 0))

                cursor.execute(
                    f"DELETE FROM {PROPERTY_BATCH_HEADER_TABLE} WHERE batch_id = %s",
                    [normalized_batch_id],
                )
                refresh_stats = _refresh_compound_property_for_touched_keys(cursor)
                conn.commit()

        logger.info(
            "Deleted property batch '%s' (seq=%s) from schema '%s': total=%s matched=%s unmatched=%s touched_pairs=%s cp(updated=%s inserted=%s deleted=%s)",
            normalized_batch_id,
            batch_seq,
            normalized_schema,
            total_rows,
            matched_rows,
            unmatched_rows,
            touched_pairs,
            refresh_stats["updated"],
            refresh_stats["inserted"],
            refresh_stats["deleted"],
        )
        return True
    except Exception as exc:
        logger.error("Failed deleting property batch '%s': %s", normalized_batch_id, exc, exc_info=True)
        return False


def list_property_batches_postgres(
    postgres_url: str,
    *,
    schema: str,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL property batch listing.")
        return False
    normalized_schema = _validate_pg_schema(schema)
    try:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=False)
                cursor.execute(
                    f"""
                    SELECT
                        b.batch_id,
                        b.batch_seq,
                        b.label,
                        b.source_file,
                        b.imported_at,
                        b.total_rows,
                        b.matched_rows,
                        b.unmatched_rows,
                        COUNT(r.clean_smiles) AS value_rows
                    FROM {PROPERTY_BATCH_HEADER_TABLE} b
                    LEFT JOIN {PROPERTY_BATCH_ROWS_TABLE} r
                           ON r.batch_id = b.batch_id
                    GROUP BY
                        b.batch_id, b.batch_seq, b.label, b.source_file, b.imported_at,
                        b.total_rows, b.matched_rows, b.unmatched_rows
                    ORDER BY b.batch_seq DESC
                    """
                )
                rows = cursor.fetchall()
        print("\n" + "=" * 80)
        print(f"Property batches in schema '{normalized_schema}'")
        print("=" * 80)
        if not rows:
            print("(empty)")
            return True
        for row in rows:
            print(
                f"- batch_id={row[0]} seq={row[1]} imported_at={row[4]} "
                f"rows(total/matched/unmatched)={row[5]}/{row[6]}/{row[7]} value_rows={row[8]}"
            )
            if str(row[2] or "").strip():
                print(f"  label: {row[2]}")
            if str(row[3] or "").strip():
                print(f"  source_file: {row[3]}")
        return True
    except Exception as exc:
        logger.error("Failed listing property batches: %s", exc, exc_info=True)
        return False


def _normalize_compound_batch_id(value: str) -> str:
    token = str(value or "").strip()
    if token:
        return token
    return f"compound_batch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


def _ensure_compound_batch_tables(cursor, *, seed_base_from_compound: bool = False) -> None:
    logger = logging.getLogger(__name__)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {COMPOUND_BATCH_BASE_TABLE} (
            clean_smiles TEXT PRIMARY KEY,
            public_id TEXT NOT NULL DEFAULT '',
            input_smiles TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {COMPOUND_BATCH_HEADER_TABLE} (
            batch_id TEXT PRIMARY KEY,
            batch_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            total_rows INTEGER NOT NULL DEFAULT 0,
            valid_rows INTEGER NOT NULL DEFAULT 0,
            dedup_rows INTEGER NOT NULL DEFAULT 0,
            new_unique_rows INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {COMPOUND_BATCH_ROWS_TABLE} (
            batch_id TEXT NOT NULL REFERENCES {COMPOUND_BATCH_HEADER_TABLE}(batch_id) ON DELETE CASCADE,
            batch_seq BIGINT NOT NULL,
            clean_smiles TEXT NOT NULL,
            public_id TEXT NOT NULL DEFAULT '',
            input_smiles TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (batch_id, clean_smiles)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_locbr_smiles
            ON {COMPOUND_BATCH_ROWS_TABLE}(clean_smiles, batch_seq DESC)
        """
    )
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_locbr_batch
            ON {COMPOUND_BATCH_ROWS_TABLE}(batch_id)
        """
    )
    if not seed_base_from_compound:
        return
    cursor.execute(f"SELECT EXISTS(SELECT 1 FROM {COMPOUND_BATCH_BASE_TABLE} LIMIT 1)")
    has_seed = bool(cursor.fetchone()[0])
    if has_seed:
        return
    try:
        cursor.execute(
            f"""
            INSERT INTO {COMPOUND_BATCH_BASE_TABLE} (clean_smiles, public_id, input_smiles)
            SELECT
                picked.clean_smiles,
                picked.public_id,
                picked.input_smiles
            FROM (
                SELECT
                    c.clean_smiles,
                    COALESCE(c.public_id, '') AS public_id,
                    COALESCE(NULLIF(c.input_smiles, ''), c.clean_smiles) AS input_smiles,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.clean_smiles
                        ORDER BY
                            CASE WHEN COALESCE(c.public_id, '') <> '' THEN 0 ELSE 1 END,
                            c.id ASC
                    ) AS rn
                FROM compound c
                WHERE c.clean_smiles IS NOT NULL
                  AND c.clean_smiles <> ''
            ) picked
            WHERE picked.rn = 1
            ON CONFLICT (clean_smiles) DO UPDATE
              SET public_id = EXCLUDED.public_id,
                  input_smiles = EXCLUDED.input_smiles
            """
        )
        logger.info(
            "Seeded %s from current compound table for incremental compound lifecycle baseline.",
            COMPOUND_BATCH_BASE_TABLE,
        )
    except Exception as exc:
        logger.warning("Failed seeding %s from compound table: %s", COMPOUND_BATCH_BASE_TABLE, exc)


def _load_compound_batch_temp_table(
    cursor,
    *,
    structures_file: str,
    smiles_column: str = "",
    id_column: str = "",
    canonicalize_smiles: bool = True,
    insert_batch_size: int = 5000,
) -> Dict[str, int]:
    logger = logging.getLogger(__name__)
    delimiter = _detect_table_delimiter(structures_file)
    cursor.execute("DROP TABLE IF EXISTS tmp_compound_upload")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_compound_upload (
            line_no BIGINT NOT NULL,
            input_smiles TEXT NOT NULL,
            clean_smiles TEXT NOT NULL,
            public_id TEXT NOT NULL DEFAULT ''
        ) ON COMMIT DROP
        """
    )
    total_rows = 0
    valid_rows = 0
    invalid_smiles_rows = 0
    buffer: List[tuple[int, str, str, str]] = []
    def _flush_upload_buffer(rows: List[tuple[int, str, str, str]]) -> None:
        if not rows:
            return
        with cursor.copy(
            "COPY tmp_compound_upload (line_no, input_smiles, clean_smiles, public_id) FROM STDIN"
        ) as copy:
            for item in rows:
                copy.write_row(item)

    with open(structures_file, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [str(h or "").strip() for h in (reader.fieldnames or [])]
        if not headers:
            raise ValueError(f"Compound batch file has no header: {structures_file}")
        smiles_col = _resolve_smiles_column(headers, preferred_column=smiles_column)
        id_col = _resolve_identifier_column(headers, preferred_column=id_column)
        logger.info(
            "Compound batch parser: smiles_column=%s id_column=%s delimiter=%s",
            smiles_col,
            id_col or "<none>",
            "TAB" if delimiter == "\t" else "CSV",
        )
        for line_no, row in enumerate(reader, start=2):
            total_rows += 1
            raw_smiles = str((row or {}).get(smiles_col) or "").strip()
            clean_smiles = _canonicalize_smiles_for_lookup(raw_smiles, canonicalize=canonicalize_smiles)
            if not clean_smiles:
                invalid_smiles_rows += 1
                continue
            raw_public_id = str((row or {}).get(id_col) or "").strip() if id_col else ""
            buffer.append((line_no, raw_smiles, clean_smiles, raw_public_id))
            valid_rows += 1
            if len(buffer) >= max(100, int(insert_batch_size)):
                _flush_upload_buffer(buffer)
                buffer = []
        if buffer:
            _flush_upload_buffer(buffer)
    cursor.execute("DROP TABLE IF EXISTS tmp_compound_upload_dedup")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_compound_upload_dedup AS
        SELECT
            clean_smiles,
            COALESCE(NULLIF(public_id, ''), 'CMPD_' || SUBSTRING(md5(clean_smiles), 1, 16)) AS public_id,
            input_smiles
        FROM (
            SELECT
                line_no,
                clean_smiles,
                public_id,
                input_smiles,
                ROW_NUMBER() OVER (
                    PARTITION BY clean_smiles
                    ORDER BY line_no DESC
                ) AS rn
            FROM tmp_compound_upload
        ) ranked
        WHERE rn = 1
        """
    )
    cursor.execute("SELECT COUNT(*) FROM tmp_compound_upload_dedup")
    dedup_rows = int(cursor.fetchone()[0])
    return {
        "total_rows": total_rows,
        "valid_rows": valid_rows,
        "invalid_smiles_rows": invalid_smiles_rows,
        "dedup_rows": dedup_rows,
    }


def _schema_has_any_compounds(cursor) -> bool:
    try:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM compound LIMIT 1)")
        row = cursor.fetchone()
        return bool((row or [False])[0])
    except Exception:
        return False


def _export_smiles_file_from_temp_compound_table(
    cursor,
    *,
    source_table: str,
    output_file: str,
) -> int:
    total_rows = 0
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    cursor.execute(
        f"""
        SELECT
            clean_smiles,
            COALESCE(NULLIF(public_id, ''), 'CMPD_' || SUBSTRING(md5(clean_smiles), 1, 16)) AS public_id
        FROM {source_table}
        WHERE clean_smiles IS NOT NULL
          AND clean_smiles <> ''
        """
    )
    with open(output_file, "w", encoding="utf-8") as handle:
        handle.write("SMILES\tID\n")
        while True:
            rows = cursor.fetchmany(10000)
            if not rows:
                break
            for row in rows:
                smiles = str((row[0] if len(row) > 0 else "") or "").strip()
                public_id = str((row[1] if len(row) > 1 else "") or "").strip()
                if not smiles:
                    continue
                if not public_id:
                    public_id = f"CMPD_{total_rows + 1:09d}"
                handle.write(f"{smiles}\t{public_id}\n")
                total_rows += 1
    return total_rows


def _bootstrap_rebuild_index_from_smiles_file(
    postgres_url: str,
    *,
    schema: str,
    smiles_file: str,
    output_dir: str,
    max_heavy_atoms: int,
    skip_attachment_enrichment: bool,
    attachment_force_recompute: bool,
    fragment_jobs: int,
    index_maintenance_work_mem_mb: int,
    index_work_mem_mb: int,
    index_parallel_workers: int,
    index_commit_every_flushes: int,
    build_construct_tables: bool,
    build_constant_smiles_mol_index: bool,
) -> bool:
    logger = logging.getLogger(__name__)
    normalized_schema = _validate_pg_schema(schema)
    os.makedirs(output_dir, exist_ok=True)
    file_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bootstrap_fragdb = os.path.join(output_dir, f"{normalized_schema}_bootstrap_{file_tag}.fragdb")
    schema_fragment_cache_file = os.path.join(output_dir, f"{normalized_schema}_compound_state.cache.fragdb")
    try:
        cache_arg = schema_fragment_cache_file if os.path.exists(schema_fragment_cache_file) else ""
        if not _run_mmpdb_fragment(
            smiles_file,
            bootstrap_fragdb,
            max_heavy_atoms=max_heavy_atoms,
            fragment_jobs=fragment_jobs,
            cache_file=cache_arg,
        ):
            return False
        ok = _index_fragments_to_postgres(
            bootstrap_fragdb,
            postgres_url,
            postgres_schema=normalized_schema,
            force_rebuild_schema=True,
            properties_file=None,
            skip_attachment_enrichment=skip_attachment_enrichment,
            attachment_force_recompute=attachment_force_recompute,
            index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            index_work_mem_mb=index_work_mem_mb,
            index_parallel_workers=index_parallel_workers,
            index_commit_every_flushes=index_commit_every_flushes,
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
            detect_existing_core_tables=False,
        )
        if not ok:
            return False
        try:
            if os.path.abspath(bootstrap_fragdb) != os.path.abspath(schema_fragment_cache_file):
                shutil.copy2(bootstrap_fragdb, schema_fragment_cache_file)
            _sync_fragment_cache_meta_from_db(
                postgres_url,
                schema=normalized_schema,
                cache_fragdb=schema_fragment_cache_file,
            )
        except Exception as exc:
            logger.warning("Failed updating bootstrap fragment cache for schema '%s': %s", normalized_schema, exc)
        return True
    finally:
        try:
            if os.path.exists(bootstrap_fragdb):
                os.remove(bootstrap_fragdb)
        except Exception:
            pass


def _build_rebuild_smiles_file_from_compound_state(
    postgres_url: str,
    *,
    schema: str,
    output_dir: str,
    file_tag: str,
    include_pending_batches: bool = True,
) -> tuple[str, int]:
    normalized_schema = _validate_pg_schema(schema)
    os.makedirs(output_dir, exist_ok=True)
    smiles_file = os.path.join(output_dir, f"{normalized_schema}_compound_state_{file_tag}.smi")
    stream_name = f"compound_state_{int(datetime.utcnow().timestamp())}"
    total_rows = 0
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
        with conn.cursor(name=stream_name) as stream:
            if include_pending_batches:
                stream.execute(
                    f"""
                    WITH merged_source AS (
                        SELECT clean_smiles, public_id, 0::BIGINT AS source_priority, 0::BIGINT AS source_seq
                        FROM {COMPOUND_BATCH_BASE_TABLE}
                        UNION ALL
                        SELECT clean_smiles, public_id, 1::BIGINT AS source_priority, batch_seq AS source_seq
                        FROM {COMPOUND_BATCH_ROWS_TABLE}
                    ),
                    ranked AS (
                        SELECT
                            clean_smiles,
                            NULLIF(public_id, '') AS public_id,
                            ROW_NUMBER() OVER (
                                PARTITION BY clean_smiles
                                ORDER BY source_priority DESC, source_seq DESC
                            ) AS rn
                        FROM merged_source
                        WHERE clean_smiles IS NOT NULL AND clean_smiles <> ''
                    )
                    SELECT
                        clean_smiles,
                        COALESCE(public_id, 'CMPD_' || SUBSTRING(md5(clean_smiles), 1, 16)) AS public_id
                    FROM ranked
                    WHERE rn = 1
                    """
                )
            else:
                stream.execute(
                    """
                    SELECT
                        c.clean_smiles,
                        COALESCE(NULLIF(c.public_id, ''), 'CMPD_' || SUBSTRING(md5(c.clean_smiles), 1, 16)) AS public_id
                    FROM (
                        SELECT
                            clean_smiles,
                            public_id,
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY clean_smiles
                                ORDER BY
                                    CASE WHEN COALESCE(public_id, '') <> '' THEN 0 ELSE 1 END,
                                    id ASC
                            ) AS rn
                        FROM compound
                        WHERE clean_smiles IS NOT NULL
                          AND clean_smiles <> ''
                    ) c
                    WHERE c.rn = 1
                    """
                )
            with open(smiles_file, "w", encoding="utf-8") as handle:
                handle.write("SMILES\tID\n")
                for row in stream:
                    smiles = str(row[0] or "").strip()
                    public_id = str(row[1] or "").strip()
                    if not smiles:
                        continue
                    if not public_id:
                        public_id = f"CMPD_{total_rows + 1:09d}"
                    handle.write(f"{smiles}\t{public_id}\n")
                    total_rows += 1
        conn.commit()
    return smiles_file, total_rows


def _write_smiles_records_file(file_path: str, rows: Iterable[Tuple[str, str, str]]) -> int:
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    total = 0
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("SMILES\tID\n")
        for idx, row in enumerate(rows, start=1):
            smiles = str((row[0] if len(row) > 0 else "") or "").strip()
            public_id = str((row[1] if len(row) > 1 else "") or "").strip()
            if not smiles:
                continue
            if not public_id:
                public_id = f"CMPD_{idx:09d}"
            handle.write(f"{smiles}\t{public_id}\n")
            total += 1
    return total


def _run_mmpdb_fragment(
    smiles_file: str,
    output_fragdb: str,
    *,
    max_heavy_atoms: int,
    fragment_jobs: int,
    cache_file: str = "",
) -> bool:
    logger = logging.getLogger(__name__)
    cache_token = str(cache_file or "").strip()
    use_cache = bool(cache_token and os.path.exists(cache_token))
    cmd = [
        sys.executable,
        "-m",
        "mmpdblib",
        "fragment",
        smiles_file,
        "-o",
        output_fragdb,
        "--has-header",
    ]
    if use_cache:
        cmd.extend(["--cache", cache_token])
    else:
        cmd.extend(["--max-heavies", str(int(max_heavy_atoms))])
    if int(fragment_jobs or 0) > 0:
        cmd.extend(["-j", str(int(fragment_jobs))])
    logger.info("Running fragment: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Fragment failed: %s", result.stderr)
        if result.stdout:
            logger.error("Fragment stdout: %s", result.stdout[-1000:])
        return False
    return True


def _run_mmpdb_fragdb_constants(
    fragdb_file: str,
    output_file: str,
) -> bool:
    logger = logging.getLogger(__name__)
    cmd = [
        sys.executable,
        "-m",
        "mmpdblib",
        "fragdb_constants",
        fragdb_file,
        "-o",
        output_file,
    ]
    logger.info("Running fragdb_constants: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("fragdb_constants failed: %s", result.stderr)
        if result.stdout:
            logger.error("fragdb_constants stdout: %s", result.stdout[-1000:])
        return False
    return True


def _load_constant_counts_from_constants_file(constants_file: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not constants_file or not os.path.exists(constants_file):
        return counts
    with open(constants_file, "r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line_no == 1 and line.lower().startswith("constant\t"):
                continue
            parts = line.split("\t")
            constant = str(parts[0] or "").strip()
            if not constant:
                continue
            raw_count = str(parts[1] if len(parts) > 1 else "1").strip()
            try:
                parsed_count = int(raw_count)
            except Exception:
                parsed_count = 1
            if parsed_count < 1:
                parsed_count = 1
            previous = int(counts.get(constant) or 0)
            if parsed_count > previous:
                counts[constant] = parsed_count
    return counts


def _load_constant_smiles_from_constants_file(constants_file: str) -> List[str]:
    counts = _load_constant_counts_from_constants_file(constants_file)
    return sorted(counts.keys())


def _write_constants_file(
    constants_file: str,
    constants: Sequence[str],
    *,
    counts: Optional[Dict[str, int]] = None,
) -> int:
    values = sorted({str(item or "").strip() for item in constants if str(item or "").strip()})
    os.makedirs(os.path.dirname(constants_file) or ".", exist_ok=True)
    count_map = counts or {}
    with open(constants_file, "w", encoding="utf-8") as handle:
        handle.write("constant\tN\n")
        for token in values:
            token_count = int(count_map.get(token) or 1)
            if token_count < 1:
                token_count = 1
            handle.write(f"{token}\t{token_count}\n")
    return len(values)


def _run_mmpdb_fragdb_partition(
    *,
    cache_fragdb: str,
    delta_fragdb: str,
    constants_file: str,
    output_fragdb: str,
) -> bool:
    logger = logging.getLogger(__name__)
    os.makedirs(os.path.dirname(output_fragdb) or ".", exist_ok=True)
    partition_template = f"{output_fragdb}.partition.{{i:04}}.fragdb"
    cmd = [
        sys.executable,
        "-m",
        "mmpdblib",
        "fragdb_partition",
        cache_fragdb,
        delta_fragdb,
        "-c",
        constants_file,
        "-n",
        "1",
        "-t",
        partition_template,
    ]
    logger.info("Running fragdb_partition: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("fragdb_partition failed: %s", result.stderr)
        if result.stdout:
            logger.error("fragdb_partition stdout: %s", result.stdout[-1000:])
        return False

    partition_files = sorted(glob.glob(f"{output_fragdb}.partition.*.fragdb"))
    if not partition_files:
        logger.error(
            "fragdb_partition produced no output files for constants file: %s",
            constants_file,
        )
        return False
    first_partition = partition_files[0]
    try:
        if os.path.exists(output_fragdb):
            os.remove(output_fragdb)
        os.replace(first_partition, output_fragdb)
    finally:
        for extra_file in partition_files[1:]:
            try:
                os.remove(extra_file)
            except Exception:
                pass
    return True


def _append_delta_fragdb_into_cache(
    *,
    cache_fragdb: str,
    delta_fragdb: str,
) -> bool:
    logger = logging.getLogger(__name__)
    if not cache_fragdb or not os.path.exists(cache_fragdb):
        return False
    if not delta_fragdb or not os.path.exists(delta_fragdb):
        return False
    merged_file = f"{cache_fragdb}.merge_{datetime.utcnow().strftime('%Y%m%d_%H%M%S%f')}.fragdb"
    cmd = [
        sys.executable,
        "-m",
        "mmpdblib",
        "fragdb_merge",
        cache_fragdb,
        delta_fragdb,
        "-o",
        merged_file,
    ]
    logger.info("Updating fragment cache by fragdb_merge: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("fragdb_merge cache update failed: %s", result.stderr)
        if result.stdout:
            logger.warning("fragdb_merge cache update stdout: %s", result.stdout[-1000:])
        try:
            if os.path.exists(merged_file):
                os.remove(merged_file)
        except Exception:
            pass
        return False
    try:
        os.replace(merged_file, cache_fragdb)
    except Exception as exc:
        logger.warning("Replacing cache fragdb failed: %s", exc)
        try:
            if os.path.exists(merged_file):
                os.remove(merged_file)
        except Exception:
            pass
        return False
    return True


def _fragment_cache_meta_file(cache_fragdb: str) -> str:
    return f"{cache_fragdb}.meta.json"


def _read_fragment_cache_meta(cache_fragdb: str) -> Optional[Dict[str, int]]:
    meta_file = _fragment_cache_meta_file(cache_fragdb)
    if not meta_file or not os.path.exists(meta_file):
        return None
    try:
        with open(meta_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    output: Dict[str, int] = {}
    for key in (
        "meta_version",
        "compound_count",
        "compound_max_id",
        "batch_count",
        "batch_seq_max",
        "batch_seq_sum",
    ):
        try:
            output[key] = int(payload.get(key, 0) or 0)
        except Exception:
            output[key] = 0
    return output


def _write_fragment_cache_meta(
    cache_fragdb: str,
    *,
    signature: Dict[str, int],
) -> bool:
    logger = logging.getLogger(__name__)
    meta_file = _fragment_cache_meta_file(cache_fragdb)
    tmp_file = f"{meta_file}.tmp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S%f')}"
    payload = {
        "meta_version": 1,
        "compound_count": int(signature.get("compound_count", 0) or 0),
        "compound_max_id": int(signature.get("compound_max_id", 0) or 0),
    }
    try:
        os.makedirs(os.path.dirname(meta_file) or ".", exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        os.replace(tmp_file, meta_file)
        return True
    except Exception as exc:
        logger.warning("Failed writing fragment cache metadata %s: %s", meta_file, exc)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except Exception:
            pass
        return False


def _remove_fragment_cache_files(cache_fragdb: str, *, reason: str = "") -> None:
    logger = logging.getLogger(__name__)
    targets = [cache_fragdb, _fragment_cache_meta_file(cache_fragdb)]
    for target in targets:
        if not target or not os.path.exists(target):
            continue
        try:
            os.remove(target)
        except Exception as exc:
            logger.warning("Failed removing fragment cache file %s: %s", target, exc)
    if reason:
        logger.info("Fragment cache invalidated: %s (%s)", cache_fragdb, reason)


def _collect_compound_cache_signature(
    postgres_url: str,
    *,
    schema: str,
) -> Dict[str, int]:
    normalized_schema = _validate_pg_schema(schema)
    compound_count = -1
    compound_max_id = 0
    require_exact_count = _env_bool("LEADOPT_MMP_CACHE_SIGNATURE_REQUIRE_EXACT_COUNT", False)
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            cursor.execute("SELECT COALESCE(MAX(id), 0)::BIGINT FROM compound")
            row = cursor.fetchone() or (0,)
            compound_max_id = int(row[0] or 0)
            if require_exact_count:
                cursor.execute("SELECT COUNT(*)::BIGINT FROM compound")
                row = cursor.fetchone() or (0,)
                compound_count = int(row[0] or 0)
            conn.commit()
    return {
        "meta_version": 1,
        "compound_count": compound_count,
        "compound_max_id": compound_max_id,
    }


def _is_fragment_cache_meta_match(
    *,
    cache_fragdb: str,
    signature: Dict[str, int],
) -> bool:
    meta = _read_fragment_cache_meta(cache_fragdb)
    if not meta:
        return False
    if int(meta.get("meta_version", 0) or 0) != int(signature.get("meta_version", 0) or 0):
        return False
    if int(meta.get("compound_max_id", 0) or 0) != int(signature.get("compound_max_id", 0) or 0):
        return False
    meta_count = int(meta.get("compound_count", -1) or -1)
    sig_count = int(signature.get("compound_count", -1) or -1)
    # Allow unknown count (-1) to keep incremental runtime independent of total dataset size.
    if meta_count > 0 and sig_count > 0 and meta_count != sig_count:
        return False
    return True


def _sync_fragment_cache_meta_from_db(
    postgres_url: str,
    *,
    schema: str,
    cache_fragdb: str,
) -> bool:
    logger = logging.getLogger(__name__)
    if not cache_fragdb or not os.path.exists(cache_fragdb):
        return False
    try:
        signature = _collect_compound_cache_signature(postgres_url, schema=schema)
    except Exception as exc:
        logger.warning(
            "Failed collecting fragment cache signature for schema '%s': %s",
            schema,
            exc,
        )
        return False
    return _write_fragment_cache_meta(cache_fragdb, signature=signature)


def _compute_clean_num_heavies_by_smiles(smiles_values: Sequence[str]) -> Dict[str, int]:
    unique_smiles = sorted({str(item or "").strip() for item in smiles_values if str(item or "").strip()})
    if not unique_smiles:
        return {}
    if not HAS_RDKIT_MMPDB or Chem is None:
        return {}
    output: Dict[str, int] = {}
    for token in unique_smiles:
        try:
            mol = Chem.MolFromSmiles(token)
        except Exception:
            mol = None
        if mol is None:
            continue
        output[token] = int(mol.GetNumHeavyAtoms())
    return output


def _load_constant_smiles_from_schema(
    postgres_url: str,
    *,
    schema: str,
) -> List[str]:
    normalized_schema = _validate_pg_schema(schema)
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            cursor.execute(
                """
                SELECT smiles
                FROM constant_smiles
                WHERE smiles IS NOT NULL
                  AND smiles <> ''
                ORDER BY smiles
                """
            )
            rows = [str(row[0] or "").strip() for row in cursor.fetchall() if str(row[0] or "").strip()]
            conn.commit()
    return rows


def _load_partner_smiles_for_constants(
    postgres_url: str,
    *,
    schema: str,
    constants: Sequence[str],
    exclude_smiles: Optional[Sequence[str]] = None,
) -> List[str]:
    constants_list = sorted({str(item or "").strip() for item in constants if str(item or "").strip()})
    if not constants_list:
        return []
    exclude_list = sorted({str(item or "").strip() for item in (exclude_smiles or []) if str(item or "").strip()})
    normalized_schema = _validate_pg_schema(schema)
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_constants")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_constants (
                    smiles TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            _copy_rows(
                cursor,
                "COPY tmp_inc_constants (smiles) FROM STDIN",
                [(value,) for value in constants_list],
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_exclude_smiles")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_exclude_smiles (
                    clean_smiles TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            if exclude_list:
                _copy_rows(
                    cursor,
                    "COPY tmp_inc_exclude_smiles (clean_smiles) FROM STDIN",
                    [(value,) for value in exclude_list],
                )
            cursor.execute(
                """
                WITH touched_pairs AS (
                    SELECT p.compound1_id, p.compound2_id
                    FROM pair p
                    INNER JOIN constant_smiles cs
                            ON cs.id = p.constant_id
                    INNER JOIN tmp_inc_constants tc
                            ON tc.smiles = cs.smiles
                ),
                touched_compounds AS (
                    SELECT compound1_id AS compound_id FROM touched_pairs
                    UNION
                    SELECT compound2_id AS compound_id FROM touched_pairs
                )
                SELECT DISTINCT c.clean_smiles
                FROM touched_compounds t
                INNER JOIN compound c
                        ON c.id = t.compound_id
                LEFT JOIN tmp_inc_exclude_smiles e
                       ON e.clean_smiles = c.clean_smiles
                WHERE c.clean_smiles IS NOT NULL
                  AND c.clean_smiles <> ''
                  AND e.clean_smiles IS NULL
                ORDER BY c.clean_smiles
                """
            )
            rows = [str(row[0] or "").strip() for row in cursor.fetchall() if str(row[0] or "").strip()]
            conn.commit()
    return rows


def _split_constants_into_shards(
    constants: Sequence[str],
    shard_count: int,
    *,
    weights: Optional[Dict[str, int]] = None,
) -> List[List[str]]:
    unique_constants = sorted({str(item or "").strip() for item in constants if str(item or "").strip()})
    if not unique_constants:
        return []
    normalized_shards = max(1, int(shard_count or 1))
    if normalized_shards <= 1 or len(unique_constants) <= 1:
        return [unique_constants]
    shard_total = min(normalized_shards, len(unique_constants))
    weight_map = weights or {}
    weighted_constants = sorted(
        unique_constants,
        key=lambda token: (-int(weight_map.get(token) or 1), token),
    )
    shards: List[List[str]] = [[] for _ in range(shard_total)]
    shard_loads: List[int] = [0 for _ in range(shard_total)]
    for token in weighted_constants:
        token_weight = int(weight_map.get(token) or 1)
        if token_weight < 1:
            token_weight = 1
        shard_idx = min(range(shard_total), key=lambda idx: (shard_loads[idx], idx))
        shards[shard_idx].append(token)
        shard_loads[shard_idx] += token_weight
    return [sorted(chunk) for chunk in shards if chunk]


def _load_active_compounds_for_smiles(
    postgres_url: str,
    *,
    schema: str,
    smiles_values: Sequence[str],
) -> List[Tuple[str, str, str]]:
    unique_smiles: List[str] = []
    seen_smiles: set[str] = set()
    for item in smiles_values:
        token = str(item or "").strip()
        if not token or token in seen_smiles:
            continue
        seen_smiles.add(token)
        unique_smiles.append(token)
    if not unique_smiles:
        return []
    normalized_schema = _validate_pg_schema(schema)
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_smiles_filter")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_smiles_filter (
                    clean_smiles TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            with cursor.copy(
                "COPY tmp_inc_smiles_filter (clean_smiles) FROM STDIN"
            ) as copy:
                for value in unique_smiles:
                    copy.write_row((value,))
            cursor.execute(
                f"""
                WITH merged_source AS (
                    SELECT
                        b.clean_smiles,
                        b.public_id,
                        b.input_smiles,
                        0::BIGINT AS source_priority,
                        0::BIGINT AS source_seq
                    FROM {COMPOUND_BATCH_BASE_TABLE} b
                    INNER JOIN tmp_inc_smiles_filter f
                            ON f.clean_smiles = b.clean_smiles
                    UNION ALL
                    SELECT
                        r.clean_smiles,
                        r.public_id,
                        r.input_smiles,
                        1::BIGINT AS source_priority,
                        r.batch_seq AS source_seq
                    FROM {COMPOUND_BATCH_ROWS_TABLE} r
                    INNER JOIN tmp_inc_smiles_filter f
                            ON f.clean_smiles = r.clean_smiles
                ),
                ranked AS (
                    SELECT
                        clean_smiles,
                        NULLIF(public_id, '') AS public_id,
                        NULLIF(input_smiles, '') AS input_smiles,
                        ROW_NUMBER() OVER (
                            PARTITION BY clean_smiles
                            ORDER BY source_priority DESC, source_seq DESC
                        ) AS rn
                    FROM merged_source
                    WHERE clean_smiles IS NOT NULL
                      AND clean_smiles <> ''
                )
                SELECT
                    r.clean_smiles,
                    COALESCE(r.public_id, 'CMPD_' || SUBSTRING(md5(r.clean_smiles), 1, 16)) AS public_id,
                    COALESCE(r.input_smiles, r.clean_smiles) AS input_smiles
                FROM ranked r
                WHERE r.rn = 1
                ORDER BY r.clean_smiles
                """
            )
            rows = [
                (
                    str(row[0] or "").strip(),
                    str(row[1] or "").strip(),
                    str(row[2] or "").strip(),
                )
                for row in cursor.fetchall()
            ]
            conn.commit()
    return [row for row in rows if row[0]]


def _ensure_compound_fragment_cache(
    postgres_url: str,
    *,
    schema: str,
    output_dir: str,
    cache_file: str,
    max_heavy_atoms: int,
    fragment_jobs: int,
) -> bool:
    logger = logging.getLogger(__name__)
    allow_cache_reseed = _env_bool("LEADOPT_MMP_ALLOW_CACHE_RESEED", False)
    if os.path.exists(cache_file):
        try:
            signature = _collect_compound_cache_signature(postgres_url, schema=schema)
        except Exception as exc:
            logger.error(
                "Failed collecting fragment cache signature for schema '%s': %s",
                schema,
                exc,
            )
            return False
        if _is_fragment_cache_meta_match(
            cache_fragdb=cache_file,
            signature=signature,
        ):
            return True
        if not allow_cache_reseed:
            logger.error(
                "Fragment cache metadata mismatch for schema '%s' and strict incremental mode is enabled. "
                "Refusing full-cache reseed to keep batch runtime independent of total library size.",
                schema,
            )
            return False
        _remove_fragment_cache_files(cache_file, reason="metadata mismatch")
    elif not allow_cache_reseed:
        logger.error(
            "Fragment cache file is missing for schema '%s' and strict incremental mode is enabled. "
            "Refusing full-cache reseed to keep batch runtime independent of total library size.",
            schema,
        )
        return False
    file_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    merged_smiles_file, merged_count = _build_rebuild_smiles_file_from_compound_state(
        postgres_url,
        schema=schema,
        output_dir=output_dir,
        file_tag=f"cache_seed_{file_tag}",
        include_pending_batches=False,
    )
    if merged_count <= 0:
        logger.error("Cannot seed fragment cache for schema '%s': no active compounds.", schema)
        return False
    logger.info(
        "Seeding compound fragment cache for schema '%s' from active lifecycle state: compounds=%s file=%s",
        schema,
        merged_count,
        merged_smiles_file,
    )
    try:
        ok = _run_mmpdb_fragment(
            merged_smiles_file,
            cache_file,
            max_heavy_atoms=max_heavy_atoms,
            fragment_jobs=fragment_jobs,
            cache_file="",
        )
        if ok:
            _sync_fragment_cache_meta_from_db(
                postgres_url,
                schema=schema,
                cache_fragdb=cache_file,
            )
        return ok
    finally:
        try:
            os.remove(merged_smiles_file)
        except Exception:
            pass


def _drop_postgres_schema_if_exists(postgres_url: str, schema: str) -> None:
    normalized_schema = _validate_pg_schema(schema)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(normalized_schema)))


def _sync_candidate_compounds_from_active_rows(
    cursor,
    *,
    candidate_smiles: Sequence[str],
    active_rows: Sequence[Tuple[str, str, str]],
) -> None:
    candidate_values = sorted({str(item or "").strip() for item in candidate_smiles if str(item or "").strip()})
    active_values = []
    for row in active_rows:
        clean_smiles = str((row[0] if len(row) > 0 else "") or "").strip()
        public_id = str((row[1] if len(row) > 1 else "") or "").strip()
        input_smiles = str((row[2] if len(row) > 2 else "") or "").strip()
        if not clean_smiles:
            continue
        if not public_id:
            public_id = f"CMPD_{abs(hash(clean_smiles)) % 1000000000:09d}"
        if not input_smiles:
            input_smiles = clean_smiles
        active_values.append((clean_smiles, public_id, input_smiles))

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_candidate_smiles")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_candidate_smiles (
            clean_smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    if candidate_values:
        _copy_rows(
            cursor,
            "COPY tmp_inc_candidate_smiles (clean_smiles) FROM STDIN",
            [(value,) for value in candidate_values],
        )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_active_compounds")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_active_compounds (
            clean_smiles TEXT PRIMARY KEY,
            public_id TEXT NOT NULL,
            input_smiles TEXT NOT NULL
        ) ON COMMIT DROP
        """
    )
    if active_values:
        _copy_rows(
            cursor,
            "COPY tmp_inc_active_compounds (clean_smiles, public_id, input_smiles) FROM STDIN",
            active_values,
        )

    cursor.execute(
        """
        UPDATE compound c
           SET input_smiles = a.input_smiles
          FROM tmp_inc_active_compounds a
         WHERE c.clean_smiles = a.clean_smiles
           AND COALESCE(NULLIF(a.input_smiles, ''), '') <> ''
           AND c.input_smiles IS DISTINCT FROM a.input_smiles
        """
    )
    cursor.execute(
        """
        UPDATE compound c
           SET public_id = a.public_id
          FROM tmp_inc_active_compounds a
         WHERE c.clean_smiles = a.clean_smiles
           AND a.public_id <> ''
           AND c.public_id IS DISTINCT FROM a.public_id
           AND NOT EXISTS (
                SELECT 1
                FROM compound c2
                WHERE c2.public_id = a.public_id
                  AND c2.id <> c.id
            )
        """
    )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_obsolete_compound_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_obsolete_compound_ids (
            id INTEGER PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    cursor.execute(
        """
        INSERT INTO tmp_inc_obsolete_compound_ids (id)
        SELECT c.id
        FROM compound c
        INNER JOIN tmp_inc_candidate_smiles t
                ON t.clean_smiles = c.clean_smiles
        LEFT JOIN tmp_inc_active_compounds a
               ON a.clean_smiles = c.clean_smiles
        WHERE a.clean_smiles IS NULL
          AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.compound1_id = c.id
            )
          AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.compound2_id = c.id
            )
        ON CONFLICT DO NOTHING
        """
    )

    cursor.execute(
        """
        DELETE FROM compound_property cp
        WHERE cp.compound_id IN (SELECT id FROM tmp_inc_obsolete_compound_ids)
        """
    )
    cursor.execute(
        """
        DELETE FROM compound c
        WHERE c.id IN (SELECT id FROM tmp_inc_obsolete_compound_ids)
        """
    )


def _cleanup_orphan_mmp_rows(cursor) -> None:
    cursor.execute(
        """
        DELETE FROM rule_environment_statistics rs
        WHERE NOT EXISTS (
            SELECT 1
            FROM rule_environment re
            WHERE re.id = rs.rule_environment_id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule_environment re
        WHERE NOT EXISTS (
            SELECT 1
            FROM pair p
            WHERE p.rule_environment_id = re.id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule r
        WHERE NOT EXISTS (
            SELECT 1
            FROM rule_environment re
            WHERE re.rule_id = r.id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM environment_fingerprint ef
        WHERE NOT EXISTS (
            SELECT 1
            FROM rule_environment re
            WHERE re.environment_fingerprint_id = ef.id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule_smiles rs
        WHERE NOT EXISTS (
            SELECT 1
            FROM rule r
            WHERE r.from_smiles_id = rs.id
        )
          AND NOT EXISTS (
            SELECT 1
            FROM rule r
            WHERE r.to_smiles_id = rs.id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM constant_smiles cs
        WHERE NOT EXISTS (
            SELECT 1
            FROM pair p
            WHERE p.constant_id = cs.id
        )
        """
    )
    cursor.execute(
        """
        DELETE FROM compound_property cp
        WHERE NOT EXISTS (
            SELECT 1
            FROM compound c
            WHERE c.id = cp.compound_id
        )
        """
    )


def _cleanup_orphan_mmp_rows_for_constants(cursor, *, affected_constants: Sequence[str]) -> None:
    constants = sorted({str(item or "").strip() for item in affected_constants if str(item or "").strip()})
    if not constants:
        return
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_cleanup_constants")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_cleanup_constants (
            smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    _copy_rows(
        cursor,
        "COPY tmp_inc_cleanup_constants (smiles) FROM STDIN",
        [(value,) for value in constants],
    )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_cleanup_rule_env_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_cleanup_rule_env_ids (
            id INTEGER PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    cursor.execute(
        """
        INSERT INTO tmp_inc_cleanup_rule_env_ids (id)
        SELECT DISTINCT p.rule_environment_id AS id
        FROM pair p
        INNER JOIN constant_smiles cs
                ON cs.id = p.constant_id
        INNER JOIN tmp_inc_cleanup_constants c
                ON c.smiles = cs.smiles
        """
    )
    # Include ids tracked before pair delete in current transaction (if available).
    cursor.execute("SELECT to_regclass('tmp_inc_removed_rule_env_ids')")
    if cursor.fetchone()[0] is not None:
        cursor.execute(
            """
            INSERT INTO tmp_inc_cleanup_rule_env_ids (id)
            SELECT rule_environment_id AS id FROM tmp_inc_removed_rule_env_ids
            ON CONFLICT DO NOTHING
            """
        )

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_cleanup_rule_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_cleanup_rule_ids AS
        SELECT DISTINCT re.rule_id AS id
        FROM rule_environment re
        INNER JOIN tmp_inc_cleanup_rule_env_ids t
                ON t.id = re.id
        """
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_cleanup_env_fp_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_cleanup_env_fp_ids AS
        SELECT DISTINCT re.environment_fingerprint_id AS id
        FROM rule_environment re
        INNER JOIN tmp_inc_cleanup_rule_env_ids t
                ON t.id = re.id
        """
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_cleanup_rule_smiles_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_cleanup_rule_smiles_ids AS
        SELECT DISTINCT r.from_smiles_id AS id
        FROM rule r
        INNER JOIN tmp_inc_cleanup_rule_ids t
                ON t.id = r.id
        UNION
        SELECT DISTINCT r.to_smiles_id AS id
        FROM rule r
        INNER JOIN tmp_inc_cleanup_rule_ids t
                ON t.id = r.id
        """
    )

    cursor.execute(
        """
        DELETE FROM rule_environment_statistics rs
        WHERE rs.rule_environment_id IN (SELECT id FROM tmp_inc_cleanup_rule_env_ids)
          AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.rule_environment_id = rs.rule_environment_id
            )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule_environment re
        WHERE re.id IN (SELECT id FROM tmp_inc_cleanup_rule_env_ids)
          AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.rule_environment_id = re.id
            )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule r
        WHERE r.id IN (SELECT id FROM tmp_inc_cleanup_rule_ids)
          AND NOT EXISTS (
                SELECT 1
                FROM rule_environment re
                WHERE re.rule_id = r.id
            )
        """
    )
    cursor.execute(
        """
        DELETE FROM environment_fingerprint ef
        WHERE ef.id IN (SELECT id FROM tmp_inc_cleanup_env_fp_ids)
          AND NOT EXISTS (
                SELECT 1
                FROM rule_environment re
                WHERE re.environment_fingerprint_id = ef.id
            )
        """
    )
    cursor.execute(
        """
        DELETE FROM rule_smiles rs
        WHERE rs.id IN (SELECT id FROM tmp_inc_cleanup_rule_smiles_ids)
          AND NOT EXISTS (
                SELECT 1
                FROM rule r
                WHERE r.from_smiles_id = rs.id
            )
          AND NOT EXISTS (
                SELECT 1
                FROM rule r
                WHERE r.to_smiles_id = rs.id
            )
        """
    )
    cursor.execute(
        """
        DELETE FROM constant_smiles cs
        WHERE cs.smiles IN (SELECT smiles FROM tmp_inc_cleanup_constants)
          AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.constant_id = cs.id
            )
        """
    )


def _update_rule_environment_counts_for_constants(cursor, *, affected_constants: Sequence[str]) -> None:
    constants = sorted({str(item or "").strip() for item in affected_constants if str(item or "").strip()})
    if not constants:
        return
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_touched_rule_env_ids")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_touched_rule_env_ids (
            id INTEGER PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_recount_constants")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_recount_constants (
            smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    _copy_rows(
        cursor,
        "COPY tmp_inc_recount_constants (smiles) FROM STDIN",
        [(value,) for value in constants],
    )
    cursor.execute(
        """
        INSERT INTO tmp_inc_touched_rule_env_ids (id)
        SELECT DISTINCT p.rule_environment_id AS id
        FROM pair p
        INNER JOIN constant_smiles cs
                ON cs.id = p.constant_id
        INNER JOIN tmp_inc_recount_constants c
                ON c.smiles = cs.smiles
        ON CONFLICT DO NOTHING
        """
    )
    cursor.execute("SELECT to_regclass('tmp_inc_removed_rule_env_ids_all')")
    if cursor.fetchone()[0] is not None:
        cursor.execute(
            """
            INSERT INTO tmp_inc_touched_rule_env_ids (id)
            SELECT DISTINCT rule_environment_id AS id
            FROM tmp_inc_removed_rule_env_ids_all
            ON CONFLICT DO NOTHING
            """
        )
    else:
        cursor.execute("SELECT to_regclass('tmp_inc_removed_rule_env_ids')")
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                """
                INSERT INTO tmp_inc_touched_rule_env_ids (id)
                SELECT DISTINCT rule_environment_id AS id
                FROM tmp_inc_removed_rule_env_ids
                ON CONFLICT DO NOTHING
                """
            )

    cursor.execute(
        """
        UPDATE rule_environment re
           SET num_pairs = stats.num_pairs
          FROM (
              SELECT p.rule_environment_id AS id, COUNT(*)::INTEGER AS num_pairs
              FROM pair p
              INNER JOIN tmp_inc_touched_rule_env_ids t
                      ON t.id = p.rule_environment_id
              GROUP BY p.rule_environment_id
          ) stats
         WHERE re.id = stats.id
        """
    )
    cursor.execute(
        """
        UPDATE rule_environment re
           SET num_pairs = 0
         WHERE re.id IN (SELECT id FROM tmp_inc_touched_rule_env_ids)
           AND NOT EXISTS (
                SELECT 1
                FROM pair p
                WHERE p.rule_environment_id = re.id
            )
        """
    )


def _update_dataset_counts(cursor) -> None:
    cursor.execute(
        """
        UPDATE dataset
           SET num_compounds = (SELECT COUNT(*) FROM compound),
               num_rules = (SELECT COUNT(*) FROM rule),
               num_pairs = (SELECT COUNT(*) FROM pair),
               num_rule_environments = (SELECT COUNT(*) FROM rule_environment),
               num_rule_environment_stats = (SELECT COUNT(*) FROM rule_environment_statistics)
         WHERE id = 1
        """
    )


def _ensure_pair_compound_lookup_indexes(cursor) -> None:
    statements: List[Tuple[str, str]] = [
        ("idx_pair_compound1", "CREATE INDEX IF NOT EXISTS idx_pair_compound1 ON pair(compound1_id)"),
        ("idx_pair_compound2", "CREATE INDEX IF NOT EXISTS idx_pair_compound2 ON pair(compound2_id)"),
    ]
    existing = _current_schema_index_names(cursor)
    missing = [name for name, _ in statements if name not in existing]
    if missing:
        _guard_large_index_auto_build(
            pair_rows_est=_estimate_current_schema_table_rows(cursor, "pair"),
            missing_indexes=missing,
            guard_label="pair lookup indexes",
        )
    _ensure_named_indexes(cursor, statements, existing_indexes=existing)


def _ensure_incremental_lookup_indexes(cursor) -> None:
    statements: List[Tuple[str, str]] = [
        ("idx_compound_clean_smiles", "CREATE INDEX IF NOT EXISTS idx_compound_clean_smiles ON compound(clean_smiles)"),
        ("idx_rule_from_to", "CREATE INDEX IF NOT EXISTS idx_rule_from_to ON rule(from_smiles_id, to_smiles_id)"),
        ("idx_constant_smiles_smiles", "CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles ON constant_smiles(smiles)"),
        ("idx_env_fp_lookup", "CREATE INDEX IF NOT EXISTS idx_env_fp_lookup ON environment_fingerprint(smarts, pseudosmiles, parent_smarts)"),
        ("idx_rule_environment_lookup", "CREATE INDEX IF NOT EXISTS idx_rule_environment_lookup ON rule_environment(rule_id, environment_fingerprint_id, radius)"),
        ("idx_pair_lookup_exact", "CREATE INDEX IF NOT EXISTS idx_pair_lookup_exact ON pair(rule_environment_id, compound1_id, compound2_id, constant_id)"),
        ("idx_pair_compound1", "CREATE INDEX IF NOT EXISTS idx_pair_compound1 ON pair(compound1_id)"),
        ("idx_pair_compound2", "CREATE INDEX IF NOT EXISTS idx_pair_compound2 ON pair(compound2_id)"),
    ]
    existing = _current_schema_index_names(cursor)
    missing = [name for name, _ in statements if name not in existing]
    if missing:
        _guard_large_index_auto_build(
            pair_rows_est=_estimate_current_schema_table_rows(cursor, "pair"),
            missing_indexes=missing,
            guard_label="incremental lookup indexes",
        )
    _ensure_named_indexes(cursor, statements, existing_indexes=existing)


def _should_refresh_incremental_dataset_counts(*, skip_incremental_analyze: bool) -> bool:
    default_refresh = not bool(skip_incremental_analyze)
    return _env_bool("MMP_LIFECYCLE_INCREMENTAL_REFRESH_DATASET_COUNTS", default=default_refresh)


def _should_auto_ensure_incremental_lookup_indexes() -> bool:
    return _env_bool("MMP_LIFECYCLE_AUTO_ENSURE_LOOKUP_INDEXES", default=True)


def _should_run_incremental_core_index_ensure() -> bool:
    # Incremental merge already guarantees required lookup indexes.
    # Full core index ensure is expensive on very large datasets and usually redundant per batch.
    return _env_bool("MMP_LIFECYCLE_INCREMENTAL_ENSURE_CORE_INDEXES", default=False)


def _should_align_incremental_id_sequences() -> bool:
    # Keep per-batch sequence alignment enabled by default for correctness,
    # while using index-tail ID lookup to avoid full-table scans.
    return _env_bool("MMP_LIFECYCLE_INCREMENTAL_ALIGN_SEQUENCES", default=True)


def _should_auto_analyze_incremental_tables() -> bool:
    # Keep query planner statistics fresh without paying full ANALYZE cost every batch.
    return _env_bool("MMP_LIFECYCLE_INCREMENTAL_AUTO_ANALYZE", default=True)


def _maybe_auto_analyze_incremental_tables(cursor, table_names: Sequence[str]) -> List[str]:
    if not _should_auto_analyze_incremental_tables():
        return []
    min_mods = max(1, _env_int("MMP_LIFECYCLE_INCREMENTAL_AUTO_ANALYZE_MIN_MODS", 3000))
    abs_threshold = max(min_mods, _env_int("MMP_LIFECYCLE_INCREMENTAL_AUTO_ANALYZE_ABS_MODS", 50000))
    rel_threshold = max(0.0, _env_float("MMP_LIFECYCLE_INCREMENTAL_AUTO_ANALYZE_REL_THRESHOLD", 0.02))
    analyzed: List[str] = []
    seen: set[str] = set()
    for table_name in table_names:
        token = str(table_name or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        cursor.execute(
            """
            SELECT
                COALESCE(c.reltuples, 0)::BIGINT AS rel_rows,
                COALESCE(s.n_mod_since_analyze, 0)::BIGINT AS n_mod_since_analyze,
                (s.last_analyze IS NULL AND s.last_autoanalyze IS NULL) AS never_analyzed
            FROM pg_class c
            LEFT JOIN pg_stat_user_tables s
                   ON s.relid = c.oid
            WHERE c.oid = to_regclass(%s)
            """,
            [token],
        )
        row = cursor.fetchone()
        if row is None:
            continue
        rel_rows = max(0, int(row[0] or 0))
        modified_rows = max(0, int(row[1] or 0))
        never_analyzed = bool(row[2])
        if modified_rows <= 0:
            continue
        if never_analyzed and modified_rows >= min_mods:
            cursor.execute(f"ANALYZE {token}")
            analyzed.append(token)
            continue
        if modified_rows < min_mods:
            continue
        if modified_rows >= abs_threshold:
            cursor.execute(f"ANALYZE {token}")
            analyzed.append(token)
            continue
        if rel_rows > 0 and (float(modified_rows) / float(rel_rows)) >= rel_threshold:
            cursor.execute(f"ANALYZE {token}")
            analyzed.append(token)
    if analyzed:
        logging.getLogger(__name__).info(
            "Incremental auto-analyze triggered for tables: %s",
            ", ".join(analyzed),
        )
    return analyzed


def _finalize_incremental_merge_state(
    cursor,
    *,
    affected_constants: Sequence[str],
    candidate_smiles: Sequence[str],
    active_rows: Sequence[Tuple[str, str, str]],
    recount_constants: bool = True,
) -> None:
    _sync_candidate_compounds_from_active_rows(
        cursor,
        candidate_smiles=candidate_smiles,
        active_rows=active_rows,
    )
    if recount_constants:
        _cleanup_orphan_mmp_rows_for_constants(
            cursor,
            affected_constants=affected_constants,
        )
        _update_rule_environment_counts_for_constants(
            cursor,
            affected_constants=affected_constants,
        )


def _merge_incremental_temp_schema_into_target(
    cursor,
    *,
    temp_schema: str,
    affected_constants: Sequence[str],
    candidate_smiles: Sequence[str],
    active_rows: Sequence[Tuple[str, str, str]],
    replace_existing_pairs_for_constants: bool = True,
    skip_finalize: bool = False,
    align_sequences: bool = True,
) -> None:
    merge_debug_stats = _env_bool("MMP_LIFECYCLE_INCREMENTAL_DEBUG_STATS", default=False)
    _logger = logging.getLogger(__name__)
    constants = sorted({str(item or "").strip() for item in affected_constants if str(item or "").strip()})
    candidate_values = sorted({str(item or "").strip() for item in candidate_smiles if str(item or "").strip()})
    active_smiles = sorted(
        {
            str((row[0] if len(row) > 0 else "") or "").strip()
            for row in active_rows
            if str((row[0] if len(row) > 0 else "") or "").strip()
        }
    )
    if not constants:
        return
    if align_sequences:
        if _should_align_incremental_id_sequences():
            for table_name in (
                "compound",
                "rule_smiles",
                "rule",
                "environment_fingerprint",
                "rule_environment",
                "constant_smiles",
                "pair",
                "property_name",
                "rule_environment_statistics",
                "compound_property",
            ):
                _pg_align_id_sequence(cursor, table_name)
        if _should_auto_ensure_incremental_lookup_indexes():
            _ensure_incremental_lookup_indexes(cursor)

    cursor.execute("DROP TABLE IF EXISTS tmp_inc_affected_constants")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_affected_constants (
            smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    _copy_rows(
        cursor,
        "COPY tmp_inc_affected_constants (smiles) FROM STDIN",
        [(value,) for value in constants],
    )
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_merge_candidate_smiles")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_merge_candidate_smiles (
            clean_smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    if candidate_values:
        _copy_rows(
            cursor,
            "COPY tmp_inc_merge_candidate_smiles (clean_smiles) FROM STDIN",
            [(value,) for value in candidate_values],
        )
    cursor.execute("DROP TABLE IF EXISTS tmp_inc_merge_active_smiles")
    cursor.execute(
        """
        CREATE TEMP TABLE tmp_inc_merge_active_smiles (
            clean_smiles TEXT PRIMARY KEY
        ) ON COMMIT DROP
        """
    )
    if active_smiles:
        _copy_rows(
            cursor,
            "COPY tmp_inc_merge_active_smiles (clean_smiles) FROM STDIN",
            [(value,) for value in active_smiles],
        )

    if replace_existing_pairs_for_constants:
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_removed_rule_env_ids")
        cursor.execute(
            """
            CREATE TEMP TABLE tmp_inc_removed_rule_env_ids AS
            SELECT DISTINCT p.rule_environment_id
            FROM pair p
            INNER JOIN constant_smiles cs
                    ON cs.id = p.constant_id
            INNER JOIN tmp_inc_affected_constants c
                    ON c.smiles = cs.smiles
            """
        )
        cursor.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS tmp_inc_removed_rule_env_ids_all (
                rule_environment_id INTEGER PRIMARY KEY
            ) ON COMMIT DROP
            """
        )
        cursor.execute(
            """
            INSERT INTO tmp_inc_removed_rule_env_ids_all (rule_environment_id)
            SELECT DISTINCT rule_environment_id
            FROM tmp_inc_removed_rule_env_ids
            ON CONFLICT DO NOTHING
            """
        )
        cursor.execute(
            """
            DELETE FROM pair p
            USING constant_smiles cs,
                  tmp_inc_affected_constants c
            WHERE p.constant_id = cs.id
              AND cs.smiles = c.smiles
            """
        )
        cursor.execute(
            """
            DELETE FROM constant_smiles cs
            USING tmp_inc_affected_constants c
            WHERE cs.smiles = c.smiles
            """
        )

    normalized_temp_schema = _validate_pg_schema(temp_schema) if temp_schema else ""
    if normalized_temp_schema:
        ts = normalized_temp_schema
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_rule_env_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_rule_env_ids AS
            SELECT DISTINCT p.rule_environment_id AS id
            FROM {ts}.pair p
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_rule_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_rule_ids AS
            SELECT DISTINCT re.rule_id AS id
            FROM {ts}.rule_environment re
            INNER JOIN tmp_inc_temp_used_rule_env_ids t
                    ON t.id = re.id
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_constant_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_constant_ids AS
            SELECT DISTINCT p.constant_id AS id
            FROM {ts}.pair p
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_compound_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_compound_ids AS
            WITH used_compounds AS (
                SELECT DISTINCT p.compound1_id AS id
                FROM {ts}.pair p
                UNION
                SELECT DISTINCT p.compound2_id AS id
                FROM {ts}.pair p
            ),
            dedup_by_smiles AS (
                SELECT
                    c.clean_smiles,
                    MIN(c.id) AS id
                FROM {ts}.compound c
                INNER JOIN used_compounds u
                        ON u.id = c.id
                GROUP BY c.clean_smiles
            )
            SELECT id
            FROM dedup_by_smiles
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_rule_smiles_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_rule_smiles_ids AS
            SELECT DISTINCT r.from_smiles_id AS id
            FROM {ts}.rule r
            INNER JOIN tmp_inc_temp_used_rule_ids t
                    ON t.id = r.id
            UNION
            SELECT DISTINCT r.to_smiles_id AS id
            FROM {ts}.rule r
            INNER JOIN tmp_inc_temp_used_rule_ids t
                    ON t.id = r.id
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_env_fp_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_env_fp_ids AS
            SELECT DISTINCT re.environment_fingerprint_id AS id
            FROM {ts}.rule_environment re
            INNER JOIN tmp_inc_temp_used_rule_env_ids t
                    ON t.id = re.id
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_temp_used_property_name_ids")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_temp_used_property_name_ids AS
            SELECT DISTINCT s.property_name_id AS id
            FROM {ts}.rule_environment_statistics s
            INNER JOIN tmp_inc_temp_used_rule_env_ids t
                    ON t.id = s.rule_environment_id
            """
        )
        cursor.execute(
            f"""
            INSERT INTO compound (public_id, input_smiles, clean_smiles, clean_num_heavies)
            SELECT t.public_id, t.input_smiles, t.clean_smiles, t.clean_num_heavies
            FROM {ts}.compound t
            INNER JOIN tmp_inc_temp_used_compound_ids u
                    ON u.id = t.id
            LEFT JOIN compound c
                   ON c.clean_smiles = t.clean_smiles
            LEFT JOIN tmp_inc_merge_active_smiles act
                   ON act.clean_smiles = t.clean_smiles
            WHERE c.id IS NULL
              AND act.clean_smiles IS NOT NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_compound")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_map_compound AS
            SELECT
                t.id AS temp_id,
                MIN(c.id) AS main_id
            FROM {ts}.compound t
            INNER JOIN tmp_inc_temp_used_compound_ids u
                    ON u.id = t.id
            INNER JOIN compound c
                    ON c.clean_smiles = t.clean_smiles
            GROUP BY t.id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_compound_temp_idx ON tmp_map_compound(temp_id)")
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_candidate_compound_ids")
        cursor.execute(
            """
            CREATE TEMP TABLE tmp_inc_candidate_compound_ids (
                id INTEGER PRIMARY KEY
            ) ON COMMIT DROP
            """
        )
        cursor.execute(
            """
            INSERT INTO tmp_inc_candidate_compound_ids (id)
            SELECT DISTINCT c.id
            FROM compound c
            INNER JOIN tmp_inc_merge_candidate_smiles t
                    ON t.clean_smiles = c.clean_smiles
            ON CONFLICT DO NOTHING
            """
        )
        if merge_debug_stats:
            cursor.execute(
                """
                SELECT
                    COUNT(*)::BIGINT AS mapped_rows,
                    COUNT(DISTINCT temp_id)::BIGINT AS mapped_temp_ids,
                    COUNT(DISTINCT main_id)::BIGINT AS mapped_main_ids
                FROM tmp_map_compound
                """
            )
            _map_stats = cursor.fetchone()
            if _map_stats:
                _logger.info(
                    "Incremental merge mapping: temp_compound_rows=%s temp_ids=%s main_ids=%s",
                    int(_map_stats[0] or 0),
                    int(_map_stats[1] or 0),
                    int(_map_stats[2] or 0),
                )
            cursor.execute(
                """
                SELECT COUNT(*)::BIGINT
                FROM (
                    SELECT main_id
                    FROM tmp_map_compound
                    GROUP BY main_id
                    HAVING COUNT(*) > 1
                ) dup
                """
            )
            _compound_map_collisions = int(cursor.fetchone()[0] or 0)
            if _compound_map_collisions > 0:
                _logger.warning(
                    "Incremental merge mapping collisions: %s main compound IDs map from >1 temp compound IDs.",
                    _compound_map_collisions,
                )

        cursor.execute(
            f"""
            INSERT INTO rule_smiles (smiles, num_heavies)
            SELECT t.smiles, t.num_heavies
            FROM {ts}.rule_smiles t
            INNER JOIN tmp_inc_temp_used_rule_smiles_ids u
                    ON u.id = t.id
            LEFT JOIN rule_smiles m
                   ON m.smiles = t.smiles
            WHERE m.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_rule_smiles")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_map_rule_smiles AS
            SELECT
                t.id AS temp_id,
                MIN(m.id) AS main_id
            FROM {ts}.rule_smiles t
            INNER JOIN tmp_inc_temp_used_rule_smiles_ids u
                    ON u.id = t.id
            INNER JOIN rule_smiles m
                    ON m.smiles = t.smiles
            GROUP BY t.id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_rule_smiles_temp_idx ON tmp_map_rule_smiles(temp_id)")

        cursor.execute("DROP TABLE IF EXISTS tmp_inc_rule_keys")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_rule_keys AS
            SELECT
                t.id AS temp_id,
                frm.main_id AS from_id,
                tos.main_id AS to_id
            FROM {ts}.rule t
            INNER JOIN tmp_inc_temp_used_rule_ids ur
                    ON ur.id = t.id
            INNER JOIN tmp_map_rule_smiles frm
                    ON frm.temp_id = t.from_smiles_id
            INNER JOIN tmp_map_rule_smiles tos
                    ON tos.temp_id = t.to_smiles_id
            """
        )
        cursor.execute(
            """
            INSERT INTO rule (from_smiles_id, to_smiles_id)
            SELECT rk.from_id, rk.to_id
            FROM tmp_inc_rule_keys rk
            LEFT JOIN rule r
                   ON r.from_smiles_id = rk.from_id
                  AND r.to_smiles_id = rk.to_id
            WHERE r.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_rule")
        cursor.execute(
            """
            CREATE TEMP TABLE tmp_map_rule AS
            SELECT
                rk.temp_id,
                MIN(r.id) AS main_id
            FROM tmp_inc_rule_keys rk
            INNER JOIN rule r
                    ON r.from_smiles_id = rk.from_id
                   AND r.to_smiles_id = rk.to_id
            GROUP BY rk.temp_id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_rule_temp_idx ON tmp_map_rule(temp_id)")

        cursor.execute(
            f"""
            INSERT INTO environment_fingerprint (smarts, pseudosmiles, parent_smarts)
            SELECT t.smarts, t.pseudosmiles, t.parent_smarts
            FROM {ts}.environment_fingerprint t
            INNER JOIN tmp_inc_temp_used_env_fp_ids u
                    ON u.id = t.id
            LEFT JOIN environment_fingerprint m
                   ON m.smarts = t.smarts
                  AND m.pseudosmiles = t.pseudosmiles
                  AND m.parent_smarts = t.parent_smarts
            WHERE m.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_env_fp")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_map_env_fp AS
            SELECT
                t.id AS temp_id,
                MIN(m.id) AS main_id
            FROM {ts}.environment_fingerprint t
            INNER JOIN tmp_inc_temp_used_env_fp_ids u
                    ON u.id = t.id
            INNER JOIN environment_fingerprint m
                    ON m.smarts = t.smarts
                   AND m.pseudosmiles = t.pseudosmiles
                   AND m.parent_smarts = t.parent_smarts
            GROUP BY t.id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_env_fp_temp_idx ON tmp_map_env_fp(temp_id)")

        cursor.execute("DROP TABLE IF EXISTS tmp_inc_rule_env_keys")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_rule_env_keys AS
            SELECT
                t.id AS temp_id,
                mr.main_id AS rule_id,
                me.main_id AS env_fp_id,
                t.radius AS radius
            FROM {ts}.rule_environment t
            INNER JOIN tmp_inc_temp_used_rule_env_ids tu
                    ON tu.id = t.id
            INNER JOIN tmp_map_rule mr
                    ON mr.temp_id = t.rule_id
            INNER JOIN tmp_map_env_fp me
                    ON me.temp_id = t.environment_fingerprint_id
            """
        )
        cursor.execute(
            """
            INSERT INTO rule_environment (rule_id, environment_fingerprint_id, radius, num_pairs)
            SELECT k.rule_id, k.env_fp_id, k.radius, 0
            FROM tmp_inc_rule_env_keys k
            LEFT JOIN rule_environment re
                   ON re.rule_id = k.rule_id
                  AND re.environment_fingerprint_id = k.env_fp_id
                  AND re.radius = k.radius
            WHERE re.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_rule_env")
        cursor.execute(
            """
            CREATE TEMP TABLE tmp_map_rule_env AS
            SELECT
                k.temp_id,
                MIN(re.id) AS main_id
            FROM tmp_inc_rule_env_keys k
            INNER JOIN rule_environment re
                    ON re.rule_id = k.rule_id
                   AND re.environment_fingerprint_id = k.env_fp_id
                   AND re.radius = k.radius
            GROUP BY k.temp_id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_rule_env_temp_idx ON tmp_map_rule_env(temp_id)")

        cursor.execute(
            f"""
            INSERT INTO constant_smiles (smiles)
            SELECT t.smiles
            FROM {ts}.constant_smiles t
            INNER JOIN tmp_inc_temp_used_constant_ids uc
                    ON uc.id = t.id
            LEFT JOIN constant_smiles m
                   ON m.smiles = t.smiles
            WHERE m.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_constant")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_map_constant AS
            SELECT
                t.id AS temp_id,
                MIN(m.id) AS main_id
            FROM {ts}.constant_smiles t
            INNER JOIN tmp_inc_temp_used_constant_ids uc
                    ON uc.id = t.id
            INNER JOIN constant_smiles m
                    ON m.smiles = t.smiles
            GROUP BY t.id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_constant_temp_idx ON tmp_map_constant(temp_id)")

        if replace_existing_pairs_for_constants:
            if merge_debug_stats:
                cursor.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {ts}.pair p
                    INNER JOIN tmp_map_rule_env mre
                            ON mre.temp_id = p.rule_environment_id
                    INNER JOIN tmp_map_compound mc1
                            ON mc1.temp_id = p.compound1_id
                    INNER JOIN tmp_map_compound mc2
                            ON mc2.temp_id = p.compound2_id
                    INNER JOIN tmp_map_constant mcs
                            ON mcs.temp_id = p.constant_id
                    WHERE mc1.main_id IN (SELECT id FROM tmp_inc_candidate_compound_ids)
                       OR mc2.main_id IN (SELECT id FROM tmp_inc_candidate_compound_ids)
                    """
                )
                _logger.info(
                    "Incremental merge replace-mode candidate mapped pairs: %s",
                    int(cursor.fetchone()[0] or 0),
                )
            cursor.execute(
                f"""
                INSERT INTO pair (rule_environment_id, compound1_id, compound2_id, constant_id)
                SELECT
                    mre.main_id AS rule_environment_id,
                    mc1.main_id AS compound1_id,
                    mc2.main_id AS compound2_id,
                    mcs.main_id AS constant_id
                FROM {ts}.pair p
                INNER JOIN tmp_map_rule_env mre
                        ON mre.temp_id = p.rule_environment_id
                INNER JOIN tmp_map_compound mc1
                        ON mc1.temp_id = p.compound1_id
                INNER JOIN tmp_map_compound mc2
                        ON mc2.temp_id = p.compound2_id
                INNER JOIN tmp_map_constant mcs
                        ON mcs.temp_id = p.constant_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc1
                       ON cc1.id = mc1.main_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc2
                       ON cc2.id = mc2.main_id
                WHERE (cc1.id IS NOT NULL OR cc2.id IS NOT NULL)
                  AND NOT EXISTS (
                        SELECT 1
                        FROM pair existing
                        WHERE existing.rule_environment_id = mre.main_id
                          AND existing.compound1_id = mc1.main_id
                          AND existing.compound2_id = mc2.main_id
                          AND existing.constant_id IS NOT DISTINCT FROM mcs.main_id
                    )
                """
            )
        else:
            cursor.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS tmp_inc_inserted_pair_ids_all (
                    pair_id INTEGER PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_candidate_pair_keys")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_candidate_pair_keys (
                    rule_environment_id INTEGER NOT NULL,
                    compound1_id INTEGER NOT NULL,
                    compound2_id INTEGER NOT NULL,
                    constant_id INTEGER
                ) ON COMMIT DROP
                """
            )
            cursor.execute(
                f"""
                INSERT INTO tmp_inc_candidate_pair_keys (
                    rule_environment_id,
                    compound1_id,
                    compound2_id,
                    constant_id
                )
                SELECT DISTINCT
                    mre.main_id AS rule_environment_id,
                    mc1.main_id AS compound1_id,
                    mc2.main_id AS compound2_id,
                    mcs.main_id AS constant_id
                FROM {ts}.pair p
                INNER JOIN tmp_map_rule_env mre
                        ON mre.temp_id = p.rule_environment_id
                INNER JOIN tmp_map_compound mc1
                        ON mc1.temp_id = p.compound1_id
                INNER JOIN tmp_map_compound mc2
                        ON mc2.temp_id = p.compound2_id
                INNER JOIN tmp_map_constant mcs
                        ON mcs.temp_id = p.constant_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc1
                       ON cc1.id = mc1.main_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc2
                       ON cc2.id = mc2.main_id
                WHERE cc1.id IS NOT NULL OR cc2.id IS NOT NULL
                """
            )
            cursor.execute(
                """
                CREATE INDEX tmp_inc_candidate_pair_lookup_idx
                ON tmp_inc_candidate_pair_keys(rule_environment_id, compound1_id, compound2_id, constant_id)
                """
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_existing_pair_keys")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_existing_pair_keys AS
                SELECT DISTINCT
                    existing.rule_environment_id,
                    existing.compound1_id,
                    existing.compound2_id,
                    existing.constant_id
                FROM pair existing
                INNER JOIN tmp_map_constant mcs
                        ON mcs.main_id = existing.constant_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc1
                       ON cc1.id = existing.compound1_id
                LEFT JOIN tmp_inc_candidate_compound_ids cc2
                       ON cc2.id = existing.compound2_id
                WHERE cc1.id IS NOT NULL OR cc2.id IS NOT NULL
                """
            )
            cursor.execute(
                """
                CREATE INDEX tmp_inc_existing_pair_lookup_idx
                ON tmp_inc_existing_pair_keys(rule_environment_id, compound1_id, compound2_id, constant_id)
                """
            )
            if merge_debug_stats:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM tmp_inc_candidate_pair_keys
                    """
                )
                _append_candidate_pairs = int(cursor.fetchone()[0] or 0)
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM tmp_inc_existing_pair_keys
                    """
                )
                _append_existing_pairs = int(cursor.fetchone()[0] or 0)
                _logger.info(
                    "Incremental merge append-mode candidate mapped pairs: total=%s existing=%s",
                    _append_candidate_pairs,
                    _append_existing_pairs,
                )
            cursor.execute(
                f"""
                WITH inserted_pairs AS (
                    INSERT INTO pair (rule_environment_id, compound1_id, compound2_id, constant_id)
                    SELECT
                        cpk.rule_environment_id,
                        cpk.compound1_id,
                        cpk.compound2_id,
                        cpk.constant_id
                    FROM tmp_inc_candidate_pair_keys cpk
                    LEFT JOIN tmp_inc_existing_pair_keys existing
                           ON existing.rule_environment_id = cpk.rule_environment_id
                          AND existing.compound1_id = cpk.compound1_id
                          AND existing.compound2_id = cpk.compound2_id
                          AND existing.constant_id IS NOT DISTINCT FROM cpk.constant_id
                    WHERE existing.rule_environment_id IS NULL
                    RETURNING id AS pair_id, rule_environment_id
                ),
                _inserted_pair_ids AS (
                    INSERT INTO tmp_inc_inserted_pair_ids_all (pair_id)
                    SELECT pair_id
                    FROM inserted_pairs
                    ON CONFLICT DO NOTHING
                ),
                counts AS (
                    SELECT rule_environment_id, COUNT(*)::INTEGER AS n
                    FROM inserted_pairs
                    GROUP BY rule_environment_id
                )
                UPDATE rule_environment re
                   SET num_pairs = COALESCE(re.num_pairs, 0) + counts.n
                  FROM counts
                 WHERE re.id = counts.rule_environment_id
                """
            )
            if merge_debug_stats:
                cursor.execute("SELECT COUNT(*) FROM tmp_inc_inserted_pair_ids_all")
                _logger.info(
                    "Incremental merge append-mode inserted pair IDs (cumulative in tx): %s",
                    int(cursor.fetchone()[0] or 0),
                )

        cursor.execute(
            f"""
            INSERT INTO property_name (name)
            SELECT t.name
            FROM {ts}.property_name t
            INNER JOIN tmp_inc_temp_used_property_name_ids up
                    ON up.id = t.id
            LEFT JOIN property_name p
                   ON p.name = t.name
            WHERE p.id IS NULL
            """
        )
        cursor.execute("DROP TABLE IF EXISTS tmp_map_property_name")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_map_property_name AS
            SELECT
                t.id AS temp_id,
                MIN(p.id) AS main_id
            FROM {ts}.property_name t
            INNER JOIN tmp_inc_temp_used_property_name_ids up
                    ON up.id = t.id
            INNER JOIN property_name p
                    ON p.name = t.name
            GROUP BY t.id
            """
        )
        cursor.execute("CREATE INDEX tmp_map_property_name_temp_idx ON tmp_map_property_name(temp_id)")
        cursor.execute("DROP TABLE IF EXISTS tmp_inc_rule_env_stats")
        cursor.execute(
            f"""
            CREATE TEMP TABLE tmp_inc_rule_env_stats AS
            SELECT
                mre.main_id AS rule_environment_id,
                mpn.main_id AS property_name_id,
                s.count,
                s.avg,
                s.std,
                s.kurtosis,
                s.skewness,
                s.min,
                s.q1,
                s.median,
                s.q3,
                s.max,
                s.paired_t,
                s.p_value
            FROM {ts}.rule_environment_statistics s
            INNER JOIN tmp_map_rule_env mre
                    ON mre.temp_id = s.rule_environment_id
            INNER JOIN tmp_map_property_name mpn
                    ON mpn.temp_id = s.property_name_id
            """
        )
        cursor.execute(
            """
            DELETE FROM rule_environment_statistics rs
            USING tmp_inc_rule_env_stats t
            WHERE rs.rule_environment_id = t.rule_environment_id
              AND rs.property_name_id = t.property_name_id
            """
        )
        cursor.execute(
            """
            INSERT INTO rule_environment_statistics (
                rule_environment_id,
                property_name_id,
                count,
                avg,
                std,
                kurtosis,
                skewness,
                min,
                q1,
                median,
                q3,
                max,
                paired_t,
                p_value
            )
            SELECT
                rule_environment_id,
                property_name_id,
                count,
                avg,
                std,
                kurtosis,
                skewness,
                min,
                q1,
                median,
                q3,
                max,
                paired_t,
                p_value
            FROM tmp_inc_rule_env_stats
            """
        )
        # Keep merge side-effects bounded: drop newly-mapped orphan graph nodes
        # that are not referenced by any pair after this merge.
        cursor.execute(
            """
            DELETE FROM rule_environment_statistics rs
            WHERE rs.rule_environment_id IN (SELECT DISTINCT main_id FROM tmp_map_rule_env)
              AND NOT EXISTS (
                    SELECT 1
                    FROM pair p
                    WHERE p.rule_environment_id = rs.rule_environment_id
                )
            """
        )
        cursor.execute(
            """
            DELETE FROM rule_environment re
            WHERE re.id IN (SELECT DISTINCT main_id FROM tmp_map_rule_env)
              AND NOT EXISTS (
                    SELECT 1
                    FROM pair p
                    WHERE p.rule_environment_id = re.id
                )
            """
        )
        cursor.execute(
            """
            DELETE FROM rule r
            WHERE r.id IN (SELECT DISTINCT main_id FROM tmp_map_rule)
              AND NOT EXISTS (
                    SELECT 1
                    FROM rule_environment re
                    WHERE re.rule_id = r.id
                )
            """
        )
        cursor.execute(
            """
            DELETE FROM environment_fingerprint ef
            WHERE ef.id IN (SELECT DISTINCT main_id FROM tmp_map_env_fp)
              AND NOT EXISTS (
                    SELECT 1
                    FROM rule_environment re
                    WHERE re.environment_fingerprint_id = ef.id
                )
            """
        )
        cursor.execute(
            """
            DELETE FROM rule_smiles rs
            WHERE rs.id IN (SELECT DISTINCT main_id FROM tmp_map_rule_smiles)
              AND NOT EXISTS (
                    SELECT 1
                    FROM rule r
                    WHERE r.from_smiles_id = rs.id
                )
              AND NOT EXISTS (
                    SELECT 1
                    FROM rule r
                    WHERE r.to_smiles_id = rs.id
                )
            """
        )
        cursor.execute(
            """
            DELETE FROM property_name pn
            WHERE pn.id IN (SELECT DISTINCT main_id FROM tmp_map_property_name)
              AND NOT EXISTS (
                    SELECT 1
                    FROM rule_environment_statistics rs
                    WHERE rs.property_name_id = pn.id
                )
            """
        )

    if not skip_finalize:
        _finalize_incremental_merge_state(
            cursor,
            affected_constants=affected_constants,
            candidate_smiles=candidate_smiles,
            active_rows=active_rows,
            recount_constants=replace_existing_pairs_for_constants,
        )


def _incremental_add_compounds_without_reindex(
    postgres_url: str,
    *,
    schema: str,
    added_rows: Sequence[Tuple[str, str, str]],
    analyze_after_update: bool = True,
    refresh_dataset_counts: bool = True,
) -> Dict[str, int]:
    normalized_schema = _validate_pg_schema(schema)
    unique_rows: List[Tuple[str, str, str]] = []
    seen_smiles: set[str] = set()
    for row in added_rows:
        clean_smiles = str((row[0] if len(row) > 0 else "") or "").strip()
        if not clean_smiles or clean_smiles in seen_smiles:
            continue
        seen_smiles.add(clean_smiles)
        public_id = str((row[1] if len(row) > 1 else "") or "").strip() or clean_smiles
        input_smiles = str((row[2] if len(row) > 2 else "") or "").strip() or clean_smiles
        unique_rows.append((clean_smiles, public_id, input_smiles))
    stats = {
        "added_smiles": len(unique_rows),
        "inserted_compounds": 0,
        "updated_compounds": 0,
        "cp_updated": 0,
        "cp_inserted": 0,
        "cp_deleted": 0,
    }
    if not unique_rows:
        return stats

    smiles_values = [item[0] for item in unique_rows]
    heavies_map = _compute_clean_num_heavies_by_smiles(smiles_values)
    payload = [
        (
            clean_smiles,
            public_id,
            input_smiles,
            int(heavies_map.get(clean_smiles, 0)),
        )
        for clean_smiles, public_id, input_smiles in unique_rows
    ]
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
            _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
            _ensure_pair_compound_lookup_indexes(cursor)
            if _should_align_incremental_id_sequences():
                _pg_align_id_sequence(cursor, "compound")

            cursor.execute("DROP TABLE IF EXISTS tmp_inc_add_rows")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_add_rows (
                    clean_smiles TEXT PRIMARY KEY,
                    public_id TEXT,
                    input_smiles TEXT,
                    clean_num_heavies INTEGER
                ) ON COMMIT DROP
                """
            )
            _copy_rows(
                cursor,
                "COPY tmp_inc_add_rows (clean_smiles, public_id, input_smiles, clean_num_heavies) FROM STDIN",
                payload,
            )
            cursor.execute(
                """
                UPDATE compound c
                   SET public_id = CASE
                           WHEN COALESCE(NULLIF(t.public_id, ''), '') <> ''
                            AND NOT EXISTS (
                                SELECT 1
                                FROM compound c2
                                WHERE c2.public_id = t.public_id
                                  AND c2.id <> c.id
                            )
                           THEN t.public_id
                           ELSE c.public_id
                       END,
                       input_smiles = CASE
                           WHEN COALESCE(NULLIF(t.input_smiles, ''), '') <> ''
                           THEN t.input_smiles
                           ELSE c.input_smiles
                       END,
                       clean_num_heavies = CASE
                           WHEN COALESCE(t.clean_num_heavies, 0) > 0 THEN t.clean_num_heavies
                           ELSE c.clean_num_heavies
                       END
                  FROM tmp_inc_add_rows t
                 WHERE c.clean_smiles = t.clean_smiles
                """
            )
            stats["updated_compounds"] = int(cursor.rowcount or 0)
            cursor.execute(
                """
                INSERT INTO compound (public_id, input_smiles, clean_smiles, clean_num_heavies)
                SELECT
                    CASE
                        WHEN c_pub.id IS NULL AND COALESCE(NULLIF(t.public_id, ''), '') <> ''
                        THEN t.public_id
                        ELSE ('INC_' || SUBSTRING(MD5(t.clean_smiles) FROM 1 FOR 16))
                    END AS public_id,
                    COALESCE(NULLIF(t.input_smiles, ''), t.clean_smiles) AS input_smiles,
                    t.clean_smiles,
                    t.clean_num_heavies
                  FROM tmp_inc_add_rows t
                  LEFT JOIN compound c
                    ON c.clean_smiles = t.clean_smiles
                  LEFT JOIN compound c_pub
                    ON c_pub.public_id = t.public_id
                 WHERE c.id IS NULL
                """
            )
            stats["inserted_compounds"] = int(cursor.rowcount or 0)
            cp_stats = _refresh_compound_property_for_smiles(cursor, smiles_values)
            stats["cp_updated"] = int(cp_stats["updated"])
            stats["cp_inserted"] = int(cp_stats["inserted"])
            stats["cp_deleted"] = int(cp_stats["deleted"])
            if refresh_dataset_counts:
                _update_dataset_counts(cursor)
            if analyze_after_update:
                cursor.execute("ANALYZE compound")
                cursor.execute("ANALYZE compound_property")
            else:
                _maybe_auto_analyze_incremental_tables(
                    cursor,
                    ["compound", "compound_property"],
                )
            conn.commit()
    return stats


def _incremental_delete_compounds_without_reindex(
    postgres_url: str,
    *,
    schema: str,
    removed_rows: Sequence[Tuple[str, str, str]],
    analyze_after_update: bool = True,
    refresh_dataset_counts: bool = True,
) -> Dict[str, int]:
    normalized_schema = _validate_pg_schema(schema)
    removed_smiles = sorted(
        {str((row[0] if len(row) > 0 else "") or "").strip() for row in removed_rows if str((row[0] if len(row) > 0 else "") or "").strip()}
    )
    stats = {
        "removed_smiles": len(removed_smiles),
        "matched_compounds": 0,
        "deleted_pairs": 0,
        "deleted_compounds": 0,
        "affected_constants": 0,
    }
    if not removed_smiles:
        return stats

    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, normalized_schema)
            _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
            _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
            _ensure_pair_compound_lookup_indexes(cursor)

            cursor.execute("DROP TABLE IF EXISTS tmp_inc_removed_smiles")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_removed_smiles (
                    clean_smiles TEXT PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            _copy_rows(
                cursor,
                "COPY tmp_inc_removed_smiles (clean_smiles) FROM STDIN",
                [(value,) for value in removed_smiles],
            )

            cursor.execute("DROP TABLE IF EXISTS tmp_inc_removed_compounds")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_removed_compounds AS
                SELECT c.id, c.clean_smiles
                FROM compound c
                INNER JOIN tmp_inc_removed_smiles r
                        ON r.clean_smiles = c.clean_smiles
                """
            )
            cursor.execute("SELECT 1 FROM tmp_inc_removed_compounds LIMIT 1")
            if cursor.fetchone() is None:
                conn.commit()
                return stats

            cursor.execute("DROP TABLE IF EXISTS tmp_inc_removed_touched_constants")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_removed_touched_constants AS
                WITH touched_constant_ids AS (
                    SELECT p.constant_id AS constant_id
                    FROM pair p
                    INNER JOIN tmp_inc_removed_compounds r
                            ON r.id = p.compound1_id
                    UNION
                    SELECT p.constant_id AS constant_id
                    FROM pair p
                    INNER JOIN tmp_inc_removed_compounds r
                            ON r.id = p.compound2_id
                )
                SELECT DISTINCT cs.smiles AS smiles
                FROM touched_constant_ids t
                INNER JOIN constant_smiles cs
                        ON cs.id = t.constant_id
                """
            )
            cursor.execute("DROP TABLE IF EXISTS tmp_inc_deleted_pair_ids")
            cursor.execute(
                """
                CREATE TEMP TABLE tmp_inc_deleted_pair_ids (
                    pair_id INTEGER PRIMARY KEY
                ) ON COMMIT DROP
                """
            )
            cursor.execute(
                """
                INSERT INTO tmp_inc_deleted_pair_ids (pair_id)
                SELECT p.id
                FROM pair p
                WHERE p.compound1_id IN (SELECT id FROM tmp_inc_removed_compounds)
                   OR p.compound2_id IN (SELECT id FROM tmp_inc_removed_compounds)
                ON CONFLICT DO NOTHING
                """
            )
            cursor.execute("SELECT COUNT(*) FROM tmp_inc_deleted_pair_ids")
            stats["deleted_pairs"] = int((cursor.fetchone() or [0])[0] or 0)
            cursor.execute(
                """
                DELETE FROM pair p
                WHERE p.id IN (SELECT pair_id FROM tmp_inc_deleted_pair_ids)
                """
            )
            cursor.execute("SELECT to_regclass('from_construct')")
            has_from_construct = cursor.fetchone()[0] is not None
            cursor.execute("SELECT to_regclass('to_construct')")
            has_to_construct = cursor.fetchone()[0] is not None
            if has_from_construct:
                cursor.execute(
                    """
                    DELETE FROM from_construct fc
                    WHERE fc.pair_id IN (SELECT pair_id FROM tmp_inc_deleted_pair_ids)
                    """
                )
            if has_to_construct:
                cursor.execute(
                    """
                    DELETE FROM to_construct tc
                    WHERE tc.pair_id IN (SELECT pair_id FROM tmp_inc_deleted_pair_ids)
                    """
                )

            cursor.execute(
                """
                DELETE FROM compound_property cp
                WHERE cp.compound_id IN (SELECT id FROM tmp_inc_removed_compounds)
                """
            )
            cursor.execute(
                """
                DELETE FROM compound c
                WHERE c.id IN (SELECT id FROM tmp_inc_removed_compounds)
                """
            )
            stats["deleted_compounds"] = int(cursor.rowcount or 0)
            stats["matched_compounds"] = stats["deleted_compounds"]

            cursor.execute("SELECT smiles FROM tmp_inc_removed_touched_constants")
            touched_constants = [str(row[0] or "").strip() for row in cursor.fetchall() if str(row[0] or "").strip()]
            stats["affected_constants"] = len(touched_constants)
            if touched_constants:
                _cleanup_orphan_mmp_rows_for_constants(
                    cursor,
                    affected_constants=touched_constants,
                )
                _update_rule_environment_counts_for_constants(
                    cursor,
                    affected_constants=touched_constants,
                )
            if refresh_dataset_counts:
                _update_dataset_counts(cursor)
            if analyze_after_update:
                for table_name in (
                    "compound",
                    "pair",
                    "rule_environment",
                    "rule",
                    "rule_smiles",
                    "constant_smiles",
                    "compound_property",
                ):
                    cursor.execute(f"ANALYZE {table_name}")
                if has_from_construct:
                    cursor.execute("ANALYZE from_construct")
                if has_to_construct:
                    cursor.execute("ANALYZE to_construct")
            else:
                auto_tables = [
                    "compound",
                    "pair",
                    "rule_environment",
                    "rule",
                    "rule_smiles",
                    "constant_smiles",
                    "compound_property",
                ]
                if has_from_construct:
                    auto_tables.append("from_construct")
                if has_to_construct:
                    auto_tables.append("to_construct")
                _maybe_auto_analyze_incremental_tables(cursor, auto_tables)
            conn.commit()
    return stats


def _incremental_reindex_compound_delta(
    postgres_url: str,
    *,
    schema: str,
    changed_rows: Sequence[Tuple[str, str, str]] = (),
    changed_rows_file: str = "",
    output_dir: str,
    max_heavy_atoms: int,
    skip_attachment_enrichment: bool,
    attachment_force_recompute: bool,
    fragment_jobs: int,
    index_maintenance_work_mem_mb: int,
    index_work_mem_mb: int,
    index_parallel_workers: int,
    index_commit_every_flushes: int,
    incremental_index_shards: int,
    incremental_index_jobs: int,
    build_construct_tables: bool,
    build_constant_smiles_mol_index: bool,
    delta_mode: str,
    skip_incremental_analyze: bool = False,
) -> bool:
    logger = logging.getLogger(__name__)
    t_total_start = time.time()
    normalized_schema = _validate_pg_schema(schema)
    normalized_mode = str(delta_mode or "").strip().lower()
    if normalized_mode not in {"add", "delete"}:
        logger.error("Unsupported incremental delta mode: %s", delta_mode)
        return False
    schema_fragment_cache_file = os.path.join(output_dir, f"{normalized_schema}_compound_state.cache.fragdb")

    unique_smiles: List[str] = []
    seen_smiles: set[str] = set()
    delta_source_file = str(changed_rows_file or "").strip()
    if delta_source_file:
        if not os.path.exists(delta_source_file):
            logger.error("Incremental delta source file not found: %s", delta_source_file)
            return False
        delimiter = _detect_table_delimiter(delta_source_file)
        with open(delta_source_file, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            headers = [str(h or "").strip() for h in (reader.fieldnames or [])]
            if not headers:
                logger.error("Incremental delta source file has no header: %s", delta_source_file)
                return False
            smiles_col = _resolve_smiles_column(headers, preferred_column="SMILES")
            for row in reader:
                clean_smiles = str((row or {}).get(smiles_col) or "").strip()
                if not clean_smiles or clean_smiles in seen_smiles:
                    continue
                seen_smiles.add(clean_smiles)
                unique_smiles.append(clean_smiles)
    else:
        for raw in changed_rows:
            clean_smiles = str((raw[0] if len(raw) > 0 else "") or "").strip()
            if not clean_smiles or clean_smiles in seen_smiles:
                continue
            seen_smiles.add(clean_smiles)
            unique_smiles.append(clean_smiles)
    if not unique_smiles:
        logger.info("No structural compound delta rows detected; skipping incremental index.")
        return True
    refresh_dataset_counts = _should_refresh_incremental_dataset_counts(
        skip_incremental_analyze=bool(skip_incremental_analyze),
    )

    unique_smiles_count = len(unique_smiles)

    if normalized_mode == "delete":
        t0 = time.time()
        removed_rows = [(smiles, f"DELTA_{idx:09d}", smiles) for idx, smiles in enumerate(unique_smiles, start=1)]
        delete_stats = _incremental_delete_compounds_without_reindex(
            postgres_url,
            schema=normalized_schema,
            removed_rows=removed_rows,
            analyze_after_update=(not bool(skip_incremental_analyze)),
            refresh_dataset_counts=refresh_dataset_counts,
        )
        logger.info(
            "Incremental delete fast-path elapsed=%.2fs removed_smiles=%s matched_compounds=%s deleted_compounds=%s deleted_pairs=%s affected_constants=%s",
            max(0.0, time.time() - t0),
            delete_stats["removed_smiles"],
            delete_stats["matched_compounds"],
            delete_stats["deleted_compounds"],
            delete_stats["deleted_pairs"],
            delete_stats["affected_constants"],
        )
        # Keep the cache to avoid full reseed cost after rollback/delete.
        # Merge step filters non-active compounds, so stale cache entries cannot be re-materialized.
        _sync_fragment_cache_meta_from_db(
            postgres_url,
            schema=normalized_schema,
            cache_fragdb=schema_fragment_cache_file,
        )
        logger.info("Incremental index total elapsed=%.2fs", max(0.0, time.time() - t_total_start))
        return True

    os.makedirs(output_dir, exist_ok=True)
    file_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    delta_smiles_file = os.path.join(output_dir, f"{normalized_schema}_delta_{normalized_mode}_{file_tag}.smi")
    delta_fragdb_file = os.path.join(output_dir, f"{normalized_schema}_delta_{normalized_mode}_{file_tag}.fragdb")
    delta_constants_file = os.path.join(output_dir, f"{normalized_schema}_delta_{normalized_mode}_{file_tag}.constants.tsv")
    candidate_smiles_file = os.path.join(output_dir, f"{normalized_schema}_candidate_{normalized_mode}_{file_tag}.smi")
    candidate_fragdb_file = os.path.join(output_dir, f"{normalized_schema}_candidate_{normalized_mode}_{file_tag}.fragdb")
    temp_schemas_to_drop: List[str] = []
    cleanup_files = [delta_smiles_file, delta_fragdb_file, delta_constants_file, candidate_smiles_file, candidate_fragdb_file]
    try:
        t0 = time.time()
        if not _ensure_compound_fragment_cache(
            postgres_url,
            schema=normalized_schema,
            output_dir=output_dir,
            cache_file=schema_fragment_cache_file,
            max_heavy_atoms=max_heavy_atoms,
            fragment_jobs=fragment_jobs,
        ):
            logger.error("Failed ensuring fragment cache for schema '%s'.", normalized_schema)
            return False
        logger.info("Incremental index phase cache_ready elapsed=%.2fs", max(0.0, time.time() - t0))

        t0 = time.time()
        delta_id_seed = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        delta_count = _write_smiles_records_file(
            delta_smiles_file,
            (
                (
                    smiles,
                    f"INCDELTA_{delta_id_seed}_{idx:09d}",
                    smiles,
                )
                for idx, smiles in enumerate(unique_smiles, start=1)
                if str(smiles or "").strip()
            ),
        )
        if delta_count <= 0:
            logger.info("Delta SMILES set is empty; skipping incremental index.")
            return True
        if not _run_mmpdb_fragment(
            delta_smiles_file,
            delta_fragdb_file,
            max_heavy_atoms=max_heavy_atoms,
            fragment_jobs=fragment_jobs,
            cache_file=schema_fragment_cache_file,
        ):
            return False
        logger.info("Incremental index phase delta_fragment elapsed=%.2fs delta_rows=%s", max(0.0, time.time() - t0), delta_count)

        if not _run_mmpdb_fragdb_constants(
            delta_fragdb_file,
            delta_constants_file,
        ):
            return False

        constant_counts = _load_constant_counts_from_constants_file(delta_constants_file)
        affected_constants = sorted(constant_counts.keys())
        if not affected_constants:
            logger.info(
                "Delta compounds generated no constants from fragdb; mode=%s schema=%s",
                normalized_mode,
                normalized_schema,
            )
            t_fast = time.time()
            fast_stats = _incremental_add_compounds_without_reindex(
                postgres_url,
                schema=normalized_schema,
                added_rows=[
                    (smiles, f"INCDELTA_{delta_id_seed}_{idx:09d}", smiles)
                    for idx, smiles in enumerate(unique_smiles, start=1)
                ],
                analyze_after_update=(not bool(skip_incremental_analyze)),
                refresh_dataset_counts=refresh_dataset_counts,
            )
            logger.info(
                "Incremental add fast-path elapsed=%.2fs (no constants): added_smiles=%s inserted_compounds=%s updated_compounds=%s cp(updated=%s inserted=%s deleted=%s).",
                max(0.0, time.time() - t_fast),
                fast_stats["added_smiles"],
                fast_stats["inserted_compounds"],
                fast_stats["updated_compounds"],
                fast_stats["cp_updated"],
                fast_stats["cp_inserted"],
                fast_stats["cp_deleted"],
            )
            _sync_fragment_cache_meta_from_db(
                postgres_url,
                schema=normalized_schema,
                cache_fragdb=schema_fragment_cache_file,
            )
            logger.info("Incremental index total elapsed=%.2fs", max(0.0, time.time() - t_total_start))
            return True
        sync_smiles_scope = [str(item or "").strip() for item in unique_smiles if str(item or "").strip()]
        sync_active_rows = _load_active_compounds_for_smiles(
            postgres_url,
            schema=normalized_schema,
            smiles_values=sync_smiles_scope,
        )
        if not sync_active_rows:
            logger.error("Failed loading active rows for incremental sync smiles set.")
            return False
        shard_groups = _split_constants_into_shards(
            affected_constants,
            incremental_index_shards,
            weights=constant_counts,
        )
        if not shard_groups:
            shard_groups = [affected_constants]
        shard_specs: List[Tuple[int, List[str], str, str, str]] = []
        for shard_seq, shard_constants in enumerate(shard_groups, start=1):
            constants_list = [str(token or "").strip() for token in shard_constants if str(token or "").strip()]
            if not constants_list:
                continue
            shard_constants_file = os.path.join(
                output_dir,
                f"{normalized_schema}_delta_{normalized_mode}_{file_tag}.constants.s{shard_seq:04d}.tsv",
            )
            shard_candidate_fragdb_file = os.path.join(
                output_dir,
                f"{normalized_schema}_candidate_{normalized_mode}_{file_tag}.s{shard_seq:04d}.fragdb",
            )
            shard_temp_schema = _validate_pg_schema(
                f"{normalized_schema[:18]}_incs_{datetime.utcnow().strftime('%H%M%S%f')[-12:]}_{shard_seq:02d}"
            )
            cleanup_files.extend([shard_constants_file, shard_candidate_fragdb_file])
            temp_schemas_to_drop.append(shard_temp_schema)
            shard_specs.append(
                (
                    shard_seq,
                    constants_list,
                    shard_constants_file,
                    shard_candidate_fragdb_file,
                    shard_temp_schema,
                )
            )

        def _prepare_incremental_shard(
            spec: Tuple[int, List[str], str, str, str]
        ) -> Tuple[int, bool, str]:
            shard_seq, shard_constants, shard_constants_file, shard_candidate_fragdb_file, shard_temp_schema = spec
            shard_constant_count = _write_constants_file(
                shard_constants_file,
                shard_constants,
                counts=constant_counts,
            )
            if shard_constant_count <= 0:
                return shard_seq, False, "empty constants"
            if not _run_mmpdb_fragdb_partition(
                cache_fragdb=schema_fragment_cache_file,
                delta_fragdb=delta_fragdb_file,
                constants_file=shard_constants_file,
                output_fragdb=shard_candidate_fragdb_file,
            ):
                return shard_seq, False, "fragdb_partition failed"
            ok = _index_fragments_to_postgres(
                shard_candidate_fragdb_file,
                postgres_url,
                postgres_schema=shard_temp_schema,
                force_rebuild_schema=True,
                properties_file=None,
                skip_attachment_enrichment=True,
                attachment_force_recompute=False,
                index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
                index_work_mem_mb=index_work_mem_mb,
                index_parallel_workers=index_parallel_workers,
                index_commit_every_flushes=index_commit_every_flushes,
                skip_mmpdb_post_index_finalize=True,
                skip_mmpdb_list_verify=True,
                build_construct_tables=False,
                build_constant_smiles_mol_index=False,
                detect_existing_core_tables=False,
            )
            if not ok:
                return shard_seq, False, "index_fragments_to_postgres failed"
            return shard_seq, True, ""

        shard_prepare_errors: List[str] = []
        jobs_requested = max(1, int(incremental_index_jobs or 1))
        jobs_effective = min(jobs_requested, max(1, len(shard_specs)))
        if jobs_effective > 1 and len(shard_specs) > 1:
            logger.info(
                "Incremental shard indexing concurrency enabled: shards=%s jobs=%s",
                len(shard_specs),
                jobs_effective,
            )
            with ThreadPoolExecutor(max_workers=jobs_effective) as executor:
                future_map = {
                    executor.submit(_prepare_incremental_shard, spec): spec[0]
                    for spec in shard_specs
                }
                for future in as_completed(future_map):
                    shard_seq = future_map[future]
                    try:
                        seq, ok, reason = future.result()
                    except Exception as exc:
                        shard_prepare_errors.append(f"shard={shard_seq} exception={exc}")
                        continue
                    if not ok:
                        shard_prepare_errors.append(f"shard={seq} reason={reason}")
        else:
            for spec in shard_specs:
                seq, ok, reason = _prepare_incremental_shard(spec)
                if not ok:
                    shard_prepare_errors.append(f"shard={seq} reason={reason}")
                    break

        if shard_prepare_errors:
            logger.error("Incremental shard preparation failed: %s", "; ".join(shard_prepare_errors))
            return False
        shard_sources: List[Tuple[str, List[str]]] = [
            (spec[4], list(spec[1]))
            for spec in sorted(shard_specs, key=lambda item: item[0])
        ]
        if not shard_sources:
            logger.error("No shard candidate schemas were created for incremental add.")
            return False
        logger.info(
            "Incremental index phase candidate_expand complete: affected_constants=%s shards=%s sync_smiles=%s",
            len(affected_constants),
            len(shard_sources),
            len(sync_smiles_scope),
        )

        t0 = time.time()
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
                for merge_seq, (temp_schema_name, shard_constants) in enumerate(shard_sources):
                    replace_existing = False
                    _merge_incremental_temp_schema_into_target(
                        cursor,
                        temp_schema=temp_schema_name,
                        affected_constants=list(shard_constants),
                        candidate_smiles=sync_smiles_scope,
                        active_rows=sync_active_rows,
                        replace_existing_pairs_for_constants=replace_existing,
                        skip_finalize=True,
                        align_sequences=(merge_seq == 0),
                    )
                _finalize_incremental_merge_state(
                    cursor,
                    affected_constants=affected_constants,
                    candidate_smiles=sync_smiles_scope,
                    active_rows=sync_active_rows,
                    recount_constants=False,
                )

                if not skip_attachment_enrichment:
                    _apply_postgres_build_tuning(
                        cursor,
                        maintenance_work_mem_mb=index_maintenance_work_mem_mb,
                        work_mem_mb=index_work_mem_mb,
                        parallel_maintenance_workers=index_parallel_workers,
                    )
                    _enrich_attachment_schema_postgres_incremental(
                        cursor,
                        normalized_schema,
                        force_recompute=attachment_force_recompute,
                        affected_constants=affected_constants,
                        enable_constant_smiles_mol_index=build_constant_smiles_mol_index,
                    )
                if _should_run_incremental_core_index_ensure():
                    _create_postgres_core_indexes(
                        cursor,
                        normalized_schema,
                        enable_constant_smiles_mol_index=build_constant_smiles_mol_index,
                    )
                if build_construct_tables:
                    _append_construct_tables_postgres_for_inserted_pairs(cursor)
                if refresh_dataset_counts:
                    _update_dataset_counts(cursor)
                refresh_stats = _refresh_compound_property_for_smiles(
                    cursor,
                    sync_smiles_scope,
                )
                if not bool(skip_incremental_analyze):
                    analyze_tables = [
                        "compound",
                        "pair",
                        "rule_environment",
                        "compound_property",
                    ]
                    analyze_tables.extend(
                        [
                            "rule_environment_statistics",
                            "rule",
                            "rule_smiles",
                            "constant_smiles",
                        ]
                    )
                    if build_construct_tables:
                        analyze_tables.extend(["from_construct", "to_construct"])
                    for table_name in analyze_tables:
                        cursor.execute(f"ANALYZE {table_name}")
                else:
                    auto_tables = [
                        "compound",
                        "pair",
                        "rule_environment",
                        "compound_property",
                        "rule_environment_statistics",
                        "rule",
                        "rule_smiles",
                        "constant_smiles",
                    ]
                    if build_construct_tables:
                        auto_tables.extend(["from_construct", "to_construct"])
                    _maybe_auto_analyze_incremental_tables(cursor, auto_tables)
                conn.commit()
        logger.info("Incremental index phase merge_finalize elapsed=%.2fs", max(0.0, time.time() - t0))

        logger.info(
            "Incremental compound index succeeded for schema '%s' mode=%s: delta_compounds=%s affected_constants=%s sync_smiles=%s cp(updated=%s inserted=%s deleted=%s).",
            normalized_schema,
            normalized_mode,
            unique_smiles_count,
            len(affected_constants),
            len(sync_smiles_scope),
            refresh_stats["updated"],
            refresh_stats["inserted"],
            refresh_stats["deleted"],
        )
        cache_append_ok = _append_delta_fragdb_into_cache(
            cache_fragdb=schema_fragment_cache_file,
            delta_fragdb=delta_fragdb_file,
        )
        if cache_append_ok:
            _sync_fragment_cache_meta_from_db(
                postgres_url,
                schema=normalized_schema,
                cache_fragdb=schema_fragment_cache_file,
            )
        else:
            _remove_fragment_cache_files(
                schema_fragment_cache_file,
                reason="delta cache append failed",
            )
        logger.info("Incremental index total elapsed=%.2fs", max(0.0, time.time() - t_total_start))
        return True
    except Exception as exc:
        logger.error(
            "Failed incremental compound index for schema '%s' mode=%s: %s",
            normalized_schema,
            normalized_mode,
            exc,
            exc_info=True,
        )
        return False
    finally:
        for temp_schema in temp_schemas_to_drop:
            try:
                _drop_postgres_schema_if_exists(postgres_url, temp_schema)
            except Exception:
                pass
        for file_path in cleanup_files:
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass


def import_compound_batch_postgres(
    postgres_url: str,
    *,
    schema: str,
    structures_file: str,
    batch_id: str = "",
    batch_label: str = "",
    batch_notes: str = "",
    smiles_column: str = "",
    id_column: str = "",
    canonicalize_smiles: bool = True,
    output_dir: str = "data",
    max_heavy_atoms: int = 50,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    fragment_jobs: int = 8,
    index_maintenance_work_mem_mb: int = 1024,
    index_work_mem_mb: int = 128,
    index_parallel_workers: int = 4,
    index_commit_every_flushes: int = 0,
    incremental_index_shards: int = 1,
    incremental_index_jobs: int = 1,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    skip_incremental_analyze: bool = False,
    overwrite_existing_batch: bool = False,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for compound batch import.")
        return False
    if not HAS_RDKIT_MMPDB or not HAS_PSYCOPG2:
        logger.error("Compound batch import needs RDKit/mmpdb and psycopg2 (for incremental index).")
        return False
    source_file = str(structures_file or "").strip()
    if not source_file or not os.path.exists(source_file):
        logger.error("Compound batch file not found: %s", source_file)
        return False
    normalized_schema = _validate_pg_schema(schema)
    normalized_batch_id = _normalize_compound_batch_id(batch_id)
    existing_materialized_rows = 0
    existing_compounds_before = False
    bootstrap_smiles_file = ""
    bootstrap_smiles_rows = 0
    incremental_delta_file = ""
    parse_stats: Dict[str, int] = {"total_rows": 0, "valid_rows": 0, "invalid_smiles_rows": 0, "dedup_rows": 0}
    new_unique_rows = 0
    inserted_rows = 0
    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
                existing_compounds_before = _schema_has_any_compounds(cursor)
                if existing_compounds_before:
                    allow_cache_reseed = _env_bool("LEADOPT_MMP_ALLOW_CACHE_RESEED", False)
                    schema_fragment_cache_file = os.path.join(
                        output_dir,
                        f"{normalized_schema}_compound_state.cache.fragdb",
                    )
                    if (not allow_cache_reseed) and (not os.path.exists(schema_fragment_cache_file)):
                        logger.error(
                            "Incremental compound import requires existing fragment cache: %s. "
                            "Strict incremental mode forbids implicit full-cache rebuild.",
                            schema_fragment_cache_file,
                        )
                        conn.rollback()
                        return False
                    if (not allow_cache_reseed) and os.path.exists(schema_fragment_cache_file):
                        try:
                            signature = _collect_compound_cache_signature(
                                postgres_url,
                                schema=normalized_schema,
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed collecting fragment cache signature for schema '%s': %s",
                                normalized_schema,
                                exc,
                            )
                            conn.rollback()
                            return False
                        if not _is_fragment_cache_meta_match(
                            cache_fragdb=schema_fragment_cache_file,
                            signature=signature,
                        ):
                            logger.error(
                                "Fragment cache metadata mismatch for schema '%s' and strict incremental mode is enabled. "
                                "Refusing import to avoid implicit full-cache rebuild.",
                                normalized_schema,
                            )
                            conn.rollback()
                            return False
                cursor.execute("DROP TABLE IF EXISTS tmp_compound_existing_batch_materialized")
                cursor.execute(
                    """
                    CREATE TEMP TABLE tmp_compound_existing_batch_materialized (
                        clean_smiles TEXT PRIMARY KEY
                    )
                    """
                )
                cursor.execute(
                    f"SELECT 1 FROM {COMPOUND_BATCH_HEADER_TABLE} WHERE batch_id = %s LIMIT 1",
                    [normalized_batch_id],
                )
                if cursor.fetchone() is not None:
                    if not overwrite_existing_batch:
                        logger.error(
                            "Compound batch id '%s' already exists in schema '%s'.",
                            normalized_batch_id,
                            normalized_schema,
                        )
                        conn.rollback()
                        return False
                    # Snapshot currently materialized rows from this batch before overwrite delete.
                    # If compounds are already in main `compound`, retrying same batch should not
                    # re-trigger structural delta indexing for them.
                    cursor.execute(
                        f"""
                        INSERT INTO tmp_compound_existing_batch_materialized(clean_smiles)
                        SELECT DISTINCT r.clean_smiles
                        FROM {COMPOUND_BATCH_ROWS_TABLE} r
                        INNER JOIN compound c ON c.clean_smiles = r.clean_smiles
                        WHERE r.batch_id = %s
                        """,
                        [normalized_batch_id],
                    )
                    cursor.execute("SELECT COUNT(*) FROM tmp_compound_existing_batch_materialized")
                    existing_materialized_rows = int(cursor.fetchone()[0] or 0)
                    logger.warning(
                        "Compound batch id '%s' already exists in schema '%s'; replacing existing rows for retry (materialized=%s).",
                        normalized_batch_id,
                        normalized_schema,
                        existing_materialized_rows,
                    )
                    cursor.execute(
                        f"DELETE FROM {COMPOUND_BATCH_ROWS_TABLE} WHERE batch_id = %s",
                        [normalized_batch_id],
                    )
                    cursor.execute(
                        f"DELETE FROM {COMPOUND_BATCH_HEADER_TABLE} WHERE batch_id = %s",
                        [normalized_batch_id],
                    )
                parse_stats = _load_compound_batch_temp_table(
                    cursor,
                    structures_file=source_file,
                    smiles_column=smiles_column,
                    id_column=id_column,
                    canonicalize_smiles=canonicalize_smiles,
                )
                if parse_stats["dedup_rows"] <= 0:
                    logger.error("No valid deduplicated compound rows parsed from: %s", source_file)
                    conn.rollback()
                    return False
                cursor.execute("DROP TABLE IF EXISTS tmp_compound_new_unique")
                cursor.execute(
                    f"""
                    CREATE TEMP TABLE tmp_compound_new_unique AS
                    SELECT d.clean_smiles, d.public_id, d.input_smiles
                    FROM tmp_compound_upload_dedup d
                    WHERE NOT EXISTS (
                            SELECT 1 FROM compound c WHERE c.clean_smiles = d.clean_smiles
                        )
                      AND NOT EXISTS (
                            SELECT 1 FROM {COMPOUND_BATCH_BASE_TABLE} b WHERE b.clean_smiles = d.clean_smiles
                        )
                      AND NOT EXISTS (
                            SELECT 1 FROM {COMPOUND_BATCH_ROWS_TABLE} r WHERE r.clean_smiles = d.clean_smiles
                        )
                      AND NOT EXISTS (
                            SELECT 1 FROM tmp_compound_existing_batch_materialized m WHERE m.clean_smiles = d.clean_smiles
                        )
                    """
                )
                cursor.execute("SELECT COUNT(*) FROM tmp_compound_new_unique")
                new_unique_rows = int(cursor.fetchone()[0] or 0)
                if (not existing_compounds_before) and new_unique_rows > 0 and existing_materialized_rows <= 0:
                    file_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    bootstrap_smiles_file = os.path.join(
                        output_dir,
                        f"{normalized_schema}_bootstrap_new_batch_{file_tag}.smi",
                    )
                    bootstrap_smiles_rows = _export_smiles_file_from_temp_compound_table(
                        cursor,
                        source_table="tmp_compound_new_unique",
                        output_file=bootstrap_smiles_file,
                    )
                else:
                    file_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    incremental_delta_file = os.path.join(
                        output_dir,
                        f"{normalized_schema}_incremental_new_batch_{file_tag}.smi",
                    )
                    _export_smiles_file_from_temp_compound_table(
                        cursor,
                        source_table="tmp_compound_new_unique",
                        output_file=incremental_delta_file,
                    )
                cursor.execute(
                    f"""
                    INSERT INTO {COMPOUND_BATCH_HEADER_TABLE}
                        (batch_id, label, source_file, notes, total_rows, valid_rows, dedup_rows, new_unique_rows)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING batch_seq
                    """,
                    [
                        normalized_batch_id,
                        str(batch_label or "").strip(),
                        source_file,
                        str(batch_notes or "").strip(),
                        parse_stats["total_rows"],
                        parse_stats["valid_rows"],
                        parse_stats["dedup_rows"],
                        new_unique_rows,
                    ],
                )
                batch_seq = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    INSERT INTO {COMPOUND_BATCH_ROWS_TABLE}
                        (batch_id, batch_seq, clean_smiles, public_id, input_smiles)
                    SELECT %s, %s, clean_smiles, public_id, input_smiles
                    FROM tmp_compound_upload_dedup
                    """,
                    [normalized_batch_id, batch_seq],
                )
                inserted_rows = int(cursor.rowcount or 0)
                conn.commit()
        logger.info(
            "Imported compound batch '%s' into schema '%s': total=%s valid=%s dedup=%s new_unique=%s inserted=%s existing_materialized_retry=%s. Starting index sync...",
            normalized_batch_id,
            normalized_schema,
            parse_stats["total_rows"],
            parse_stats["valid_rows"],
            parse_stats["dedup_rows"],
            new_unique_rows,
            inserted_rows,
            existing_materialized_rows,
        )
        if bootstrap_smiles_rows > 0:
            logger.info(
                "Compound batch '%s' detected empty target schema '%s'; using bootstrap full-index strategy (rows=%s).",
                normalized_batch_id,
                normalized_schema,
                bootstrap_smiles_rows,
            )
            try:
                return _bootstrap_rebuild_index_from_smiles_file(
                    postgres_url,
                    schema=normalized_schema,
                    smiles_file=bootstrap_smiles_file,
                    output_dir=output_dir,
                    max_heavy_atoms=max_heavy_atoms,
                    skip_attachment_enrichment=skip_attachment_enrichment,
                    attachment_force_recompute=attachment_force_recompute,
                    fragment_jobs=fragment_jobs,
                    index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
                    index_work_mem_mb=index_work_mem_mb,
                    index_parallel_workers=index_parallel_workers,
                    index_commit_every_flushes=index_commit_every_flushes,
                    build_construct_tables=build_construct_tables,
                    build_constant_smiles_mol_index=build_constant_smiles_mol_index,
                )
            finally:
                try:
                    if bootstrap_smiles_file and os.path.exists(bootstrap_smiles_file):
                        os.remove(bootstrap_smiles_file)
                except Exception:
                    pass
        return _incremental_reindex_compound_delta(
            postgres_url,
            schema=normalized_schema,
            changed_rows=(),
            changed_rows_file=incremental_delta_file,
            output_dir=output_dir,
            max_heavy_atoms=max_heavy_atoms,
            skip_attachment_enrichment=skip_attachment_enrichment,
            attachment_force_recompute=attachment_force_recompute,
            fragment_jobs=fragment_jobs,
            index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            index_work_mem_mb=index_work_mem_mb,
            index_parallel_workers=index_parallel_workers,
            index_commit_every_flushes=index_commit_every_flushes,
            incremental_index_shards=incremental_index_shards,
            incremental_index_jobs=incremental_index_jobs,
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
            delta_mode="add",
            skip_incremental_analyze=skip_incremental_analyze,
        )
    except Exception as exc:
        try:
            if bootstrap_smiles_file and os.path.exists(bootstrap_smiles_file):
                os.remove(bootstrap_smiles_file)
        except Exception:
            pass
        try:
            if incremental_delta_file and os.path.exists(incremental_delta_file):
                os.remove(incremental_delta_file)
        except Exception:
            pass
        logger.error("Failed importing compound batch: %s", exc, exc_info=True)
        return False
    finally:
        try:
            if incremental_delta_file and os.path.exists(incremental_delta_file):
                os.remove(incremental_delta_file)
        except Exception:
            pass


def delete_compound_batch_postgres(
    postgres_url: str,
    *,
    schema: str,
    batch_id: str,
    output_dir: str = "data",
    max_heavy_atoms: int = 50,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    fragment_jobs: int = 8,
    index_maintenance_work_mem_mb: int = 1024,
    index_work_mem_mb: int = 128,
    index_parallel_workers: int = 4,
    index_commit_every_flushes: int = 0,
    incremental_index_shards: int = 1,
    incremental_index_jobs: int = 1,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    skip_incremental_analyze: bool = False,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for compound batch delete.")
        return False
    normalized_schema = _validate_pg_schema(schema)
    normalized_batch_id = str(batch_id or "").strip()
    removed_rows_payload: List[Tuple[str, str, str]] = []
    if not normalized_batch_id:
        logger.error("--delete_compounds_batch requires a non-empty batch id.")
        return False
    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_compound_batch_tables(cursor, seed_base_from_compound=True)
                _ensure_property_batch_tables(cursor, seed_base_from_compound_property=True)
                cursor.execute(
                    f"""
                    SELECT batch_id, batch_seq, total_rows, valid_rows, dedup_rows, new_unique_rows
                    FROM {COMPOUND_BATCH_HEADER_TABLE}
                    WHERE batch_id = %s
                    LIMIT 1
                    """,
                    [normalized_batch_id],
                )
                row = cursor.fetchone()
                if row is None:
                    logger.error("Compound batch '%s' not found in schema '%s'.", normalized_batch_id, normalized_schema)
                    conn.rollback()
                    return False
                cursor.execute("DROP TABLE IF EXISTS tmp_inc_deleted_batch_rows")
                cursor.execute(
                    f"""
                    CREATE TEMP TABLE tmp_inc_deleted_batch_rows AS
                    SELECT clean_smiles, public_id, input_smiles
                    FROM {COMPOUND_BATCH_ROWS_TABLE}
                    WHERE batch_id = %s
                    """,
                    [normalized_batch_id],
                )
                cursor.execute(f"DELETE FROM {COMPOUND_BATCH_HEADER_TABLE} WHERE batch_id = %s", [normalized_batch_id])
                cursor.execute("DROP TABLE IF EXISTS tmp_inc_removed_after_delete")
                cursor.execute(
                    f"""
                    CREATE TEMP TABLE tmp_inc_removed_after_delete AS
                    SELECT d.clean_smiles, d.public_id, d.input_smiles
                    FROM tmp_inc_deleted_batch_rows d
                    LEFT JOIN {COMPOUND_BATCH_BASE_TABLE} b
                           ON b.clean_smiles = d.clean_smiles
                    LEFT JOIN {COMPOUND_BATCH_ROWS_TABLE} r
                           ON r.clean_smiles = d.clean_smiles
                    WHERE b.clean_smiles IS NULL
                      AND r.clean_smiles IS NULL
                    """
                )
                cursor.execute(
                    """
                    SELECT clean_smiles, public_id, input_smiles
                    FROM tmp_inc_removed_after_delete
                    """
                )
                removed_rows_payload = [
                    (
                        str(item[0] or "").strip(),
                        str(item[1] or "").strip(),
                        str(item[2] or "").strip(),
                    )
                    for item in cursor.fetchall()
                    if str(item[0] or "").strip()
                ]
                conn.commit()
        logger.info(
            "Deleted compound batch '%s' from schema '%s'. Starting index sync (removed_structural=%s)...",
            normalized_batch_id,
            normalized_schema,
            len(removed_rows_payload),
        )
        return _incremental_reindex_compound_delta(
            postgres_url,
            schema=normalized_schema,
            changed_rows=removed_rows_payload,
            output_dir=output_dir,
            max_heavy_atoms=max_heavy_atoms,
            skip_attachment_enrichment=skip_attachment_enrichment,
            attachment_force_recompute=attachment_force_recompute,
            fragment_jobs=fragment_jobs,
            index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            index_work_mem_mb=index_work_mem_mb,
            index_parallel_workers=index_parallel_workers,
            index_commit_every_flushes=index_commit_every_flushes,
            incremental_index_shards=incremental_index_shards,
            incremental_index_jobs=incremental_index_jobs,
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
            delta_mode="delete",
            skip_incremental_analyze=skip_incremental_analyze,
        )
    except Exception as exc:
        logger.error("Failed deleting compound batch '%s': %s", normalized_batch_id, exc, exc_info=True)
        return False


def list_compound_batches_postgres(
    postgres_url: str,
    *,
    schema: str,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for compound batch listing.")
        return False
    normalized_schema = _validate_pg_schema(schema)
    try:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_compound_batch_tables(cursor, seed_base_from_compound=False)
                cursor.execute(
                    f"""
                    SELECT
                        b.batch_id,
                        b.batch_seq,
                        b.label,
                        b.source_file,
                        b.imported_at,
                        b.total_rows,
                        b.valid_rows,
                        b.dedup_rows,
                        b.new_unique_rows,
                        COUNT(r.clean_smiles) AS inserted_rows
                    FROM {COMPOUND_BATCH_HEADER_TABLE} b
                    LEFT JOIN {COMPOUND_BATCH_ROWS_TABLE} r
                           ON r.batch_id = b.batch_id
                    GROUP BY
                        b.batch_id, b.batch_seq, b.label, b.source_file, b.imported_at,
                        b.total_rows, b.valid_rows, b.dedup_rows, b.new_unique_rows
                    ORDER BY b.batch_seq DESC
                    """
                )
                rows = cursor.fetchall()
        print("\n" + "=" * 80)
        print(f"Compound batches in schema '{normalized_schema}'")
        print("=" * 80)
        if not rows:
            print("(empty)")
            return True
        for row in rows:
            print(
                f"- batch_id={row[0]} seq={row[1]} imported_at={row[4]} "
                f"rows(total/valid/dedup/new_unique/inserted)={row[5]}/{row[6]}/{row[7]}/{row[8]}/{row[9]}"
            )
            if str(row[2] or "").strip():
                print(f"  label: {row[2]}")
            if str(row[3] or "").strip():
                print(f"  source_file: {row[3]}")
        return True
    except Exception as exc:
        logger.error("Failed listing compound batches: %s", exc, exc_info=True)
        return False


def apply_property_metadata_postgres(postgres_url: str, schema: str, metadata_file: str) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL metadata import.")
        return False
    metadata_path = str(metadata_file or "").strip()
    if not metadata_path:
        return True
    if not os.path.exists(metadata_path):
        logger.error("Property metadata file not found: %s", metadata_path)
        return False

    normalized_schema = _validate_pg_schema(schema)
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            first_line = handle.readline()
            handle.seek(0)
            delimiter = "\t" if ("\t" in first_line) else ","
            reader = csv.DictReader(handle, delimiter=delimiter)
            rows = [dict(row or {}) for row in reader]
    except Exception as exc:
        logger.error("Failed to parse property metadata file %s: %s", metadata_path, exc)
        return False
    if not rows:
        logger.warning("Property metadata file is empty: %s", metadata_path)
        return True

    metadata_columns = ("base", "unit", "display_name", "display_base", "display_unit", "change_displayed")
    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                for column_name in metadata_columns:
                    if not _pg_column_exists(cursor, normalized_schema, "property_name", column_name):
                        cursor.execute(f"ALTER TABLE property_name ADD COLUMN {column_name} VARCHAR(255)")
                for row in rows:
                    prop_name = str(row.get("property_name") or row.get("name") or "").strip()
                    if not prop_name:
                        continue
                    values = [str(row.get(column_name) or "").strip() for column_name in metadata_columns]
                    cursor.execute(
                        """
                        UPDATE property_name
                           SET base = %s,
                               unit = %s,
                               display_name = %s,
                               display_base = %s,
                               display_unit = %s,
                               change_displayed = %s
                         WHERE name = %s
                        """,
                        [*values, prop_name],
                    )
                conn.commit()
        logger.info("Applied property metadata from %s to PostgreSQL schema '%s'", metadata_path, normalized_schema)
        return True
    except Exception as exc:
        logger.error("Failed to apply property metadata to PostgreSQL: %s", exc, exc_info=True)
        return False


def finalize_postgres_database(
    postgres_url: str,
    *,
    schema: str = "public",
    force_recompute_num_frags: bool = False,
    index_maintenance_work_mem_mb: int = 0,
    index_work_mem_mb: int = 0,
    index_parallel_workers: int = 0,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL finalize step. Install: pip install psycopg[binary]")
        return False

    normalized_schema = _validate_pg_schema(schema)
    normalized_index_maintenance_mem_mb = max(0, int(index_maintenance_work_mem_mb or 0))
    normalized_index_work_mem_mb = max(0, int(index_work_mem_mb or 0))
    normalized_index_parallel_workers = max(0, int(index_parallel_workers or 0))
    enable_construct_tables = bool(build_construct_tables)
    enable_constant_smiles_mol_index = bool(build_constant_smiles_mol_index)
    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _ensure_postgres_attachment_columns(cursor, normalized_schema)
                _assert_postgres_attachment_columns_ready(cursor, normalized_schema)
                _apply_postgres_build_tuning(
                    cursor,
                    maintenance_work_mem_mb=normalized_index_maintenance_mem_mb,
                    work_mem_mb=normalized_index_work_mem_mb,
                    parallel_maintenance_workers=normalized_index_parallel_workers,
                )
                _enrich_attachment_schema_postgres(
                    cursor,
                    normalized_schema,
                    force_recompute=force_recompute_num_frags,
                    enable_constant_smiles_mol_index=enable_constant_smiles_mol_index,
                )
                if enable_construct_tables:
                    _rebuild_construct_tables_postgres(cursor)
                _create_postgres_core_indexes(
                    cursor,
                    normalized_schema,
                    enable_constant_smiles_mol_index=enable_constant_smiles_mol_index,
                )
                cursor.execute("ANALYZE")
                conn.commit()

            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                cursor.execute("SELECT COUNT(*) FROM compound")
                compounds = int(cursor.fetchone()[0])
                cursor.execute("SELECT COUNT(*) FROM rule")
                rules = int(cursor.fetchone()[0])
                cursor.execute("SELECT COUNT(*) FROM pair")
                pairs = int(cursor.fetchone()[0])
                if enable_construct_tables:
                    cursor.execute("SELECT COUNT(*) FROM from_construct")
                    from_construct_rows = int(cursor.fetchone()[0])
                    cursor.execute("SELECT COUNT(*) FROM to_construct")
                    to_construct_rows = int(cursor.fetchone()[0])
                else:
                    from_construct_rows = 0
                    to_construct_rows = 0

        logger.info(
            "PostgreSQL finalize complete: compounds=%s rules=%s pairs=%s from_construct=%s to_construct=%s",
            compounds,
            rules,
            pairs,
            from_construct_rows,
            to_construct_rows,
        )
        return True
    except Exception as exc:
        logger.error("Failed finalizing PostgreSQL mmpdb: %s", exc, exc_info=True)
        return False


def create_sample_data(output_dir: str) -> str:
    """Create sample compound data for testing"""
    logger = logging.getLogger(__name__)
    
    # Create sample SMILES data with diverse drug-like compounds
    sample_compounds = [
        # Known drugs
        "CC(C)Cc1ccc(cc1)[C@@H](C)C(=O)O",  # Ibuprofen
        "CC(=O)Oc1ccccc1C(=O)O",  # Aspirin
        "CCc1ccc(cc1)C(=O)Nc2ccc(cc2)S(=O)(=O)N",  # Celecoxib-like
        "COc1ccc2nc(sc2c1)S(=O)Cc3ncc(c(c3C)OC)C",  # Omeprazole
        "Cc1c(cccc1Cl)Cc2cnc(nc2N)N",  # Pyrimethamine
        "CC(C)(C)NCC(c1ccc(c(c1)O)CO)O",  # Salbutamol
        "CN1CCC[C@H]1c2cccnc2",  # Nicotine
        "CC(C)NCC(c1ccc(cc1)O)O",  # Isoproterenol
        "CCN(CC)CCNC(=O)c1cc(ccc1OC)N",  # Procainamide
        
        # Drug-like compounds with variety in scaffolds
        "COc1ccc(cc1)CCN",  # Phenethylamine core
        "c1ccc2c(c1)c(cn2)CCN",  # Indole core
        "CCN(CC)c1ccc(cc1)C(=O)O",  # Aromatic carboxyl
        "Cc1ccc(cc1)S(=O)(=O)Nc2ncccn2",  # Sulfonamide
        "COc1cc2c(cc1OC)CCN(C2)C",  # Benzisoquinoline
        "CCc1ccc(cc1)C(CC)C(=O)O",  # Propionic acid
        "Nc1ccc(cc1)S(=O)(=O)Nc2nc(cc(n2)OC)OC",  # Diaminopyrimidine
        "c1cc2c(cc1N)nc(nc2N)N",  # Pteridine core
        "COc1ccc2c(c1)c(cn2C(C)C)c3nc4ccccc4[nH]3",  # Benzimidazole
        "CC1COc2c(O1)ccc(c2)CCN",  # Benzodioxane
        
        # Additional structural diversity
        "CCN1CCN(CC1)c2ccc(cc2)OC",  # Piperazine
        "c1coc2c1cccc2N",  # Benzofuran
        "CC1CCC(CC1)NC(=O)c2ccccc2",  # Cyclohexyl benzamide
        "COc1cccc(c1)C(=O)Nc2ccccc2",  # Methoxy benzanilide
        "c1cc2c(cc1Cl)nc(nc2N)N",  # Quinazoline
        "CCc1nnc(s1)SCC(=O)Nc2ccccc2",  # Thiadiazole
    ]
    
    # Create SMILES file
    smiles_file = os.path.join(output_dir, "sample_compounds.smi")
    
    with open(smiles_file, 'w') as f:
        # Write header in mmpdb format (SMILES<TAB>ID)
        f.write("SMILES\tID\n")
        for i, smiles in enumerate(sample_compounds):
            try:
                mol = Chem.MolFromSmiles(smiles)
                if mol:  # Only include valid SMILES
                    f.write(f"{smiles}\tSAMPLE_{i+1:03d}\n")
            except Exception as e:
                logger.warning(f"Skipping invalid SMILES: {smiles} - {e}")
    
    logger.info(f"Created sample compounds file: {smiles_file}")
    logger.info(f"Total compounds: {len(sample_compounds)}")
    
    return smiles_file

def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="Setup MMP database for lead optimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:

从ChEMBL自动下载并导入PostgreSQL:
  python -m lead_optimization.mmp_lifecycle.engine --download_chembl --postgres_url postgresql://user:pass@host:5432/db

创建示例数据库并导入PostgreSQL:
  python -m lead_optimization.mmp_lifecycle.engine --create_sample --postgres_url postgresql://user:pass@host:5432/db

从SMILES文件创建数据库:
  python -m lead_optimization.mmp_lifecycle.engine --smiles_file compounds.smi --postgres_url postgresql://user:pass@host:5432/db

从结构+属性文件创建数据库（例如 ChEMBL_CYP3A4_hERG）:
  python -m lead_optimization.mmp_lifecycle.engine --structures_file ChEMBL_CYP3A4_hERG_structures.smi --properties_file ChEMBL_CYP3A4_hERG_props.txt --property_metadata_file ChEMBL_CYP3A4_hERG_metadata.csv --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl_cyp3a4_herg

增量导入属性批次（SMILES + 一个或多个属性列）:
  python -m lead_optimization.mmp_lifecycle.engine --import_properties_batch property_batch.tsv --property_batch_label "assay_2026w08" --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

列出属性批次:
  python -m lead_optimization.mmp_lifecycle.engine --list_properties_batches --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

删除属性批次并回滚:
  python -m lead_optimization.mmp_lifecycle.engine --delete_properties_batch batch_20260221_101500 --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

增量导入化合物批次（仅增量重建受影响常量，不做全量index）:
  python -m lead_optimization.mmp_lifecycle.engine --import_compounds_batch compounds_batch.tsv --compound_batch_label "wave_03" --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

列出化合物批次:
  python -m lead_optimization.mmp_lifecycle.engine --list_compounds_batches --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

删除化合物批次并自动增量重建:
  python -m lead_optimization.mmp_lifecycle.engine --delete_compounds_batch compound_batch_20260221_101500 --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl36_full

SMILES文件格式:
每行一个化合物，格式为: SMILES<TAB>ID<TAB>MW(可选)
例如:
CCc1ccc(cc1)C(=O)Nc2ccc(cc2)S(=O)(=O)N    CHEMBL123    354.4
COc1cc2c(cc1OC)CCN(C2)C    CHEMBL456    193.2
        """
    )
    
    # Input options (build flow)
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument(
        '--smiles_file',
        type=str,
        help='包含SMILES的输入文件'
    )
    input_group.add_argument(
        '--structures_file',
        type=str,
        help='包含SMILES结构的输入文件（带表头，常用于hERG/CYP等属性数据集）'
    )
    input_group.add_argument(
        '--download_chembl',
        action='store_true',
        help='从ChEMBL自动下载化合物数据构建数据库'
    )
    input_group.add_argument(
        '--create_sample',
        action='store_true',
        help='创建示例化合物数据库'
    )
    input_group.add_argument(
        '--fragments_file',
        type=str,
        help='已存在的 .fragdb 文件（跳过fragment步骤，直接index到PostgreSQL）'
    )
    # ChEMBL download options
    parser.add_argument(
        '--subset_size',
        type=int,
        help=argparse.SUPPRESS
    )
    
    parser.add_argument(
        '--min_mw',
        type=float,
        default=100.0,
        help='最小分子量过滤 (默认: 100.0)'
    )
    
    parser.add_argument(
        '--max_mw',
        type=float,
        default=800.0,
        help='最大分子量过滤 (默认: 800.0)'
    )

    parser.add_argument(
        '--properties_file',
        type=str,
        default='',
        help='属性文件（TSV/CSV，首列为ID；用于mmpdb index --properties）'
    )

    parser.add_argument(
        '--property_metadata_file',
        type=str,
        default='',
        help='属性元数据文件（property_name/base/unit/display_*），导入PostgreSQL后写入property_name'
    )

    parser.add_argument(
        '--import_properties_batch',
        type=str,
        default='',
        help='增量属性批次导入文件（CSV/TSV，至少包含一列SMILES和一列数值属性）'
    )

    parser.add_argument(
        '--delete_properties_batch',
        type=str,
        default='',
        help='按 batch_id 删除增量属性批次，并自动回滚受影响的 compound_property'
    )

    parser.add_argument(
        '--list_properties_batches',
        action='store_true',
        help='列出当前 schema 下所有增量属性批次'
    )

    parser.add_argument(
        '--property_batch_id',
        type=str,
        default='',
        help='导入增量属性时的批次ID（默认自动生成 batch_YYYYMMDD_HHMMSS）'
    )

    parser.add_argument(
        '--property_batch_label',
        type=str,
        default='',
        help='导入增量属性时的批次标签'
    )

    parser.add_argument(
        '--property_batch_notes',
        type=str,
        default='',
        help='导入增量属性时的批次备注'
    )

    parser.add_argument(
        '--property_batch_smiles_column',
        type=str,
        default='',
        help='增量属性文件中SMILES列名（默认自动识别 smiles/canonical_smiles）'
    )

    parser.add_argument(
        '--property_batch_no_canonicalize_smiles',
        action='store_true',
        help='增量属性导入时不做RDKit canonicalization（仅当输入SMILES已与compound.clean_smiles一致时使用）'
    )

    parser.add_argument(
        '--import_compounds_batch',
        type=str,
        default='',
        help='增量化合物批次导入文件（CSV/TSV，必须包含SMILES列；仅增量重建受影响常量）'
    )

    parser.add_argument(
        '--delete_compounds_batch',
        type=str,
        default='',
        help='按 batch_id 删除增量化合物批次（仅增量重建受影响常量）'
    )

    parser.add_argument(
        '--list_compounds_batches',
        action='store_true',
        help='列出当前 schema 下所有增量化合物批次'
    )

    parser.add_argument(
        '--compound_batch_id',
        type=str,
        default='',
        help='导入增量化合物时的批次ID（默认自动生成 compound_batch_YYYYMMDD_HHMMSS）'
    )

    parser.add_argument(
        '--compound_batch_label',
        type=str,
        default='',
        help='导入增量化合物时的批次标签'
    )

    parser.add_argument(
        '--compound_batch_notes',
        type=str,
        default='',
        help='导入增量化合物时的批次备注'
    )

    parser.add_argument(
        '--compound_batch_smiles_column',
        type=str,
        default='',
        help='增量化合物文件中SMILES列名（默认自动识别 smiles/canonical_smiles）'
    )

    parser.add_argument(
        '--compound_batch_id_column',
        type=str,
        default='',
        help='增量化合物文件中ID列名（默认自动识别 id/public_id/cmpd_chemblid）'
    )

    parser.add_argument(
        '--compound_batch_no_canonicalize_smiles',
        action='store_true',
        help='增量化合物导入时不做RDKit canonicalization（仅当输入SMILES已与compound.clean_smiles一致时使用）'
    )
    
    parser.add_argument(
        '--output_dir',
        type=str,
        default='data',
        help='输出目录 (默认: data)'
    )

    parser.add_argument(
        '--postgres_url',
        type=str,
        default=str(os.getenv('LEAD_OPT_MMP_DB_URL', '') or '').strip(),
        help='PostgreSQL连接串，例如 postgresql://user:pass@host:5432/leadopt'
    )

    parser.add_argument(
        '--postgres_schema',
        type=str,
        default=str(os.getenv('LEAD_OPT_MMP_DB_SCHEMA', 'public') or 'public').strip(),
        help='PostgreSQL schema 名称 (默认: public)'
    )
    
    # Parameters
    parser.add_argument(
        '--max_heavy_atoms',
        type=int,
        default=50,
        help='最大重原子数 (默认: 50)'
    )

    parser.add_argument(
        '--skip_attachment_enrichment',
        action='store_true',
        help='跳过 attachment/num_frags 增强（不推荐）'
    )

    parser.add_argument(
        '--attachment_force_recompute',
        action='store_true',
        help='强制重算 num_frags（即使已有值）'
    )

    parser.add_argument(
        '--fragment_jobs',
        type=int,
        default=max(1, min(16, int(os.cpu_count() or 1))),
        help='mmpdb fragment 并行线程数 (默认: min(16, CPU核数))'
    )

    parser.add_argument(
        '--pg_index_maintenance_work_mem_mb',
        type=int,
        default=2048,
        help='索引构建阶段 maintenance_work_mem (MB, 默认: 2048)'
    )

    parser.add_argument(
        '--pg_index_work_mem_mb',
        type=int,
        default=64,
        help='索引/排序阶段 work_mem (MB, 默认: 64)'
    )

    parser.add_argument(
        '--pg_index_parallel_workers',
        type=int,
        default=2,
        help='索引构建并行 worker 上限 (max_parallel_maintenance_workers)'
    )

    parser.add_argument(
        '--pg_incremental_index_shards',
        type=int,
        default=1,
        help='增量结构重建时按受影响常量分片数量（>1 可降低单次索引峰值）'
    )

    parser.add_argument(
        '--pg_incremental_index_jobs',
        type=int,
        default=1,
        help='增量分片索引并发作业数（建议 <= 可用CPU/内存能力）'
    )

    parser.add_argument(
        '--pg_skip_construct_tables',
        action='store_true',
        help='跳过 from_construct/to_construct 重型构建（可显著降低内存和时间）'
    )

    parser.add_argument(
        '--pg_skip_constant_smiles_mol_index',
        action='store_true',
        help='跳过 constant_smiles 的 smiles_mol GiST（通常不影响当前推理链路）'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='覆盖现有数据库'
    )

    parser.add_argument(
        '--keep_fragdb',
        action='store_true',
        help='保留中间 .fragdb 文件（默认构建成功后自动删除）'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)

    postgres_url = str(args.postgres_url or "").strip()
    build_mode_selected = bool(
        args.smiles_file or args.structures_file or args.download_chembl or args.create_sample or args.fragments_file
    )
    property_batch_import_file = str(args.import_properties_batch or "").strip()
    property_batch_delete_id = str(args.delete_properties_batch or "").strip()
    property_batch_list_mode = bool(args.list_properties_batches)
    property_batch_mode_count = (
        int(bool(property_batch_import_file))
        + int(bool(property_batch_delete_id))
        + int(property_batch_list_mode)
    )
    compound_batch_import_file = str(args.import_compounds_batch or "").strip()
    compound_batch_delete_id = str(args.delete_compounds_batch or "").strip()
    compound_batch_list_mode = bool(args.list_compounds_batches)
    compound_batch_mode_count = (
        int(bool(compound_batch_import_file))
        + int(bool(compound_batch_delete_id))
        + int(compound_batch_list_mode)
    )
    if property_batch_mode_count > 1:
        parser.error(
            "Only one property batch operation can be selected: "
            "--import_properties_batch or --delete_properties_batch or --list_properties_batches."
        )
    if compound_batch_mode_count > 1:
        parser.error(
            "Only one compound batch operation can be selected: "
            "--import_compounds_batch or --delete_compounds_batch or --list_compounds_batches."
        )
    if property_batch_mode_count > 0 and compound_batch_mode_count > 0:
        parser.error("Property-batch operation and compound-batch operation cannot be combined in one run.")
    if property_batch_mode_count == 0 and compound_batch_mode_count == 0 and not build_mode_selected:
        parser.error(
            "Build mode requires one input source: "
            "--smiles_file/--structures_file/--download_chembl/--create_sample/--fragments_file; "
            "or run a property/compound batch mode."
        )
    if (property_batch_mode_count > 0 or compound_batch_mode_count > 0) and build_mode_selected:
        parser.error(
            "Batch operation cannot be combined with build input options in one run."
        )

    if args.fragment_jobs < 1:
        args.fragment_jobs = 1
    if args.pg_index_maintenance_work_mem_mb < 0:
        args.pg_index_maintenance_work_mem_mb = 0
    if args.pg_index_work_mem_mb < 0:
        args.pg_index_work_mem_mb = 0
    if args.pg_index_parallel_workers < 0:
        args.pg_index_parallel_workers = 0
    if args.pg_incremental_index_shards < 1:
        args.pg_incremental_index_shards = 1
    if args.pg_incremental_index_jobs < 1:
        args.pg_incremental_index_jobs = 1

    try:
        if not postgres_url:
            logger.error("--postgres_url is required (PostgreSQL-only runtime).")
            sys.exit(1)
        if not HAS_PSYCOPG:
            logger.error("PostgreSQL import requested but psycopg is not installed.")
            logger.error("  pip install psycopg[binary]")
            sys.exit(1)

        if property_batch_mode_count > 0:
            if property_batch_list_mode:
                ok = list_property_batches_postgres(
                    postgres_url,
                    schema=args.postgres_schema,
                )
                if not ok:
                    sys.exit(1)
                sys.exit(0)

            if property_batch_delete_id:
                ok = _run_with_registry_status(
                    postgres_url,
                    args.postgres_schema,
                    "delete_property_batch",
                    lambda: delete_property_batch_postgres(
                        postgres_url,
                        schema=args.postgres_schema,
                        batch_id=property_batch_delete_id,
                    ),
                )
                if not ok:
                    sys.exit(1)
                print("\n" + "=" * 60)
                print("属性批次删除完成!")
                print("=" * 60)
                print(f"PostgreSQL: {postgres_url}")
                print(f"schema: {args.postgres_schema}")
                print(f"deleted batch_id: {property_batch_delete_id}")
                sys.exit(0)

            normalized_import_batch_id = _normalize_property_batch_id(args.property_batch_id)
            property_metadata_file = str(args.property_metadata_file or "").strip()

            def _do_property_batch_import() -> bool:
                ok = import_property_batch_postgres(
                    postgres_url,
                    schema=args.postgres_schema,
                    property_file=property_batch_import_file,
                    batch_id=normalized_import_batch_id,
                    batch_label=args.property_batch_label,
                    batch_notes=args.property_batch_notes,
                    smiles_column=args.property_batch_smiles_column,
                    canonicalize_smiles=not args.property_batch_no_canonicalize_smiles,
                )
                if not ok:
                    return False
                if property_metadata_file:
                    return apply_property_metadata_postgres(
                        postgres_url,
                        args.postgres_schema,
                        property_metadata_file,
                    )
                return True

            import_ok = _run_with_registry_status(
                postgres_url,
                args.postgres_schema,
                "import_property_batch",
                _do_property_batch_import,
            )
            if not import_ok:
                sys.exit(1)
            print("\n" + "=" * 60)
            print("属性批次导入完成!")
            print("=" * 60)
            print(f"PostgreSQL: {postgres_url}")
            print(f"schema: {args.postgres_schema}")
            print(f"batch_id: {normalized_import_batch_id}")
            sys.exit(0)

        if compound_batch_mode_count > 0:
            if not HAS_RDKIT_MMPDB:
                logger.error("Compound batch operation requires RDKit/mmpdb.")
                logger.error("  pip install rdkit mmpdb requests")
                sys.exit(1)
            if not HAS_PSYCOPG2:
                logger.error(
                    "Compound batch incremental index requires psycopg2 (mmpdb PostgreSQL writer backend)."
                )
                logger.error("  pip install psycopg2-binary")
                sys.exit(1)

            if compound_batch_list_mode:
                ok = list_compound_batches_postgres(
                    postgres_url,
                    schema=args.postgres_schema,
                )
                if not ok:
                    sys.exit(1)
                sys.exit(0)

            if compound_batch_delete_id:
                ok = _run_with_registry_status(
                    postgres_url,
                    args.postgres_schema,
                    "delete_compound_batch",
                    lambda: delete_compound_batch_postgres(
                        postgres_url,
                        schema=args.postgres_schema,
                        batch_id=compound_batch_delete_id,
                        output_dir=args.output_dir,
                        max_heavy_atoms=args.max_heavy_atoms,
                        skip_attachment_enrichment=args.skip_attachment_enrichment,
                        attachment_force_recompute=args.attachment_force_recompute,
                        fragment_jobs=args.fragment_jobs,
                        index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
                        index_work_mem_mb=args.pg_index_work_mem_mb,
                        index_parallel_workers=args.pg_index_parallel_workers,
                        incremental_index_shards=args.pg_incremental_index_shards,
                        incremental_index_jobs=args.pg_incremental_index_jobs,
                        build_construct_tables=not args.pg_skip_construct_tables,
                        build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
                    ),
                )
                if not ok:
                    sys.exit(1)
                print("\n" + "=" * 60)
                print("化合物批次删除并增量索引完成!")
                print("=" * 60)
                print(f"PostgreSQL: {postgres_url}")
                print(f"schema: {args.postgres_schema}")
                print(f"deleted compound batch_id: {compound_batch_delete_id}")
                sys.exit(0)

            normalized_compound_batch_id = _normalize_compound_batch_id(args.compound_batch_id)
            property_metadata_file = str(args.property_metadata_file or "").strip()

            def _do_compound_batch_import() -> bool:
                ok = import_compound_batch_postgres(
                    postgres_url,
                    schema=args.postgres_schema,
                    structures_file=compound_batch_import_file,
                    batch_id=normalized_compound_batch_id,
                    batch_label=args.compound_batch_label,
                    batch_notes=args.compound_batch_notes,
                    smiles_column=args.compound_batch_smiles_column,
                    id_column=args.compound_batch_id_column,
                    canonicalize_smiles=not args.compound_batch_no_canonicalize_smiles,
                    output_dir=args.output_dir,
                    max_heavy_atoms=args.max_heavy_atoms,
                    skip_attachment_enrichment=args.skip_attachment_enrichment,
                    attachment_force_recompute=args.attachment_force_recompute,
                    fragment_jobs=args.fragment_jobs,
                    index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
                    index_work_mem_mb=args.pg_index_work_mem_mb,
                    index_parallel_workers=args.pg_index_parallel_workers,
                    incremental_index_shards=args.pg_incremental_index_shards,
                    incremental_index_jobs=args.pg_incremental_index_jobs,
                    build_construct_tables=not args.pg_skip_construct_tables,
                    build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
                )
                if not ok:
                    return False
                if property_metadata_file:
                    return apply_property_metadata_postgres(
                        postgres_url,
                        args.postgres_schema,
                        property_metadata_file,
                    )
                return True

            import_ok = _run_with_registry_status(
                postgres_url,
                args.postgres_schema,
                "import_compound_batch",
                _do_compound_batch_import,
            )
            if not import_ok:
                sys.exit(1)
            print("\n" + "=" * 60)
            print("化合物批次导入并增量索引完成!")
            print("=" * 60)
            print(f"PostgreSQL: {postgres_url}")
            print(f"schema: {args.postgres_schema}")
            print(f"compound batch_id: {normalized_compound_batch_id}")
            sys.exit(0)

        # Build mode: check build dependencies
        if not HAS_RDKIT_MMPDB:
            logger.error("RDKit and mmpdb are required. Please install them:")
            logger.error("  pip install rdkit mmpdb requests")
            sys.exit(1)
        if not HAS_PSYCOPG2:
            logger.error(
                "mmpdb PostgreSQL index backend requires psycopg2, but it is not installed."
            )
            logger.error("  pip install psycopg2-binary")
            logger.error("  # or install lead_optimization/requirements.txt again")
            sys.exit(1)
        
        # Create output directory
        os.makedirs(args.output_dir, exist_ok=True)

        smiles_file = None
        fragments_input_file = None
        properties_file = str(args.properties_file or "").strip()
        property_metadata_file = str(args.property_metadata_file or "").strip()

        if args.create_sample:
            logger.info("Creating sample compound database...")
            smiles_file = create_sample_data(args.output_dir)

        elif args.download_chembl:
            logger.info("Downloading ChEMBL compound data...")
            if args.subset_size:
                logger.warning(
                    "--subset_size=%s is for debugging only; production build should use full dataset.",
                    args.subset_size,
                )
            smiles_file = download_chembl_data(
                args.output_dir,
                subset_size=args.subset_size,
                min_mw=args.min_mw,
                max_mw=args.max_mw,
            )
            if not smiles_file:
                logger.error("Failed to download ChEMBL data")
                sys.exit(1)

        elif args.smiles_file:
            smiles_file = args.smiles_file
            if not os.path.exists(smiles_file):
                logger.error(f"SMILES file not found: {smiles_file}")
                sys.exit(1)

        elif args.structures_file:
            smiles_file = str(args.structures_file or "").strip()
            if not smiles_file or not os.path.exists(smiles_file):
                logger.error("structures_file not found: %s", smiles_file)
                sys.exit(1)
        elif args.fragments_file:
            fragments_input_file = str(args.fragments_file or "").strip()
            if not fragments_input_file or not os.path.exists(fragments_input_file):
                logger.error("fragments_file not found: %s", fragments_input_file)
                sys.exit(1)
            if not fragments_input_file.endswith(".fragdb"):
                logger.warning(
                    "fragments_file does not end with .fragdb: %s (continuing anyway)",
                    fragments_input_file,
                )

        # Validate SMILES file
        if smiles_file and not validate_smiles_file(smiles_file):
            logger.error("SMILES file validation failed")
            sys.exit(1)
        if properties_file and not os.path.exists(properties_file):
            logger.error("properties_file not found: %s", properties_file)
            sys.exit(1)
        if property_metadata_file and not os.path.exists(property_metadata_file):
            logger.error("property_metadata_file not found: %s", property_metadata_file)
            sys.exit(1)

        if fragments_input_file:
            logger.info("Indexing existing fragments file into PostgreSQL...")

            def _do_fragdb_index_build() -> bool:
                ok = _index_fragments_to_postgres(
                    fragments_input_file,
                    postgres_url,
                    postgres_schema=args.postgres_schema,
                    force_rebuild_schema=args.force,
                    properties_file=properties_file if properties_file else None,
                    skip_attachment_enrichment=args.skip_attachment_enrichment,
                    attachment_force_recompute=args.attachment_force_recompute,
                    index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
                    index_work_mem_mb=args.pg_index_work_mem_mb,
                    index_parallel_workers=args.pg_index_parallel_workers,
                    build_construct_tables=not args.pg_skip_construct_tables,
                    build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
                )
                if not ok:
                    return False
                if property_metadata_file:
                    return apply_property_metadata_postgres(
                        postgres_url,
                        args.postgres_schema,
                        property_metadata_file,
                    )
                return True

            build_ok = _run_with_registry_status(
                postgres_url,
                args.postgres_schema,
                "index_fragdb",
                _do_fragdb_index_build,
            )
        else:
            base_name = os.path.splitext(os.path.basename(str(smiles_file or "").strip()))[0] or "dataset"
            fragments_file = os.path.join(args.output_dir, f"{base_name}.fragdb")
            if os.path.exists(fragments_file):
                if args.force:
                    try:
                        os.remove(fragments_file)
                        logger.info("Removed existing fragments file due to --force: %s", fragments_file)
                    except Exception as exc:
                        logger.error("Failed to remove old fragments file %s: %s", fragments_file, exc)
                        sys.exit(1)
                else:
                    logger.error("Fragments file already exists: %s", fragments_file)
                    logger.error("Use --force to overwrite")
                    sys.exit(1)

            logger.info("Building MMP dataset directly into PostgreSQL...")

            def _do_full_database_build() -> bool:
                ok = create_mmp_database(
                    smiles_file,
                    postgres_url,
                    args.max_heavy_atoms,
                    output_dir=args.output_dir,
                    postgres_schema=args.postgres_schema,
                    properties_file=properties_file if properties_file else None,
                    skip_attachment_enrichment=args.skip_attachment_enrichment,
                    attachment_force_recompute=args.attachment_force_recompute,
                    force_rebuild_schema=args.force,
                    keep_fragments=args.keep_fragdb,
                    fragment_jobs=args.fragment_jobs,
                    index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
                    index_work_mem_mb=args.pg_index_work_mem_mb,
                    index_parallel_workers=args.pg_index_parallel_workers,
                    build_construct_tables=not args.pg_skip_construct_tables,
                    build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
                )
                if not ok:
                    return False
                if property_metadata_file:
                    return apply_property_metadata_postgres(
                        postgres_url,
                        args.postgres_schema,
                        property_metadata_file,
                    )
                return True

            build_ok = _run_with_registry_status(
                postgres_url,
                args.postgres_schema,
                "build_database",
                _do_full_database_build,
            )
        if not build_ok:
            logger.error("MMP database setup failed")
            sys.exit(1)
        logger.info("PostgreSQL database is ready.")

        # Print usage instructions
        print("\n" + "=" * 60)
        print("数据库设置完成!")
        print("=" * 60)
        print(f"PostgreSQL: {postgres_url}")
        print(f"schema: {args.postgres_schema}")
        print("运行时建议:")
        print(f"  export LEAD_OPT_MMP_DB_URL='{postgres_url}'")
        print(f"  export LEAD_OPT_MMP_DB_SCHEMA='{args.postgres_schema}'")
        print("\n现在可以在 VBio Lead Optimization 工作流中运行 MMP 查询与候选打分。")
    
    except KeyboardInterrupt:
        logger.info("Setup interrupted by user")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
