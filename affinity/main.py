# 来自 https://github.com/ohuelab/boltzina 的修改版本

import numpy as np
import subprocess
import argparse
import pickle
import json
from dataclasses import asdict
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
from boltz.main import get_cache_path, MSAModuleArgs

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
        accelerator: str = "gpu",
        devices: int = 1,
        strategy: str = "auto",
        num_workers: int = 1,
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
        self.accelerator = accelerator
        self.devices = devices
        self.strategy = strategy
        self.num_workers = num_workers
        self.predict_affinity_args = predict_affinity_args
        self.pairformer_args = pairformer_args
        
        if msa_args is None:
            # Match upstream Boltz2 defaults for affinity (subsample + no dropout)
            self.msa_args = asdict(
                MSAModuleArgs(
                    subsample_msa=True,
                    num_subsampled_msa=1024,
                    use_paired_feature=True,
                )
            )
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

    def _sanitize_atom_name(self, name: str) -> str:
        """Sanitize atom names to ASCII 32..95 and uppercase for featurizer compatibility."""
        if name is None:
            return "X"
        cleaned = str(name).strip().upper()
        if not cleaned:
            return "X"
        safe_chars = []
        for ch in cleaned:
            code = ord(ch)
            if 32 <= code <= 95:
                safe_chars.append(ch)
            else:
                safe_chars.append("X")
        return "".join(safe_chars)[:4] or "X"
    
    def _add_temporary_ligand_to_ccd(self, ligand_name: str, ligand_mol):
        """Temporarily add a custom ligand to CCD for processing with original naming.
        
        Args:
            ligand_name: Original name of the ligand from the structure file (e.g., "Z91")
            ligand_mol: RDKit molecule object of the ligand or wrapped molecule
        """
        # Store ligand with its original name but add task-specific prefix to avoid conflicts
        task_specific_name = f"{self.task_id}_{ligand_name}"
        unique_ligand_name = self.unique_ligand_name  # Still reserve a unique name for tracking
        
        # Add temporary ligand to local CCD copy with both names
        # Use original name for structure parsing compatibility
        self.ccd[ligand_name] = ligand_mol
        self.ccd[task_specific_name] = ligand_mol  # Backup with task prefix
        self.custom_ligands.add(ligand_name)
        self.custom_ligands.add(task_specific_name)
        print(f"Task {self.task_id}: Added custom ligand '{ligand_name}' (and backup '{task_specific_name}') to local CCD cache")
        
        # Write custom ligand to local mols directory with original name
        self._write_custom_ligand_to_local_mols_dir(ligand_name, ligand_mol)
        self._write_custom_ligand_to_local_mols_dir(task_specific_name, ligand_mol)  # Backup
        
        return ligand_name  # Return original name for structure compatibility
    
    def _setup_local_mols_directory(self):
        """Set up local mols directory with canonical molecules and any custom ligands."""
        local_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
        local_mols_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy canonical molecules from global cache to local directory
        try:
            import shutil
            global_mols_dir = self.cache_dir / 'mols'
            if global_mols_dir.exists():
                print(f"Task {self.task_id}: Copying canonical molecules from {global_mols_dir} to {local_mols_dir}")
                
                # Copy all .pkl files from global to local
                copied_count = 0
                for pkl_file in global_mols_dir.glob("*.pkl"):
                    local_pkl_file = local_mols_dir / pkl_file.name
                    if not local_pkl_file.exists():  # Don't overwrite custom ligands
                        shutil.copy2(pkl_file, local_pkl_file)
                        copied_count += 1
                
                print(f"✓ Task {self.task_id}: Copied {copied_count} canonical molecules to local directory")
                
                # List all molecules now available in local directory
                local_molecules = list(local_mols_dir.glob("*.pkl"))
                print(f"Task {self.task_id}: Local molecules directory contains {len(local_molecules)} molecules:")
                for mol_file in sorted(local_molecules):
                    print(f"  - {mol_file.stem}")
                    
                # Check if our custom ligands are present
                for custom_ligand in self.custom_ligands:
                    custom_file = local_mols_dir / f"{custom_ligand}.pkl"
                    if custom_file.exists():
                        print(f"✓ Task {self.task_id}: Custom ligand '{custom_ligand}' is available")
                    else:
                        print(f"⚠️  Task {self.task_id}: Custom ligand '{custom_ligand}' is missing!")
                        
                # Show summary of available ligands
                available_ligands = [f.stem for f in local_mols_dir.glob("*.pkl")]
                print(f"Task {self.task_id}: All available ligands: {sorted(available_ligands)}")
                        
            else:
                print(f"⚠️  Task {self.task_id}: Global mols directory not found at {global_mols_dir}")
        except Exception as e:
            print(f"⚠️  Task {self.task_id}: Failed to copy canonical molecules: {e}")
            import traceback
            traceback.print_exc()

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
        for ligand_name in list(self.custom_ligands):  # Create a copy to avoid modification during iteration
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
        # Handle different error message formats
        patterns = [
            r"CCD component ([A-Za-z0-9]+) not found!",  # Format: "CCD component Z05 not found!"
            r"CCD component '([^']+)' not found",        # Format: "CCD component 'Z05' not found"
            r"CCD component \"([^\"]+)\" not found",     # Format: "CCD component "Z05" not found"
            r"component ([A-Za-z0-9]+) not found",       # Format: "component Z05 not found"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, error_message)
            if match:
                ligand_name = match.group(1)
                print(f"Extracted ligand name '{ligand_name}' from error: {error_message}")
                return ligand_name
        
        print(f"Could not extract ligand name from error: {error_message}")
        return None

    def _try_create_custom_ligand(self, complex_file: Path, ligand_name: str, record_id: str) -> bool:
        """Attempt to create a custom ligand definition from the structure file."""
        try:
            print(f"Attempting to create custom ligand definition for '{ligand_name}'...")
            
            # Try to extract ligand from structure file using gemmi
            import gemmi
            import tempfile
            
            try:
                # Read structure based on file type
                if complex_file.suffix.lower() == '.cif':
                    doc = gemmi.cif.read(str(complex_file))
                    block = doc[0]
                    structure = gemmi.make_structure_from_block(block)
                elif complex_file.suffix.lower() == '.pdb':
                    structure = gemmi.read_structure(str(complex_file))
                else:
                    print(f"Unsupported file format for ligand extraction: {complex_file.suffix}")
                    return False
                
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
                
                # Validate that the ligand has sufficient atoms
                if len(ligand_residue) < 1:
                    print(f"Ligand '{ligand_name}' has no atoms")
                    return False
                
                # Create a temporary SDF file for the ligand
                temp_sdf = tempfile.NamedTemporaryFile(suffix='.sdf', delete=False, mode='w')
                try:
                    # Write basic SDF header
                    temp_sdf.write(f"{ligand_name}\n")
                    temp_sdf.write("  Generated from structure coordinates\n")
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
                        
                        # Ensure we have valid coordinates
                        if not (pos.x and pos.y and pos.z):
                            print(f"Warning: Atom {atom_name} has invalid coordinates")
                        
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
                            print(f"Successfully sanitized custom ligand '{ligand_name}'")
                            
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
                                print(f"Used permissive sanitization for custom ligand '{ligand_name}'")
                                
                            except Exception as e2:
                                print(f"Permissive sanitization also failed: {e2}")
                                # Last resort: create a very basic molecule representation
                                try:
                                    # Create minimal molecule just for structure processing
                                    mol = Chem.MolFromMolFile(temp_sdf.name, sanitize=False)
                                    # Don't sanitize at all, just use raw coordinates
                                    print(f"Using unsanitized molecule for '{ligand_name}' as last resort")
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
                                    atom.SetProp('name', self._sanitize_atom_name(atom_names[i]))
                                else:
                                    # Fallback naming
                                    element = atom.GetSymbol()
                                    atom.SetProp('name', self._sanitize_atom_name(f"{element}{i+1}"))
                            
                            print(f"Set atom names for {mol.GetNumAtoms()} atoms in custom ligand")
                            
                            # Add to CCD temporarily with unique name
                            unique_ligand_name = self._add_temporary_ligand_to_ccd(ligand_name, mol)
                            print(f"Successfully created custom ligand definition for '{ligand_name}' as '{unique_ligand_name}'")
                            
                            # Also add the original ligand name as an alias to ensure parsing works
                            if ligand_name not in self.ccd:
                                self.ccd[ligand_name] = mol
                                self.custom_ligands.add(ligand_name)
                                print(f"Added original ligand name '{ligand_name}' as alias in CCD")
                                
                                # Also write the alias to local mols directory
                                self._write_custom_ligand_to_local_mols_dir(ligand_name, mol)
                            
                            # Retry structure preparation (only for record_id != "precheck")
                            if record_id != "precheck":
                                try:
                                    # Convert PDB to CIF if needed
                                    cif_file = complex_file
                                    if complex_file.suffix.lower() == '.pdb':
                                        cif_file = self._pdb_to_cif(complex_file)
                                        if not cif_file:
                                            print(f"Warning: Could not convert PDB to CIF for structure preparation")
                                            return False
                                    
                                    parsed_structure = parse_mmcif(
                                        path=str(cif_file),
                                        mols=self.ccd,
                                        moldir=self.work_dir / "boltz_out" / "processed" / "mols",
                                        call_compute_interfaces=False
                                    )
                                    
                                    pose_output_dir = self.work_dir / "boltz_out" / "predictions" / record_id
                                    pose_output_dir.mkdir(parents=True, exist_ok=True)
                                    output_path = pose_output_dir / f"pre_affinity_{record_id}.npz"
                                    structure_v2 = parsed_structure.data
                                    structure_v2.dump(output_path)
                                    
                                    # Write custom ligands for this record to enable inference
                                    self._write_custom_mols_for_record(record_id)
                                    
                                    print(f"✓ Structure preparation succeeded after creating custom ligand '{ligand_name}'")
                                    return True
                                except Exception as e:
                                    print(f"Structure preparation still failed after custom ligand creation: {e}")
                                    import traceback
                                    print("Detailed traceback for custom ligand processing:")
                                    traceback.print_exc()
                                    return False
                            else:
                                # For precheck, just return success if we created the ligand
                                print(f"✓ Custom ligand '{ligand_name}' pre-created successfully")
                                return True
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
            elif complex_file.suffix.lower() == '.pdb':
                structure = gemmi.read_structure(str(complex_file))
            else:
                print(f"Unsupported file format for ligand checking: {complex_file.suffix}")
                return
                
            # Find all unique ligand names
            ligand_names = set()
            for model in structure:
                for chain in model:
                    for residue in chain:
                        # Skip standard amino acids, nucleotides, and common solvents
                        if residue.name not in {'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 
                                               'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 
                                               'THR', 'TRP', 'TYR', 'VAL', 'A', 'C', 'G', 'T', 'U', 
                                               'DA', 'DC', 'DG', 'DT', 'HOH', 'WAT', 'H2O', 
                                               'NA', 'CL', 'MG', 'CA', 'K', 'ZN', 'FE', 'SO4', 'PO4', 'ACE', 'NME'}:
                            ligand_names.add(residue.name)
                
            # Check if each ligand exists in CCD, if not, create it with unique name
            for ligand_name in ligand_names:
                if ligand_name not in self.ccd:
                    print(f"Pre-creating custom ligand definition for '{ligand_name}'...")
                    success = self._try_create_custom_ligand(complex_file, ligand_name, "precheck")
                    if not success:
                        print(f"Warning: Could not pre-create ligand definition for '{ligand_name}', will attempt during structure parsing")
                    else:
                        print(f"Successfully pre-created ligand definition for '{ligand_name}'")
                        
        except ImportError:
            print("Warning: gemmi library not available for ligand pre-checking")
        except Exception as e:
            print(f"Warning: Error during ligand pre-checking: {e}")
            # Don't raise error, let the main parsing handle it

    def predict(self, file_paths: List[str]):
        """Main prediction method with proper CCD cleanup and individual file error handling."""
        processed_successfully = False
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

            try:
                self._prepare_and_score(files_to_process)
                processed_successfully = True
            except ValueError as e:
                error_str = str(e)
                if "No files could be successfully processed" in error_str:
                    print(f"\n❌ Batch processing failed: {error_str}")
                    print("\nNo files were successfully processed. Please check your input files and try again.")
                    return
                else:
                    # Re-raise other ValueError exceptions
                    raise e
            
            # Only extract results and save CSV if processing was successful
            if processed_successfully:
                self._extract_results(files_to_process)
                self.save_results_csv()
            
        finally:
            # Always cleanup temporary ligands and release CCD name, even if an error occurred
            self._cleanup_temporary_ligands()
            print(f"Task {self.task_id}: CCD cache cleanup completed.")

    def predict_with_separate_inputs(self, protein_file: str, ligand_file: str, output_prefix: str = "combined_complex"):
        """
        Predict binding affinity using separate protein PDB and ligand structure files.
        
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
            if ligand_path.suffix.lower() not in ['.sdf', '.mol', '.mol2', '.pdb', '.ent']:
                raise ValueError("Ligand file must be SDF, MOL, MOL2, or PDB format")
            
            print(f"Processing protein: {protein_path.name}")
            print(f"Processing ligand: {ligand_path.name}")
            print(f"Using unique ligand identifier: {self.unique_ligand_name}")
            
            # Create combined complex structure as standard PDB
            complex_file = self._create_standard_complex_pdb(protein_path, ligand_path, output_prefix)
            
            # Convert the PDB to CIF for proper processing
            complex_cif = self._convert_complex_pdb_to_cif(complex_file)
            
            # Use standard complex prediction pipeline with CIF file
            self.predict([str(complex_cif)])
            
        except Exception as e:
            error_str = str(e)
            if "zero-size array to reduction operation minimum" in error_str:
                print(f"Error in separate input prediction: Caught RuntimeError in DataLoader worker process.")
                print(f"Original error: {error_str}")
                print(f"\nThis error typically occurs when:")
                print(f"1. The ligand coordinates are invalid or missing")
                print(f"2. The combined structure has formatting issues")
                print(f"3. The ligand was not properly detected during processing")
                print(f"4. The protein-ligand complex lacks proper molecular connectivity")
                print(f"\nSuggested solutions:")
                print(f"- Verify that both protein and ligand files have valid 3D coordinates")
                print(f"- Check that the ligand file is in the correct format (SDF, MOL, MOL2, PDB)")
                print(f"- Ensure the protein file contains a complete protein structure")
                print(f"- Try using a different ligand format or protein structure")
                raise ValueError(f"Separate input prediction failed: {error_str}")
            else:
                print(f"Error in separate input prediction: {error_str}")
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
        
        # Use unified naming strategy: always try "LIG" first for separate inputs
        # Check if "LIG" is already in use in this task - if so, generate unique name
        preferred_name = "LIG"
        
        # Check if "LIG" conflicts with any existing ligands in this task
        conflict_detected = False
        if "LIG" in self.ccd and "LIG" in self.custom_ligands:
            # "LIG" is already registered by this task, check if it's the same molecule
            try:
                existing_mol = self.ccd["LIG"]
                # For simplicity, assume different separate input calls use different molecules
                # This ensures each separate input gets its own unique identifier if needed
                conflict_detected = True
            except:
                pass
        
        if conflict_detected:
            # Use unique naming for this ligand
            unique_ligand_name = self._add_temporary_ligand_to_ccd(f"SEP_{self.task_id}", ligand_mol)
            print(f"Conflict detected: Using unique ligand name for separate input: {unique_ligand_name}")
            # Still use "LIG" as residue name in PDB, but track with unique CCD name
            target_resname = "LIG"
        else:
            # Use "LIG" as both CCD name and residue name
            unique_ligand_name = self._add_temporary_ligand_to_ccd("LIG", ligand_mol)
            target_resname = "LIG"
            print(f"Using unified ligand name for separate input: LIG (CCD: {unique_ligand_name})")
        
        # Read protein file and convert to PDB if needed
        if protein_path.suffix.lower() == '.pdb':
            protein_content = protein_path.read_text()
        elif protein_path.suffix.lower() == '.cif':
            protein_content = self._convert_cif_to_pdb_content(protein_path)
        else:
            raise ValueError(f"Unsupported protein file format: {protein_path.suffix}")
        
        # Clean and validate protein content for proper PDB format
        protein_content = self._clean_protein_content(protein_content)
        
        # Generate ligand PDB lines and CONECT bonds preserving original coordinates
        # Note: _generate_ligand_pdb_lines_preserve_coords now returns (lines, bonds)
        ligand_pdb_lines, ligand_bonds = self._generate_ligand_pdb_lines_preserve_coords(ligand_mol)
        
        # Combine protein and ligand into standard PDB format
        combined_content = self._combine_to_standard_pdb(
            protein_content,
            ligand_pdb_lines,
            ligand_bonds=ligand_bonds,
        )
        
        # Write combined file
        combined_file.write_text(combined_content)
        print(f"Created standard complex: {combined_file}")
        print(f"Coordinates preserved from original files")
        
        return combined_file

    def _clean_protein_content(self, protein_content: str) -> str:
        """
        Clean protein PDB content to ensure proper formatting and remove problematic records.
        This helps avoid alignment mismatches in downstream processing.
        """
        lines = []
        
        for line in protein_content.split('\n'):
            # Preserve fixed-width columns; only trim trailing whitespace/newlines.
            line = line.rstrip()
            
            # Skip empty lines and problematic record types
            if not line:
                continue
                
            # Keep standard PDB record types
            if line.startswith(('HEADER', 'TITLE', 'COMPND', 'SOURCE', 'KEYWDS', 'EXPDTA', 
                              'AUTHOR', 'REVDAT', 'JRNL', 'REMARK')):
                lines.append(line)
                continue
            
            # Process ATOM and HETATM records
            if line.startswith(('ATOM', 'HETATM')):
                # Ensure line is long enough for proper PDB format
                if len(line) < 54:
                    continue
                    
                try:
                    record_type = line[0:6].strip()
                    atom_name = line[12:16].strip()
                    alt_loc = line[16:17].strip()
                    res_name = line[17:20].strip()
                    chain_id = line[21:22].strip()
                    res_seq = line[22:26].strip()
                    icode = line[26:27].strip()
                    x = line[30:38].strip()
                    y = line[38:46].strip()
                    z = line[46:54].strip()
                    
                    # Skip if essential coordinates are missing
                    if not (x and y and z):
                        continue
                        
                    # Skip alternative locations (keep only the first occurrence)
                    if alt_loc and alt_loc != 'A' and alt_loc != ' ':
                        continue

                    # Ensure a chain ID exists for downstream parsing
                    if len(line) > 21 and line[21] == ' ':
                        line = f"{line[:21]}A{line[22:]}"
                    
                    # For HETATM, skip if it's water or common ions (we only want ligands)
                    elif record_type == 'HETATM':
                        skip_hetatm = {'HOH', 'WAT', 'H2O', 'NA', 'CL', 'MG', 'CA', 'K', 'ZN', 'FE', 'SO4', 'PO4'}
                        if res_name in skip_hetatm:
                            continue
                    # Preserve original ATOM/HETATM line to avoid column shifts
                    lines.append(line)
                    
                except (ValueError, IndexError) as e:
                    # Skip malformed lines
                    print(f"Warning: Skipping malformed line: {line[:50]}... Error: {e}")
                    continue
                    
            # Keep other essential records (skip END/ENDMDL to append ligand later)
            elif line.startswith(('MODEL', 'TER', 'CONECT')):
                lines.append(line)
        
        cleaned_content = '\n'.join(lines)
        
        # Report cleaning results
        atom_lines = [l for l in lines if l.startswith(('ATOM', 'HETATM'))]
        print(f"Protein content cleaned: {len(atom_lines)} atoms kept from original file")
        
        return cleaned_content

    def _convert_cif_to_pdb_content(self, cif_file: Path) -> str:
        """Convert CIF file to PDB content using gemmi with improved handling."""
        import gemmi
        try:
            structure = gemmi.read_structure(str(cif_file))
            
            # Remove waters and other unwanted components
            structure.remove_waters()
            structure.remove_alternative_conformations() 
            structure.remove_empty_chains()
            
            pdb_lines = []
            atom_count = 0
            
            # Add header
            pdb_lines.append("HEADER    PROTEIN STRUCTURE")
            
            for model in structure:
                for chain in model:
                    for residue in chain:
                        # Only keep standard amino acids for protein chains
                        standard_aa = {
                            'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY',
                            'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER',
                            'THR', 'TRP', 'TYR', 'VAL'
                        }
                        
                        if residue.name not in standard_aa:
                            continue  # Skip non-standard residues
                            
                        for atom in residue:
                            atom_count += 1
                            # Format as PDB ATOM record
                            line = f"ATOM  {atom_count:5d} {atom.name:4s} {residue.name:3s} {chain.name:1s}{residue.seqid.num:4d}    {atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}{atom.occ:6.2f}{atom.b_iso:6.2f}          {atom.element.name:2s}"
                            pdb_lines.append(line)
            
            pdb_lines.append("END")
            pdb_content = '\n'.join(pdb_lines)
            
            print(f"Converted CIF to PDB: {atom_count} protein atoms")
            return pdb_content
            
        except Exception as e:
            raise ValueError(f"Failed to convert CIF to PDB: {e}")

    def _generate_ligand_pdb_lines_preserve_coords(self, ligand_mol) -> tuple[str, list[tuple[int, int]]]:
        """Generate ligand PDB lines and bond pairs preserving original coordinates."""
        from rdkit.Chem import AllChem
        
        ligand_lines = ""
        atom_serial = 1  # Will be renumbered later
        bond_pairs: list[tuple[int, int]] = []
        
        # Use unified naming strategy: prefer "LIG", fall back to unique name if conflicts
        resname = "LIG"
        
        # Ensure we have a 3D conformer (should already exist from loading)
        if ligand_mol.GetNumConformers() == 0:
            print("Warning: No conformer found, generating coordinates...")
            AllChem.EmbedMolecule(ligand_mol, randomSeed=42)
            if AllChem.MMFFOptimizeMolecule(ligand_mol) != 0:
                print("Warning: MMFF optimization failed")
        
        conf = ligand_mol.GetConformer()
        
        print(f"Generating ligand PDB with unified residue name: {resname}")
        
        # Generate PDB lines using original coordinates (no translation)
        element_counts = {}
        for i, atom in enumerate(ligand_mol.GetAtoms()):
            pos = conf.GetAtomPosition(i)
            # Use original coordinates directly - NO TRANSLATION
            x, y, z = pos.x, pos.y, pos.z

            # Get element and create meaningful atom name
            element = atom.GetSymbol()

            atom_name = None
            # Prefer original atom names from the ligand file when available
            for prop in ("name", "_TriposAtomName", "_atomName"):
                if atom.HasProp(prop):
                    raw_name = atom.GetProp(prop).strip()
                    if raw_name:
                        atom_name = raw_name
                        break

            if not atom_name:
                # Count elements for naming
                if element not in element_counts:
                    element_counts[element] = 0
                element_counts[element] += 1

                # Create atom name following PDB conventions
                if element == 'H':
                    atom_name = f"H{element_counts[element]}"
                elif element_counts[element] == 1:
                    atom_name = element
                else:
                    atom_name = f"{element}{element_counts[element]}"

            atom_name = self._sanitize_atom_name(atom_name)
            # Ensure atom name is properly formatted (left-aligned, max 4 chars)
            atom_name = atom_name[:4].ljust(4)
            
            # Standard PDB HETATM format with proper spacing and fixed fields
            line = f"HETATM{atom_serial:5d} {atom_name} {resname} L   1    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00          {element:>2s}"
            ligand_lines += line + "\n"
            atom_serial += 1
            
            # Print first few coordinates for verification
            if i < 3:
                print(f"  Atom {i+1} ({element}): ({x:.3f}, {y:.3f}, {z:.3f})")
        
        # Collect bond pairs using ligand-local 1-based indices
        for bond in ligand_mol.GetBonds():
            a = bond.GetBeginAtomIdx() + 1
            b = bond.GetEndAtomIdx() + 1
            bond_pairs.append((a, b))

        if bond_pairs:
            print(f"Generated {len(bond_pairs)} ligand bonds for CONECT records")
        else:
            print("Warning: No ligand bonds detected; CONECT records will be empty")

        return ligand_lines.rstrip('\n'), bond_pairs  # Remove trailing newline

    def _combine_to_standard_pdb(
        self,
        protein_content: str,
        ligand_lines: str,
        ligand_bonds: Optional[list[tuple[int, int]]] = None,
    ) -> str:
        """Combine protein and ligand into a standard PDB format with improved formatting."""
        lines = []
        
        # Add proper PDB header with unique identifier
        from datetime import datetime
        timestamp = datetime.now().strftime("%d-%b-%y").upper()
        lines.append(f"HEADER    PROTEIN-LIGAND COMPLEX                  {timestamp}   COMP")
        lines.append("REMARK   1 COMPLEX GENERATED FROM SEPARATE FILES")
        lines.append("REMARK   2 PROTEIN-LIGAND COMPLEX FOR AFFINITY PREDICTION")
        lines.append("REMARK   3 GENERATED BY BOLTZ-WEBUI")
        
        # Add protein lines (preserve original formatting)
        protein_lines = protein_content.split('\n')
        non_blank_chain_ids = set()
        max_serial = 0
        atom_count = 0
        last_chain_id = None
        last_res_seq = None
        last_res_name = None

        for line in protein_lines:
            # Preserve fixed-width columns for PDB parsing.
            line = line.rstrip()
            if line.startswith(('ATOM', 'HETATM')):
                if len(line) >= 54:
                    atom_count += 1
                    if len(line) > 21 and line[21].strip():
                        non_blank_chain_ids.add(line[21])
                    # Track last residue info for TER
                    last_res_name = line[17:20].strip() if len(line) >= 20 else last_res_name
                    try:
                        last_res_seq = int(line[22:26].strip())
                    except ValueError:
                        pass
                    if len(line) > 21 and line[21].strip():
                        last_chain_id = line[21]
                    # Track max atom serial to offset ligand atoms
                    try:
                        serial = int(line[6:11].strip())
                        if serial > max_serial:
                            max_serial = serial
                    except ValueError:
                        pass
                    lines.append(line)
            elif line.startswith(('REMARK', 'HEADER')):
                if 'GENERATED' not in line and 'COMPLEX' not in line:  # Avoid duplicate remarks
                    lines.append(line)
            elif line.startswith('TER'):
                lines.append(line)
        
        # Add TER record before ligand if we have protein atoms and no trailing TER
        if atom_count > 0:
            if not lines or not lines[-1].startswith("TER"):
                ter_chain = last_chain_id or (next(iter(sorted(non_blank_chain_ids))) if non_blank_chain_ids else "A")
                ter_res_name = (last_res_name or "ALA").strip()[:3].upper() or "ALA"
                lines.append(f"TER   {max_serial + 1:5d}      {ter_res_name:>3s} {ter_chain:1s}{(last_res_seq or 1):4d}")
        
        # Add ligand lines with proper chain designation
        lines.append("REMARK   4 LIGAND COORDINATES")
        ligand_lines_list = ligand_lines.strip().split('\n')
        ligand_serial_offset = max_serial
        ligand_atom_count = 0
        for ligand_line in ligand_lines_list:
            if ligand_line.strip():
                ligand_atom_count += 1
                # Re-number ligand atoms sequentially and set chain to L
                parts = list(ligand_line)
                # Set atom number
                atom_num_str = f"{ligand_serial_offset + ligand_atom_count:5d}"
                parts[6:11] = list(atom_num_str)
                # Ensure chain is L for ligand
                parts[21] = 'L'
                # Set residue number to 1 for ligand
                parts[22:26] = list("   1")
                
                formatted_line = ''.join(parts)
                lines.append(formatted_line)

        # Add CONECT records for ligand bonds to preserve topology
        if ligand_bonds:
            conect_map: dict[int, set[int]] = {}
            for a, b in ligand_bonds:
                a_serial = a + ligand_serial_offset
                b_serial = b + ligand_serial_offset
                conect_map.setdefault(a_serial, set()).add(b_serial)
                conect_map.setdefault(b_serial, set()).add(a_serial)

            for atom_serial in sorted(conect_map.keys()):
                partners = sorted(conect_map[atom_serial])
                # PDB CONECT supports up to 4 partners per line; split if needed
                for i in range(0, len(partners), 4):
                    chunk = partners[i:i + 4]
                    conect_line = f"CONECT{atom_serial:5d}" + "".join(f"{p:5d}" for p in chunk)
                    lines.append(conect_line)
        
        # Add proper PDB ending
        lines.append("END")
        
        combined_content = '\n'.join(lines)
        
        # Validation report
        total_atoms = len([l for l in lines if l.startswith(('ATOM', 'HETATM'))])
        protein_atoms = len([l for l in lines if l.startswith('ATOM')])
        ligand_atoms = len([l for l in lines if l.startswith('HETATM')])
        print(f"Combined PDB statistics: {total_atoms} total atoms ({protein_atoms} protein, {ligand_atoms} ligand)")
        
        return combined_content

    def _convert_complex_pdb_to_cif(self, pdb_file: Path) -> Path:
        """Convert the generated complex PDB file to CIF format using maxit."""
        cif_dir = self.output_dir / "cif_files"
        cif_dir.mkdir(exist_ok=True)
        
        cif_file = cif_dir / f"{pdb_file.stem}.cif"
        
        try:
            # Use maxit to convert PDB to CIF
            import subprocess
            cmd = ["maxit", "-input", str(pdb_file), "-output", str(cif_file), "-o", "1"]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            if cif_file.exists():
                print(f"Successfully converted complex to CIF: {cif_file}")
                return cif_file
            else:
                print("CIF conversion failed, using original PDB file")
                return pdb_file
                
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Warning: maxit conversion failed: {e}")
            print("Using original PDB file (may cause alignment issues)")
            return pdb_file
        except Exception as e:
            print(f"Unexpected error in PDB to CIF conversion: {e}")
            return pdb_file

    def _parse_mol2_coords(self, mol2_text: str) -> tuple[list[tuple[float, float, float]], list[str]]:
        """Parse mol2 ATOM block to extract coordinates and atom names."""
        coords: list[tuple[float, float, float]] = []
        names: list[str] = []
        in_atoms = False

        for raw in mol2_text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("@<TRIPOS>ATOM"):
                in_atoms = True
                continue
            if line.startswith("@<TRIPOS>") and in_atoms:
                in_atoms = False
                continue
            if not in_atoms:
                continue

            parts = line.split()
            if len(parts) < 6:
                continue
            # parts: atom_id, atom_name, x, y, z, atom_type, ...
            try:
                name = parts[1]
                x = float(parts[2])
                y = float(parts[3])
                z = float(parts[4])
            except (ValueError, IndexError):
                continue
            names.append(name)
            coords.append((x, y, z))

        return coords, names

    def _load_ligand_from_file(self, ligand_file: Path):
        """Load ligand molecule from SDF, MOL, MOL2, or PDB file, preserving original coordinates."""
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from rdkit.Geometry import Point3D
        
        try:
            # Load molecule based on file format, preserving hydrogens and coordinates
            if ligand_file.suffix.lower() in ['.sdf']:
                supplier = Chem.SDMolSupplier(str(ligand_file), removeHs=False)
                mol = next(supplier)
            elif ligand_file.suffix.lower() in ['.mol']:
                mol = Chem.MolFromMolFile(str(ligand_file), removeHs=False)
            elif ligand_file.suffix.lower() in ['.mol2']:
                mol2_text = ligand_file.read_text(errors="ignore")
                mol = Chem.MolFromMol2Block(mol2_text, removeHs=False, sanitize=False)
                if mol is None:
                    mol = Chem.MolFromMol2File(str(ligand_file), removeHs=False)
            elif ligand_file.suffix.lower() in ['.pdb', '.ent']:
                mol = Chem.MolFromPDBFile(str(ligand_file), removeHs=False, sanitize=False)
                if mol is not None:
                    try:
                        Chem.SanitizeMol(mol)
                    except Exception as sanitize_error:
                        print(f"Warning: PDB ligand sanitization failed: {sanitize_error}")
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

            # For MOL2 inputs, explicitly restore coordinates if RDKit dropped them
            if ligand_file.suffix.lower() == ".mol2" and not has_valid_3d_coords:
                coords, atom_names = self._parse_mol2_coords(mol2_text)
                if coords and len(coords) == mol.GetNumAtoms():
                    conf = Chem.Conformer(mol.GetNumAtoms())
                    for idx, (x, y, z) in enumerate(coords):
                        conf.SetAtomPosition(idx, Point3D(x, y, z))
                    mol.RemoveAllConformers()
                    mol.AddConformer(conf, assignId=True)
                    if atom_names and len(atom_names) == mol.GetNumAtoms():
                        for atom, name in zip(mol.GetAtoms(), atom_names):
                            if name:
                                safe_name = self._sanitize_atom_name(name)
                                atom.SetProp("_TriposAtomName", safe_name)
                                atom.SetProp("name", safe_name)
                    has_valid_3d_coords = True
                    print(f"Restored MOL2 coordinates from {ligand_file.name}")

            # Ensure atom names are sanitized for featurizer compatibility
            for idx, atom in enumerate(mol.GetAtoms()):
                preferred = None
                for prop in ("name", "_TriposAtomName", "_atomName"):
                    if atom.HasProp(prop):
                        value = atom.GetProp(prop).strip()
                        if value:
                            preferred = value
                            break
                if not preferred:
                    preferred = f"{atom.GetSymbol()}{idx + 1}"
                safe_name = self._sanitize_atom_name(preferred)
                atom.SetProp("name", safe_name)
                if atom.HasProp("_TriposAtomName"):
                    atom.SetProp("_TriposAtomName", safe_name)

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
            
            # For complex files, we need to generate unified ligand names
            # and rewrite the structure file with the new names
            rewritten_cif_file = self._rewrite_complex_with_unified_ligand_names(cif_file, record_id)
            if rewritten_cif_file != cif_file:
                print(f"Rewrote complex file with unified ligand names: {rewritten_cif_file}")
                # Update the cif_file reference to use the rewritten version
                files_to_process[i] = (original_path, rewritten_cif_file)
                cif_file = rewritten_cif_file
            
            if original_path.suffix.lower() == '.pdb':
                # For PDB files, handle both specific ligand names and auto-detection
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
                        # Use unified detection: look for "LIG", user-specified ligand, or our unique ligand names
                        if (resname == "LIG" or  # Unified standard name
                            resname == self.ligand_resname or 
                            resname in self.custom_ligands or 
                            resname.startswith('Z')):  # Fallback unique ligand format
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
                
                # Check if we have any valid ligands (using unified strategy)
                valid_ligands = [res for res in hetatm_resnames if 
                               res == "LIG" or  # Unified standard name
                               res == self.ligand_resname or res in self.custom_ligands or res.startswith('Z')]
                
                if not valid_ligands:
                    available_resnames = ", ".join(sorted(hetatm_resnames))
                    # Provide more helpful error message for unified naming
                    if "LIG" in hetatm_resnames:
                        # "LIG" is available, this should work
                        pass
                    elif self.ligand_resname not in hetatm_resnames:
                        raise ValueError(f"Error: No standard ligand residue names found in {original_path.name}. "
                                       f"Found {hetatm_count} HETATM records with residue names: {available_resnames}. "
                                       f"Expected 'LIG' (unified standard) or '{self.ligand_resname}'. "
                                       f"Please ensure your PDB file uses standard ligand naming or check your input file.")

                if filtered_pdb_lines:
                    temp_pdb_path = self.work_dir / f"{record_id}_ligands.pdb"
                    temp_pdb_path.write_text("\n".join(filtered_pdb_lines))
                    mol = Chem.MolFromPDBFile(str(temp_pdb_path))
                    temp_pdb_path.unlink()
                    
                    if mol is not None:
                        # Calculate actual molecular weight
                        try:
                            from rdkit.Chem import rdMolDescriptors
                            actual_mw = rdMolDescriptors.CalcExactMolWt(mol)
                            print(f"Calculated molecular weight for ligands: {actual_mw:.2f} Da")
                            ligand_found = True
                        except Exception as e:
                            print(f"Warning: Failed to calculate molecular weight, using default: {e}")
                            actual_mw = 400.0
                            ligand_found = True
                    else:
                        # Don't raise error immediately - the rewritten file might work
                        print(f"Warning: Failed to parse ligands from original PDB, using rewritten structure")
                        ligand_found = True  # Assume the rewritten file will work
                
            elif original_path.suffix.lower() == '.cif':
                # For CIF files, rely on the rewriting process to handle ligands
                try:
                    parsed_structure = self._parse_cif_for_validation(cif_file)
                    
                    if parsed_structure is None:
                        raise ValueError(f"Error: No ligand molecules found in {original_path.name}. "
                                       f"This file appears to contain only protein/polymer chains and no ligands. "
                                       f"Please provide a protein-ligand complex structure file.")
                    
                    # If we have a structure (mock or real), assume ligands are present
                    ligand_found = True
                    
                except ValueError as e:
                    # Re-raise validation errors
                    raise e
                except Exception as e:
                    # For other parsing errors, be more permissive
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
            
            # Prepare structure with error handling for individual files
            try:
                self._prepare_structure(cif_file, record_id)
                print(f"✅ Successfully prepared structure for {original_path.name}")
            except Exception as e:
                print(f"❌ Failed to prepare structure for {original_path.name}: {e}")
                # Remove this record from processing
                if record_id in record_ids:
                    record_ids.remove(record_id)
                if (original_path, cif_file, actual_mw) in files_with_ligands:
                    files_with_ligands.remove((original_path, cif_file, actual_mw))
                # Continue with other files
                continue

        # If no files have valid ligands or passed structure preparation, raise an error
        if not record_ids:
            raise ValueError("Error: No files could be successfully processed. All input files either lack required ligand structures or failed structure preparation.")

        print(f"✅ Successfully prepared {len(record_ids)} files for affinity prediction")
        self._update_manifest(record_ids, files_with_ligands, self.ligand_resname)
        
        try:
            self._score_poses()
        except Exception as e:
            print(f"❌ Error during affinity prediction: {e}")
            raise e

    def _rewrite_complex_with_unified_ligand_names(self, complex_file: Path, record_id: str) -> Path:
        """
        Rewrite a complex structure file to use unified ligand residue names.
        Uses "LIG" by default, only falls back to Z-prefix unique names if conflicts occur.
        
        Returns:
            Path to the rewritten file (same as input if no rewriting needed)
        """
        try:
            import gemmi
            
            # Read the structure
            if complex_file.suffix.lower() == '.cif':
                doc = gemmi.cif.read(str(complex_file))
                block = doc[0]
                structure = gemmi.make_structure_from_block(block)
            else:
                # For PDB files, convert to CIF first
                structure = gemmi.read_structure(str(complex_file))
            
            # Identify non-standard residues (potential ligands)
            standard_residues = {
                'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 
                'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 
                'THR', 'TRP', 'TYR', 'VAL',  # Standard amino acids
                'A', 'C', 'G', 'T', 'U', 'DA', 'DC', 'DG', 'DT',  # Nucleotides
                'HOH', 'WAT', 'H2O',  # Water
                'NA', 'CL', 'MG', 'CA', 'K', 'ZN', 'FE'  # Common ions
            }
            
            # Find ligands and create unified naming strategy
            ligand_residues = {}
            ligand_name_mapping = {}
            ligand_types = {}  # Track different ligand types
            lig_counter = 0
            
            # First pass: identify all unique ligand types
            for model in structure:
                for chain in model:
                    for residue in chain:
                        if residue.name not in standard_residues:
                            if residue.name not in ligand_types:
                                # Extract ligand molecule for comparison
                                ligand_mol = self._extract_ligand_from_residue(residue)
                                if ligand_mol is not None:
                                    ligand_types[residue.name] = ligand_mol
            
            # Second pass: create unified naming strategy
            for original_name, ligand_mol in ligand_types.items():
                # Try to use "LIG" as the preferred residue name
                target_resname = "LIG"
                
                # Check if "LIG" is already in use by another ligand type or conflicts
                conflict_detected = False
                
                # Check if "LIG" already exists in the structure
                for model in structure:
                    for chain in model:
                        for residue in chain:
                            if residue.name == "LIG" and residue.name != original_name:
                                conflict_detected = True
                                break
                        if conflict_detected:
                            break
                    if conflict_detected:
                        break
                
                # Also check if "LIG" is already mapped to a different ligand type
                for mapped_orig, mapped_target in ligand_name_mapping.items():
                    if mapped_target == "LIG" and mapped_orig != original_name:
                        conflict_detected = True
                        break
                
                # If conflict detected, use Z-prefix unique name
                if conflict_detected:
                    # Generate unique CCD name and add to CCD
                    unique_ccd_name = self._add_temporary_ligand_to_ccd(original_name, ligand_mol)
                    
                    # Generate Z-prefix residue name
                    if '_' in unique_ccd_name:
                        unique_part = unique_ccd_name.split('_')[-1]
                        try:
                            hex_value = int(unique_part[:6], 16) % 100
                            target_resname = f"Z{hex_value:02d}"
                        except ValueError:
                            hash_val = hash(unique_part) % 100
                            target_resname = f"Z{hash_val:02d}"
                    else:
                        hash_val = hash(unique_ccd_name) % 100
                        target_resname = f"Z{hash_val:02d}"
                    
                    # Ensure the safe residue name is different from the original
                    attempt = 0
                    while target_resname == original_name and attempt < 100:
                        attempt += 1
                        hash_val = (hash(unique_ccd_name + str(attempt)) % 100)
                        target_resname = f"Z{hash_val:02d}"
                    
                    ligand_residues[original_name] = (unique_ccd_name, target_resname)
                    print(f"Conflict detected: Mapping ligand '{original_name}' -> CCD:'{unique_ccd_name}' -> PDB:'{target_resname}'")
                else:
                    # No conflict, use "LIG" as residue name
                    unique_ccd_name = self._add_temporary_ligand_to_ccd("LIG", ligand_mol)
                    ligand_residues[original_name] = (unique_ccd_name, "LIG")
                    print(f"Unified naming: Mapping ligand '{original_name}' -> CCD:'{unique_ccd_name}' -> PDB:'LIG'")
                
                ligand_name_mapping[original_name] = target_resname
            
            # If no ligands found, return original file
            if not ligand_name_mapping:
                print(f"No ligands detected in {complex_file.name}, using original file")
                return complex_file
            
            # Rewrite the structure with new residue names
            for model in structure:
                for chain in model:
                    for residue in chain:
                        if residue.name in ligand_name_mapping:
                            residue.name = ligand_name_mapping[residue.name]
            
            # Ensure the new residue names are also registered in CCD
            for original_name, (unique_ccd_name, target_resname) in ligand_residues.items():
                if target_resname not in self.ccd and unique_ccd_name in self.ccd:
                    # Add the target residue name as an alias to the unique CCD name
                    self.ccd[target_resname] = self.ccd[unique_ccd_name]
                    self.custom_ligands.add(target_resname)
                    print(f"Added alias '{target_resname}' for CCD entry '{unique_ccd_name}'")
                    
                    # Also write the alias to local mols directory
                    self._write_custom_ligand_to_local_mols_dir(target_resname, self.ccd[target_resname])
            
            # Write the modified structure to a new file
            rewritten_dir = self.work_dir / "rewritten_complexes"
            rewritten_dir.mkdir(parents=True, exist_ok=True)
            
            rewritten_file = rewritten_dir / f"{record_id}_{complex_file.stem}_unified.cif"
            
            # Write as mmCIF format for consistency
            try:
                # Try different gemmi write methods
                if hasattr(structure, 'write_cif'):
                    structure.write_cif(str(rewritten_file))
                elif hasattr(structure, 'make_mmcif_document'):
                    doc = structure.make_mmcif_document()
                    doc.write_file(str(rewritten_file))
                else:
                    # Fallback: write as PDB and convert
                    temp_pdb = rewritten_file.with_suffix('.pdb')
                    structure.write_pdb(str(temp_pdb))
                    
                    # Convert PDB back to CIF using maxit if available
                    try:
                        import subprocess
                        subprocess.run(["maxit", "-input", str(temp_pdb), "-output", str(rewritten_file), "-o", "1"], 
                                     check=True, capture_output=True)
                        temp_pdb.unlink()  # Clean up temp PDB
                    except:
                        # If maxit fails, just use the PDB file
                        rewritten_file = temp_pdb
                        
            except Exception as write_error:
                print(f"Warning: Failed to write structure file: {write_error}")
                return complex_file
            
            print(f"✓ Rewrote complex structure with unified ligand naming strategy")
            print(f"  Original: {complex_file}")
            print(f"  Rewritten: {rewritten_file}")
            print(f"  Ligand mappings: {len(ligand_name_mapping)} ligand type(s)")
            for orig, target in ligand_name_mapping.items():
                if target == "LIG":
                    print(f"    '{orig}' -> 'LIG' (unified naming)")
                else:
                    print(f"    '{orig}' -> '{target}' (conflict resolution)")
            
            return rewritten_file
            
        except ImportError:
            print("Warning: gemmi not available, cannot rewrite complex structure")
            return complex_file
        except Exception as e:
            print(f"Warning: Failed to rewrite complex structure: {e}")
            import traceback
            traceback.print_exc()
            return complex_file
    
    def _extract_ligand_from_residue(self, residue) -> Optional[object]:
        """
        Extract ligand molecule from a gemmi residue object and convert to RDKit format.
        """
        try:
            from rdkit import Chem
            from rdkit.Chem import AllChem
            
            # Create a simple SDF-like representation
            mol_block = ""
            mol_block += f"{residue.name}\n"
            mol_block += "  Generated from structure\n"
            mol_block += "\n"
            
            atoms = list(residue)
            if len(atoms) == 0:
                return None
            
            # Write atom count
            mol_block += f"{len(atoms):3d}  0  0  0  0  0  0  0  0  0  0 V2000\n"
            
            # Write atoms
            for atom in atoms:
                pos = atom.pos
                element = atom.element.name
                mol_block += f"{pos.x:10.4f}{pos.y:10.4f}{pos.z:10.4f} {element:2s}  0  0  0  0  0  0  0  0  0  0  0  0\n"
            
            mol_block += "M  END\n"
            
            # Parse with RDKit
            mol = Chem.MolFromMolBlock(mol_block, sanitize=False)
            if mol is not None:
                # Set basic properties
                mol.SetProp('_Name', residue.name)
                mol.SetProp('name', residue.name)
                mol.SetProp('id', residue.name)
                
                # Set atom names
                for i, atom in enumerate(atoms):
                    if i < mol.GetNumAtoms():
                        mol_atom = mol.GetAtomWithIdx(i)
                        mol_atom.SetProp('name', atom.name)
                
                # Try basic sanitization
                try:
                    Chem.SanitizeMol(mol, sanitizeOps=Chem.SANITIZE_ALL)
                except:
                    # If sanitization fails, use permissive settings
                    try:
                        sanitize_ops = (Chem.SANITIZE_ALL ^ 
                                      Chem.SANITIZE_PROPERTIES ^ 
                                      Chem.SANITIZE_KEKULIZE)
                        Chem.SanitizeMol(mol, sanitizeOps=sanitize_ops)
                    except:
                        # Last resort: use unsanitized
                        pass
                
                return mol
            
        except Exception as e:
            print(f"Warning: Failed to extract ligand from residue {residue.name}: {e}")
        
        return None

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
            
            # Ensure we have a CIF file for parsing
            cif_file = complex_file
            if complex_file.suffix.lower() == '.pdb':
                print(f"Converting PDB to CIF for structure preparation: {complex_file.name}")
                cif_file = self._pdb_to_cif(complex_file)
                if not cif_file:
                    raise ValueError(f"Could not convert PDB file {complex_file.name} to CIF format")
                print(f"Successfully converted to CIF: {cif_file.name}")
            
            print(f"Parsing structure file: {cif_file.name}")
            try:
                parsed_structure = parse_mmcif(
                    path=str(cif_file),
                    mols=self.ccd,
                    moldir=self.work_dir / "boltz_out" / "processed" / "mols",
                    call_compute_interfaces=False
                )
            except Exception as parse_error:
                print(f"❌ Structure parsing failed for {cif_file.name}: {type(parse_error).__name__}: {parse_error}")
                import traceback
                traceback.print_exc()
                raise ValueError(f"Structure parsing failed for {complex_file.name}: {parse_error}")
            
            if parsed_structure is None:
                raise ValueError(f"Structure parsing returned None for {complex_file.name}")
            
            print(f"Extracting structure data from parsed result...")
            try:
                structure_v2 = parsed_structure.data
            except Exception as data_error:
                print(f"❌ Failed to extract data from parsed structure: {type(data_error).__name__}: {data_error}")
                import traceback
                traceback.print_exc()
                raise ValueError(f"Failed to extract structure data for {complex_file.name}: {data_error}")
            
            if structure_v2 is None:
                raise ValueError(f"Structure data is None after parsing for {complex_file.name}")
            
            print(f"Structure parsing completed. Type: {type(structure_v2)}")
            
            # Basic validation - check that we have the essential structure components
            if not hasattr(structure_v2, 'atoms') or not hasattr(structure_v2, 'chains'):
                raise ValueError(f"Structure missing essential components (atoms/chains) for {complex_file.name}")
            
            if len(structure_v2.atoms) == 0:
                raise ValueError(f"Structure has no atoms for {complex_file.name}")
            
            if len(structure_v2.chains) == 0:
                raise ValueError(f"Structure has no chains for {complex_file.name}")
            
            print(f"Structure has {len(structure_v2.atoms)} atoms and {len(structure_v2.chains)} chains")
            
            print(f"Saving structure data to: {output_path}")
            try:
                structure_v2.dump(output_path)
            except Exception as dump_error:
                print(f"❌ Failed to save structure data: {type(dump_error).__name__}: {dump_error}")
                import traceback
                traceback.print_exc()
                raise ValueError(f"Failed to save structure data for {complex_file.name}: {dump_error}")
            
            # Write custom ligands for this record if any exist
            self._write_custom_mols_for_record(record_id)
            print(f"✅ Structure preparation completed successfully for {complex_file.name}")
            
        except ValueError as e:
            error_str = str(e)
            if "CCD component" in error_str and "not found" in error_str:
                # Try to handle custom ligands automatically
                missing_ligand = self._extract_ligand_name_from_error(error_str)
                if missing_ligand:
                    print(f"Attempting to create custom ligand definition for missing component: {missing_ligand}")
                    success = self._try_create_custom_ligand(complex_file, missing_ligand, record_id)
                    if success:
                        print(f"Successfully created custom ligand '{missing_ligand}', retrying structure preparation...")
                        return  # Structure was successfully prepared with custom ligand
                    else:
                        print(f"Failed to create custom ligand definition for '{missing_ligand}'")
                
                error_msg = (f"Chemical component error: {e}\n"
                           f"\nThis error occurs when the structure file contains ligand residue name(s) that are not recognized.\n"
                           f"Missing component: '{missing_ligand or 'unknown'}'\n"
                           f"\nCommon causes and solutions:\n"
                           f"1. Non-standard ligand names: Rename ligands to standard PDB codes (e.g., ATP, ADP, GTP)\n"
                           f"2. Custom ligands: Ensure the structure file has valid coordinates for all atoms\n"
                           f"3. Corrupted structure: Check that the PDB/CIF file is properly formatted\n"
                           f"4. Missing hydrogen atoms: Try adding hydrogens to the ligand structure\n"
                           f"\nFile: {complex_file.name}\n"
                           f"Suggested fixes:\n"
                           f"- Use a structure file with standard PDB ligand names\n"
                           f"- Edit the ligand residue name in your structure file to a known component\n"
                           f"- Provide a separate protein and ligand file using 'separate' mode")
                print(error_msg)
                raise ValueError(error_msg)
            else:
                raise e
        except RecursionError as re:
            # Handle recursion errors that can occur during structure parsing
            error_msg = (f"Recursion error during structure processing: {complex_file.name}\n"
                       f"This typically indicates a problem with the structure file format or content.\n"
                       f"\nSuggested solutions:\n"
                       f"1. Check that the input file is a valid PDB or mmCIF structure file\n"
                       f"2. Ensure the file contains both protein and ligand coordinates\n"
                       f"3. Try using separate protein and ligand files instead\n"
                       f"4. Verify the file is not corrupted and follows standard format conventions\n"
                       f"\nOriginal error: {str(re)}")
            print(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Unexpected error preparing structure for {complex_file.name}: {e}")
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
        
        # Set up local mols directory with canonical molecules and custom ligands
        self._setup_local_mols_directory()
        
        boltz_model = load_boltz2_model(
            skip_run_structure=self.skip_run_structure,
            use_kernels=self.use_kernels,
            run_trunk_and_structure=self.run_trunk_and_structure,
            confidence_prediction=not self.skip_run_structure,
            predict_affinity_args=self.predict_affinity_args,
            pairformer_args=self.pairformer_args,
            msa_args=self.msa_args,
            steering_args=self.steering_args,
            diffusion_process_args=self.diffusion_process_args
        )
        
        try:
            predict_affinity(
                self.work_dir,
                model_module=boltz_model,
                output_dir=str(self.work_dir / "boltz_out" / "predictions"),
                structures_dir=str(self.work_dir / "boltz_out" / "predictions"),
                constraints_dir=str(self.work_dir / "boltz_out" / "processed" / "constraints"),
                extra_mols_dir=str(self.work_dir / "boltz_out" / "processed" / "mols"),
                manifest_path=self.work_dir / "boltz_out" / "processed" / "manifest.json",
                mol_dir=str(self.work_dir / "boltz_out" / "processed" / "mols"),  # Use local mols directory
                num_workers=self.num_workers,
                batch_size=1, 
                accelerator=self.accelerator,
                devices=self.devices,
                strategy=self.strategy,
            )
        except RuntimeError as e:
            error_str = str(e)
            if "zero-size array to reduction operation minimum" in error_str:
                raise ValueError(
                    "Affinity prediction failed: No valid ligand atoms found for cropping.\n"
                    "This error occurs when the structure contains no ligand atoms that can be used for affinity prediction.\n"
                    "\nPossible causes:\n"
                    "1. The ligand was not properly detected in the structure file\n"
                    "2. Ligand coordinates are invalid or missing\n"
                    "3. The ligand residue name doesn't match the expected format\n"
                    "4. Structure parsing failed to identify ligand atoms correctly\n"
                    "5. The ligand chain was not properly assigned as NONPOLYMER type\n"
                    "\nSuggested solutions:\n"
                    "- Verify that your structure file contains ligand atoms as HETATM records\n"
                    "- Check that ligand coordinates are valid (not all zeros)\n"
                    "- Ensure the ligand residue name matches standard PDB conventions\n"
                    "- Try using separate protein and ligand files with 'separate' mode\n"
                    "- Check the structure file for formatting issues or corruption\n"
                    "- Verify that the custom ligand definition was created correctly\n"
                    f"\nOriginal error: {error_str}"
                )
            elif "All indices have failed" in error_str:
                raise ValueError(
                    "Affinity prediction failed: Unable to process any structure tokens.\n"
                    "This error indicates that the structure could not be properly processed for prediction.\n"
                    "\nPossible causes:\n"
                    "1. Invalid or corrupted structure file\n"
                    "2. Missing essential structure components (protein or ligand)\n"
                    "3. Incompatible file format or structure representation\n"
                    "4. Insufficient or invalid molecular coordinates\n"
                    "5. Problems with custom ligand definitions in CCD cache\n"
                    "\nSuggested solutions:\n"
                    "- Verify the structure file is valid and properly formatted\n"
                    "- Ensure both protein and ligand components are present\n"
                    "- Try a different structure file or format\n"
                    "- Use separate protein and ligand files with 'separate' mode\n"
                    "- Check that custom ligands were properly added to the system\n"
                    f"\nOriginal error: {error_str}"
                )
            else:
                # Re-raise the original error if it's not one we specifically handle
                raise e
        except ValueError as e:
            # Pass through ValueError exceptions from our own validation
            raise e
        except Exception as e:
            print(f"Unexpected error during affinity prediction: {e}")
            import traceback
            traceback.print_exc()
            raise e

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
    separate_parser.add_argument("--ligand", required=True, help="Ligand structure file (SDF, MOL, MOL2, or PDB format).")
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
