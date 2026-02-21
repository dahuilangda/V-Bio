#!/usr/bin/env python3
# /data/boltz_webui/lead_optimization/setup_mmpdb.py

"""
Setup script for initializing mmpdb database for lead optimization
Enhanced with ChEMBL data download capabilities
"""

import argparse
import csv
import os
import sys
import logging
import subprocess
import shutil
import re
from typing import List, Optional

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


def _run_mmpdb_index_with_schema_safe_patch(index_args: List[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    patch_runner_code = r"""
import sys
from mmpdblib import cli, index_writers, schema as mmp_schema

def _q_ident(value):
    return '"' + str(value).replace('"', '""') + '"'

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
    mmp_schema.create_schema(self.db, mmp_schema.PostgresConfig)

index_writers.PostgresIndexWriter.create_schema = _patched_create_schema
sys.argv = ["mmpdb"] + sys.argv[1:]
cli.main()
"""
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
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
) -> bool:
    """Index an existing .fragdb file into PostgreSQL and run finalize steps."""
    logger = logging.getLogger(__name__)
    normalized_schema = _validate_pg_schema(postgres_schema)
    try:
        if not _is_postgres_dsn(str(postgres_url or "").strip()):
            logger.error("PostgreSQL DSN is required. Received: %s", postgres_url)
            return False

        _ensure_postgres_schema_exists(postgres_url, normalized_schema)
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
        index_env = os.environ.copy()
        index_pgoptions: List[str] = [f"-c search_path={normalized_schema},public"]
        if int(index_maintenance_work_mem_mb or 0) > 0:
            index_pgoptions.append(f"-c maintenance_work_mem={int(index_maintenance_work_mem_mb)}MB")
        if int(index_work_mem_mb or 0) > 0:
            index_pgoptions.append(f"-c work_mem={int(index_work_mem_mb)}MB")
        if int(index_parallel_workers or 0) > 0:
            index_pgoptions.append(f"-c max_parallel_maintenance_workers={int(index_parallel_workers)}")
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
        result = _run_mmpdb_index_with_schema_safe_patch(index_args, index_env)

        if result.returncode != 0:
            logger.error(f"Index creation failed: {result.stderr}")
            logger.error(f"Index stdout: {result.stdout}")
            logger.info(f"Fragments file kept for debugging: {fragments_file}")
            return False

        logger.info("PostgreSQL indexing completed successfully")
        if result.stdout:
            logger.info(f"Index output: {result.stdout[-500:]}")  # Last 500 chars

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
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
        )
        if not index_ok:
            logger.info("Fragments file kept for debugging: %s", fragments_file)
            return False

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
    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_compound_public_id ON compound(public_id)",
        "CREATE INDEX IF NOT EXISTS idx_compound_clean_smiles ON compound(clean_smiles)",
        "CREATE INDEX IF NOT EXISTS idx_rule_from_to ON rule(from_smiles_id, to_smiles_id)",
        "CREATE INDEX IF NOT EXISTS idx_rule_environment_rule_radius ON rule_environment(rule_id, radius)",
        "CREATE INDEX IF NOT EXISTS idx_pair_rule_env ON pair(rule_environment_id)",
        "CREATE INDEX IF NOT EXISTS idx_pair_constant ON pair(constant_id)",
        "CREATE INDEX IF NOT EXISTS idx_prop_name_name ON property_name(name)",
        "CREATE INDEX IF NOT EXISTS idx_compound_prop_compound_prop ON compound_property(compound_id, property_name_id)",
        "CREATE INDEX IF NOT EXISTS idx_rule_env_stats_prop ON rule_environment_statistics(rule_environment_id, property_name_id)",
    ]
    for statement in index_statements:
        cursor.execute(statement)

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")
    if enable_constant_smiles_mol_index and _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")


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

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags ON rule_smiles(num_frags)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags_heavies ON rule_smiles(num_frags, num_heavies)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_constant_smiles_num_frags ON constant_smiles(num_frags)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pair_rule_env_constant ON pair(rule_environment_id, constant_id)")

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        cursor.execute("UPDATE rule_smiles SET smiles_mol = mol_from_smiles(smiles) WHERE smiles_mol IS NULL")
    else:
        cursor.execute("ALTER TABLE rule_smiles ADD COLUMN smiles_mol mol")
        cursor.execute("UPDATE rule_smiles SET smiles_mol = mol_from_smiles(smiles)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")
    if enable_constant_smiles_mol_index:
        if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
            cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles) WHERE smiles_mol IS NULL")
        else:
            cursor.execute("ALTER TABLE constant_smiles ADD COLUMN smiles_mol mol")
            cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")


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
  python setup_mmpdb.py --download_chembl --postgres_url postgresql://user:pass@host:5432/db

