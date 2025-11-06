import requests
import zipfile
import io
import json
import os

from .constants import API_URL
API_TOKEN = os.getenv('BOLTZ_API_TOKEN', 'development-api-token')

def submit_job(yaml_content: str, use_msa: bool, model_name: str = None, backend: str = 'boltz'):
    """
    Sends a prediction request to the backend API.
    """
    endpoint = f"{API_URL}/predict"
    headers = {'X-API-Token': API_TOKEN}
    files = {'yaml_file': ('config.yaml', yaml_content, 'application/x-yaml')}
    data = {
        'use_msa_server': str(use_msa).lower(),
        'priority': 'high',
        'backend': backend
    }
    if model_name:
        data['model'] = model_name

    response = requests.post(endpoint, headers=headers, files=files, data=data)
    response.raise_for_status() 
    return response.json().get('task_id')

def predict_affinity(input_file_content: str, input_filename: str, ligand_resname: str):
    """
    Sends an affinity prediction request to the backend API.
    """
    endpoint = f"{API_URL}/api/affinity"
    headers = {'X-API-Token': API_TOKEN}
    files = {'input_file': (input_filename, input_file_content, 'application/octet-stream')}
    data = {
        'ligand_resname': ligand_resname,
        'priority': 'default'
    }

    response = requests.post(endpoint, headers=headers, files=files, data=data)
    response.raise_for_status()
    return response.json().get('task_id')

def predict_affinity_separate(protein_content: str, protein_filename: str, 
                             ligand_content: bytes, ligand_filename: str, 
                             ligand_resname: str = "LIG", output_prefix: str = "complex"):
    """
    Sends separate protein and ligand files for affinity prediction to the backend API.
    
    Note: ligand_resname is kept for backward compatibility but is automatically 
    set to "LIG" by the backend for separate inputs.
    """
    endpoint = f"{API_URL}/api/affinity_separate"
    headers = {'X-API-Token': API_TOKEN}
    
    files = {
        'protein_file': (protein_filename, protein_content, 'application/octet-stream'),
        'ligand_file': (ligand_filename, ligand_content, 'application/octet-stream')
    }
    
    data = {
        'ligand_resname': ligand_resname,
        'output_prefix': output_prefix,
        'priority': 'default'
    }

    response = requests.post(endpoint, headers=headers, files=files, data=data)
    response.raise_for_status()
    return response.json().get('task_id')


def get_status(task_id: str):
    """
    Fetches the status of a specific task from the backend.
    """
    endpoint = f"{API_URL}/status/{task_id}"
    response = requests.get(endpoint)
    response.raise_for_status()
    return response.json()

