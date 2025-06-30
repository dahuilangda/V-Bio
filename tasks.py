import os
import traceback
import tempfile
import json
import subprocess
import sys
import shutil
import logging

import requests
from celery_app import celery_app
from gpu_manager import acquire_gpu, release_gpu
import config

# Configure standard logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Subprocess timeout in seconds (e.g., 2 hours)
SUBPROCESS_TIMEOUT = 7200

def upload_result_to_central_api(task_id: str, local_file_path: str) -> dict:
    """
    Uploads a local file to the centralized API server.
    """
    upload_url = f"{config.CENTRAL_API_URL}/upload_result/{task_id}"
    logger.info(f"Task {task_id}: Starting upload from '{local_file_path}' to '{upload_url}'.")

    with open(local_file_path, 'rb') as f:
        files = {'file': (f"{task_id}_results.zip", f)}
        
        response = requests.post(
            upload_url,
            files=files,
            timeout=(10, 300)  # (connection timeout, read timeout)
        )
        
        response.raise_for_status()
        logger.info(f"Task {task_id}: Results uploaded successfully. Server response: {response.json()}")
        return response.json()

@celery_app.task(bind=True)
def predict_task(self, predict_args: dict):
    """
    Celery task responsible for launching an isolated subprocess to perform computation.
    This task includes GPU management, subprocess timeout control, and authenticated upload.
    """
    gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None 

    try:
        logger.info(f"Task {task_id}: Attempting to acquire GPU.")
        gpu_id = acquire_gpu(task_id=task_id, timeout=3600) 
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting computation.'})
        logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
        
        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz_task_{task_id}_")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        predict_args['output_archive_path'] = output_archive_path

        args_file_path = os.path.join(task_temp_dir, 'args.json')
        with open(args_file_path, 'w') as f:
            json.dump(predict_args, f)
        logger.info(f"Task {task_id}: Arguments saved to '{args_file_path}'.")

        proc_env = os.environ.copy()
        proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        
        command = [
            sys.executable,
            "run_single_prediction.py",
            args_file_path 
        ]

        logger.info(f"Task {task_id}: Running prediction on GPU {gpu_id}. Subprocess timeout: {SUBPROCESS_TIMEOUT}s. Command: {' '.join(command)}")
        self.update_state(state='PROGRESS', meta={'status': f'Running prediction on GPU {gpu_id}'})
        
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env
        )
        
        try:
            stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            process.kill()
            stdout, stderr = process.communicate()
            error_message = (
                f"Subprocess for task {task_id} timed out after {SUBPROCESS_TIMEOUT} seconds.\n"
                f"Stderr:\n{stderr}\nStdout:\n{stdout}"
            )
            logger.error(error_message)
            raise TimeoutError(error_message) from e

        if process.returncode != 0:
            error_message = f"Subprocess for task {task_id} failed with exit code {process.returncode}.\nStderr:\n{stderr}\nStdout:\n{stdout}"
            logger.error(error_message)
            raise RuntimeError(error_message)
        
        logger.info(f"Task {task_id}: Subprocess completed successfully. Checking for results archive.")

        if not os.path.exists(output_archive_path):
            error_message = f"Subprocess completed, but no results archive found at expected path: {output_archive_path}. Stderr: {stderr}"
            logger.error(error_message)
            raise FileNotFoundError(error_message)
        
        self.update_state(state='PROGRESS', meta={'status': f'Uploading results for task {task_id}'})
        logger.info(f"Task {task_id}: Results archive found at '{output_archive_path}'. Initiating upload.")
        
        upload_response = upload_result_to_central_api(task_id, output_archive_path)
        
        final_meta = {
            'status': 'Complete', 
            'gpu_id': gpu_id, 
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path) 
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        logger.info(f"Task {task_id}: Prediction completed and results uploaded successfully. Final status: SUCCESS.")
        return final_meta

    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        self.update_state(state='FAILURE', meta={
            'exc_type': type(e).__name__,
            'exc_message': str(e),
            'traceback': traceback.format_exc(),
        })
        raise e

    finally:
        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id) 
            logger.info(f"Task {task_id}: Released GPU {gpu_id}.")
        
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            logger.info(f"Task {task_id}: Cleaned up temporary directory '{task_temp_dir}'.")