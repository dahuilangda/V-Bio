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
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

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


CORE_TABLES = [
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
]

PG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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

def create_mmp_database(
    smiles_file: str,
    output_db: str,
    max_heavy_atoms: int = 50,
    *,
    properties_file: Optional[str] = None,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
) -> bool:
    """Create MMP database using mmpdb"""
    logger = logging.getLogger(__name__)
    
    try:
        # Create output directory
        os.makedirs(os.path.dirname(output_db), exist_ok=True)
        
        # Temporary fragments file (.fragdb)
        fragments_file = f"{output_db}.fragdb"
        
        # Step 1: Fragment compounds
        logger.info("Step 1: Fragmenting compounds (this may take a while for large datasets)...")
        fragment_cmd = [
            sys.executable, "-m", "mmpdblib", "fragment", 
            smiles_file,
            "-o", fragments_file,
            "--max-heavies", str(max_heavy_atoms),
            "--has-header"  # Our SMILES file has a header
        ]
        
        logger.info(f"Running: {' '.join(fragment_cmd)}")
        result = subprocess.run(fragment_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Fragmentation failed: {result.stderr}")
            logger.error(f"Fragmentation stdout: {result.stdout}")
            return False
        
        logger.info("Fragmentation completed successfully")
        if result.stdout:
            logger.info(f"Fragment output: {result.stdout[-500:]}")  # Last 500 chars
        
        # Check fragments file
        if not os.path.exists(fragments_file):
            logger.error("Fragments file not created")
            return False
        
        # Step 2: Create index (MMP database)
        logger.info("Step 2: Creating MMP database index (this will take some time)...")
        index_cmd = [
            sys.executable, "-m", "mmpdblib", "index",
            fragments_file,
            "-o", output_db
        ]
        if properties_file:
            index_cmd.extend(["--properties", properties_file])
        
        logger.info(f"Running: {' '.join(index_cmd)}")
        result = subprocess.run(index_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Index creation failed: {result.stderr}")
            logger.error(f"Index stdout: {result.stdout}")
            # Keep fragments file for debugging
            logger.info(f"Fragments file kept for debugging: {fragments_file}")
            return False
        
        logger.info("Database indexing completed successfully")
        if result.stdout:
            logger.info(f"Index output: {result.stdout[-500:]}")  # Last 500 chars
        
        # Keep fragments file as backup (don't delete it)
        logger.info(f"Fragments file preserved: {fragments_file}")
        
        # Verify database
        if verify_mmp_database(output_db):
            if not skip_attachment_enrichment:
                if not enrich_attachment_schema(
                    output_db,
                    force_recompute=attachment_force_recompute,
                ):
                    logger.warning(
                        "Attachment enrichment failed; database is still usable but "
                        "single/multi-attachment filtering quality will be degraded."
                    )
            return True
        else:
            logger.error("Database verification failed")
            return False
        
    except Exception as e:
        logger.error(f"Database creation failed: {e}")
        return False

def verify_mmp_database(db_path: str) -> bool:
    """Verify MMP database structure and content"""
    logger = logging.getLogger(__name__)
    
    if not os.path.exists(db_path):
        logger.error(f"Database file does not exist: {db_path}")
        return False
    
    try:
        file_size = os.path.getsize(db_path)
        logger.info(f"MMP database created: {db_path}")
        logger.info(f"Database size: {file_size / (1024*1024):.2f} MB")
        
        # Use mmpdb list command to verify
        list_cmd = [sys.executable, "-m", "mmpdblib", "list", db_path]
        result = subprocess.run(list_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Database verification failed: {result.stderr}")
            return False
        
        logger.info("Database verification successful:")
        logger.info(result.stdout)
        
        # Also do a basic SQLite verification
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check compound count
        cursor.execute("SELECT COUNT(*) FROM compound")
        compound_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM rule")
        rule_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM pair")
        pair_count = cursor.fetchone()[0]
        
        conn.close()
        
        logger.info(f"Database statistics:")
        logger.info(f"  - Compounds: {compound_count}")
        logger.info(f"  - Rules: {rule_count}")
        logger.info(f"  - Pairs: {pair_count}")
        
        if compound_count == 0 or rule_count == 0:
            logger.warning("Database tables are empty")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Database verification failed: {e}")
        return False


def _has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    for row in cursor.fetchall():
        if str(row[1] or "") == column_name:
            return True
    return False


def _distribution_map(cursor: sqlite3.Cursor, table_name: str) -> Dict[int, int]:
    cursor.execute(
        f"SELECT COALESCE(num_frags, -1) AS num_frags, COUNT(*) FROM {table_name} GROUP BY COALESCE(num_frags, -1)"
    )
    stats: Dict[int, int] = {}
    for row in cursor.fetchall():
        stats[int(row[0])] = int(row[1])
    return stats


def enrich_attachment_schema(db_path: str, force_recompute: bool = False) -> bool:
    """Inline attachment enrichment for single/multi attachment aware query flow."""
    logger = logging.getLogger(__name__)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")

        # Both rule_smiles and constant_smiles carry attachment count.
        if not _has_column(conn, "rule_smiles", "num_frags"):
            cursor.execute("ALTER TABLE rule_smiles ADD COLUMN num_frags INTEGER")
        if not _has_column(conn, "constant_smiles", "num_frags"):
            cursor.execute("ALTER TABLE constant_smiles ADD COLUMN num_frags INTEGER")
        conn.commit()

        rule_where = ""
        constant_where = ""
        if not force_recompute:
            rule_where = "WHERE num_frags IS NULL OR num_frags < 1 OR num_frags > 3"
            constant_where = "WHERE num_frags IS NULL OR num_frags < 0 OR num_frags > 3"

        # Rule fragments: at least 1 attachment in valid MMP rules.
        cursor.execute(
            f"""
            UPDATE rule_smiles
            SET num_frags = CASE
                WHEN instr(smiles, '[*:3]') > 0 THEN 3
                WHEN instr(smiles, '[*:2]') > 0 THEN 2
                WHEN instr(smiles, '[*:1]') > 0 THEN 1
                WHEN instr(smiles, '*') > 0 THEN CASE
                    WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 3 THEN 3
                    WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 2 THEN 2
                    ELSE 1
                END
                ELSE 1
            END
            {rule_where}
            """
        )
        # Constant fragments: allow 0 when star is absent.
        cursor.execute(
            f"""
            UPDATE constant_smiles
            SET num_frags = CASE
                WHEN instr(smiles, '[*:3]') > 0 THEN 3
                WHEN instr(smiles, '[*:2]') > 0 THEN 2
                WHEN instr(smiles, '[*:1]') > 0 THEN 1
                WHEN instr(smiles, '*') > 0 THEN CASE
                    WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 3 THEN 3
                    WHEN (LENGTH(smiles) - LENGTH(REPLACE(smiles, '*', ''))) >= 2 THEN 2
                    ELSE 1
                END
                ELSE 0
            END
            {constant_where}
            """
        )
        conn.commit()

        # Query speed indexes for one-attachment / multi-attachment filtering.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags ON rule_smiles(num_frags)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_smiles_num_frags_heavies ON rule_smiles(num_frags, num_heavies)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_constant_smiles_num_frags ON constant_smiles(num_frags)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_from_to ON rule(from_smiles_id, to_smiles_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_environment_rule_radius ON rule_environment(rule_id, radius)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pair_rule_env_constant ON pair(rule_environment_id, constant_id)"
        )

        # Helper views for downstream analytics / debugging.
        cursor.execute(
            """
            CREATE VIEW IF NOT EXISTS leadopt_from_construct AS
            SELECT
                p.id AS pair_id,
                p.rule_environment_id AS rule_environment_id,
                COALESCE(p.constant_id, -1) AS constant_id,
                re.rule_id AS rule_id,
                re.radius AS radius,
                r.from_smiles_id AS rule_smiles_id,
                COALESCE(rs.num_frags, 1) AS num_frags
            FROM pair p
            INNER JOIN rule_environment re ON re.id = p.rule_environment_id
            INNER JOIN rule r ON r.id = re.rule_id
            INNER JOIN rule_smiles rs ON rs.id = r.from_smiles_id
            """
        )
        cursor.execute(
            """
            CREATE VIEW IF NOT EXISTS leadopt_to_construct AS
            SELECT
                p.id AS pair_id,
                p.rule_environment_id AS rule_environment_id,
                COALESCE(p.constant_id, -1) AS constant_id,
                re.rule_id AS rule_id,
                re.radius AS radius,
                r.to_smiles_id AS rule_smiles_id,
                COALESCE(rs.num_frags, 1) AS num_frags
            FROM pair p
            INNER JOIN rule_environment re ON re.id = p.rule_environment_id
            INNER JOIN rule r ON r.id = re.rule_id
            INNER JOIN rule_smiles rs ON rs.id = r.to_smiles_id
            """
        )
        conn.commit()

        rule_stats = _distribution_map(cursor, "rule_smiles")
        constant_stats = _distribution_map(cursor, "constant_smiles")
        cursor.execute(
            """
            SELECT
                COALESCE(rs_from.num_frags, 1) AS from_num_frags,
                COALESCE(rs_to.num_frags, 1) AS to_num_frags,
                COUNT(*) AS n_rules
            FROM rule r
            INNER JOIN rule_smiles rs_from ON rs_from.id = r.from_smiles_id
            INNER JOIN rule_smiles rs_to ON rs_to.id = r.to_smiles_id
            GROUP BY COALESCE(rs_from.num_frags, 1), COALESCE(rs_to.num_frags, 1)
            ORDER BY from_num_frags, to_num_frags
            """
        )
        transition_rows = cursor.fetchall()

        logger.info("Attachment enrichment complete for DB: %s", db_path)
        logger.info("rule_smiles num_frags distribution: %s", rule_stats)
        logger.info("constant_smiles num_frags distribution: %s", constant_stats)
        logger.info("attachment transition matrix (from_num_frags -> to_num_frags): %s", transition_rows)
        return True
    except Exception as exc:
        logger.error("Attachment enrichment failed: %s", exc, exc_info=True)
        return False
    finally:
        conn.close()


def _validate_pg_schema(schema: str) -> str:
    token = str(schema or "public").strip().lower()
    if not token:
        return "public"
    if not PG_IDENTIFIER_RE.match(token):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema}")
    return token


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


def _create_postgres_base_tables(cursor, schema: str) -> None:
    table_statements = [
        """
        CREATE TABLE IF NOT EXISTS dataset (
            id INTEGER PRIMARY KEY,
            mmpdb_version INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            creation_date TIMESTAMP NOT NULL,
            fragment_options VARCHAR(2000) NOT NULL,
            index_options VARCHAR(2000) NOT NULL,
            is_symmetric INTEGER NOT NULL,
            num_compounds INTEGER,
            num_rules INTEGER,
            num_pairs INTEGER,
            num_rule_environments INTEGER,
            num_rule_environment_stats INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS compound (
            id INTEGER PRIMARY KEY,
            public_id VARCHAR(255) NOT NULL,
            input_smiles VARCHAR(255) NOT NULL,
            clean_smiles VARCHAR(255) NOT NULL,
            clean_num_heavies INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rule_smiles (
            id INTEGER PRIMARY KEY,
            smiles VARCHAR(255) NOT NULL,
            num_heavies INTEGER,
            num_frags INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS constant_smiles (
            id INTEGER PRIMARY KEY,
            smiles VARCHAR(255),
            num_frags INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rule (
            id INTEGER PRIMARY KEY,
            from_smiles_id INTEGER NOT NULL,
            to_smiles_id INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS environment_fingerprint (
            id INTEGER PRIMARY KEY,
            smarts VARCHAR(1000) NOT NULL,
            pseudosmiles VARCHAR(400) NOT NULL,
            parent_smarts VARCHAR(1000),
            environment_radius INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rule_environment (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER,
            environment_fingerprint_id INTEGER,
            radius INTEGER,
            num_pairs INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pair (
            id INTEGER PRIMARY KEY,
            rule_environment_id INTEGER NOT NULL,
            compound1_id INTEGER NOT NULL,
            compound2_id INTEGER NOT NULL,
            constant_id INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS property_name (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            base VARCHAR(64),
            unit VARCHAR(64),
            display_name VARCHAR(255),
            display_base VARCHAR(64),
            display_unit VARCHAR(64),
            change_displayed VARCHAR(64)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS compound_property (
            id INTEGER PRIMARY KEY,
            compound_id INTEGER NOT NULL,
            property_name_id INTEGER NOT NULL,
            value DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rule_environment_statistics (
            id INTEGER PRIMARY KEY,
            rule_environment_id INTEGER,
            property_name_id INTEGER NOT NULL,
            count INTEGER NOT NULL,
            avg DOUBLE PRECISION NOT NULL,
            std DOUBLE PRECISION,
            kurtosis DOUBLE PRECISION,
            skewness DOUBLE PRECISION,
            min DOUBLE PRECISION NOT NULL,
            q1 DOUBLE PRECISION NOT NULL,
            median DOUBLE PRECISION NOT NULL,
            q3 DOUBLE PRECISION NOT NULL,
            max DOUBLE PRECISION NOT NULL,
            paired_t DOUBLE PRECISION,
            p_value DOUBLE PRECISION
        )
        """,
    ]
    for statement in table_statements:
        cursor.execute(statement)

    if _pg_column_exists(cursor, schema, "rule_smiles", "smiles_mol"):
        cursor.execute("DROP INDEX IF EXISTS idx_rule_smiles_smiles_mol")
    if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
        cursor.execute("DROP INDEX IF EXISTS idx_constant_smiles_smiles_mol")


def _ensure_postgres_copy_schema_extensions(cursor, schema: str) -> None:
    """Backfill columns expected by attachment-aware workflows before COPY."""
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


def _assert_postgres_copy_schema_ready(cursor, schema: str) -> None:
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
        raise RuntimeError(f"PostgreSQL schema missing required columns for COPY: {', '.join(missing)}")
    logging.getLogger(__name__).info(
        "PostgreSQL attachment columns verified: %s",
        ", ".join(f"{table}.{column}" for table, column in required_columns),
    )


def _create_postgres_core_indexes(cursor, schema: str) -> None:
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
    if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_constant_smiles_smiles_mol ON constant_smiles USING gist(smiles_mol)")


def _drop_postgres_import_indexes(cursor) -> None:
    index_names = [
        "idx_compound_public_id",
        "idx_compound_clean_smiles",
        "idx_rule_from_to",
        "idx_rule_environment_rule_radius",
        "idx_pair_rule_env",
        "idx_pair_constant",
        "idx_prop_name_name",
        "idx_compound_prop_compound_prop",
        "idx_rule_env_stats_prop",
        "idx_rule_smiles_num_frags",
        "idx_rule_smiles_num_frags_heavies",
        "idx_constant_smiles_num_frags",
        "idx_pair_rule_env_constant",
        "idx_rule_smiles_smiles_mol",
        "idx_constant_smiles_smiles_mol",
    ]
    for index_name in index_names:
        cursor.execute(f"DROP INDEX IF EXISTS {index_name}")


def _iter_sqlite_table_rows(sqlite_db: str, table_name: str, batch_size: int = 20000):
    conn = sqlite3.connect(sqlite_db)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name}")
        column_names = [str(desc[0]) for desc in cursor.description]
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield column_names, rows
    finally:
        conn.close()