创建示例数据库并导入PostgreSQL:
  python setup_mmpdb.py --create_sample --postgres_url postgresql://user:pass@host:5432/db

从SMILES文件创建数据库:
  python setup_mmpdb.py --smiles_file compounds.smi --postgres_url postgresql://user:pass@host:5432/db

从结构+属性文件创建数据库（例如 ChEMBL_CYP3A4_hERG）:
  python setup_mmpdb.py --structures_file ChEMBL_CYP3A4_hERG_structures.smi --properties_file ChEMBL_CYP3A4_hERG_props.txt --property_metadata_file ChEMBL_CYP3A4_hERG_metadata.csv --postgres_url postgresql://user:pass@host:5432/db --postgres_schema chembl_cyp3a4_herg

SMILES文件格式:
每行一个化合物，格式为: SMILES<TAB>ID<TAB>MW(可选)
例如:
CCc1ccc(cc1)C(=O)Nc2ccc(cc2)S(=O)(=O)N    CHEMBL123    354.4
COc1cc2c(cc1OC)CCN(C2)C    CHEMBL456    193.2
        """
    )
    
    # Input options
    input_group = parser.add_mutually_exclusive_group(required=True)
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
        default=1024,
        help='索引构建阶段 maintenance_work_mem (MB, 默认: 1024)'
    )

    parser.add_argument(
        '--pg_index_work_mem_mb',
        type=int,
        default=128,
        help='索引/排序阶段 work_mem (MB, 默认: 128)'
    )

    parser.add_argument(
        '--pg_index_parallel_workers',
        type=int,
        default=max(1, min(8, int((os.cpu_count() or 2) // 2))),
        help='索引构建并行 worker 上限 (max_parallel_maintenance_workers)'
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

    if args.fragment_jobs < 1:
        args.fragment_jobs = 1
    if args.pg_index_maintenance_work_mem_mb < 0:
        args.pg_index_maintenance_work_mem_mb = 0
    if args.pg_index_work_mem_mb < 0:
        args.pg_index_work_mem_mb = 0
    if args.pg_index_parallel_workers < 0:
        args.pg_index_parallel_workers = 0
    
    try:
        # Check if RDKit and mmpdb are available
        if not HAS_RDKIT_MMPDB:
            logger.error("RDKit and mmpdb are required. Please install them:")
            logger.error("  pip install rdkit mmpdb requests")
            sys.exit(1)
        if not str(args.postgres_url or "").strip():
            logger.error("--postgres_url is required (PostgreSQL-only runtime).")
            sys.exit(1)
        if str(args.postgres_url or "").strip() and not HAS_PSYCOPG:
            logger.error("PostgreSQL import requested but psycopg is not installed.")
            logger.error("  pip install psycopg[binary]")
            sys.exit(1)
        if str(args.postgres_url or "").strip() and not HAS_PSYCOPG2:
            logger.error(
                "mmpdb PostgreSQL index backend requires psycopg2, but it is not installed."
            )
            logger.error("  pip install psycopg2-binary")
            logger.error("  # or install lead_optimization/requirements.txt again")
            sys.exit(1)
        
        # Create output directory
        os.makedirs(args.output_dir, exist_ok=True)

        postgres_url = str(args.postgres_url or "").strip()
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
            build_ok = _index_fragments_to_postgres(
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
            build_ok = create_mmp_database(
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
        if not build_ok:
            logger.error("MMP database setup failed")
            sys.exit(1)

        if property_metadata_file:
            metadata_ok = apply_property_metadata_postgres(
                postgres_url,
                args.postgres_schema,
                property_metadata_file,
            )
            if not metadata_ok:
                logger.error("PostgreSQL property metadata import failed")
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
