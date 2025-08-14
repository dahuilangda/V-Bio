#!/usr/bin/env python3
# /data/boltz_webui/lead_optimization/setup_mmpdb.py

"""
Setup script for initializing mmpdb database for lead optimization
Enhanced with ChEMBL data download capabilities
"""

import argparse
import os
import sys
import logging
import tempfile
import subprocess
import shutil
from pathlib import Path
from typing import List, Optional

try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors
    import requests
    import gzip
    import sqlite3
    from io import StringIO
    HAS_RDKIT_MMPDB = True
except ImportError:
    HAS_RDKIT_MMPDB = False

def setup_logging():
    """Setup logging"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def download_chembl_data(output_dir: str, subset_size: Optional[int] = None, 
                        min_mw: float = 100, max_mw: float = 800) -> str:
    """Download ChEMBL compound data and create SMILES file"""
    logger = logging.getLogger(__name__)
    
    # ChEMBL download URLs - using latest version
    chembl_urls = {
        'compounds': 'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/chembl_35_chemreps.txt.gz',
        'activities': 'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/chembl_35_activities.txt.gz'
    }
    
    # Alternative URLs if main site is down
    alternative_urls = {
        'compounds': [
            'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_32/chembl_32_chemreps.txt.gz',
            'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_31/chembl_31_chemreps.txt.gz'
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
        # Process the raw ChEMBL file
        logger.info("Processing ChEMBL data (this may take a while)...")
        
        with open(chembl_raw_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        logger.info(f"Total lines in ChEMBL file: {len(lines)}")
        
        # Parse header to find column indices
        header = lines[0].strip().split('\t')
        
        try:
            chembl_id_idx = header.index('chembl_id')
            smiles_idx = header.index('canonical_smiles')
            # Try different molecular weight column names
            mw_idx = None
            for mw_col in ['mw_freebase', 'full_mwt', 'molecular_weight']:
                if mw_col in header:
                    mw_idx = header.index(mw_col)
                    break
            
            if mw_idx is None:
                logger.warning("No molecular weight column found, using RDKit calculation")
                
        except ValueError as e:
            logger.error(f"Required column not found in ChEMBL data: {e}")
            logger.info(f"Available columns: {header[:10]}...")  # Show first 10 columns
            return None
        
        # Process compounds
        valid_compounds = []
        processed_count = 0
        skipped_count = 0
        
        with open(smiles_file, 'w') as f:
            # Write header in mmpdb format (SMILES<TAB>ID)
            f.write("SMILES\tID\n")
            
            for line in lines[1:]:  # Skip header
                processed_count += 1
                
                if processed_count % 10000 == 0:
                    logger.info(f"Processed {processed_count}/{len(lines)-1} compounds, kept {len(valid_compounds)}")
                
                # Limit subset size if specified
                if subset_size and len(valid_compounds) >= subset_size:
                    logger.info(f"Reached target subset size of {subset_size}")
                    break
                
                try:
                    parts = line.strip().split('\t')
                    
                    if len(parts) <= max(chembl_id_idx, smiles_idx):
                        skipped_count += 1
                        continue
                    
                    chembl_id = parts[chembl_id_idx].strip()
                    smiles = parts[smiles_idx].strip()
                    
                    # Skip empty entries
                    if not chembl_id or not smiles or smiles == 'NULL':
                        skipped_count += 1
                        continue
                    
                    # Validate SMILES with RDKit
                    mol = Chem.MolFromSmiles(smiles)
                    if not mol:
                        skipped_count += 1
                        continue
                    
                    # Get molecular weight
                    if mw_idx is not None and len(parts) > mw_idx:
                        try:
                            mw = float(parts[mw_idx].strip())
                        except (ValueError, TypeError):
                            mw = Descriptors.MolWt(mol)
                    else:
                        mw = Descriptors.MolWt(mol)
                    
                    # Filter by molecular weight
                    if not (min_mw <= mw <= max_mw):
                        skipped_count += 1
                        continue
                    
                    # Additional drug-likeness filters
                    if not is_drug_like(mol):
                        skipped_count += 1
                        continue
                    
                    # Write to file in correct mmpdb format (no MW column)
                    f.write(f"{smiles}\t{chembl_id}\n")
                    valid_compounds.append({
                        'smiles': smiles,
                        'chembl_id': chembl_id,
                        'mw': mw
                    })
                    
                except Exception as e:
                    skipped_count += 1
                    continue
        
        logger.info(f"ChEMBL data processing completed:")
        logger.info(f"  - Total processed: {processed_count}")
        logger.info(f"  - Valid compounds: {len(valid_compounds)}")
        logger.info(f"  - Skipped: {skipped_count}")
        logger.info(f"  - Filter efficiency: {len(valid_compounds)/processed_count*100:.1f}%")
        logger.info(f"  - Output file: {smiles_file}")
        
        if len(valid_compounds) == 0:
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
                
                total_count += 1
                
                # Parse line (SMILES + optional ID)
                parts = line.split('\t')
                if len(parts) < 1:
                    logger.warning(f"Line {line_num}: Invalid format")
                    continue
                
                smiles = parts[0].strip()
                
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

def create_mmp_database(smiles_file: str, output_db: str, max_heavy_atoms: int = 50) -> bool:
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

从ChEMBL自动下载构建数据库:
  python setup_mmpdb.py --download_chembl --output_db data/chembl_mmp.db

从ChEMBL下载指定数量化合物:
  python setup_mmpdb.py --download_chembl --subset_size 100000 --output_db data/chembl_mmp.db

创建示例数据库:
  python setup_mmpdb.py --create_sample --output_db data/sample_mmp.db

从SMILES文件创建数据库:
  python setup_mmpdb.py --smiles_file compounds.smi --output_db data/mmp_database.db

下载预构建数据库:
  python setup_mmpdb.py --download_prebuilt --output_db data/prebuilt_mmp.db

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
        help='ChEMBL数据子集大小 (默认: 50000, 设置为None表示下载全部)'
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
    
    # Output options
    parser.add_argument(
        '--output_db',
        type=str,
        default='data/mmp_database.db',
        help='输出数据库路径 (默认: data/mmp_database.db)'
    )
    
    parser.add_argument(
        '--output_dir',
        type=str,
        default='data',
        help='输出目录 (默认: data)'
    )
    
    # Parameters
    parser.add_argument(
        '--max_heavy_atoms',
        type=int,
        default=50,
        help='最大重原子数 (默认: 50)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='覆盖现有数据库'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)
    
    try:
        # Check if RDKit and mmpdb are available
        if not HAS_RDKIT_MMPDB:
            logger.error("RDKit and mmpdb are required. Please install them:")
            logger.error("  pip install rdkit mmpdb requests")
            sys.exit(1)
        
        # Create output directory
        os.makedirs(args.output_dir, exist_ok=True)
        
        # Check if database already exists
        if os.path.exists(args.output_db) and not args.force:
            logger.error(f"Database already exists: {args.output_db}")
            logger.error("Use --force to overwrite")
            sys.exit(1)
        
        # Handle different input types
        smiles_file = None
        
        if args.create_sample:
            logger.info("Creating sample compound database...")
            smiles_file = create_sample_data(args.output_dir)
        
        elif args.download_chembl:
            logger.info("Downloading ChEMBL compound data...")
            smiles_file = download_chembl_data(
                args.output_dir, 
                subset_size=args.subset_size,
                min_mw=args.min_mw,
                max_mw=args.max_mw
            )
            if not smiles_file:
                logger.error("Failed to download ChEMBL data")
                sys.exit(1)
        
        elif args.smiles_file:
            smiles_file = args.smiles_file
            if not os.path.exists(smiles_file):
                logger.error(f"SMILES file not found: {smiles_file}")
                sys.exit(1)
        
        elif args.download_prebuilt:
            logger.info("Downloading prebuilt database...")
            if download_prebuilt_database(args.output_db):
                logger.info("Prebuilt database downloaded successfully")
                sys.exit(0)
            else:
                logger.error("Failed to download prebuilt database")
                sys.exit(1)
        
        # Validate SMILES file
        if smiles_file and not validate_smiles_file(smiles_file):
            logger.error("SMILES file validation failed")
            sys.exit(1)
        
        # Create MMP database
        logger.info(f"Creating MMP database: {args.output_db}")
        
        if create_mmp_database(smiles_file, args.output_db, args.max_heavy_atoms):
            logger.info("MMP database setup completed successfully!")
            logger.info(f"Database location: {args.output_db}")
            
            # Print usage instructions
            print("\n" + "="*60)
            print("数据库设置完成!")
            print("="*60)
            print(f"数据库位置: {args.output_db}")
            print(f"配置文件中设置: mmp_database.database_path = \"{args.output_db}\"")
            print("\n现在可以运行先导化合物优化:")
            print("python run_optimization.py --input_compound \"YOUR_SMILES\" --target_protein target.yaml")
            print("\n数据库统计信息:")
            verify_mmp_database(args.output_db)
            
        else:
            logger.error("MMP database setup failed")
            sys.exit(1)
    
    except KeyboardInterrupt:
        logger.info("Setup interrupted by user")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
