# 来自 https://github.com/ohuelab/boltzina 的修改版本

import numpy as np
import subprocess
import argparse
import pickle
import json
import csv
import re
import uuid
import time
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from rdkit import Chem
Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

from affinity.boltzina.data.parse.mmcif import parse_mmcif
from affinity.boltzina.affinity.predict_affinity import load_boltz2_model, predict_affinity
from boltz.main import get_cache_path

# CCD名称管理器，确保同一批次运行的不要使用相同的CCD名称
import redis
import random
import string

class CCDNameManager:
    """管理CCD名称分配，确保并发任务不会使用相同的CCD名称"""
    
    def __init__(self, redis_client=None):
        self.redis_client = redis_client or self._get_redis_client()
        self.reserved_names_key = "ccd_reserved_names"
        self.name_expiry_seconds = 3600  # 1小时后自动清理
    
    def _get_redis_client(self):
        """获取Redis客户端"""
        try:
            from gpu_manager import get_redis_client
            return get_redis_client()
        except:
            # 如果无法获取Redis，使用本地管理（降级方案）
            return None
    
    def reserve_unique_name(self, base_name: str, task_id: str) -> str:
        """为任务保留一个唯一的CCD名称"""
        if not self.redis_client:
            # 降级方案：使用随机后缀
            suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            return f"{base_name}_{suffix}"
        
        max_attempts = 50  # 最大尝试次数
        for attempt in range(max_attempts):
            # 生成候选名称
            if attempt == 0:
                # 首次尝试使用task_id作为后缀
                candidate_name = f"{base_name}_{task_id[:8]}"
            else:
                # 后续尝试使用随机后缀
                suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                candidate_name = f"{base_name}_{suffix}"
            
            # 尝试在Redis中保留这个名称
            reserve_key = f"{self.reserved_names_key}:{candidate_name}"
            if self.redis_client.set(reserve_key, task_id, nx=True, ex=self.name_expiry_seconds):
                print(f"Task {task_id}: Reserved unique CCD name '{candidate_name}'")
                return candidate_name
        
        # 如果所有尝试都失败，使用带时间戳的名称作为最后手段
        import time
        timestamp_suffix = str(int(time.time() * 1000))[-8:]  # 使用时间戳的最后8位
        fallback_name = f"{base_name}_{timestamp_suffix}"
        reserve_key = f"{self.reserved_names_key}:{fallback_name}"
        self.redis_client.set(reserve_key, task_id, ex=self.name_expiry_seconds)
        print(f"Task {task_id}: Using fallback CCD name '{fallback_name}'")
        return fallback_name
    
    def release_name(self, ccd_name: str, task_id: str):
        """释放CCD名称"""
        if not self.redis_client:
            return
        
        reserve_key = f"{self.reserved_names_key}:{ccd_name}"
        # 验证这个名称确实是由当前任务保留的
        current_holder = self.redis_client.get(reserve_key)
        if current_holder:
            # 处理Redis返回值可能是bytes或str的情况
            if isinstance(current_holder, bytes):
                current_holder_str = current_holder.decode()
            else:
                current_holder_str = current_holder
                
            if current_holder_str == task_id:
                self.redis_client.delete(reserve_key)
                print(f"Task {task_id}: Released CCD name '{ccd_name}'")
            else:
                print(f"Task {task_id}: Warning - tried to release CCD name '{ccd_name}' but it's owned by task '{current_holder_str}'")
        else:
            print(f"Task {task_id}: Warning - tried to release CCD name '{ccd_name}' but it's not found in Redis")

# 全局CCD名称管理器实例
_ccd_name_manager = CCDNameManager()

