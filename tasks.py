import os
import sys
import glob
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
import base64
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import importlib.util

from werkzeug.utils import secure_filename

import requests
from celery_app import celery_app
from gpu_manager import acquire_gpu, release_gpu, get_redis_client
import config

BASE_DIR = Path(__file__).resolve().parent

def _ensure_repo_root_on_path() -> Path | None:
    """Ensure the repo root (containing affinity/) is on sys.path."""
    candidates = [BASE_DIR, BASE_DIR.parent]
    for candidate in candidates:
        if (candidate / "affinity").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    return None

_ensure_repo_root_on_path()

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
PROGRESS_TTL_SECONDS = 3600
PROGRESS_UPDATE_INTERVAL = 20
SCREENING_TASK_TIMEOUT = 12 * 3600
OPTIMIZATION_TASK_TIMEOUT = 12 * 3600


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

def _store_progress(redis_client, key: str, payload: dict, ttl: int = PROGRESS_TTL_SECONDS) -> None:
    """Persist task progress payload to Redis."""
    try:
        redis_client.setex(key, ttl, json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to store progress for {key}: {e}")

def _write_base64_file(encoded_content: str, path: str, text_mode: bool = False) -> None:
    """Write base64 encoded content to disk."""
    raw = base64.b64decode(encoded_content)
    if text_mode:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(raw.decode('utf-8'))
    else:
        with open(path, 'wb') as f:
            f.write(raw)

def _read_virtual_screening_progress(output_dir: str) -> dict:
    """Read virtual screening checkpoint progress from output directory."""
    checkpoint_dir = os.path.join(output_dir, "checkpoints")
    if not os.path.isdir(checkpoint_dir):
        return {}

    checkpoint_files = sorted(
        glob.glob(os.path.join(checkpoint_dir, "*.json")),
        key=os.path.getmtime,
        reverse=True
    )
    if not checkpoint_files:
        return {}

    try:
        with open(checkpoint_files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read virtual screening checkpoint: {e}")
        return {}

    start_time = data.get('start_time') or time.time()
    elapsed = max(0.0, time.time() - float(start_time))
    completed = len(data.get('completed_tasks', []))
    total = int(data.get('total_molecules') or 0)
    failed = len(data.get('failed_tasks', []))
    progress_percent = (completed / total * 100) if total > 0 else 0.0
    estimated_remaining = 0.0

    if completed > 0 and total > completed:
        avg_time = elapsed / completed
        estimated_remaining = avg_time * (total - completed)

    eta_time = None
    if estimated_remaining:
        eta_time = (datetime.now() + timedelta(seconds=estimated_remaining)).isoformat()

    return {
        "session_id": data.get('session_id'),
        "completed_molecules": completed,
        "total_molecules": total,
        "failed_molecules": failed,
        "results_count": data.get('results_count', 0),
        "best_score": data.get('best_score', 0.0),
        "elapsed_seconds": elapsed,
        "progress_percent": progress_percent,
        "estimated_remaining_seconds": estimated_remaining,
        "estimated_completion_time": eta_time,
        "checkpoint_updated": data.get('last_update')
    }

def _read_lead_optimization_progress(output_dir: str,
                                     elapsed: float,
                                     expected_candidates: Optional[int] = None,
                                     expected_compounds: Optional[int] = None) -> dict:
    """Read lead optimization progress based on output files."""
    progress = {}

    if expected_compounds:
        summary_paths = glob.glob(os.path.join(output_dir, "compound_*", "optimization_summary.json"))
        completed = len(summary_paths)
        progress_percent = (completed / expected_compounds * 100) if expected_compounds > 0 else 0.0
        estimated_remaining = 0.0
        if completed > 0 and expected_compounds > completed:
            avg_time = elapsed / completed
            estimated_remaining = avg_time * (expected_compounds - completed)
        eta_time = None
        if estimated_remaining:
            eta_time = (datetime.now() + timedelta(seconds=estimated_remaining)).isoformat()

        progress.update({
            "completed_compounds": completed,
            "total_compounds": expected_compounds,
            "progress_percent": progress_percent,
            "estimated_remaining_seconds": estimated_remaining,
            "estimated_completion_time": eta_time
        })
        return progress

    hint_path = os.path.join(output_dir, "optimization_progress.json")
    if os.path.exists(hint_path):
        try:
            with open(hint_path, 'r', encoding='utf-8') as f:
                hint = json.load(f)
            hint_expected = hint.get("expected_candidates")
            if isinstance(hint_expected, int) and hint_expected > 0:
                expected_candidates = hint_expected
        except Exception:
            pass

    csv_path = os.path.join(output_dir, "optimization_results.csv")
    if not os.path.exists(csv_path):
        return progress

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = f.readlines()
    except Exception as e:
        logger.warning(f"Failed to read optimization progress CSV: {e}")
        return progress

    processed = max(0, len(rows) - 1)
    progress_percent = 0.0
    estimated_remaining = 0.0

    if expected_candidates:
        progress_percent = (processed / expected_candidates * 100) if expected_candidates > 0 else 0.0
        if processed > 0 and expected_candidates > processed:
            avg_time = elapsed / processed
            estimated_remaining = avg_time * (expected_candidates - processed)

    eta_time = None
    if estimated_remaining:
        eta_time = (datetime.now() + timedelta(seconds=estimated_remaining)).isoformat()

    progress.update({
        "processed_candidates": processed,
        "expected_candidates": expected_candidates,
        "progress_percent": progress_percent,
        "estimated_remaining_seconds": estimated_remaining,
        "estimated_completion_time": eta_time
    })
    return progress

def _mmpdb_available() -> bool:
    """Check if mmpdb CLI is available in current environment."""
    if shutil.which('mmpdb'):
        return True
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'mmpdb', '--help'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def _extract_protein_chain_ids_from_pdb(pdb_path: str) -> list[str]:
    """Extract unique protein chain IDs from ATOM records in a PDB file."""
    chain_ids: set[str] = set()
    try:
        with open(pdb_path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.startswith("ATOM"):
                    continue
                if len(line) <= 21:
                    continue
                chain_id = line[21].strip()
                if chain_id:
                    chain_ids.add(chain_id)
    except Exception:
        return []
    return sorted(chain_ids)

def _load_lead_optimization_config():
    """Load lead_optimization config without relying on package import."""
    config_path = BASE_DIR / "lead_optimization" / "config.py"
    spec = importlib.util.spec_from_file_location("lead_optimization.config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load lead_optimization config from {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_config()

def upload_result_to_central_api(task_id: str, local_file_path: str, filename: str) -> dict:
    """
    Uploads a local file to the centralized API server.
    """
    upload_url = f"{config.CENTRAL_API_URL}/upload_result/{task_id}"
    logger.info(f"Task {task_id}: Starting upload from '{local_file_path}' to '{upload_url}'.")

    with open(local_file_path, 'rb') as f:
        files = {'file': (filename, f)}
        
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
        
        upload_response = upload_result_to_central_api(task_id, output_archive_path, os.path.basename(output_archive_path))
        
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
    

@celery_app.task(bind=True)
def affinity_task(self, affinity_args: dict):
    """
    Celery task for running affinity prediction.
    """
    gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None
    tracker = None

    try:
        redis_client = get_redis_client()
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing affinity task")

        logger.info(f"Task {task_id}: Attempting to acquire GPU for affinity prediction.")
        tracker.update_status("acquiring_gpu", "Waiting for GPU allocation")

        gpu_id = acquire_gpu(task_id=task_id, timeout=3600)
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting affinity prediction.'})
        logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
        tracker.update_status("gpu_acquired", f"Using GPU {gpu_id}")

        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz_affinity_task_{task_id}_")
        
        # Handle different input modes: complex file vs separate files
        input_file_path = None  # Initialize to avoid UnboundLocalError
        
        if 'protein_file_content' in affinity_args and 'ligand_file_content' in affinity_args:
            # Separate protein and ligand files mode
            protein_content = affinity_args['protein_file_content']
            ligand_content = affinity_args['ligand_file_content']
            protein_filename = affinity_args['protein_filename']
            ligand_filename = affinity_args['ligand_filename']
            
            # Write protein file
            protein_file_path = os.path.join(task_temp_dir, protein_filename)
            with open(protein_file_path, 'w') as f:
                f.write(protein_content)
            
            # Write ligand file
            ligand_file_path = os.path.join(task_temp_dir, ligand_filename)
            with open(ligand_file_path, 'wb' if ligand_filename.lower().endswith('.sdf') else 'w') as f:
                if ligand_filename.lower().endswith('.sdf'):
                    f.write(ligand_content.encode('utf-8'))
                else:
                    f.write(ligand_content)
            
            output_csv_path = os.path.join(task_temp_dir, f"{task_id}_affinity_results.csv")
            
            # For separate inputs, we'll update input_file_path after prediction to use generated complex
            input_file_path = protein_file_path  # Temporary, will be updated after prediction
            input_filename = protein_filename    # Temporary, will be updated after prediction
            
            args_for_script = {
                'task_temp_dir': task_temp_dir,
                'protein_file_path': protein_file_path,
                'ligand_file_path': ligand_file_path,
                'ligand_resname': 'LIG',  # Fixed ligand name for separate inputs
                'output_prefix': affinity_args.get('output_prefix', 'complex'),
                'output_csv_path': output_csv_path
            }
            
        else:
            # Original complex file mode
            input_file_content = affinity_args['input_file_content']
            input_filename = affinity_args['input_filename']
            input_file_path = os.path.join(task_temp_dir, input_filename)
            with open(input_file_path, 'w') as f:
                f.write(input_file_content)

            if input_filename.lower().endswith('.pdb'):
                cif_filename = f"{os.path.splitext(input_filename)[0]}.cif"
                cif_path = os.path.join(task_temp_dir, cif_filename)
                
                # Check if maxit is available
                try:
                    # subprocess.run(["maxit", "--help"], capture_output=True, check=True)
                    #上面的不行, 换一种
                    subprocess.run(["which", "maxit"], capture_output=True, check=True)
                except (subprocess.CalledProcessError, FileNotFoundError):
                    error_message = (
                        "maxit tool is not installed or not accessible. "
                        "Please install maxit to convert PDB files to mmCIF format. "
                        "You can install it from: https://sw-tools.rcsb.org/apps/MAXIT/index.html"
                    )
                    logger.error(error_message)
                    tracker.update_status("failed", "maxit tool not available")
                    raise RuntimeError(error_message)
                
                try:
                    result = subprocess.run(["maxit", "-input", input_file_path, "-output", cif_path, "-o", "1"], 
                                          check=True, capture_output=True, text=True)
                    
                    # Verify the generated CIF file has proper format and content
                    if os.path.exists(cif_path):
                        with open(cif_path, 'r') as f:
                            cif_content = f.read().strip()
                        
                        # Check if the CIF file is empty or has content
                        if not cif_content:
                            error_message = f"maxit generated empty CIF file for {input_filename}"
                            logger.error(error_message)
                            tracker.update_status("failed", "Empty CIF file generated")
                            raise RuntimeError(error_message)
                        
                        # Check if the CIF file starts with data_ directive
                        if not cif_content.startswith('data_'):
                            # Fix the CIF file by adding proper header
                            fixed_content = f"data_protein\n#\n{cif_content}"
                            with open(cif_path, 'w') as f:
                                f.write(fixed_content)
                            logger.info(f"Fixed CIF format for {cif_filename} - added data_ header")
                        
                        # Verify the fixed file can be read
                        with open(cif_path, 'r') as f:
                            final_content = f.read()
                        
                        if len(final_content) < 50:  # Suspiciously short file
                            logger.warning(f"Generated CIF file seems unusually short: {len(final_content)} characters")
                    
                    input_file_path = cif_path
                    input_filename = cif_filename
                    logger.info(f"Successfully converted {input_filename} to CIF format using maxit")
                except subprocess.CalledProcessError as e:
                    error_message = f"maxit failed for {input_filename}: {e.stderr}"
                    logger.error(error_message)
                    tracker.update_status("failed", "PDB to CIF conversion failed")
                    raise RuntimeError(error_message) from e

            output_csv_path = os.path.join(task_temp_dir, f"{task_id}_affinity_results.csv")

            args_for_script = {
                'task_temp_dir': task_temp_dir,
                'input_file_path': input_file_path,
                'ligand_resname': affinity_args['ligand_resname'],
                'output_csv_path': output_csv_path
            }

        args_file_path = os.path.join(task_temp_dir, 'args.json')
        with open(args_file_path, 'w') as f:
            json.dump(args_for_script, f)
        
        logger.info(f"Task {task_id}: Arguments saved to '{args_file_path}'.")
        tracker.update_status("preparing", "Setting up temporary workspace for affinity prediction")

        proc_env = os.environ.copy()
        proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)

        command = [
            sys.executable,
            "run_affinity_prediction.py",
            args_file_path
        ]

        logger.info(f"Task {task_id}: Running affinity prediction on GPU {gpu_id}. Command: {' '.join(command)}")
        self.update_state(state='PROGRESS', meta={'status': f'Running affinity prediction on GPU {gpu_id}'})
        tracker.update_status("running", f"Executing affinity prediction with GPU {gpu_id}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env
        )

        tracker.register_process(process.pid)

        try:
            stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            process.kill()
            stdout, stderr = process.communicate()
            error_message = (
                f"Subprocess for affinity task {task_id} timed out after {SUBPROCESS_TIMEOUT} seconds.\n"
                f"Stderr:\n{stderr}\nStdout:\n{stdout}"
            )
            logger.error(error_message)
            tracker.update_status("timeout", f"Process timeout after {SUBPROCESS_TIMEOUT}s")
            raise TimeoutError(error_message) from e

        if process.returncode != 0:
            error_message = f"Subprocess for affinity task {task_id} failed with exit code {process.returncode}.\nStderr:\n{stderr}\nStdout:\n{stdout}"
            logger.error(error_message)
            tracker.update_status("failed", f"Process failed with exit code {process.returncode}")
            raise RuntimeError(error_message)

        logger.info(f"Task {task_id}: Affinity subprocess completed successfully. Checking for results CSV.")
        tracker.update_status("processing_output", "Processing affinity results")

        if not os.path.exists(output_csv_path):
            error_message = f"Subprocess completed, but no results CSV found at expected path: {output_csv_path}. Stderr: {stderr}"
            logger.error(error_message)
            tracker.update_status("failed", "No results CSV found")
            raise FileNotFoundError(error_message)
        
        import zipfile
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_affinity_results.zip")
        with zipfile.ZipFile(output_archive_path, 'w') as zipf:
            zipf.write(output_csv_path, os.path.basename(output_csv_path))
            
            # For separate inputs, include the generated complex file if it exists
            if 'protein_file_path' in args_for_script and 'ligand_file_path' in args_for_script:
                # Look for generated complex files
                combined_dir = os.path.join(task_temp_dir, 'boltzina_output', 'combined_complexes')
                if os.path.exists(combined_dir):
                    for file in os.listdir(combined_dir):
                        if file.endswith('.pdb'):
                            complex_file_path = os.path.join(combined_dir, file)
                            zipf.write(complex_file_path, f"generated_complex_{file}")
                            
                            # Convert the generated complex to CIF for 3D visualization
                            cif_filename = f"{os.path.splitext(file)[0]}.cif"
                            cif_path = os.path.join(task_temp_dir, cif_filename)
                            
                            try:
                                # Check if maxit is available
                                subprocess.run(["which", "maxit"], capture_output=True, check=True)
                                
                                # Convert to CIF
                                result = subprocess.run(["maxit", "-input", complex_file_path, "-output", cif_path, "-o", "1"], 
                                                      check=True, capture_output=True, text=True)
                                
                                # Verify and fix CIF format
                                if os.path.exists(cif_path):
                                    with open(cif_path, 'r') as f:
                                        cif_content = f.read().strip()
                                    
                                    if cif_content and '_atom_site' in cif_content:
                                        if not cif_content.startswith('data_'):
                                            fixed_content = f"data_complex\n#\n{cif_content}"
                                            with open(cif_path, 'w') as f:
                                                f.write(fixed_content)
                                            logger.info(f"Fixed CIF format for {cif_filename}")
                                        
                                        # Add CIF file to archive
                                        zipf.write(cif_path, f"complex_{cif_filename}")
                                        logger.info(f"Generated and included CIF file: {cif_filename}")
                                    else:
                                        logger.warning(f"Generated CIF file is incomplete or missing _atom_site section")
                                        
                            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                                logger.warning(f"Could not convert complex to CIF format: {e}")
                                # CIF conversion failure is not critical, continue without it
                            
                            input_file_path = complex_file_path  # Update for logging
                            break
            
            # Include original input file if it exists
            if input_file_path and os.path.exists(input_file_path):
                zipf.write(input_file_path, os.path.basename(input_file_path))

        logger.info(f"Task {task_id}: Results archived to '{output_archive_path}'.")

        upload_response = upload_result_to_central_api(task_id, output_archive_path, os.path.basename(output_archive_path))

        final_meta = {
            'status': 'Complete',
            'gpu_id': gpu_id,
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path)
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        logger.info(f"Task {task_id}: Affinity prediction completed and results uploaded successfully. Final status: SUCCESS.")
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

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")


@celery_app.task(bind=True)
def boltz2score_task(self, score_args: dict):
    """
    Celery task for running Boltz2Score (confidence; optional affinity).
    """
    gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None
    tracker = None

    try:
        redis_client = get_redis_client()
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing Boltz2Score task")

        logger.info(f"Task {task_id}: Attempting to acquire GPU for Boltz2Score.")
        tracker.update_status("acquiring_gpu", "Waiting for GPU allocation")

        gpu_id = acquire_gpu(task_id=task_id, timeout=3600)
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting Boltz2Score.'})
        logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
        tracker.update_status("gpu_acquired", f"Using GPU {gpu_id}")

        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz2score_task_{task_id}_")
        input_filename = None
        input_file_path = None
        extra_archive_files = []
        inputs_dir = None
        using_separate_inputs = False

        if 'protein_file_content' in score_args and 'ligand_file_content' in score_args:
            using_separate_inputs = True
            protein_filename = secure_filename(score_args['protein_filename'])
            ligand_filename = secure_filename(score_args['ligand_filename'])

            protein_file_path = os.path.join(task_temp_dir, protein_filename)
            with open(protein_file_path, 'w', encoding='utf-8') as f:
                f.write(score_args['protein_file_content'])

            ligand_file_path = os.path.join(task_temp_dir, ligand_filename)
            with open(ligand_file_path, 'w', encoding='utf-8') as f:
                f.write(score_args['ligand_file_content'])

            raw_prefix = score_args.get('output_prefix', 'complex')
            output_prefix = secure_filename(raw_prefix) or "complex"
            combined_pdb_path = None
            combined_cif_path = None

            try:
                _ensure_repo_root_on_path()
                from affinity.main import Boltzina
            except Exception as exc:
                logger.exception(
                    "Task %s: Failed to import affinity module for separate-input Boltz2Score.",
                    task_id,
                )
                raise RuntimeError(
                    "Failed to import affinity module for separate-input Boltz2Score. "
                    f"Details: {type(exc).__name__}: {exc}"
                ) from exc

            boltz_output_dir = os.path.join(task_temp_dir, "boltz2score_pre")
            boltz_work_dir = os.path.join(task_temp_dir, "boltz2score_pre_work")
            boltzina = Boltzina(
                output_dir=boltz_output_dir,
                work_dir=boltz_work_dir,
                ligand_resname="LIG",
            )
            try:
                combined_pdb = boltzina._create_standard_complex_pdb(
                    Path(protein_file_path),
                    Path(ligand_file_path),
                    output_prefix
                )
                combined_cif = boltzina._convert_complex_pdb_to_cif(combined_pdb)
                combined_pdb_path = combined_pdb
                combined_cif_path = combined_cif
            finally:
                try:
                    boltzina._cleanup_temporary_ligands()
                except Exception as cleanup_error:
                    logger.warning(f"Task {task_id}: Failed to cleanup temporary ligands: {cleanup_error}")

            combined_pdb_copy = os.path.join(task_temp_dir, "combined_complex.pdb")
            shutil.copyfile(str(combined_pdb_path), combined_pdb_copy)
            extra_archive_files.append(combined_pdb_copy)

            detected_target_chain = None
            if not score_args.get('target_chain'):
                detected_chain_ids = _extract_protein_chain_ids_from_pdb(combined_pdb_copy)
                if detected_chain_ids:
                    detected_target_chain = ",".join(detected_chain_ids)

            combined_cif_copy = None
            if combined_cif_path and Path(combined_cif_path).suffix.lower() == ".cif" and os.path.exists(combined_cif_path):
                combined_cif_copy = os.path.join(task_temp_dir, "combined_complex.cif")
                shutil.copyfile(str(combined_cif_path), combined_cif_copy)
                extra_archive_files.append(combined_cif_copy)

            # Prefer PDB for scoring to avoid mmCIF conversion edge cases with external tools
            input_file_path = combined_pdb_copy
            input_filename = os.path.basename(input_file_path)

            inputs_dir = os.path.join(task_temp_dir, "inputs")
            os.makedirs(inputs_dir, exist_ok=True)
            shutil.copyfile(protein_file_path, os.path.join(inputs_dir, protein_filename))
            shutil.copyfile(ligand_file_path, os.path.join(inputs_dir, ligand_filename))
            extra_archive_files.extend([
                os.path.join(inputs_dir, protein_filename),
                os.path.join(inputs_dir, ligand_filename),
            ])

        else:
            input_filename = secure_filename(score_args['input_filename'])
            input_file_path = os.path.join(task_temp_dir, input_filename)
            with open(input_file_path, 'w', encoding='utf-8') as f:
                f.write(score_args['input_file_content'])

        output_dir = os.path.join(task_temp_dir, "output")
        work_dir = os.path.join(task_temp_dir, "work")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        command = [
            sys.executable,
            str(BASE_DIR / "Boltz2Score" / "boltz2score.py"),
            "--input", input_file_path,
            "--output_dir", output_dir,
            "--work_dir", work_dir,
            "--accelerator", "gpu",
            "--devices", "1",
            "--num_workers", "0",
        ]

        target_chain = score_args.get('target_chain') or (detected_target_chain if using_separate_inputs else None)
        ligand_chain = score_args.get('ligand_chain')
        if using_separate_inputs:
            target_chain = target_chain or "A"
            ligand_chain = ligand_chain or "L"
        if target_chain:
            command.extend(["--target_chain", target_chain])
        if ligand_chain:
            command.extend(["--ligand_chain", ligand_chain])
        if score_args.get('affinity_refine'):
            command.append("--affinity_refine")

        tracker.update_status("running", "Executing Boltz2Score subprocess")
        logger.info(f"Task {task_id}: Running Boltz2Score. Command: {' '.join(command)}")

        proc_env = os.environ.copy()
        proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env,
            cwd=str(BASE_DIR),
        )

        tracker.register_process(process.pid)

        try:
            stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            process.kill()
            stdout, stderr = process.communicate()
            error_message = (
                f"Subprocess for Boltz2Score task {task_id} timed out after {SUBPROCESS_TIMEOUT} seconds.\n"
                f"Stderr:\n{stderr}\nStdout:\n{stdout}"
            )
            logger.error(error_message)
            tracker.update_status("timeout", f"Process timeout after {SUBPROCESS_TIMEOUT}s")
            raise TimeoutError(error_message) from e

        if process.returncode != 0:
            error_message = f"Subprocess for Boltz2Score task {task_id} failed with exit code {process.returncode}.\nStderr:\n{stderr}\nStdout:\n{stdout}"
            logger.error(error_message)
            tracker.update_status("failed", f"Process failed with exit code {process.returncode}")
            raise RuntimeError(error_message)

        tracker.update_status("processing_output", "Packaging Boltz2Score results")

        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        with zipfile.ZipFile(output_archive_path, 'w') as zipf:
            for root, _, files in os.walk(output_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, output_dir)
                    zipf.write(file_path, arcname)

            archive_candidates = set(extra_archive_files)
            if input_file_path and os.path.exists(input_file_path):
                archive_candidates.add(input_file_path)

            for file_path in sorted(archive_candidates):
                if not os.path.exists(file_path):
                    continue
                if inputs_dir and os.path.commonpath([inputs_dir, file_path]) == inputs_dir:
                    arcname = os.path.relpath(file_path, task_temp_dir)
                else:
                    arcname = os.path.basename(file_path)
                zipf.write(file_path, arcname)

        upload_response = upload_result_to_central_api(task_id, output_archive_path, os.path.basename(output_archive_path))

        final_meta = {
            'status': 'Complete',
            'gpu_id': gpu_id,
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path)
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        tracker.update_status("completed", "Task completed successfully")
        logger.info(f"Task {task_id}: Boltz2Score completed and results uploaded successfully.")
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

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")


@celery_app.task(bind=True)
def virtual_screening_task(self, screening_args: dict):
    """
    Celery task for running virtual screening pipeline.
    """
    task_id = self.request.id
    task_temp_dir = None
    tracker = None
    redis_client = get_redis_client()
    progress_key = f"virtual_screening:progress:{task_id}"
    start_time = time.time()

    try:
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing virtual screening task")

        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz_screening_{task_id}_")
        input_dir = os.path.join(task_temp_dir, "inputs")
        output_dir = os.path.join(config.VIRTUAL_SCREENING_OUTPUT_DIR, task_id)
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        target_filename = screening_args['target_filename']
        target_path = os.path.join(input_dir, target_filename)
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(screening_args['target_content'])

        library_filename = screening_args['library_filename']
        library_path = os.path.join(input_dir, library_filename)
        _write_base64_file(screening_args['library_base64'], library_path, text_mode=False)

        options = screening_args.get('options', {})
        command = [
            sys.executable,
            str(BASE_DIR / "virtual_screening" / "run_screening.py"),
            "--target", target_path,
            "--library", library_path,
            "--output_dir", output_dir,
            "--server_url", config.CENTRAL_API_URL,
            "--api_token", config.BOLTZ_API_TOKEN
        ]

        if options.get('library_type'):
            command.extend(["--library_type", str(options['library_type'])])
        if options.get('max_molecules') is not None:
            command.extend(["--max_molecules", str(options['max_molecules'])])
        if options.get('batch_size') is not None:
            command.extend(["--batch_size", str(options['batch_size'])])
        if options.get('max_workers') is not None:
            command.extend(["--max_workers", str(options['max_workers'])])
        if options.get('timeout') is not None:
            command.extend(["--timeout", str(options['timeout'])])
        if options.get('retry_attempts') is not None:
            command.extend(["--retry_attempts", str(options['retry_attempts'])])
        if options.get('use_msa_server'):
            command.append("--use_msa_server")
        if options.get('binding_affinity_weight') is not None:
            command.extend(["--binding_affinity_weight", str(options['binding_affinity_weight'])])
        if options.get('structural_stability_weight') is not None:
            command.extend(["--structural_stability_weight", str(options['structural_stability_weight'])])
        if options.get('confidence_weight') is not None:
            command.extend(["--confidence_weight", str(options['confidence_weight'])])
        if options.get('min_binding_score') is not None:
            command.extend(["--min_binding_score", str(options['min_binding_score'])])
        if options.get('top_n') is not None:
            command.extend(["--top_n", str(options['top_n'])])
        if options.get('report_only'):
            command.append("--report_only")
        if options.get('auto_enable_affinity'):
            command.append("--auto_enable_affinity")
        if options.get('enable_affinity'):
            command.append("--enable_affinity")
        if options.get('log_level'):
            command.extend(["--log_level", str(options['log_level'])])
        if options.get('force'):
            command.append("--force")
        if options.get('dry_run'):
            command.append("--dry_run")

        log_path = os.path.join(task_temp_dir, "virtual_screening.log")
        logger.info(f"Task {task_id}: Running virtual screening. Command: {' '.join(command)}")
        tracker.update_status("running", "Virtual screening subprocess started")

        with open(log_path, 'w', encoding='utf-8') as log_file:
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(BASE_DIR)
            )

            tracker.register_process(process.pid)

            last_progress_update = 0.0
            task_timeout = options.get('task_timeout') or SCREENING_TASK_TIMEOUT

            while True:
                now = time.time()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                    progress_payload = _read_virtual_screening_progress(output_dir)
                    progress_payload.update({
                        "task_id": task_id,
                        "status": "running",
                        "start_time": datetime.fromtimestamp(start_time).isoformat(),
                        "elapsed_seconds": now - start_time
                    })
                    _store_progress(redis_client, progress_key, progress_payload)
                    self.update_state(state='PROGRESS', meta=progress_payload)
                    last_progress_update = now

                if now - start_time > task_timeout:
                    process.kill()
                    raise TimeoutError(f"Virtual screening task {task_id} timed out after {task_timeout} seconds.")

                if process.poll() is not None:
                    break

                time.sleep(5)

        if process.returncode != 0:
            raise RuntimeError(f"Virtual screening task {task_id} failed. See log: {log_path}")

        tracker.update_status("packaging", "Packaging screening results")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_virtual_screening_results.zip")
        shutil.make_archive(output_archive_path[:-4], 'zip', output_dir)

        upload_response = upload_result_to_central_api(
            task_id,
            output_archive_path,
            os.path.basename(output_archive_path)
        )

        final_meta = {
            'status': 'Complete',
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path)
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        tracker.update_status("completed", "Virtual screening completed successfully")

        completed_payload = {
            "task_id": task_id,
            "status": "completed",
            "completed_at": datetime.now().isoformat()
        }
        _store_progress(redis_client, progress_key, completed_payload)
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
        failed_payload = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
            "failed_at": datetime.now().isoformat()
        }
        _store_progress(redis_client, progress_key, failed_payload)
        raise e

    finally:
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            logger.info(f"Task {task_id}: Cleaned up temporary directory '{task_temp_dir}'.")

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")