def _truncate_postgres_tables(cursor, tables: List[str]) -> None:
    if not tables:
        return
    table_list = ", ".join(tables)
    cursor.execute(f"TRUNCATE TABLE {table_list} RESTART IDENTITY")


def _copy_rows_to_postgres(
    cursor,
    table_name: str,
    column_names: List[str],
    rows: List[tuple],
    *,
    flush_rows: int = 2000,
) -> None:
    if not rows:
        return
    column_sql = ", ".join(column_names)
    # Use text COPY + write_row so Python None is mapped to SQL NULL correctly.
    # CSV mode would quote "\\N" and turn it into a literal string, which breaks numeric columns.
    copy_sql = f"COPY {table_name} ({column_sql}) FROM STDIN"
    with cursor.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row(row)


def _copy_single_table_sqlite_to_postgres(
    sqlite_db: str,
    postgres_url: str,
    schema: str,
    table_name: str,
    *,
    batch_size: int,
    flush_rows: int,
) -> tuple[str, int]:
    total_rows = 0
    with psycopg.connect(postgres_url, autocommit=False) as conn:
        with conn.cursor() as cursor:
            _pg_set_search_path(cursor, schema)
            first_columns: List[str] = []
            for column_names, rows in _iter_sqlite_table_rows(sqlite_db, table_name, batch_size=batch_size):
                if not first_columns:
                    first_columns = column_names
                _copy_rows_to_postgres(
                    cursor,
                    table_name,
                    first_columns,
                    rows,
                    flush_rows=flush_rows,
                )
                total_rows += len(rows)
            conn.commit()
    return table_name, total_rows


