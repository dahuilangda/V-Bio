# tasks.py
import os
import traceback
import tempfile
import json
import subprocess
import sys
import shutil

import requests
from celery_app import celery_app
from gpu_manager import acquire_gpu, release_gpu
import config

def upload_result_to_central_api(task_id: str, local_file_path: str) -> dict:
    """
    将本地文件上传到中心化的 API 服务器。
    """
    upload_url = f"{config.CENTRAL_API_URL}/upload_result/{task_id}"
    print(f"Task {task_id}: Uploading result from '{local_file_path}' to {upload_url}")

    with open(local_file_path, 'rb') as f:
        files = {'file': (f"{task_id}_results.zip", f)}
        response = requests.post(upload_url, files=files, timeout=300)
        response.raise_for_status()
        return response.json()

@celery_app.task(bind=True)
def predict_task(self, predict_args: dict):
    """
    Celery 任务，职责是启动一个完全隔离的子进程来执行计算。
    """
    gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None 

    try:
        gpu_id = acquire_gpu(timeout=3600)
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Launching computation.'})
        task_temp_dir = tempfile.mkdtemp()
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        predict_args['output_archive_path'] = output_archive_path

        args_file_path = os.path.join(task_temp_dir, 'args.json')
        with open(args_file_path, 'w') as f:
            json.dump(predict_args, f)

        proc_env = os.environ.copy()
        proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        
        command = [
            sys.executable,
            "run_single_prediction.py",
            args_file_path 
        ]

        self.update_state(state='PROGRESS', meta={'status': f'Running prediction on GPU {gpu_id}'})
        
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env
        )
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            error_message = f"Subprocess failed with exit code {process.returncode}.\nStderr:\n{stderr}\nStdout:\n{stdout}"
            raise RuntimeError(error_message)

        if not os.path.exists(output_archive_path):
            raise FileNotFoundError(f"Subprocess finished but the result archive was not found at the expected path: {output_archive_path}. Stderr: {stderr}")

        upload_response = upload_result_to_central_api(task_id, output_archive_path)
        
        return {'status': 'Complete', 'gpu_id': gpu_id, 'result': upload_response}

    except Exception as e:
        self.update_state(state='FAILURE', meta={
            'exc_type': type(e).__name__,
            'exc_message': str(e),
            'traceback': traceback.format_exc(),
        })
        raise e

    finally:
        if gpu_id != -1:
            release_gpu(gpu_id)
            print(f"Task {task_id}: Released GPU {gpu_id}.")
        
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            print(f"Task {task_id}: Cleaned up temporary directory {task_temp_dir}.")