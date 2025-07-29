import os
import traceback
import tempfile
import json
import subprocess
import sys
import shutil
import logging
import signal
import threading
import time
from datetime import datetime

import requests
from celery_app import celery_app
from gpu_manager import acquire_gpu, release_gpu, get_redis_client
import config

try:
    import psutil
except ImportError:
    psutil = None

# Configure standard logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Subprocess timeout in seconds (e.g., 3 hours)
SUBPROCESS_TIMEOUT = 10800  # 增加到3小时
HEARTBEAT_INTERVAL = 60  # 心跳间隔（秒）


class TaskProgressTracker:
    """跟踪任务进度和状态的类"""
    
    def __init__(self, task_id, redis_client):
        self.task_id = task_id
        self.redis_client = redis_client
        self.heartbeat_key = f"task_heartbeat:{task_id}"
        self.status_key = f"task_status:{task_id}"
        self.process_key = f"task_process:{task_id}"
        self._stop_heartbeat = False
        self._heartbeat_thread = None
    
    def start_heartbeat(self):
        """启动心跳线程"""
        self._stop_heartbeat = False
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_worker, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"Task {self.task_id}: Started heartbeat monitoring")
    
    def stop_heartbeat(self):
        """停止心跳线程"""
        self._stop_heartbeat = True
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        # 清理Redis键
        try:
            self.redis_client.delete(self.heartbeat_key)
            self.redis_client.delete(self.status_key)
            self.redis_client.delete(self.process_key)
        except Exception as e:
            logger.warning(f"Failed to cleanup Redis keys for task {self.task_id}: {e}")
    
    def _heartbeat_worker(self):
        """心跳工作线程"""
        while not self._stop_heartbeat:
            try:
                current_time = datetime.now().isoformat()
                self.redis_client.setex(self.heartbeat_key, HEARTBEAT_INTERVAL * 2, current_time)
                time.sleep(HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.error(f"Heartbeat error for task {self.task_id}: {e}")
                break
    
    def update_status(self, status, details=None):
        """更新任务状态"""
        try:
            status_data = {
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "details": details
            }
            self.redis_client.setex(self.status_key, 3600, json.dumps(status_data))
            logger.info(f"Task {self.task_id}: Status updated to {status}")
        except Exception as e:
            logger.error(f"Failed to update status for task {self.task_id}: {e}")
    
    def register_process(self, pid):
        """注册进程ID"""
        try:
            process_data = {
                "pid": pid,
                "start_time": datetime.now().isoformat()
            }
            self.redis_client.setex(self.process_key, 3600, json.dumps(process_data))
            logger.info(f"Task {self.task_id}: Registered process {pid}")
        except Exception as e:
            logger.error(f"Failed to register process for task {self.task_id}: {e}")

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
    This task includes GPU management, subprocess timeout control, progress tracking, and authenticated upload.
    """
    gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None 
    tracker = None

    try:
        # 初始化进度跟踪器
        redis_client = get_redis_client()
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing task")
        
        logger.info(f"Task {task_id}: Attempting to acquire GPU.")
        tracker.update_status("acquiring_gpu", "Waiting for GPU allocation")
        
        gpu_id = acquire_gpu(task_id=task_id, timeout=3600) 
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting computation.'})
        logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
        tracker.update_status("gpu_acquired", f"Using GPU {gpu_id}")
        
        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz_task_{task_id}_")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        predict_args['output_archive_path'] = output_archive_path

        args_file_path = os.path.join(task_temp_dir, 'args.json')
        with open(args_file_path, 'w') as f:
            json.dump(predict_args, f)
        logger.info(f"Task {task_id}: Arguments saved to '{args_file_path}'.")
        tracker.update_status("preparing", "Setting up temporary workspace")

        proc_env = os.environ.copy()
        proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        
        command = [
            sys.executable,
            "run_single_prediction.py",
            args_file_path 
        ]

        logger.info(f"Task {task_id}: Running prediction on GPU {gpu_id}. Subprocess timeout: {SUBPROCESS_TIMEOUT}s. Command: {' '.join(command)}")
        self.update_state(state='PROGRESS', meta={'status': f'Running prediction on GPU {gpu_id}'})
        tracker.update_status("running", f"Executing prediction with GPU {gpu_id}")
        
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env
        )
        
        # 注册进程ID用于监控
        tracker.register_process(process.pid)
        
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
            tracker.update_status("timeout", f"Process timeout after {SUBPROCESS_TIMEOUT}s")
            raise TimeoutError(error_message) from e

        if process.returncode != 0:
            error_message = f"Subprocess for task {task_id} failed with exit code {process.returncode}.\nStderr:\n{stderr}\nStdout:\n{stdout}"
            logger.error(error_message)
            tracker.update_status("failed", f"Process failed with exit code {process.returncode}")
            raise RuntimeError(error_message)
        
        logger.info(f"Task {task_id}: Subprocess completed successfully. Checking for results archive.")
        tracker.update_status("processing_output", "Processing results")

        if not os.path.exists(output_archive_path):
            error_message = f"Subprocess completed, but no results archive found at expected path: {output_archive_path}. Stderr: {stderr}"
            logger.error(error_message)
            tracker.update_status("failed", "No results archive found")
            raise FileNotFoundError(error_message)
        
        self.update_state(state='PROGRESS', meta={'status': f'Uploading results for task {task_id}'})
        logger.info(f"Task {task_id}: Results archive found at '{output_archive_path}'. Initiating upload.")
        tracker.update_status("uploading", "Uploading results to central API")
        
        upload_response = upload_result_to_central_api(task_id, output_archive_path)
        
        final_meta = {
            'status': 'Complete', 
            'gpu_id': gpu_id, 
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path) 
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        logger.info(f"Task {task_id}: Prediction completed and results uploaded successfully. Final status: SUCCESS.")
        tracker.update_status("completed", "Task completed successfully")
        return final_meta

    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        if tracker:
            tracker.update_status("failed", str(e))
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
        
        # 停止心跳监控
        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")


@celery_app.task(bind=True)
def get_task_status_info(self, task_id):
    """获取任务状态信息"""
    try:
        redis_client = get_redis_client()
        
        # 获取心跳信息
        heartbeat = redis_client.get(f"task_heartbeat:{task_id}")
        status = redis_client.get(f"task_status:{task_id}")
        process = redis_client.get(f"task_process:{task_id}")
        
        result = {
            "task_id": task_id,
            "heartbeat": json.loads(heartbeat.decode()) if heartbeat else None,
            "status": json.loads(status.decode()) if status else None,
            "process": json.loads(process.decode()) if process else None
        }
        
        return result
        
    except Exception as e:
        return {
            "task_id": task_id,
            "error": str(e)
        }


@celery_app.task(bind=True)
def cleanup_stuck_task(self, task_id):
    """清理卡住的任务"""
    try:
        redis_client = get_redis_client()
        
        # 获取进程信息
        process_key = f"task_process:{task_id}"
        process_data = redis_client.get(process_key)
        
        if process_data:
            process_info = json.loads(process_data.decode())
            pid = process_info.get("pid")
            
            if pid:
                try:
                    # 尝试终止进程
                    if psutil:
                        if psutil.pid_exists(pid):
                            p = psutil.Process(pid)
                            p.terminate()
                            logger.info(f"Terminated process {pid} for task {task_id}")
                            
                            # 等待进程结束
                            try:
                                p.wait(timeout=10)
                            except psutil.TimeoutExpired:
                                # 强制杀死
                                p.kill()
                                logger.info(f"Killed process {pid} for task {task_id}")
                    else:
                        # 使用系统调用
                        try:
                            os.kill(pid, signal.SIGTERM)
                            logger.info(f"Sent SIGTERM to process {pid} for task {task_id}")
                            time.sleep(5)
                            # 检查进程是否还存在，如果存在则强制杀死
                            try:
                                os.kill(pid, 0)  # 检查进程是否存在
                                os.kill(pid, signal.SIGKILL)
                                logger.info(f"Killed process {pid} for task {task_id}")
                            except ProcessLookupError:
                                # 进程已经结束
                                pass
                        except ProcessLookupError:
                            logger.info(f"Process {pid} not found for task {task_id}")
                except Exception as e:
                    logger.error(f"Failed to terminate process {pid}: {e}")
        
        # 清理Redis键
        keys_to_delete = [
            f"task_heartbeat:{task_id}",
            f"task_status:{task_id}", 
            f"task_process:{task_id}"
        ]
        
        for key in keys_to_delete:
            redis_client.delete(key)
        
        # 撤销Celery任务
        from celery_app import celery_app
        celery_app.control.revoke(task_id, terminate=True)
        
        logger.info(f"Cleaned up stuck task {task_id}")
        return {"status": "success", "message": f"Task {task_id} cleaned up successfully"}
        
    except Exception as e:
        logger.error(f"Failed to cleanup task {task_id}: {e}")
        return {"status": "error", "message": str(e)}