def _enrich_attachment_schema_postgres(cursor, schema: str, force_recompute: bool = False) -> None:
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
    if _pg_column_exists(cursor, schema, "constant_smiles", "smiles_mol"):
        cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles) WHERE smiles_mol IS NULL")
    else:
        cursor.execute("ALTER TABLE constant_smiles ADD COLUMN smiles_mol mol")
        cursor.execute("UPDATE constant_smiles SET smiles_mol = mol_from_smiles(smiles)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_smiles_smiles_mol ON rule_smiles USING gist(smiles_mol)")
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


def import_sqlite_to_postgres(
    sqlite_db: str,
    postgres_url: str,
    *,
    schema: str = "public",
    force_recompute_num_frags: bool = False,
    copy_batch_size: int = 10000,
    copy_flush_rows: int = 2000,
    copy_workers: int = 1,
) -> bool:
    logger = logging.getLogger(__name__)
    if not HAS_PSYCOPG:
        logger.error("psycopg is required for PostgreSQL import. Please install: pip install psycopg[binary]")
        return False
    if not sqlite_db or not os.path.exists(sqlite_db):
        logger.error("SQLite source DB not found: %s", sqlite_db)
        return False

    normalized_schema = _validate_pg_schema(schema)
    normalized_batch_size = max(1000, int(copy_batch_size or 10000))
    normalized_flush_rows = max(100, int(copy_flush_rows or 2000))
    normalized_workers = max(1, int(copy_workers or 1))
    logger.info("Importing SQLite mmpdb into PostgreSQL schema '%s' from: %s", normalized_schema, sqlite_db)
    logger.info(
        "PostgreSQL COPY settings: workers=%s batch_size=%s flush_rows=%s",
        normalized_workers,
        normalized_batch_size,
        normalized_flush_rows,
    )
    try:
        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS rdkit")
                if normalized_schema != "public":
                    cursor.execute(SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(normalized_schema)))
                _pg_set_search_path(cursor, normalized_schema)
                _create_postgres_base_tables(cursor, normalized_schema)
                _ensure_postgres_copy_schema_extensions(cursor, normalized_schema)
                _assert_postgres_copy_schema_ready(cursor, normalized_schema)
                logger.info("PostgreSQL COPY schema ready for attachment-aware columns.")
                _drop_postgres_import_indexes(cursor)
                _truncate_postgres_tables(cursor, CORE_TABLES)
                conn.commit()

        effective_workers = min(len(CORE_TABLES), normalized_workers)
        logger.info("Starting table copy with %s worker(s)...", effective_workers)
        if effective_workers == 1:
            for table_name in CORE_TABLES:
                logger.info("Copying table: %s", table_name)
                copied_table, total_rows = _copy_single_table_sqlite_to_postgres(
                    sqlite_db,
                    postgres_url,
                    normalized_schema,
                    table_name,
                    batch_size=normalized_batch_size,
                    flush_rows=normalized_flush_rows,
                )
                logger.info("Copied %s rows into %s", total_rows, copied_table)
        else:
            futures = []
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                for table_name in CORE_TABLES:
                    futures.append(
                        executor.submit(
                            _copy_single_table_sqlite_to_postgres,
                            sqlite_db,
                            postgres_url,
                            normalized_schema,
                            table_name,
                            batch_size=normalized_batch_size,
                            flush_rows=normalized_flush_rows,
                        )
                    )
                for future in as_completed(futures):
                    copied_table, total_rows = future.result()
                    logger.info("Copied %s rows into %s", total_rows, copied_table)

        with psycopg.connect(postgres_url, autocommit=False) as conn:
            with conn.cursor() as cursor:
                _pg_set_search_path(cursor, normalized_schema)
                _enrich_attachment_schema_postgres(
                    cursor,
                    normalized_schema,
                    force_recompute=force_recompute_num_frags,
                )
                _rebuild_construct_tables_postgres(cursor)
                _create_postgres_core_indexes(cursor, normalized_schema)
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
                cursor.execute("SELECT COUNT(*) FROM from_construct")
                from_construct_rows = int(cursor.fetchone()[0])
                cursor.execute("SELECT COUNT(*) FROM to_construct")
                to_construct_rows = int(cursor.fetchone()[0])

            logger.info(
                "PostgreSQL import complete: compounds=%s rules=%s pairs=%s from_construct=%s to_construct=%s",
                compounds,
                rules,
                pairs,
                from_construct_rows,
                to_construct_rows,
            )
        return True
    except Exception as exc:
        logger.error("Failed importing SQLite mmpdb to PostgreSQL: %s", exc, exc_info=True)
        return False

