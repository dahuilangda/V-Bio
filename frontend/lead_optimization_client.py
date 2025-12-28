import os
from typing import Optional, Dict

import requests

from .constants import API_URL

API_TOKEN = os.getenv('BOLTZ_API_TOKEN', 'development-api-token')


def submit_lead_optimization(
    target_config_content: str,
    input_compound: str = None,
    input_filename: str = None,
    input_file_content: bytes = None,
    options: Optional[Dict] = None,
):
    endpoint = f"{API_URL}/api/lead_optimization/submit"
    headers = {'X-API-Token': API_TOKEN}

    files = {
        'target_config': ('target_config.yaml', target_config_content, 'application/x-yaml')
    }

    if input_file_content is not None:
        files['input_file'] = (input_filename or 'input.csv', input_file_content, 'application/octet-stream')

    data = {}
    if input_compound:
        data['input_compound'] = input_compound

    if options:
        for key, value in options.items():
            if value is not None:
                data[key] = str(value)

    response = requests.post(endpoint, headers=headers, files=files, data=data)
    response.raise_for_status()
    return response.json().get('task_id')


def get_lead_optimization_status(task_id: str):
    endpoint = f"{API_URL}/api/lead_optimization/status/{task_id}"
    headers = {'X-API-Token': API_TOKEN}
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()


def download_lead_optimization_results(task_id: str):
    endpoint = f"{API_URL}/api/lead_optimization/results/{task_id}"
    headers = {'X-API-Token': API_TOKEN}
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.content


def terminate_task(task_id: str):
    endpoint = f"{API_URL}/tasks/{task_id}"
    headers = {'X-API-Token': API_TOKEN}
    response = requests.delete(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()