@celery_app.task(bind=True)
def lead_optimization_task(self, optimization_args: dict):
    """
    Celery task for running lead optimization pipeline.
    """
    task_id = self.request.id
    task_temp_dir = None
    tracker = None
    redis_client = get_redis_client()
    progress_key = f"lead_optimization:progress:{task_id}"
    start_time = time.time()

    def _count_compounds(path: str) -> int:
        if not path or not os.path.exists(path):
            return 0
        if path.endswith('.csv'):
            import csv
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                return sum(1 for _ in reader)
        count = 0
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    count += 1
        return count

    try:
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing lead optimization task")

        task_temp_dir = tempfile.mkdtemp(prefix=f"boltz_optimization_{task_id}_")
        input_dir = os.path.join(task_temp_dir, "inputs")
        output_dir = os.path.join(config.LEAD_OPTIMIZATION_OUTPUT_DIR, task_id)
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        opt_config = _load_lead_optimization_config()
        db_path = opt_config.mmp_database.database_path
        if not db_path or not os.path.exists(db_path):
            raise RuntimeError(f"MMP database not found: {db_path}")
        if not _mmpdb_available():
            raise RuntimeError("mmpdb CLI not available. Install mmpdb or ensure it is in PATH.")

        target_filename = optimization_args['target_filename']
        target_path = os.path.join(input_dir, target_filename)
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(optimization_args['target_content'])

        input_compound = optimization_args.get('input_compound')
        input_file_path = None
        expected_compounds = None

        if optimization_args.get('input_file_base64'):
            input_filename = optimization_args.get('input_filename', 'input_compounds.csv')
            input_file_path = os.path.join(input_dir, input_filename)
            _write_base64_file(optimization_args['input_file_base64'], input_file_path, text_mode=False)
            expected_compounds = _count_compounds(input_file_path)

        options = optimization_args.get('options', {})

        command = [
            sys.executable,
            str(BASE_DIR / "lead_optimization" / "run_optimization.py"),
            "--target_config", target_path,
            "--output_dir", output_dir
        ]

        if input_compound:
            command.extend(["--input_compound", input_compound])
        elif input_file_path:
            command.extend(["--input_file", input_file_path])
        else:
            raise ValueError("Either input_compound or input_file must be provided for lead optimization.")

        if options.get('optimization_strategy'):
            command.extend(["--optimization_strategy", str(options['optimization_strategy'])])
        if options.get('max_candidates') is not None:
            command.extend(["--max_candidates", str(options['max_candidates'])])
        if options.get('iterations') is not None:
            command.extend(["--iterations", str(options['iterations'])])
        if options.get('batch_size') is not None:
            command.extend(["--batch_size", str(options['batch_size'])])
        if options.get('top_k_per_iteration') is not None:
            command.extend(["--top_k_per_iteration", str(options['top_k_per_iteration'])])
        if options.get('diversity_weight') is not None:
            command.extend(["--diversity_weight", str(options['diversity_weight'])])
        if options.get('similarity_threshold') is not None:
            command.extend(["--similarity_threshold", str(options['similarity_threshold'])])
        if options.get('max_similarity_threshold') is not None:
            command.extend(["--max_similarity_threshold", str(options['max_similarity_threshold'])])
        if options.get('diversity_selection_strategy'):
            command.extend(["--diversity_selection_strategy", str(options['diversity_selection_strategy'])])
        if options.get('max_chiral_centers') is not None:
            command.extend(["--max_chiral_centers", str(options['max_chiral_centers'])])
        if options.get('generate_report'):
            command.append("--generate_report")
        if options.get('core_smarts'):
            command.extend(["--core_smarts", str(options['core_smarts'])])
        if options.get('exclude_smarts'):
            command.extend(["--exclude_smarts", str(options['exclude_smarts'])])
        if options.get('rgroup_smarts'):
            command.extend(["--rgroup_smarts", str(options['rgroup_smarts'])])
        if options.get('variable_smarts'):
            command.extend(["--variable_smarts", str(options['variable_smarts'])])
        if options.get('variable_const_smarts'):
            command.extend(["--variable_const_smarts", str(options['variable_const_smarts'])])
        if options.get('verbosity') is not None:
            command.extend(["--verbosity", str(options['verbosity'])])
        if options.get('backend'):
            command.extend(["--backend", str(options['backend'])])

        env = os.environ.copy()
        env["BOLTZ_API_TOKEN"] = config.BOLTZ_API_TOKEN
        env["PYTHONPATH"] = f"{BASE_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}"

        log_path = os.path.join(output_dir, "lead_optimization.log")
        logger.info(f"Task {task_id}: Running lead optimization. Command: {' '.join(command)}")
        tracker.update_status("running", "Lead optimization subprocess started")

        expected_candidates = None
        if input_compound and options.get('max_candidates') is not None and options.get('iterations') is not None:
            expected_candidates = int(options['max_candidates']) * int(options['iterations'])

        with open(log_path, 'w', encoding='utf-8') as log_file:
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=str(BASE_DIR)
            )

            tracker.register_process(process.pid)

            last_progress_update = 0.0
            task_timeout = options.get('task_timeout') or OPTIMIZATION_TASK_TIMEOUT

            while True:
                now = time.time()
                if now - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                    elapsed = now - start_time
                    progress_payload = _read_lead_optimization_progress(
                        output_dir,
                        elapsed,
                        expected_candidates=expected_candidates,
                        expected_compounds=expected_compounds
                    )
                    progress_payload.update({
                        "task_id": task_id,
                        "status": "running",
                        "start_time": datetime.fromtimestamp(start_time).isoformat(),
                        "elapsed_seconds": elapsed,
                        "expected_compounds": expected_compounds
                    })
                    _store_progress(redis_client, progress_key, progress_payload)
                    self.update_state(state='PROGRESS', meta=progress_payload)
                    last_progress_update = now

                if now - start_time > task_timeout:
                    process.kill()
                    raise TimeoutError(f"Lead optimization task {task_id} timed out after {task_timeout} seconds.")

                if process.poll() is not None:
                    break

                time.sleep(5)

        if process.returncode != 0:
            raise RuntimeError(f"Lead optimization task {task_id} failed. See log: {log_path}")

        tracker.update_status("packaging", "Packaging optimization results")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_lead_optimization_results.zip")
        shutil.make_archive(output_archive_path[:-4], 'zip', output_dir)

        upload_response = upload_result_to_central_api(
            task_id,
            output_archive_path,
            os.path.basename(output_archive_path)
        )

        final_meta = {
            'status': 'Complete',
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path)
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        tracker.update_status("completed", "Lead optimization completed successfully")

        completed_payload = {
            "task_id": task_id,
            "status": "completed",
            "completed_at": datetime.now().isoformat()
        }
        _store_progress(redis_client, progress_key, completed_payload)
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
        failed_payload = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
            "failed_at": datetime.now().isoformat()
        }
        _store_progress(redis_client, progress_key, failed_payload)
        raise e

    finally:
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            logger.info(f"Task {task_id}: Cleaned up temporary directory '{task_temp_dir}'.")

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")