def download_prebuilt_database(output_db: str) -> bool:
    """Download prebuilt ChEMBL MMP database from external source"""
    logger = logging.getLogger(__name__)
    
    # URLs for prebuilt MMP databases (these would need to be real URLs)
    prebuilt_urls = [
        # These are example URLs - in practice you'd host these somewhere
        # "https://github.com/rdkit/mmpdb/releases/download/v2.1/chembl_30_mmp.db.gz",
        # "https://zenodo.org/record/4725643/files/chembl_mmp_database.db.gz"
    ]
    
    if not prebuilt_urls:
        logger.error("No prebuilt database URLs configured")
        logger.info("Use --download_chembl to build from ChEMBL data")
        return False
    
    for url in prebuilt_urls:
        try:
            logger.info(f"Attempting to download prebuilt database from: {url}")
            
            response = requests.get(url, stream=True, timeout=600)
            response.raise_for_status()
            
            # Create output directory
            os.makedirs(os.path.dirname(output_db), exist_ok=True)
            
            # Download and decompress
            if url.endswith('.gz'):
                logger.info("Decompressing database...")
                decompressed_data = gzip.decompress(response.content)
                
                with open(output_db, 'wb') as f:
                    f.write(decompressed_data)
            else:
                with open(output_db, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            # Verify database
            if verify_mmp_database(output_db):
                logger.info(f"Successfully downloaded prebuilt database: {output_db}")
                return True
            else:
                logger.warning("Downloaded database failed verification")
                if os.path.exists(output_db):
                    os.remove(output_db)
                
        except Exception as e:
            logger.warning(f"Failed to download from {url}: {e}")
            continue
    
    logger.error("All prebuilt database URLs failed")
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
        '--download_prebuilt',
        action='store_true',
        help='下载预构建的数据库'
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
    
    # Output options
    parser.add_argument(
        '--output_db',
        type=str,
        default='lead_optimization/data/.mmp_staging.db',
        help='中间staging SQLite路径（仅构建阶段使用）'
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
        '--pg_copy_workers',
        type=int,
        default=max(1, int(os.cpu_count() or 1)),
        help='PostgreSQL导入并行worker数 (默认: 当前机器CPU核数)'
    )

    parser.add_argument(
        '--pg_copy_batch_size',
        type=int,
        default=10000,
        help='SQLite->PostgreSQL每批读取行数 (默认: 10000)'
    )

    parser.add_argument(
        '--pg_copy_flush_rows',
        type=int,
        default=2000,
        help='COPY缓冲区分段写入行数 (默认: 2000, 降低内存峰值)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='覆盖现有数据库'
    )

    parser.add_argument(
        '--keep_staging_db',
        action='store_true',
        help='保留构建中间SQLite文件（默认构建成功后自动删除）'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)

    if args.pg_copy_workers < 1:
        args.pg_copy_workers = 1
    if args.pg_copy_batch_size < 1000:
        args.pg_copy_batch_size = 1000
    if args.pg_copy_flush_rows < 100:
        args.pg_copy_flush_rows = 100
    
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
        
        # Create output directory
        os.makedirs(args.output_dir, exist_ok=True)

        sqlite_source_db = ""
        smiles_file = None
        properties_file = str(args.properties_file or "").strip()
        property_metadata_file = str(args.property_metadata_file or "").strip()

        # Check if staging DB already exists.
        if os.path.exists(args.output_db) and not args.force:
            logger.error(f"Staging database already exists: {args.output_db}")
            logger.error("Use --force to overwrite")
            sys.exit(1)

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

        elif args.download_prebuilt:
            logger.info("Downloading prebuilt database...")
            if not download_prebuilt_database(args.output_db):
                logger.error("Failed to download prebuilt database")
                sys.exit(1)
            if not args.skip_attachment_enrichment:
                enrich_ok = enrich_attachment_schema(
                    args.output_db,
                    force_recompute=args.attachment_force_recompute,
                )
                if not enrich_ok:
                    logger.warning(
                        "Prebuilt DB downloaded but attachment enrichment failed; attachment filtering may be degraded."
                    )
            logger.info("Prebuilt database downloaded successfully")
            sqlite_source_db = args.output_db

        if not sqlite_source_db:
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

            logger.info(f"Creating staging MMP database: {args.output_db}")
            build_ok = create_mmp_database(
                smiles_file,
                args.output_db,
                args.max_heavy_atoms,
                properties_file=properties_file if properties_file else None,
                skip_attachment_enrichment=args.skip_attachment_enrichment,
                attachment_force_recompute=args.attachment_force_recompute,
            )
            if not build_ok:
                logger.error("MMP database setup failed")
                sys.exit(1)
            sqlite_source_db = args.output_db
            logger.info("MMP staging database setup completed successfully")

        postgres_url = str(args.postgres_url or "").strip()
        if not sqlite_source_db:
            logger.error("Cannot import to PostgreSQL because staging SQLite DB is missing.")
            sys.exit(1)
        logger.info("Starting PostgreSQL import...")
        import_ok = import_sqlite_to_postgres(
            sqlite_source_db,
            postgres_url,
            schema=args.postgres_schema,
            force_recompute_num_frags=args.attachment_force_recompute,
            copy_batch_size=args.pg_copy_batch_size,
            copy_flush_rows=args.pg_copy_flush_rows,
            copy_workers=args.pg_copy_workers,
        )
        if not import_ok:
            logger.error("PostgreSQL import failed")
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

        if not args.keep_staging_db and sqlite_source_db and os.path.exists(sqlite_source_db):
            try:
                os.remove(sqlite_source_db)
                logger.info("Removed staging SQLite DB: %s", sqlite_source_db)
            except Exception as exc:
                logger.warning("Failed to remove staging SQLite DB %s: %s", sqlite_source_db, exc)

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
