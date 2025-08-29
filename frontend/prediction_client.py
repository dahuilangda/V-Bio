import requests
import zipfile
import io
import json
import os

from .constants import API_URL
API_TOKEN = os.getenv('API_SECRET_TOKEN', 'your_default_api_token')

def submit_job(yaml_content: str, use_msa: bool, model_name: str = None):
    """
    Sends a prediction request to the backend API.
    """
    endpoint = f"{API_URL}/predict"
    headers = {'X-API-Token': API_TOKEN}
    files = {'yaml_file': ('config.yaml', yaml_content, 'application/x-yaml')}
    data = {
        'use_msa_server': str(use_msa).lower(),
        'priority': 'default' # Hardcoding priority for now
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

    with zipfile.ZipFile(io.BytesIO(raw_zip_bytes), 'r') as zip_ref:
        for filename in zip_ref.namelist():
            if filename.endswith('.cif'):
                cif_content = zip_ref.read(filename).decode('utf-8')
            elif 'confidence' in filename and filename.endswith('.json'):
                confidence_data = json.loads(zip_ref.read(filename))
            elif 'affinity' in filename and filename.endswith('.json'):
                affinity_data = json.loads(zip_ref.read(filename))

    processed_results = {
        "cif": cif_content,
        "confidence": confidence_data,
        "affinity": affinity_data
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
