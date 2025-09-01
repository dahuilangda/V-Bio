# 来自 https://github.com/ohuelab/boltzina 的修改版本

import numpy as np
import subprocess
import argparse
import pickle
import json
import csv
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from rdkit import Chem
Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

from affinity.boltzina.data.parse.mmcif import parse_mmcif
from affinity.boltzina.affinity.predict_affinity import load_boltz2_model, predict_affinity
from boltz.main import get_cache_path

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
        
        self.cache_dir = Path(get_cache_path())
        self.ccd_path = self.cache_dir / 'ccd.pkl'
        self.ccd = self._load_ccd()


    def _load_ccd(self) -> Dict[str, Any]:
        """Load CCD cache and ensure clean state for new predictions."""
        if self.ccd_path.exists():
            with self.ccd_path.open('rb') as file:
                ccd = pickle.load(file)
                # Always remove any previous temporary ligands to prevent contamination
                if self.base_ligand_name in ccd:
                    ccd.pop(self.base_ligand_name)
                # Remove any custom ligand entries that might have been left from previous runs
                custom_ligands_to_remove = [key for key in ccd.keys() if key.startswith('CUSTOM_') or key == self.ligand_resname]
                for custom_key in custom_ligands_to_remove:
                    ccd.pop(custom_key, None)
                return ccd
        else:
            return {}
    
    def _add_temporary_ligand_to_ccd(self, ligand_name: str, ligand_mol):
        """Temporarily add a custom ligand to CCD for processing.
        
        Args:
            ligand_name: Name of the ligand to add
            ligand_mol: RDKit molecule object of the ligand or wrapped molecule
        """
        # Store the original state for restoration
        if not hasattr(self, '_original_ccd_state'):
            self._original_ccd_state = self.ccd.copy()
        
        # Add temporary ligand (use the molecule as-is, whether raw RDKit or wrapped)
        self.ccd[ligand_name] = ligand_mol
        print(f"Temporarily added custom ligand '{ligand_name}' to CCD cache")
        
        # CRITICAL: Also save custom ligand as individual .pkl file for inference
        self._write_custom_ligand_to_mols_dir(ligand_name, ligand_mol)
    
    def _write_custom_ligand_to_mols_dir(self, ligand_name: str, ligand_mol):
        """Write a custom ligand as an individual .pkl file to the mols directory."""
        try:
            # Get the cache directory that inference will use
            from boltz.main import get_cache_path
            from pathlib import Path
            cache_dir = Path(get_cache_path())
            inference_mols_dir = cache_dir / 'mols'
            
            # Also write to our local mols directory for backup
            local_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
            
            # Ensure both directories exist
            inference_mols_dir.mkdir(parents=True, exist_ok=True)
            local_mols_dir.mkdir(parents=True, exist_ok=True)
            
            # Write to both locations
            import pickle
            
            # Primary location: inference cache directory
            inference_pkl_path = inference_mols_dir / f"{ligand_name}.pkl"
            with open(inference_pkl_path, 'wb') as f:
                pickle.dump(ligand_mol, f)
            print(f"✓ Wrote custom ligand '{ligand_name}' to inference cache: {inference_pkl_path}")
            
            # Backup location: local processed directory  
            local_pkl_path = local_mols_dir / f"{ligand_name}.pkl"
            with open(local_pkl_path, 'wb') as f:
                pickle.dump(ligand_mol, f)
            print(f"✓ Wrote custom ligand '{ligand_name}' to local backup: {local_pkl_path}")
            
        except Exception as e:
            print(f"⚠️  Failed to write custom ligand '{ligand_name}' to mols directory: {e}")
            import traceback
            traceback.print_exc()
    
    def _cleanup_temporary_ligands(self):
        """Remove temporary ligands from CCD and cache directories to prevent contamination."""
        # Clean up CCD cache
        if hasattr(self, '_original_ccd_state'):
            # Restore original CCD state
            self.ccd = self._original_ccd_state.copy()
            delattr(self, '_original_ccd_state')
            print("Cleaned up temporary ligands from CCD cache")
        else:
            # Fallback: remove known temporary entries
            temp_keys_to_remove = []
            for key in self.ccd.keys():
                if (key == self.ligand_resname or 
                    key == self.base_ligand_name or 
                    key.startswith('CUSTOM_') or
                    key.startswith('TEMP_')):
                    temp_keys_to_remove.append(key)
            
            for key in temp_keys_to_remove:
                self.ccd.pop(key, None)
                print(f"Removed temporary ligand '{key}' from CCD cache")
        
        # Clean up custom ligands from inference cache directory
        try:
            from boltz.main import get_cache_path
            from pathlib import Path
            cache_dir = Path(get_cache_path())
            inference_mols_dir = cache_dir / 'mols'
            
            if inference_mols_dir.exists():
                # Remove our custom ligand files
                for pkl_file in inference_mols_dir.glob("*.pkl"):
                    ligand_name = pkl_file.stem
                    if (ligand_name == self.ligand_resname or 
                        ligand_name == self.base_ligand_name or
                        ligand_name.startswith('CUSTOM_') or
                        ligand_name.startswith('TEMP_') or
                        ligand_name == 'LIG1'):  # Specifically target LIG1
                        try:
                            pkl_file.unlink()
                            print(f"Removed custom ligand file: {pkl_file}")
                        except Exception as e:
                            print(f"Failed to remove {pkl_file}: {e}")
                            
        except Exception as e:
            print(f"Error cleaning up inference cache directory: {e}")

    def _write_custom_mols_for_inference(self):
        """Write any custom ligands to the extra_mols directory for inference."""
        if not hasattr(self, '_original_ccd_state'):
            # No custom ligands were added
            return
            
        extra_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
        extra_mols_dir.mkdir(parents=True, exist_ok=True)
        
        # Find custom ligands (those not in original state)
        custom_ligands = {}
        if hasattr(self, '_original_ccd_state'):
            for ligand_name, ligand_mol in self.ccd.items():
                if ligand_name not in self._original_ccd_state:
                    custom_ligands[ligand_name] = ligand_mol
        
        if custom_ligands:
            print(f"Writing {len(custom_ligands)} custom ligand(s) to extra_mols_dir for inference...")
            
            # We need to create a pickle file for each record/structure
            # Since we don't know the record IDs at this point, we'll create them during structure preparation
            # For now, store the custom ligands for later use
            if not hasattr(self, '_custom_ligands_for_inference'):
                self._custom_ligands_for_inference = custom_ligands
                
            print(f"Prepared {len(custom_ligands)} custom ligand(s) for inference export")
        else:
            print("No custom ligands to write for inference")
            
    def _write_custom_mols_for_record(self, record_id: str):
        """Write custom ligands for a specific record."""
        if not hasattr(self, '_custom_ligands_for_inference'):
            return
            
        extra_mols_dir = self.work_dir / "boltz_out" / "processed" / "mols"
        extra_mols_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            import pickle
            
            # Write each custom ligand as a separate .pkl file (CRITICAL for load_molecules)
            for ligand_name, ligand_mol in self._custom_ligands_for_inference.items():
                ligand_pkl_path = extra_mols_dir / f"{ligand_name}.pkl"
                with open(ligand_pkl_path, 'wb') as f:
                    pickle.dump(ligand_mol, f)
                print(f"✓ Wrote custom ligand '{ligand_name}' to {ligand_pkl_path}")
            
            # Also create the record-specific pickle file
            record_pkl_path = extra_mols_dir / f"{record_id}.pkl"
            with open(record_pkl_path, 'wb') as f:
                pickle.dump(self._custom_ligands_for_inference, f)
            
            print(f"✓ Wrote {len(self._custom_ligands_for_inference)} custom ligand(s) for record '{record_id}'")
            
        except Exception as e:
            print(f"⚠️  Failed to write custom ligands for record '{record_id}': {e}")

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
                            
                            # Add to CCD temporarily
                            self._add_temporary_ligand_to_ccd(ligand_name, mol)
                            print(f"Successfully created custom ligand definition for '{ligand_name}'")
                            
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
            # Always cleanup temporary ligands, even if an error occurred
            self._cleanup_temporary_ligands()
            print("CCD cache cleanup completed.")

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
                
                for line in pdb_content.splitlines():
                    if line.startswith('HETATM') and len(line) > 20:
                        resname = line[17:20].strip()
                        hetatm_resnames.add(resname)
                        if resname == self.ligand_resname:
                            filtered_pdb_lines.append(line)

                if not hetatm_resnames:
                    raise ValueError(f"Error: No HETATM records found in {original_path.name}. "
                                   f"This file appears to contain only protein atoms and no ligands. "
                                   f"Please provide a protein-ligand complex structure file.")
                
                if self.ligand_resname not in hetatm_resnames:
                    available_resnames = ", ".join(sorted(hetatm_resnames))
                    raise ValueError(f"Error: Ligand residue name '{self.ligand_resname}' not found in {original_path.name}. "
                                   f"Available ligand residue names in this file are: {available_resnames}. "
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
    parser.add_argument("input_files", nargs="+", help="One or more PDB or CIF files of protein-ligand complexes.")
    parser.add_argument("--output_dir", default="boltzina_output", help="Directory to save results.")
    parser.add_argument("--ligand_resname", default="LIG", help="Residue name of the ligand in PDB files (e.g., LIG, UNK).")
    args = parser.parse_args()

    boltzina = Boltzina(output_dir=args.output_dir, ligand_resname=args.ligand_resname)
    boltzina.predict(args.input_files)

if __name__ == "__main__":
    main()