def download_and_process_results(task_id: str):
    """
    Downloads the result ZIP file for a task and processes its contents.
    """
    endpoint = f"{API_URL}/results/{task_id}"
    response = requests.get(endpoint)
    response.raise_for_status()
    
    raw_zip_bytes = response.content
    
    cif_content = ""
    confidence_data = {}
    affinity_data = {}
    backend = "boltz"
    af3_summary_conf = None
    af3_confidences = None
    af3_primary_cif = None

    with zipfile.ZipFile(io.BytesIO(raw_zip_bytes), 'r') as zip_ref:
        for filename in zip_ref.namelist():
            if filename.endswith('.cif'):
                file_bytes = zip_ref.read(filename).decode('utf-8')
                if "af3/output/" in filename:
                    backend = "alphafold3"
                    # Prefer aggregated model (without seed-specific directories)
                    if af3_primary_cif is None or ("seed-" not in filename and "model.cif" in filename):
                        af3_primary_cif = file_bytes
                else:
                    # Use the first non-AF3 CIF as the default structure
                    if not cif_content:
                        cif_content = file_bytes
            elif 'confidence' in filename and filename.endswith('.json'):
                json_bytes = zip_ref.read(filename)
                data = json.loads(json_bytes)
                if "af3/output/" in filename:
                    backend = "alphafold3"
                    if "summary_confidences" in filename:
                        # Prefer top-level summary (without seed-specific directory)
                        if af3_summary_conf is None or "seed-" not in filename:
                            af3_summary_conf = data
                    elif "confidences.json" in filename:
                        if af3_confidences is None or "seed-" not in filename:
                            af3_confidences = data
                else:
                    confidence_data = data
            elif 'affinity' in filename and filename.endswith('.json'):
                affinity_data = json.loads(zip_ref.read(filename))

    # Finalize CIF content for AF3 runs if present
    if backend == "alphafold3" and af3_primary_cif:
        cif_content = af3_primary_cif

    # Construct AF3 confidence metrics if available
    if backend == "alphafold3":
        af3_metrics = {}

        if af3_confidences:
            atom_plddts = af3_confidences.get("atom_plddts") or []
            if atom_plddts:
                af3_metrics["complex_plddt"] = sum(atom_plddts) / len(atom_plddts)

            pae_matrix = af3_confidences.get("pae") or []
            flattened_pae = [
                value
                for row in pae_matrix if isinstance(row, list)
                for value in row
                if isinstance(value, (int, float))
            ]
            if flattened_pae:
                af3_metrics["complex_pde"] = sum(flattened_pae) / len(flattened_pae)

        if af3_summary_conf:
            ptm = af3_summary_conf.get("ptm")
            if isinstance(ptm, (int, float)):
                af3_metrics["ptm"] = ptm

            iptm = af3_summary_conf.get("iptm")
            if iptm is None:
                chain_pair_iptm = af3_summary_conf.get("chain_pair_iptm")
                if (
                    isinstance(chain_pair_iptm, list)
                    and chain_pair_iptm
                    and isinstance(chain_pair_iptm[0], list)
                    and chain_pair_iptm[0]
                    and isinstance(chain_pair_iptm[0][0], (int, float))
                ):
                    iptm = chain_pair_iptm[0][0]
            if isinstance(iptm, (int, float)):
                af3_metrics["iptm"] = iptm

            ranking_score = af3_summary_conf.get("ranking_score")
            if isinstance(ranking_score, (int, float)):
                af3_metrics["ranking_score"] = ranking_score

            fraction_disordered = af3_summary_conf.get("fraction_disordered")
            if isinstance(fraction_disordered, (int, float)):
                af3_metrics["fraction_disordered"] = fraction_disordered

        # Merge AF3 metrics into confidence data and tag backend
        confidence_data.update(af3_metrics)
        confidence_data["backend"] = "alphafold3"
    elif confidence_data:
        confidence_data["backend"] = "boltz"

    processed_results = {
        "cif": cif_content,
        "confidence": confidence_data,
        "affinity": affinity_data,
        "backend": backend
    }
    
    return processed_results, raw_zip_bytes

def run_affinity_prediction_from_file(protein_structure_content: str, protein_filename: str, ligand_smiles: str):
    """
    Sends a protein structure file and ligand SMILES to the backend for affinity prediction.
    This will skip the structure prediction step.
    """
    # This endpoint will be updated to handle both sequence and file-based predictions
    endpoint = f"{API_URL}/predict" 
    headers = {'X-API-Token': API_TOKEN}
    
    # The server will expect 'protein_structure_file' and 'ligand_smiles'
    files = {'protein_structure_file': (protein_filename, protein_structure_content, 'application/octet-stream')}
    data = {'ligand_smiles': ligand_smiles}
    
    try:
        response = requests.post(endpoint, headers=headers, files=files, data=data)
        
        # This will raise an HTTPError for bad responses (4xx or 5xx)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        # This block will handle connection errors and bad responses
        try:
            # Try to parse the JSON error response from the server
            return e.response.json()
        except (AttributeError, json.JSONDecodeError):
            # Fallback for connection errors or non-JSON responses
            return {"error": "Failed to connect or parse response from server.", "details": str(e)}