class Boltzina:
    def __init__(
        self,
        output_dir: str,
        work_dir: Optional[str] = None,
        boltz_override: bool = False,
        skip_run_structure: bool = True,
        use_kernels: bool = False,
        clean_intermediate_files: bool = True,
        predict_affinity_args: Optional[dict] = None,
        pairformer_args: Optional[dict] = None,
        msa_args: Optional[dict] = None,
        steering_args: Optional[dict] = None,
        diffusion_process_args: Optional[dict] = None,
        run_trunk_and_structure: bool = True,
        ligand_resname: str = "LIG",
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(work_dir) if work_dir else self.output_dir / "boltz_work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        
        self.boltz_override = boltz_override
        self.skip_run_structure = skip_run_structure
        self.use_kernels = use_kernels
        self.clean_intermediate_files = clean_intermediate_files
        self.predict_affinity_args = predict_affinity_args
        self.pairformer_args = pairformer_args
        
        if msa_args is None:
            self.msa_args = {
                "subsample_msa": False,
                "use_paired_feature": True,
                "msa_s": 64,
                "msa_transition_n": 256,
                "msa_blocks": 4,
                "msa_dropout": 0.15,
                "z_dropout": 0.15,
            }
        else:
            self.msa_args = msa_args

        self.steering_args = steering_args
        self.diffusion_process_args = diffusion_process_args
        self.run_trunk_and_structure = run_trunk_and_structure
        
        self.results = []
        self.base_ligand_name = "MOL"
        self.fname = "prediction_target"
        self.ligand_resname = ligand_resname
        
        # 获取task_id用于唯一标识，如果无法获取则使用uuid
        self.task_id = self._get_current_task_id()
        
        # 使用CCD名称管理器来获取唯一的配体名称
        self.unique_ligand_name = _ccd_name_manager.reserve_unique_name(self.ligand_resname, self.task_id)
        print(f"Task {self.task_id}: Reserved unique CCD name: {self.unique_ligand_name}")
        
        self.cache_dir = Path(get_cache_path())
        self.ccd_path = self.cache_dir / 'ccd.pkl'
        self.ccd = self._load_ccd()
        
        # Track custom ligands added by this instance (使用唯一名称)
        self.custom_ligands = set()

    def _get_current_task_id(self) -> str:
        """获取当前Celery任务ID，如果不在Celery环境中则生成UUID"""
        try:
            # 尝试获取Celery任务ID
            from celery import current_task
            if current_task and current_task.request and current_task.request.id:
                return current_task.request.id
        except:
            pass
        
        # 如果无法获取Celery任务ID，生成一个唯一ID
        return str(uuid.uuid4())[:8]

    def _load_ccd(self) -> Dict[str, Any]:
        """Load CCD cache safely without global locking."""
        if self.ccd_path.exists():
            with self.ccd_path.open('rb') as file:
                ccd = pickle.load(file)
                return ccd
        else:
            return {}
    
    def _add_temporary_ligand_to_ccd(self, ligand_name: str, ligand_mol):
        """Temporarily add a custom ligand to CCD for processing with unique naming.
        
        Args:
            ligand_name: Name of the ligand to add
            ligand_mol: RDKit molecule object of the ligand or wrapped molecule
        """
        # Use the already reserved unique ligand name
        unique_ligand_name = self.unique_ligand_name
        
        # Add temporary ligand to local CCD copy only (not global cache)
        self.ccd[unique_ligand_name] = ligand_mol
        self.custom_ligands.add(unique_ligand_name)
        print(f"Task {self.task_id}: Added unique custom ligand '{unique_ligand_name}' to local CCD cache")
        
        # Write custom ligand to local mols directory only
        self._write_custom_ligand_to_local_mols_dir(unique_ligand_name, ligand_mol)
        
        return unique_ligand_name
    
    def _write_custom_ligand_to_local_mols_dir(self, ligand_name: str, ligand_mol):
        """Write a custom ligand to local mols directory only (not global cache)."""
        try:
            # Write to local mols directory only
            local_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
            local_mols_dir.mkdir(parents=True, exist_ok=True)
            
            import pickle
            
            # Write to local directory only
            local_pkl_path = local_mols_dir / f"{ligand_name}.pkl"
            with open(local_pkl_path, 'wb') as f:
                pickle.dump(ligand_mol, f)
            print(f"✓ Wrote custom ligand '{ligand_name}' to local directory: {local_pkl_path}")
            
        except Exception as e:
            print(f"⚠️  Failed to write custom ligand '{ligand_name}' to local mols directory: {e}")
            import traceback
            traceback.print_exc()
    
    def _cleanup_temporary_ligands(self):
        """Remove temporary ligands added by this instance and release CCD name."""
        # Clean up only the custom ligands added by this instance
        for ligand_name in self.custom_ligands:
            if ligand_name in self.ccd:
                self.ccd.pop(ligand_name)
                print(f"Task {self.task_id}: Removed custom ligand '{ligand_name}' from local CCD cache")
        
        # Clean up custom ligand files from local directory only
        try:
            local_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
            if local_mols_dir.exists():
                for ligand_name in self.custom_ligands:
                    pkl_file = local_mols_dir / f"{ligand_name}.pkl"
                    if pkl_file.exists():
                        try:
                            pkl_file.unlink()
                            print(f"Task {self.task_id}: Removed custom ligand file: {pkl_file}")
                        except Exception as e:
                            print(f"Task {self.task_id}: Failed to remove {pkl_file}: {e}")
                            
        except Exception as e:
            print(f"Task {self.task_id}: Error cleaning up local mols directory: {e}")
        
        # 释放在CCD名称管理器中保留的名称
        if hasattr(self, 'unique_ligand_name'):
            _ccd_name_manager.release_name(self.unique_ligand_name, self.task_id)
        
        # Clear the set of custom ligands
        self.custom_ligands.clear()
        print(f"Task {self.task_id}: Cleanup completed for this instance")

    def _write_custom_mols_for_inference(self):
        """Write any custom ligands to the extra_mols directory for inference."""
        if not self.custom_ligands:
            # No custom ligands were added
            return
            
        extra_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
        extra_mols_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"Custom ligands available for inference: {list(self.custom_ligands)}")
        
        # Custom ligands are already written to local directory in _write_custom_ligand_to_local_mols_dir
        # This method is kept for compatibility but no additional action needed
            
    def _write_custom_mols_for_record(self, record_id: str):
        """Write custom ligands for a specific record."""
        if not self.custom_ligands:
            return
            
        extra_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
        extra_mols_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            import pickle
            
            # Custom ligands are already written as individual .pkl files
            # This method is kept for compatibility
            print(f"✓ Custom ligands already available for record '{record_id}': {list(self.custom_ligands)}")
            
        except Exception as e:
            print(f"⚠️  Failed to confirm custom ligands for record '{record_id}': {e}")

    def _extract_ligand_name_from_error(self, error_message: str) -> Optional[str]:
        """Extract ligand name from CCD error message."""
        import re
        match = re.search(r"CCD component '([^']+)' not found", error_message)
        return match.group(1) if match else None

    def _try_create_custom_ligand(self, complex_file: Path, ligand_name: str, record_id: str) -> bool:
        """Attempt to create a custom ligand definition from the structure file."""
        try:
            print(f"Attempting to create custom ligand definition for '{ligand_name}'...")
            
            # Try to extract ligand from CIF file using gemmi
            import gemmi
            import tempfile
            
            try:
                doc = gemmi.cif.read(str(complex_file))
                block = doc[0]
                structure = gemmi.make_structure_from_block(block)
                
                # Find the ligand in the structure
                ligand_residue = None
                for model in structure:
                    for chain in model:
                        for residue in chain:
                            if residue.name == ligand_name:
                                ligand_residue = residue
                                break
                        if ligand_residue:
                            break
                    if ligand_residue:
                        break
                
                if not ligand_residue:
                    print(f"Could not find ligand '{ligand_name}' in structure")
                    return False
                
                # Create a temporary SDF file for the ligand
                temp_sdf = tempfile.NamedTemporaryFile(suffix='.sdf', delete=False, mode='w')
                try:
                    # Write basic SDF header
                    temp_sdf.write(f"{ligand_name}\n")
                    temp_sdf.write("  Generated from CIF coordinates\n")
                    temp_sdf.write("\n")
                    
                    # Count atoms
                    atom_count = len(ligand_residue)
                    temp_sdf.write(f"{atom_count:3d}  0  0  0  0  0  0  0  0  0  0 V2000\n")
                    
                    # Write atom coordinates and elements
                    atom_names = []
                    for i, atom in enumerate(ligand_residue):
                        pos = atom.pos
                        element = atom.element.name
                        atom_name = atom.name if hasattr(atom, 'name') else f"{element}{i+1}"
                        atom_names.append(atom_name)
                        temp_sdf.write(f"{pos.x:10.4f}{pos.y:10.4f}{pos.z:10.4f} {element:2s}  0  0  0  0  0  0  0  0  0  0  0  0\n")
                    
                    # Write end of molecule
                    temp_sdf.write("M  END\n")
                    temp_sdf.write("$$$$\n")
                    temp_sdf.close()
                    
                    # Try to read the SDF with RDKit
                    from rdkit import Chem
                    from rdkit.Chem import rdDetermineBonds
                    
                    mol = Chem.MolFromMolFile(temp_sdf.name, sanitize=False)
                    if mol is not None:
                        # Try to determine bonds and sanitize with more permissive settings
                        try:
                            # First try with automatic bond determination
                            rdDetermineBonds.DetermineBonds(mol, charge=0)
                            Chem.SanitizeMol(mol, sanitizeOps=Chem.SANITIZE_ALL)
                            
                        except Exception as e1:
                            print(f"Standard sanitization failed: {e1}")
                            # Try more permissive sanitization
                            try:
                                # Reset molecule
                                mol = Chem.MolFromMolFile(temp_sdf.name, sanitize=False)
                                # Skip certain sanitization steps that might be problematic
                                sanitize_ops = (Chem.SANITIZE_ALL ^ 
                                              Chem.SANITIZE_PROPERTIES ^ 
                                              Chem.SANITIZE_KEKULIZE)
                                Chem.SanitizeMol(mol, sanitizeOps=sanitize_ops)
                                print("Used permissive sanitization for custom ligand")
                                
                            except Exception as e2:
                                print(f"Permissive sanitization also failed: {e2}")
                                # Last resort: create a very basic molecule representation
                                try:
                                    # Create minimal molecule just for structure processing
                                    mol = Chem.MolFromMolFile(temp_sdf.name, sanitize=False)
                                    # Don't sanitize at all, just use raw coordinates
                                    print("Using unsanitized molecule as last resort")
                                except Exception as e3:
                                    print(f"Even raw molecule creation failed: {e3}")
                                    return False
                        
                        if mol is not None:
                            # Add essential molecule properties
                            mol.SetProp('_Name', ligand_name)
                            mol.SetProp('name', ligand_name)
                            mol.SetProp('id', ligand_name)
                            mol.SetProp('resname', ligand_name)
                            
                            # CRITICAL: Set atom names for each atom in the molecule
                            for i, atom in enumerate(mol.GetAtoms()):
                                if i < len(atom_names):
                                    atom.SetProp('name', atom_names[i])
                                else:
                                    # Fallback naming
                                    element = atom.GetSymbol()
                                    atom.SetProp('name', f"{element}{i+1}")
                            
                            print(f"Set atom names for {mol.GetNumAtoms()} atoms in custom ligand")
                            
                            # Add to CCD temporarily with unique name
                            unique_ligand_name = self._add_temporary_ligand_to_ccd(ligand_name, mol)
                            print(f"Successfully created custom ligand definition for '{ligand_name}' as '{unique_ligand_name}'")
                            
                            # Retry structure preparation
                            try:
                                parsed_structure = parse_mmcif(
                                    path=str(complex_file),
                                    mols=self.ccd,
                                    moldir=self.work_dir / "boltz_out" / "processed" / "mols",
                                    call_compute_interfaces=False
                                )
                                
                                pose_output_dir = self.work_dir / "boltz_out" / "predictions" / record_id
                                output_path = pose_output_dir / f"pre_affinity_{record_id}.npz"
                                structure_v2 = parsed_structure.data
                                structure_v2.dump(output_path)
                                
                                # Write custom ligands for this record to enable inference
                                self._write_custom_mols_for_record(record_id)
                                
                                return True
                            except Exception as e:
                                print(f"Structure preparation still failed after custom ligand creation: {e}")
                                import traceback
                                print("Detailed traceback for custom ligand processing:")
                                traceback.print_exc()
                                return False
                    else:
                        print(f"Could not create RDKit molecule for ligand '{ligand_name}'")
                        return False
                        
                finally:
                    # Clean up temporary file
                    import os
                    try:
                        os.unlink(temp_sdf.name)
                    except:
                        pass
                        
            except Exception as e:
                print(f"Error processing CIF file for custom ligand: {e}")
                return False
                
        except ImportError:
            print("gemmi library not available for custom ligand processing")
            return False
        except Exception as e:
            print(f"Unexpected error in custom ligand creation: {e}")
            return False

    def _check_and_create_custom_ligands(self, complex_file: Path):
        """Proactively check for custom ligands and create them before structure parsing."""
        try:
            import gemmi
            
            # Read structure to identify non-standard ligands
            if complex_file.suffix.lower() == '.cif':
                doc = gemmi.cif.read(str(complex_file))
                block = doc[0]
                structure = gemmi.make_structure_from_block(block)
                
                # Find all unique ligand names
                ligand_names = set()
                for model in structure:
                    for chain in model:
                        for residue in chain:
                            # Skip standard amino acids and nucleotides
                            if residue.name not in {'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 
                                                   'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 
                                                   'THR', 'TRP', 'TYR', 'VAL', 'A', 'C', 'G', 'T', 'U', 
                                                   'DA', 'DC', 'DG', 'DT', 'HOH', 'WAT'}:
                                ligand_names.add(residue.name)
                
                # Check if each ligand exists in CCD, if not, create it with unique name
                for ligand_name in ligand_names:
                    if ligand_name not in self.ccd:
                        print(f"Pre-creating custom ligand definition for '{ligand_name}'...")
                        success = self._try_create_custom_ligand(complex_file, ligand_name, "precheck")
                        if not success:
                            print(f"Warning: Failed to pre-create ligand '{ligand_name}', will try again during parsing")
                            
        except Exception as e:
            print(f"Warning: Could not pre-check ligands: {e}")
            # This is not critical, parsing will handle it

    def predict(self, file_paths: List[str]):
        """Main prediction method with proper CCD cleanup."""
        try:
            files_to_process = []
            for file_path in file_paths:
                path = Path(file_path)
                if path.suffix.lower() == '.pdb':
                    cif_file = self._pdb_to_cif(path)
                    if cif_file:
                        files_to_process.append((path, cif_file))
                elif path.suffix.lower() == '.cif':
                    files_to_process.append((path, path))
                else:
                    print(f"Skipping unsupported file format: {path.name}")

            if not files_to_process:
                print("No valid PDB or CIF files to process.")
                return

            self._prepare_and_score(files_to_process)
            self._extract_results(files_to_process)
            self.save_results_csv()
            
        finally:
            # Always cleanup temporary ligands and release CCD name, even if an error occurred
            self._cleanup_temporary_ligands()
            print(f"Task {self.task_id}: CCD cache cleanup completed.")

    def predict_with_separate_inputs(self, protein_file: str, ligand_file: str, output_prefix: str = "combined_complex"):
        """
        Predict binding affinity using separate protein PDB and ligand SDF files.
        
        This method combines the separate files into a standard PDB complex and then
        uses the standard complex prediction pipeline.
        
        Note: For separate inputs, the ligand is automatically assigned a unique name
        to avoid conflicts between parallel runs.
        """
        try:
            protein_path = Path(protein_file)
            ligand_path = Path(ligand_file)
            
            # Validate input files
            if not protein_path.exists():
                raise FileNotFoundError(f"Protein file not found: {protein_file}")
            if not ligand_path.exists():
                raise FileNotFoundError(f"Ligand file not found: {ligand_file}")
            
            if protein_path.suffix.lower() not in ['.pdb', '.cif']:
                raise ValueError("Protein file must be PDB or CIF format")
            if ligand_path.suffix.lower() not in ['.sdf', '.mol', '.mol2']:
                raise ValueError("Ligand file must be SDF, MOL, or MOL2 format")
            
            print(f"Processing protein: {protein_path.name}")
            print(f"Processing ligand: {ligand_path.name}")
            print(f"Using unique ligand identifier: {self.unique_ligand_name}")
            
            # Create combined complex structure as standard PDB
            complex_file = self._create_standard_complex_pdb(protein_path, ligand_path, output_prefix)
            
            # Use standard complex prediction pipeline
            self.predict([str(complex_file)])
            
        except Exception as e:
            print(f"Error in separate input prediction: {e}")
            raise

    def _create_standard_complex_pdb(self, protein_file: Path, ligand_file: Path, output_prefix: str) -> Path:
        """
        Create a standard PDB complex file from separate protein and ligand files.
        This method preserves the original coordinates from both files.
        """
        combined_dir = self.output_dir / "combined_complexes"
        combined_dir.mkdir(exist_ok=True)
        
        combined_file = combined_dir / f"{output_prefix}_complex.pdb"
        
        # Convert paths to Path objects if they are strings
        protein_path = Path(protein_file)
        ligand_path = Path(ligand_file)
        
        # Load ligand from file (preserving original coordinates)
        ligand_mol = self._load_ligand_from_file(ligand_path)
        if ligand_mol is None:
            raise ValueError(f"Failed to load ligand from {ligand_file}")
        
        # Read protein file and convert to PDB if needed
        if protein_path.suffix.lower() == '.pdb':
            protein_content = protein_path.read_text()
        elif protein_path.suffix.lower() == '.cif':
            protein_content = self._convert_cif_to_pdb_content(protein_path)
        else:
            raise ValueError(f"Unsupported protein file format: {protein_path.suffix}")
        
        # Generate ligand PDB lines preserving original coordinates
        ligand_pdb_lines = self._generate_ligand_pdb_lines_preserve_coords(ligand_mol)
        
        # Combine protein and ligand into standard PDB format
        combined_content = self._combine_to_standard_pdb(protein_content, ligand_pdb_lines)
        
        # Write combined file
        combined_file.write_text(combined_content)
        print(f"Created standard complex: {combined_file}")
        print(f"Coordinates preserved from original files")
        
        return combined_file

    def _convert_cif_to_pdb_content(self, cif_file: Path) -> str:
        """Convert CIF file to PDB content using gemmi."""
        import gemmi
        try:
            structure = gemmi.read_structure(str(cif_file))
            pdb_content = ""
            for model in structure:
                for chain in model:
                    for residue in chain:
                        for atom in residue:
                            # Format as PDB ATOM record
                            line = f"ATOM  {atom.serial:5d} {atom.name:4s} {residue.name:3s} {chain.name:1s}{residue.seqid.num:4d}    {atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}{atom.occ:6.2f}{atom.b_iso:6.2f}          {atom.element.name:2s}\n"
                            pdb_content += line
            return pdb_content
        except Exception as e:
            raise ValueError(f"Failed to convert CIF to PDB: {e}")

    def _analyze_protein_structure(self, protein_content: str) -> Dict[str, Any]:
        """Analyze protein structure to determine optimal ligand placement."""
        protein_atoms = []
        ca_atoms = []
        
        for line in protein_content.split('\n'):
            if line.startswith('ATOM') and len(line) >= 54:
                try:
                    # Extract coordinates using PDB format positions
                    x = float(line[30:38].strip())
                    y = float(line[38:46].strip()) 
                    z = float(line[46:54].strip())
                    atom_name = line[12:16].strip()
                    
                    protein_atoms.append([x, y, z])
                    
                    # Collect CA atoms for center calculation
                    if atom_name == 'CA':
                        ca_atoms.append([x, y, z])
                        
                except (ValueError, IndexError):
                    continue
        
        if not protein_atoms:
            print("Warning: No protein atoms found, using default ligand position")
            return {
                'center': np.array([0.0, 0.0, 0.0]),
                'size': np.array([10.0, 10.0, 10.0]),
                'ca_center': np.array([0.0, 0.0, 0.0])
            }
        
        protein_coords = np.array(protein_atoms)
        protein_center = np.mean(protein_coords, axis=0)
        protein_size = np.max(protein_coords, axis=0) - np.min(protein_coords, axis=0)
        
        # Use CA atoms for better center if available
        if ca_atoms:
            ca_coords = np.array(ca_atoms)
            ca_center = np.mean(ca_coords, axis=0)
        else:
            ca_center = protein_center
        
        print(f"Protein analysis:")
        print(f"  Center: ({protein_center[0]:.2f}, {protein_center[1]:.2f}, {protein_center[2]:.2f})")
        print(f"  Size: ({protein_size[0]:.2f}, {protein_size[1]:.2f}, {protein_size[2]:.2f})")
        print(f"  CA center: ({ca_center[0]:.2f}, {ca_center[1]:.2f}, {ca_center[2]:.2f})")
        
        return {
            'center': protein_center,
            'size': protein_size,
            'ca_center': ca_center,
            'coords': protein_coords
        }

    def _generate_positioned_ligand_pdb_lines(self, ligand_mol, protein_info: Dict[str, Any]) -> str:
        """Generate ligand PDB lines with intelligent positioning near the protein."""
        from rdkit.Chem import AllChem
        import numpy as np
        
        ligand_lines = ""
        atom_serial = 1  # Will be renumbered later
        
        # Ensure we have a 3D conformer
        if ligand_mol.GetNumConformers() == 0:
            print("Generating 3D conformer for ligand...")
            AllChem.EmbedMolecule(ligand_mol, randomSeed=42)
            if AllChem.MMFFOptimizeMolecule(ligand_mol) != 0:
                print("Warning: MMFF optimization failed, using basic coordinates")
        
        conf = ligand_mol.GetConformer()
        
        # Calculate ligand centroid
        ligand_positions = []
        for i in range(ligand_mol.GetNumAtoms()):
            pos = conf.GetAtomPosition(i)
            ligand_positions.append([pos.x, pos.y, pos.z])
        
        ligand_coords = np.array(ligand_positions)
        ligand_centroid = np.mean(ligand_coords, axis=0)
        
        # Intelligent positioning strategy
        protein_center = protein_info['ca_center']
        protein_size = protein_info['size']
        
        # Place ligand at a reasonable distance from protein surface
        # Use the largest dimension to determine placement distance
        max_protein_dimension = np.max(protein_size)
        placement_distance = max_protein_dimension * 0.6  # 60% of max dimension
        
        # Place ligand slightly offset from protein center
        # This simulates a binding site location
        offset_direction = np.array([1.0, 0.5, 0.0])  # Slightly to the side
        offset_direction = offset_direction / np.linalg.norm(offset_direction)
        
        target_position = protein_center + offset_direction * placement_distance
        
        # Calculate translation needed
        translation = target_position - ligand_centroid
        
        print(f"Ligand positioning:")
        print(f"  Original centroid: ({ligand_centroid[0]:.2f}, {ligand_centroid[1]:.2f}, {ligand_centroid[2]:.2f})")
        print(f"  Target position: ({target_position[0]:.2f}, {target_position[1]:.2f}, {target_position[2]:.2f})")
        print(f"  Translation: ({translation[0]:.2f}, {translation[1]:.2f}, {translation[2]:.2f})")
        
        # Generate PDB lines with proper formatting
        element_counts = {}
        for i, atom in enumerate(ligand_mol.GetAtoms()):
            pos = conf.GetAtomPosition(i)
            x = pos.x + translation[0]
            y = pos.y + translation[1] 
            z = pos.z + translation[2]
            
            # Get element and create meaningful atom name
            element = atom.GetSymbol()
            
            # Count elements for naming
            if element not in element_counts:
                element_counts[element] = 0
            element_counts[element] += 1
            
            # Create atom name
            if element == 'H':
                atom_name = f"H{element_counts[element]}"
            elif element_counts[element] == 1:
                atom_name = element
            else:
                atom_name = f"{element}{element_counts[element]}"
            
            # Ensure atom name is properly formatted (left-aligned, max 4 chars)
            atom_name = atom_name[:4].ljust(4)
            
            # Standard PDB HETATM format with proper spacing
            line = f"HETATM{atom_serial:5d} {atom_name} LIG L   1    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00          {element:>2s}\n"
            ligand_lines += line
            atom_serial += 1
        
        return ligand_lines

    def _generate_ligand_pdb_lines_preserve_coords(self, ligand_mol) -> str:
        """Generate ligand PDB lines preserving original coordinates from SDF/MOL file."""
        from rdkit.Chem import AllChem
        
        ligand_lines = ""
        atom_serial = 1  # Will be renumbered later
        
        # Use unique ligand name to avoid conflicts
        ligand_name = self.unique_ligand_name
        
        # Ensure we have a 3D conformer (should already exist from loading)
        if ligand_mol.GetNumConformers() == 0:
            print("Warning: No conformer found, generating coordinates...")
            AllChem.EmbedMolecule(ligand_mol, randomSeed=42)
            if AllChem.MMFFOptimizeMolecule(ligand_mol) != 0:
                print("Warning: MMFF optimization failed")
        
        conf = ligand_mol.GetConformer()
        
        print(f"Preserving original ligand coordinates with unique name: {ligand_name}")
        
        # Generate PDB lines using original coordinates (no translation)
        element_counts = {}
        for i, atom in enumerate(ligand_mol.GetAtoms()):
            pos = conf.GetAtomPosition(i)
            # Use original coordinates directly - NO TRANSLATION
            x, y, z = pos.x, pos.y, pos.z
            
            # Get element and create meaningful atom name
            element = atom.GetSymbol()
            
            # Count elements for naming
            if element not in element_counts:
                element_counts[element] = 0
            element_counts[element] += 1
            
            # Create atom name
            if element == 'H':
                atom_name = f"H{element_counts[element]}"
            elif element_counts[element] == 1:
                atom_name = element
            else:
                atom_name = f"{element}{element_counts[element]}"
            
            # Ensure atom name is properly formatted (left-aligned, max 4 chars)
            atom_name = atom_name[:4].ljust(4)
            
            # Use the first 3 characters of unique ligand name for residue name in PDB
            resname = ligand_name[:3].upper()
            
            # Standard PDB HETATM format with proper spacing
            line = f"HETATM{atom_serial:5d} {atom_name} {resname} L   1    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00          {element:>2s}\n"
            ligand_lines += line
            atom_serial += 1
            
            # Print first few coordinates for verification
            if i < 3:
                print(f"  Atom {i+1} ({element}): ({x:.3f}, {y:.3f}, {z:.3f})")
        
        return ligand_lines

    def _combine_to_standard_pdb(self, protein_content: str, ligand_lines: str) -> str:
        """Combine protein and ligand into a standard PDB format."""
        lines = []
        
        # Add proper PDB header with unique identifier
        from datetime import datetime
        timestamp = datetime.now().strftime("%d-%b-%y").upper()
        lines.append(f"HEADER    PROTEIN-LIGAND COMPLEX                  {timestamp}   COMP\n")
        lines.append("REMARK   1 COMPLEX GENERATED FROM SEPARATE FILES\n")
        lines.append("REMARK   2 PROTEIN-LIGAND COMPLEX FOR AFFINITY PREDICTION\n")
        lines.append("REMARK   3 GENERATED BY BOLTZ-WEBUI\n")
        
        # Add protein lines (ensure proper formatting)
        protein_lines = protein_content.split('\n')
        atom_count = 0
        
        for line in protein_lines:
            line = line.strip()
            if line.startswith(('ATOM', 'HETATM')):
                # Ensure proper PDB format
                if len(line) >= 54:
                    atom_count += 1
                    # Re-number atoms sequentially
                    formatted_line = f"{line[:6]}{atom_count:5d}{line[11:]}\n"
                    lines.append(formatted_line)
            elif line.startswith(('REMARK', 'HEADER')):
                if 'GENERATED' not in line and 'COMPLEX' not in line:  # Avoid duplicate remarks
                    lines.append(line + '\n')
        
        # Add TER record before ligand if we have protein atoms
        if atom_count > 0:
            # Find the last protein ATOM/HETATM line to get residue info
            last_protein_atom_line = None
            for line in reversed(protein_lines):
                if line.startswith(('ATOM', 'HETATM')) and len(line) >= 54:
                    last_protein_atom_line = line
                    break
            
            if last_protein_atom_line:
                parts = last_protein_atom_line.split()
                if len(parts) >= 6:
                    resname = parts[3] if len(parts) > 3 else 'UNK'
                    resseq = parts[5] if len(parts) > 5 else '1'
                    lines.append(f"TER   {atom_count + 1:5d}      {resname} A{resseq}\n")
                else:
                    lines.append(f"TER   {atom_count + 1:5d}      UNK A   1\n")
            else:
                lines.append(f"TER   {atom_count + 1:5d}      UNK A   1\n")
        
        # Add ligand lines
        lines.append("REMARK   4 LIGAND COORDINATES\n")
        ligand_lines_list = ligand_lines.strip().split('\n')
        for ligand_line in ligand_lines_list:
            if ligand_line.strip():
                atom_count += 1
                # Re-number ligand atoms sequentially
                formatted_line = f"{ligand_line[:6]}{atom_count:5d}{ligand_line[11:]}\n"
                lines.append(formatted_line)
        
        # Add proper PDB ending
        lines.append("END\n")
        
        return ''.join(lines)

    def _load_ligand_from_file(self, ligand_file: Path):
        """Load ligand molecule from SDF, MOL, or MOL2 file, preserving original coordinates."""
        from rdkit import Chem
        from rdkit.Chem import AllChem
        
        try:
            # Load molecule based on file format, preserving hydrogens and coordinates
            if ligand_file.suffix.lower() in ['.sdf']:
                supplier = Chem.SDMolSupplier(str(ligand_file), removeHs=False)
                mol = next(supplier)
            elif ligand_file.suffix.lower() in ['.mol']:
                mol = Chem.MolFromMolFile(str(ligand_file), removeHs=False)
            elif ligand_file.suffix.lower() in ['.mol2']:
                mol = Chem.MolFromMol2File(str(ligand_file), removeHs=False)
            else:
                raise ValueError(f"Unsupported ligand file format: {ligand_file.suffix}")
            
            if mol is None:
                raise ValueError("Failed to parse ligand molecule")
            
            # Check if we have 3D coordinates
            has_valid_3d_coords = False
            if mol.GetNumConformers() > 0:
                conf = mol.GetConformer()
                # Check if coordinates are meaningful (not all zero, have variation)
                coords = []
                for i in range(mol.GetNumAtoms()):
                    pos = conf.GetAtomPosition(i)
                    coords.append([pos.x, pos.y, pos.z])
                
                if len(coords) > 1:
                    coord_array = np.array(coords)
                    coord_std = np.std(coord_array)
                    if coord_std > 0.001:  # Some variation in coordinates
                        has_valid_3d_coords = True
                        print(f"Using original 3D coordinates from {ligand_file.name}")
                        print(f"  Coordinate range: X({coord_array[:, 0].min():.3f} to {coord_array[:, 0].max():.3f})")
                        print(f"                    Y({coord_array[:, 1].min():.3f} to {coord_array[:, 1].max():.3f})")
                        print(f"                    Z({coord_array[:, 2].min():.3f} to {coord_array[:, 2].max():.3f})")
            
            # Only add hydrogens if we don't have them or don't have valid coordinates
            if not has_valid_3d_coords:
                print(f"No valid 3D coordinates found in {ligand_file.name}, generating new ones...")
                # Add hydrogens and generate coordinates
                mol = Chem.AddHs(mol)
                print(f"Added hydrogens: {mol.GetNumAtoms()} total atoms")
                
                # Generate 3D coordinates
                result = AllChem.EmbedMolecule(mol, randomSeed=42, useRandomCoords=True)
                if result != 0:
                    print("Warning: Standard embedding failed, trying alternative method")
                    AllChem.EmbedMolecule(mol, randomSeed=42, useRandomCoords=True, maxAttempts=10)
                
                # Optimize geometry
                try:
                    optimization_result = AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
                    if optimization_result == 0:
                        print("Successfully optimized ligand geometry")
                    else:
                        print("MMFF optimization did not converge, using UFF")
                        AllChem.UFFOptimizeMolecule(mol, maxIters=500)
                except Exception as e:
                    print(f"Geometry optimization failed: {e}, using unoptimized coordinates")
            else:
                # We have good coordinates, preserve them
                print("Preserving original coordinates exactly as provided")
                print(f"Using original molecule with {mol.GetNumAtoms()} atoms (no hydrogen addition)")
                
                # Do NOT add hydrogens to preserve exact coordinates
                # The affinity prediction should work with the original molecule structure
            
            # Final validation
            if mol.GetNumConformers() == 0:
                raise ValueError("Failed to get 3D coordinates for ligand")
            
            return mol
            
        except Exception as e:
            print(f"Error loading ligand from {ligand_file}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _cif_to_pdb(self, cif_file: Path) -> Optional[Path]:
        """Convert CIF file to PDB format using maxit."""
        pdb_dir = self.output_dir / "temp_pdb_files"
        pdb_dir.mkdir(exist_ok=True)
        
        pdb_file = pdb_dir / f"{cif_file.stem}.pdb"
        
        try:
            cmd = ["maxit", "-input", str(cif_file), "-output", str(pdb_file), "-o", "1"]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return pdb_file
        except subprocess.CalledProcessError as e:
            print(f"Error converting CIF to PDB: {e.stderr}")
            return None

    def _generate_ligand_pdb_lines(self, mol, resname: str, chain_id: str = "L") -> List[str]:
        """Generate PDB HETATM lines for a ligand molecule."""
        from rdkit import Chem
        
        pdb_lines = []
        atom_idx = 1
        
        # Get conformer (3D coordinates)
        if mol.GetNumConformers() == 0:
            print("Warning: No 3D coordinates found for ligand")
            return []
        
        conf = mol.GetConformer()
        
        for atom in mol.GetAtoms():
            pos = conf.GetAtomPosition(atom.GetIdx())
            element = atom.GetSymbol()
            
            # Get atom name, try to use a reasonable naming scheme
            atom_name = f"{element}{atom.GetIdx() + 1}"
            if len(atom_name) > 4:
                atom_name = atom_name[:4]
            
            # Format PDB HETATM line
            pdb_line = f"HETATM{atom_idx:5d} {atom_name:4s} {resname:3s} {chain_id:1s}   1    " \
                      f"{pos.x:8.3f}{pos.y:8.3f}{pos.z:8.3f}  1.00 20.00           {element:2s}"
            
            pdb_lines.append(pdb_line)
            atom_idx += 1
        
        return pdb_lines

    def _combine_protein_ligand(self, protein_content: str, ligand_lines: List[str]) -> str:
        """Combine protein PDB content with ligand lines."""
        lines = protein_content.splitlines()
        
        # Find insertion point (before END or at the end)
        insert_idx = len(lines)
        for i, line in enumerate(lines):
            if line.startswith('END'):
                insert_idx = i
                break
        
        # Insert ligand lines
        combined_lines = lines[:insert_idx] + ligand_lines + lines[insert_idx:]
        
        # Add TER record after ligand if END exists
        if insert_idx < len(lines) and lines[insert_idx].startswith('END'):
            combined_lines.insert(-1, "TER")
        
        return '\n'.join(combined_lines)

    def _pdb_to_cif(self, pdb_file: Path) -> Optional[Path]:
        cif_dir = self.output_dir / "cif_files"
        cif_dir.mkdir(exist_ok=True)
        base_name = pdb_file.stem
        complex_cif = cif_dir / f"{base_name}.cif"

        if complex_cif.exists() and not self.boltz_override:
            return complex_cif

        try:
            # Convert to CIF
            cmd1 = ["maxit", "-input", str(pdb_file), "-output", str(complex_cif), "-o", "1"]
            subprocess.run(cmd1, check=True, capture_output=True, text=True)
            return complex_cif
        except subprocess.CalledProcessError as e:
            print(f"Error processing {pdb_file.name}: {e.stderr}")
            return None

    def _prepare_and_score(self, files_to_process: List[Tuple[Path, Path]]):
        boltz_out_dir = self.work_dir / "boltz_out"
        processed_dir = boltz_out_dir / "processed"
        mols_dir = processed_dir / "mols"
        constraints_dir = processed_dir / "constraints"
        
        processed_dir.mkdir(parents=True, exist_ok=True)
        mols_dir.mkdir(parents=True, exist_ok=True)
        constraints_dir.mkdir(parents=True, exist_ok=True)

        record_ids = []
        files_with_ligands = []  # Track files that have valid ligands
        
        for i, (original_path, cif_file) in enumerate(files_to_process):
            record_id = f"{self.fname}_{i}"
            
            mol = None
            ligand_found = False
            actual_mw = 400.0  # Default fallback
            
            if original_path.suffix.lower() == '.pdb':
                # First, check if the specified ligand residue name exists in the PDB file
                pdb_content = original_path.read_text()
                hetatm_resnames = set()
                filtered_pdb_lines = []
                atom_count = 0
                hetatm_count = 0
                
                for line in pdb_content.splitlines():
                    if line.startswith('ATOM') and len(line) > 20:
                        atom_count += 1
                    elif line.startswith('HETATM') and len(line) > 20:
                        hetatm_count += 1
                        resname = line[17:20].strip()
                        hetatm_resnames.add(resname)
                        if resname == self.ligand_resname:
                            filtered_pdb_lines.append(line)

                # Detailed error reporting for different scenarios
                if hetatm_count == 0:
                    if atom_count > 0:
                        raise ValueError(f"Error: No ligand molecules (HETATM records) found in {original_path.name}. "
                                       f"This file contains {atom_count} protein atoms but no ligands. "
                                       f"For affinity prediction, you need a protein-ligand complex structure file. "
                                       f"\n\nPossible solutions:\n"
                                       f"1. Use a different PDB file that contains both protein and ligand\n"
                                       f"2. Use the 'separate' mode with separate protein and ligand files\n"
                                       f"3. Add ligand coordinates to your PDB file as HETATM records")
                    else:
                        raise ValueError(f"Error: Invalid PDB file {original_path.name}. "
                                       f"No ATOM or HETATM records found.")
                
                if self.ligand_resname not in hetatm_resnames:
                    available_resnames = ", ".join(sorted(hetatm_resnames))
                    raise ValueError(f"Error: Ligand residue name '{self.ligand_resname}' not found in {original_path.name}. "
                                   f"Found {hetatm_count} HETATM records with residue names: {available_resnames}. "
                                   f"Please use one of the available residue names or check your input file.")

                if filtered_pdb_lines:
                    temp_pdb_path = self.work_dir / f"{record_id}_{self.ligand_resname}.pdb"
                    temp_pdb_path.write_text("\n".join(filtered_pdb_lines))
                    mol = Chem.MolFromPDBFile(str(temp_pdb_path))
                    temp_pdb_path.unlink()
                    
                    if mol is not None:
                        # Calculate actual molecular weight
                        try:
                            from rdkit.Chem import rdMolDescriptors
                            actual_mw = rdMolDescriptors.CalcExactMolWt(mol)
                            print(f"Calculated molecular weight for {self.ligand_resname}: {actual_mw:.2f} Da")
                            ligand_found = True
                        except Exception as e:
                            print(f"Warning: Failed to calculate molecular weight, using default: {e}")
                            actual_mw = 400.0
                            ligand_found = True
                    else:
                        raise ValueError(f"Error: Failed to parse ligand with residue name '{self.ligand_resname}' "
                                       f"from {original_path.name}. The ligand structure may be invalid.")
                
            elif original_path.suffix.lower() == '.cif':
                # For CIF files, we'll use a simpler and more robust validation approach
                try:
                    parsed_structure = self._parse_cif_for_validation(cif_file)
                    
                    if parsed_structure is None:
                        raise ValueError(f"Error: No ligand molecules found in {original_path.name}. "
                                       f"This file appears to contain only protein/polymer chains and no ligands. "
                                       f"Please provide a protein-ligand complex structure file.")
                    
                    # If we have a mock structure or real structure, assume ligands are present
                    # The detailed validation will happen during main processing
                    ligand_found = True
                    
                except ValueError as e:
                    # Re-raise validation errors (like "No ligand molecules found")
                    raise e
                except Exception as e:
                    # For other parsing errors, we'll be more permissive with CIF files
                    # since they can be complex to parse and the main processing might handle it better
                    print(f"Warning: Could not fully validate ligand presence in CIF file {original_path.name}. "
                          f"Proceeding with processing. Error: {e}")
                    ligand_found = True

            if not ligand_found:
                raise ValueError(f"Error: No valid ligand found in {original_path.name}. "
                               f"Affinity prediction requires protein-ligand complex structures.")
            
            # Only add to processing list if ligand is found
            record_ids.append(record_id)
            files_with_ligands.append((original_path, cif_file, actual_mw))

            if mol:
                 with open(mols_dir / f"{record_id}.pkl", "wb") as f:
                    pickle.dump({self.base_ligand_name: mol}, f)

            constraints_data = {
                'rdkit_bounds_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (2,)),
                    ('is_bond', np.bool_),
                    ('is_angle', np.bool_),
                    ('upper_bound', np.float32),
                    ('lower_bound', np.float32),
                ]),
                'chiral_atom_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (4,)),
                    ('is_reference', np.bool_),
                    ('is_r', np.bool_),
                ]),
                'stereo_bond_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (4,)),
                    ('is_reference', np.bool_),
                    ('is_e', np.bool_),
                ]),
                'planar_bond_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (6,)),
                ]),
                'planar_ring_5_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (5,)),
                ]),
                'planar_ring_6_constraints': np.array([], dtype=[
                    ('atom_idxs', np.int64, (6,)),
                ]),
            }
            np.savez(constraints_dir / f"{record_id}.npz", **constraints_data)
            self._prepare_structure(cif_file, record_id)

        # If no files have valid ligands, raise an error
        if not record_ids:
            raise ValueError("Error: No files with valid ligands found. All input files appear to lack the required ligand structures.")

        self._update_manifest(record_ids, files_with_ligands, self.ligand_resname)
        
        self._score_poses()

    def _parse_cif_for_validation(self, cif_file: Path):
        """Parse CIF file to validate ligand presence without full processing."""
        try:
            # For CIF files, we'll do a more robust approach
            # First try to use gemmi to quickly check for non-polymer entities
            import gemmi
            
            try:
                block = gemmi.cif.read(str(cif_file))[0]
                structure = gemmi.make_structure_from_block(block)
                structure.merge_chain_parts()
                structure.remove_waters()
                structure.remove_hydrogens()
                structure.remove_alternative_conformations()
                structure.remove_empty_chains()
                
                # Check for non-polymer entities (ligands)
                has_ligands = False
                for entity_id, entity in enumerate(structure.entities):
                    if entity.entity_type.name in {"NonPolymer", "Branched"}:
                        has_ligands = True
                        break
                
                if not has_ligands:
                    # Check raw structure for any residues that might be ligands
                    standard_residues = {
                        "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", 
                        "HIS", "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", 
                        "THR", "TRP", "TYR", "VAL",  # Standard amino acids
                        "A", "C", "G", "T", "U", "DA", "DC", "DG", "DT", "HOH"
                    }
                    for model in structure:
                        for chain in model:
                            for residue in chain:
                                if residue.name not in standard_residues:
                                    has_ligands = True
                                    break
                            if has_ligands:
                                break
                        if has_ligands:
                            break
                
                if not has_ligands:
                    return None  # No ligands found
                
                # If we found ligands with quick check, return a simple structure
                return type('MockParsedStructure', (), {
                    'data': type('MockData', (), {
                        'chains': [type('MockChain', (), {'mol_type': 3})]  # Mock NONPOLYMER chain
                    })()
                })()
                    
            except Exception:
                # If gemmi fails, we'll assume there might be ligands and let full processing handle it
                # Return a mock structure that indicates potential ligands
                return type('MockParsedStructure', (), {
                    'data': type('MockData', (), {
                        'chains': [type('MockChain', (), {'mol_type': 3})]  # Mock NONPOLYMER chain
                    })()
                })()
            
        except Exception as e:
            # If all validation methods fail, raise an error
            raise ValueError(f"Failed to parse CIF file {cif_file.name}: {str(e)}")

    def _prepare_structure(self, complex_file: Path, record_id: str):
        pose_output_dir = self.work_dir / "boltz_out" / "predictions" / record_id
        pose_output_dir.mkdir(parents=True, exist_ok=True)
        output_path = pose_output_dir / f"pre_affinity_{record_id}.npz"

        if output_path.exists() and not self.boltz_override:
            return

        try:
            # Pre-emptively check for custom ligands in CIF/PDB files and create them if needed
            self._check_and_create_custom_ligands(complex_file)
            
            parsed_structure = parse_mmcif(
                path=str(complex_file),
                mols=self.ccd,
                moldir=self.work_dir / "boltz_out" / "processed" / "mols",
                call_compute_interfaces=False
            )
            structure_v2 = parsed_structure.data
            structure_v2.dump(output_path)
            
            # Write custom ligands for this record if any exist
            self._write_custom_mols_for_record(record_id)
        except ValueError as e:
            if "CCD component" in str(e) and "not found" in str(e):
                # Try to handle custom ligands automatically
                missing_ligand = self._extract_ligand_name_from_error(str(e))
                if missing_ligand:
                    success = self._try_create_custom_ligand(complex_file, missing_ligand, record_id)
                    if success:
                        return  # Structure was successfully prepared with custom ligand
                
                error_msg = (f"Structure preparation failed for {complex_file.name}: {e}\n"
                           f"\nThis error typically occurs when:\n"
                           f"1. The ligand residue name ('{missing_ligand or self.ligand_resname}') is not a standard PDB chemical component\n"
                           f"2. The structure file uses custom or non-standard ligand names\n"
                           f"3. The local chemical component dictionary is incomplete\n"
                           f"\nSuggested solutions:\n"
                           f"- Use standard PDB ligand names (e.g., ATP, ADP, GTP, NAD, etc.)\n"
                           f"- Check if the ligand name in your structure file matches common PDB components\n"
                           f"- Rename the ligand in your structure file to a standard name if possible\n"
                           f"- For custom ligands, ensure the structure contains valid atomic coordinates\n"
                           f"- Contact support if you believe this is a valid standard ligand")
                print(error_msg)
                raise ValueError(error_msg)
            else:
                raise e
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error preparing structure for {complex_file.name}: {e}")
            raise e

    def _update_manifest(self, record_ids: List[str], files_with_ligands: List[Tuple[Path, Path, float]], ligand_resname: str):
        records = []
        for i, record_id in enumerate(record_ids):
            # Get the actual molecular weight for this record
            actual_mw = files_with_ligands[i][2] if i < len(files_with_ligands) else 400.0
            
            record = {
                "id": record_id,
                "structure": {
                    "resolution": None, "method": None, "deposited": None, "released": None,
                    "revised": None, "num_chains": 2, "num_interfaces": None, "pH": None, "temperature": None
                },
                "chains": [
                    {
                        "chain_id": 0, "chain_name": "A", "mol_type": 0, "cluster_id": -1,
                        "msa_id": -1, "num_residues": -1, "valid": True, "entity_id": 0
                    },
                    {
                        "chain_id": 1, "chain_name": "B", "mol_type": 3, "cluster_id": -1,
                        "msa_id": -1, "num_residues": 1, "valid": True, "entity_id": 1
                    }
                ],
                "interfaces": [],
                "inference_options": { "pocket_constraints": [], "contact_constraints": [] },
                "templates": [],
                "md": None,
                "affinity": { "chain_id": 1, "mw": actual_mw }  # Using actual calculated molecular weight
            }
            records.append(record)
            
        manifest = {"records": records}
        with open(self.work_dir / "boltz_out" / "processed" / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=4)

    def _score_poses(self):
        print(f"DEBUG: msa_args = {self.msa_args}")
        
        boltz_model = load_boltz2_model(
            skip_run_structure=self.skip_run_structure,
            use_kernels=self.use_kernels,
            run_trunk_and_structure=self.run_trunk_and_structure,
            predict_affinity_args=self.predict_affinity_args,
            pairformer_args=self.pairformer_args,
            msa_args=self.msa_args,
            steering_args=self.steering_args,
            diffusion_process_args=self.diffusion_process_args
        )
        predict_affinity(
            self.work_dir,
            model_module=boltz_model,
            output_dir=str(self.work_dir / "boltz_out" / "predictions"),
            structures_dir=str(self.work_dir / "boltz_out" / "predictions"),
            constraints_dir=str(self.work_dir / "boltz_out" / "processed" / "constraints"),
            extra_mols_dir=str(self.work_dir / "boltz_out" / "processed" / "mols"),
            manifest_path=self.work_dir / "boltz_out" / "processed" / "manifest.json",
            num_workers=1, 
            batch_size=1, 
        )

    def _extract_results(self, files_to_process: List[Tuple[Path, Path]]):
        results = []
        for i, (original_path, cif_file) in enumerate(files_to_process):
            record_id = f"{self.fname}_{i}"
            affinity_file = self.work_dir / "boltz_out" / "predictions" / record_id / f"affinity_{record_id}.json"
            if affinity_file.exists():
                with open(affinity_file, "r") as f:
                    affinity_data = json.load(f)
                affinity_data['input_file'] = str(original_path)
                results.append(affinity_data)
        self.results = results

    def save_results_csv(self, output_file: Optional[str] = None):
        if not self.results:
            print("No results to save.")
            return
        
        output_file = Path(output_file) if output_file else self.output_dir / "affinity_results.csv"
        
        fieldnames = ['input_file', 'affinity_pred_value', 'affinity_probability_binary']
        # Add other fields if they exist
        if self.results and 'affinity_pred_value1' in self.results[0]:
            fieldnames.extend(['affinity_pred_value1', 'affinity_probability_binary1', 'affinity_pred_value2', 'affinity_probability_binary2'])

        with open(output_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.results)
        print(f"Results saved to {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Predict binding affinity for protein-ligand complexes using Boltzina.")
    
    # Create subparsers for different modes
    subparsers = parser.add_subparsers(dest='mode', help='Prediction modes')
    
    # Mode 1: Complex files
    complex_parser = subparsers.add_parser('complex', help='Predict from protein-ligand complex files')
    complex_parser.add_argument("input_files", nargs="+", help="One or more PDB or CIF files of protein-ligand complexes.")
    complex_parser.add_argument("--output_dir", default="boltzina_output", help="Directory to save results.")
    complex_parser.add_argument("--ligand_resname", default="LIG", help="Residue name of the ligand in PDB files (e.g., LIG, UNK).")
    
    # Mode 2: Separate files
    separate_parser = subparsers.add_parser('separate', help='Predict from separate protein and ligand files')
    separate_parser.add_argument("--protein", required=True, help="Protein structure file (PDB or CIF format).")
    separate_parser.add_argument("--ligand", required=True, help="Ligand structure file (SDF, MOL, or MOL2 format).")
    separate_parser.add_argument("--output_dir", default="boltzina_output", help="Directory to save results.")
    separate_parser.add_argument("--output_prefix", default="complex", help="Prefix for output files.")
    # Note: ligand_resname is automatically set to "LIG" for separate inputs
    
    # Legacy mode for backward compatibility
    parser.add_argument("input_files", nargs="*", help="One or more PDB or CIF files (legacy mode).")
    parser.add_argument("--output_dir", default="boltzina_output", help="Directory to save results.")
    parser.add_argument("--ligand_resname", default="LIG", help="Residue name of the ligand.")
    parser.add_argument("--protein", help="Protein structure file (for separate mode).")
    parser.add_argument("--ligand", help="Ligand structure file (for separate mode).")
    parser.add_argument("--output_prefix", default="complex", help="Prefix for output files.")
    
    args = parser.parse_args()
    
    # For separate input mode, always use "LIG" as ligand_resname
    if args.mode == 'separate':
        boltzina = Boltzina(output_dir=args.output_dir, ligand_resname="LIG")
    else:
        boltzina = Boltzina(output_dir=args.output_dir, ligand_resname=args.ligand_resname)
    
    # Determine which mode to use
    if args.mode == 'complex':
        boltzina.predict(args.input_files)
    elif args.mode == 'separate':
        boltzina.predict_with_separate_inputs(args.protein, args.ligand, args.output_prefix)
    else:
        # Legacy mode
        if args.protein and args.ligand:
            # For legacy separate mode, also use "LIG"
            boltzina_separate = Boltzina(output_dir=args.output_dir, ligand_resname="LIG")
            boltzina_separate.predict_with_separate_inputs(args.protein, args.ligand, args.output_prefix)
        elif args.input_files:
            boltzina.predict(args.input_files)
        else:
            parser.print_help()
            print("\nError: You must provide either complex files or separate protein and ligand files.")

if __name__ == "__main__":
    main()