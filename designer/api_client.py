# /Boltz-WebUI/designer/api_client.py

import requests
import time
import os
import zipfile
from typing import Optional, Dict

class BoltzApiClient:
    """
    A client to interact with the Boltz-WebUI prediction API.
    """
    def __init__(self, server_url: str, api_token: str):
        if server_url.endswith('/'):
            server_url = server_url[:-1]
        self.server_url = server_url
        self.headers = {"X-API-Token": api_token}
        print(f"API Client initialized for server: {self.server_url}")

    def submit_job(self, yaml_path: str, use_msa_server: bool = False, model_name: str = None, backend: str = 'boltz') -> Optional[str]:
        """Submits a prediction job using a YAML file.
        
        Args:
            yaml_path: Path to the YAML configuration file
            use_msa_server: Whether to use MSA server when sequences don't have cached MSAs
            model_name: Specific model to use (e.g., 'boltz1' for glycopeptide design)
            backend: Prediction backend to use ('boltz' or 'alphafold3')
        """
        predict_url = f"{self.server_url}/predict"
        try:
            with open(yaml_path, 'rb') as f:
                files = {'yaml_file': (os.path.basename(yaml_path), f)}
                # 添加参数
                data = {
                    'use_msa_server': str(use_msa_server).lower(),
                    'priority': 'default',
                    'backend': backend
                }
                # 如果指定了模型，添加模型参数
                if model_name:
                    data['model'] = model_name
                response = requests.post(predict_url, headers=self.headers, files=files, data=data, timeout=30)

            if response.status_code == 202:
                task_id = response.json().get('task_id')
                print(f"Successfully submitted job. Task ID: {task_id}")
                if use_msa_server:
                    print(f"MSA server enabled: will generate MSAs for sequences without cache")
                if model_name:
                    print(f"Using model: {model_name}")
                return task_id
            else:
                print(f"Error submitting job: {response.status_code} - {response.text}")
                return None
        except requests.exceptions.RequestException as e:
            print(f"An error occurred during job submission: {e}")
            return None

    def poll_status(self, task_id: str, poll_interval: int = 15) -> Optional[Dict]:
        """Polls the status of a task until it's completed or fails."""
        status_url = f"{self.server_url}/status/{task_id}"
        while True:
            try:
                response = requests.get(status_url, timeout=30)
                if response.status_code == 200:
                    status_data = response.json()
                    state = status_data.get('state')
                    print(f"Polling task {task_id}... Current state: {state}")
                    if state in ["SUCCESS", "FAILURE"]:
                        return status_data
                else:
                    print(f"Warning: Could not get status for task {task_id} (HTTP {response.status_code})")
                
                time.sleep(poll_interval)

            except requests.exceptions.RequestException as e:
                print(f"An error occurred while polling status: {e}")
                time.sleep(poll_interval)


    def download_and_unzip_results(self, task_id: str, output_dir: str) -> Optional[str]:
        """Downloads and unzips the results for a successful task."""
        results_url = f"{self.server_url}/results/{task_id}"
        zip_path = os.path.join(output_dir, f"{task_id}_results.zip")
        extract_path = os.path.join(output_dir, task_id)

        try:
            print(f"Downloading results for task {task_id}...")
            response = requests.get(results_url, timeout=300)
            if response.status_code == 200:
                with open(zip_path, 'wb') as f:
                    f.write(response.content)

                print(f"Unzipping results to {extract_path}...")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_path)
                
                os.remove(zip_path) # Clean up the zip file
                return extract_path
            else:
                print(f"Error downloading results: {response.status_code} - {response.text}")
                return None
        except (requests.exceptions.RequestException, zipfile.BadZipFile) as e:
            print(f"An error occurred during result download/extraction: {e}")
            return None
