# 来自 https://github.com/ohuelab/boltzina 的修改版本

import numpy as np
import subprocess
import argparse
import pickle
import json
import csv
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
        if self.ccd_path.exists():
            with self.ccd_path.open('rb') as file:
                ccd = pickle.load(file)
                if self.base_ligand_name in ccd:
                    ccd.pop(self.base_ligand_name)
                return ccd
        else:
            return {}

    def predict(self, file_paths: List[str]):
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
        for i, (original_path, cif_file) in enumerate(files_to_process):
            record_id = f"{self.fname}_{i}"
            record_ids.append(record_id)

            mol = None
            if original_path.suffix.lower() == '.pdb':
                pdb_content = original_path.read_text()
                filtered_pdb_lines = []
                for line in pdb_content.splitlines():
                    if line[17:20].strip() == self.ligand_resname:
                        filtered_pdb_lines.append(line)

                if filtered_pdb_lines:
                    temp_pdb_path = self.work_dir / f"{record_id}_{self.ligand_resname}.pdb"
                    temp_pdb_path.write_text("\n".join(filtered_pdb_lines))
                    mol = Chem.MolFromPDBFile(str(temp_pdb_path))
                    temp_pdb_path.unlink()
                else:
                    print(f"Warning: No HETATM records found for residue name '{self.ligand_resname}' in {original_path.name}. Skipping molecule processing for this file.")
            elif original_path.suffix.lower() == '.cif':
                pass # CIF files are handled by parse_mmcif later, no RDKit mol extraction here.

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

        self._update_manifest(record_ids)
        self._score_poses()

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
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error preparing structure for {complex_file.name}: {e}")

    def _update_manifest(self, record_ids: List[str]):
        records = []
        for record_id in record_ids:
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
                "affinity": { "chain_id": 1, "mw": 400.0 } # Using a placeholder MW
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