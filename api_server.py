import os
import json
import logging
import glob
import time
import hashlib
import shutil
import psutil
import signal
import base64
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from typing import Dict, List, Optional
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from celery.result import AsyncResult
import config
from celery_app import celery_app
from tasks import predict_task, affinity_task, virtual_screening_task, lead_optimization_task
from gpu_manager import get_redis_client, release_gpu, get_gpu_status

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Task Monitor Class ---
class TaskMonitor:
    """任务监控和清理工具，集成在API服务器中"""
    
    def __init__(self):
        self.redis_client = get_redis_client()
        self.max_task_duration = timedelta(hours=3)  # 最长允许运行时间
        self.max_stuck_duration = timedelta(minutes=30)  # 无进展的最长时间
        
    def get_stuck_tasks(self) -> List[Dict]:
        """检测卡死的任务"""
        stuck_tasks = []
        gpu_status = get_gpu_status()
        
        for gpu_id, task_id in gpu_status['in_use'].items():
            task_info = self._analyze_task(task_id)
            if task_info and task_info['is_stuck']:
                task_info['gpu_id'] = gpu_id
                stuck_tasks.append(task_info)
                
        return stuck_tasks
    
    def _analyze_task(self, task_id: str) -> Optional[Dict]:
        """分析单个任务状态"""
        try:
            # 获取Celery任务结果
            result = AsyncResult(task_id, app=celery_app)
            
            # 获取任务启动时间（从Redis或其他地方）
            task_start_key = f"task_start:{task_id}"
            start_time_str = self.redis_client.get(task_start_key)
            
            if not start_time_str:
                # 如果没有记录开始时间，假设是最近开始的
                start_time = datetime.now() - timedelta(minutes=5)
                self.redis_client.setex(task_start_key, 86400, start_time.isoformat())
            else:
                start_time = datetime.fromisoformat(start_time_str)
            
            # 获取任务最后更新时间
            last_update_key = f"task_update:{task_id}"
            last_update_str = self.redis_client.get(last_update_key)
            last_update = datetime.fromisoformat(last_update_str) if last_update_str else start_time
            
            now = datetime.now()
            running_time = now - start_time
            stuck_time = now - last_update
            
            # 检查是否卡死
            is_stuck = False
            reason = ""
            
            if running_time > self.max_task_duration:
                is_stuck = True
                reason = f"运行时间过长 ({running_time})"
            elif stuck_time > self.max_stuck_duration and result.state in ['PENDING', 'PROGRESS']:
                is_stuck = True
                reason = f"无进展时间过长 ({stuck_time})"
            elif result.state == 'FAILURE':
                is_stuck = True
                reason = "任务已失败但GPU未释放"
            
            # 检查进程是否存在
            processes = self._find_task_processes(task_id)
            if not processes and result.state in ['PENDING', 'PROGRESS']:
                is_stuck = True
                reason = "任务进程不存在但状态显示运行中"
            
            return {
                'task_id': task_id,
                'state': result.state,
                'start_time': start_time.isoformat(),
                'last_update': last_update.isoformat(),
                'running_time': str(running_time),
                'stuck_time': str(stuck_time),
                'is_stuck': is_stuck,
                'reason': reason,
                'processes': len(processes),
                'meta': result.info if hasattr(result, 'info') else {}
            }
            
        except Exception as e:
            logger.error(f"分析任务 {task_id} 时出错: {e}")
            return None
    
    def _find_task_processes(self, task_id: str) -> List[Dict]:
        """查找与任务相关的进程"""
        processes = []
        
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'cpu_percent']):
                try:
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if task_id in cmdline or f"boltz_task_{task_id}" in cmdline:
                        processes.append({
                            'pid': proc.info['pid'],
                            'name': proc.info['name'],
                            'cmdline': cmdline,
                            'create_time': datetime.fromtimestamp(proc.info['create_time']).isoformat(),
                            'cpu_percent': proc.cpu_percent()
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                    
        except Exception as e:
            logger.error(f"查找进程时出错: {e}")
            
        return processes
    
    def kill_stuck_tasks(self, task_ids: List[str] = None, force: bool = False) -> Dict:
        """清理卡死的任务"""
        if task_ids is None:
            stuck_tasks = self.get_stuck_tasks()
            task_ids = [task['task_id'] for task in stuck_tasks]
        
        results = {
            'killed_tasks': [],
            'failed_to_kill': [],
            'released_gpus': []
        }
        
        for task_id in task_ids:
            try:
                success = self._kill_single_task(task_id, force)
                if success:
                    results['killed_tasks'].append(task_id)
                else:
                    results['failed_to_kill'].append(task_id)
            except Exception as e:
                logger.error(f"清理任务 {task_id} 时出错: {e}")
                results['failed_to_kill'].append(task_id)
        
        # 释放GPU
        gpu_status = get_gpu_status()
        for gpu_id, task_id in gpu_status['in_use'].items():
            if task_id in results['killed_tasks']:
                try:
                    release_gpu(int(gpu_id), task_id)
                    results['released_gpus'].append(gpu_id)
                    logger.info(f"已释放GPU {gpu_id} (任务 {task_id})")
                except Exception as e:
                    logger.error(f"释放GPU {gpu_id} 时出错: {e}")
        
        return results
    
    def _kill_single_task(self, task_id: str, force: bool = False) -> bool:
        """清理单个任务"""
        try:
            # 撤销Celery任务
            celery_app.control.revoke(task_id, terminate=True)
            logger.info(f"已撤销Celery任务: {task_id}")
            
            # 查找并终止相关进程
            processes = self._find_task_processes(task_id)
            for proc_info in processes:
                try:
                    proc = psutil.Process(proc_info['pid'])
                    if force:
                        proc.kill()
                        logger.info(f"已强制终止进程 {proc_info['pid']}")
                    else:
                        proc.terminate()
                        logger.info(f"已发送终止信号给进程 {proc_info['pid']}")
                        
                        # 等待进程退出
                        try:
                            proc.wait(timeout=10)
                        except psutil.TimeoutExpired:
                            proc.kill()
                            logger.info(f"进程 {proc_info['pid']} 未响应终止信号，已强制终止")
                            
                except psutil.NoSuchProcess:
                    logger.info(f"进程 {proc_info['pid']} 已不存在")
                except Exception as e:
                    logger.error(f"终止进程 {proc_info['pid']} 时出错: {e}")
                    return False
            
            # 清理Redis中的任务记录
            self.redis_client.delete(f"task_start:{task_id}")
            self.redis_client.delete(f"task_update:{task_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"清理任务 {task_id} 时出错: {e}")
            return False
    
    def clean_completed_tasks(self) -> Dict:
        """清理已完成但未释放GPU的任务"""
        gpu_status = get_gpu_status()
        results = {
            'cleaned_gpus': [],
            'failed_to_clean': []
        }
        
        for gpu_id, task_id in gpu_status['in_use'].items():
            try:
                result = AsyncResult(task_id, app=celery_app)
                if result.state in ['SUCCESS', 'FAILURE', 'REVOKED']:
                    release_gpu(int(gpu_id), task_id)
                    results['cleaned_gpus'].append(gpu_id)
                    logger.info(f"已清理GPU {gpu_id} (任务 {task_id}, 状态: {result.state})")
            except Exception as e:
                logger.error(f"清理GPU {gpu_id} 时出错: {e}")
                results['failed_to_clean'].append(gpu_id)
        
        return results

# 创建全局任务监控实例
task_monitor = TaskMonitor()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.RESULTS_BASE_DIR

# MSA 缓存配置
MSA_CACHE_CONFIG = {
    'cache_dir': '/tmp/boltz_msa_cache',
    'max_age_days': 7,  # 缓存文件最大保存天数
    'max_size_gb': 5,   # 缓存目录最大大小（GB）
    'enable_cache': True
}

try:
    os.makedirs(config.RESULTS_BASE_DIR, exist_ok=True)
    logger.info(f"Results base directory ensured: {config.RESULTS_BASE_DIR}")
except OSError as e:
    logger.critical(f"Failed to create results directory {config.RESULTS_BASE_DIR}: {e}")

# --- Authentication Decorator ---
def require_api_token(f):
    """
    Decorator to validate API token from request headers.
    Logs unauthorized access attempts.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('X-API-Token')
        if not token or not hasattr(config, 'BOLTZ_API_TOKEN') or token != config.BOLTZ_API_TOKEN:
            logger.warning(f"Unauthorized API access attempt from {request.remote_addr} to {request.path}")
            return jsonify({'error': 'Unauthorized. Invalid or missing API token.'}), 403
        logger.debug(f"API token validated for {request.path} from {request.remote_addr}")
        return f(*args, **kwargs)
    return decorated_function

def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y"}

def _parse_int(value: Optional[str], default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _parse_float(value: Optional[str], default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default

def _load_progress(redis_key: str) -> Optional[Dict]:
    try:
        redis_client = get_redis_client()
        raw = redis_client.get(redis_key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Failed to read progress from redis: {e}")
        return None

# --- API Endpoints ---

@app.route('/predict', methods=['POST'])
@require_api_token
def handle_predict():
    """
    Receives prediction requests, processes YAML input, and dispatches Celery tasks.
    Handles file upload validation and task queuing based on priority.
    """
    logger.info("Received prediction request.")

    if 'yaml_file' not in request.files:
        logger.error("Missing 'yaml_file' in prediction request. Client IP: %s", request.remote_addr)
        return jsonify({'error': "Request form must contain a 'yaml_file' part"}), 400

    yaml_file = request.files['yaml_file']
    
    if yaml_file.filename == '':
        logger.error("No selected file for 'yaml_file' in prediction request.")
        return jsonify({'error': 'No selected file for yaml_file'}), 400

    try:
        yaml_content = yaml_file.read().decode('utf-8')
        logger.debug("YAML file successfully read and decoded.")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode yaml_file as UTF-8. Client IP: {request.remote_addr}")
        return jsonify({'error': "Failed to decode yaml_file. Ensure it's a valid UTF-8 text file."}), 400
    except IOError as e:
        logger.exception(f"Failed to read yaml_file from request: {e}. Client IP: {request.remote_addr}")
        return jsonify({'error': f"Failed to read yaml_file: {e}"}), 400

    use_msa_server_str = request.form.get('use_msa_server', 'false')
    use_msa_server = str(use_msa_server_str).strip().lower() == 'true'
    logger.info(f"use_msa_server parameter received: {use_msa_server} for client {request.remote_addr}.")
    
    # 处理模型参数
    model_name = request.form.get('model', None)
    if model_name:
        logger.info(f"model parameter received: {model_name} for client {request.remote_addr}.")

    backend_raw = request.form.get('backend', 'boltz')
    backend = str(backend_raw).strip().lower()
    if backend not in ['boltz', 'alphafold3']:
        logger.warning(f"Invalid backend '{backend}' provided by client {request.remote_addr}. Defaulting to 'boltz'.")
        backend = 'boltz'
    logger.info(f"backend parameter received: {backend} for client {request.remote_addr}.")
    
    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning(f"Invalid priority '{priority}' provided by client {request.remote_addr}. Defaulting to 'default'.")
        priority = 'default'

    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE
    logger.info(f"Prediction priority: {priority}, targeting queue: '{target_queue}' for client {request.remote_addr}.")

    predict_args = {
        'yaml_content': yaml_content,
        'use_msa_server': use_msa_server,
        'model_name': model_name,
        'backend': backend
    }

    try:
        task = predict_task.apply_async(args=[predict_args], queue=target_queue)
        logger.info(
            f"Task {task.id} dispatched to queue: '{target_queue}' with use_msa_server={use_msa_server}, backend={backend}."
        )
    except Exception as e:
        logger.exception(f"Failed to dispatch Celery task for prediction request from {request.remote_addr}: {e}")
        return jsonify({'error': 'Failed to dispatch prediction task.', 'details': str(e)}), 500
    
    return jsonify({'task_id': task.id}), 202


@app.route('/api/virtual_screening/submit', methods=['POST'])
@require_api_token
def submit_virtual_screening():
    """Submit a virtual screening job."""
    logger.info("Received virtual screening submission request.")

    if 'target_file' not in request.files or 'library_file' not in request.files:
        return jsonify({'error': "Request must include 'target_file' and 'library_file'."}), 400

    target_file = request.files['target_file']
    library_file = request.files['library_file']

    if target_file.filename == '' or library_file.filename == '':
        return jsonify({'error': 'Target file or library file is empty.'}), 400

    try:
        target_content = target_file.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'error': 'Failed to decode target_file as UTF-8.'}), 400

    library_bytes = library_file.read()
    library_base64 = base64.b64encode(library_bytes).decode('ascii')

    options = {
        'library_type': request.form.get('library_type'),
        'max_molecules': _parse_int(request.form.get('max_molecules')),
        'batch_size': _parse_int(request.form.get('batch_size')),
        'max_workers': _parse_int(request.form.get('max_workers')),
        'timeout': _parse_int(request.form.get('timeout')),
        'retry_attempts': _parse_int(request.form.get('retry_attempts')),
        'use_msa_server': _parse_bool(request.form.get('use_msa_server'), False),
        'binding_affinity_weight': _parse_float(request.form.get('binding_affinity_weight')),
        'structural_stability_weight': _parse_float(request.form.get('structural_stability_weight')),
        'confidence_weight': _parse_float(request.form.get('confidence_weight')),
        'min_binding_score': _parse_float(request.form.get('min_binding_score')),
        'top_n': _parse_int(request.form.get('top_n')),
        'report_only': _parse_bool(request.form.get('report_only'), False),
        'auto_enable_affinity': _parse_bool(request.form.get('auto_enable_affinity'), False),
        'enable_affinity': _parse_bool(request.form.get('enable_affinity'), False),
        'log_level': request.form.get('log_level'),
        'force': _parse_bool(request.form.get('force'), False),
        'dry_run': _parse_bool(request.form.get('dry_run'), False),
        'task_timeout': _parse_int(request.form.get('task_timeout'))
    }

    screening_args = {
        'target_filename': secure_filename(target_file.filename),
        'target_content': target_content,
        'library_filename': secure_filename(library_file.filename),
        'library_base64': library_base64,
        'options': options
    }

    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        priority = 'default'
    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE

    try:
        task = virtual_screening_task.apply_async(args=[screening_args], queue=target_queue)
    except Exception as e:
        logger.exception("Failed to dispatch virtual screening task: %s", e)
        return jsonify({'error': 'Failed to dispatch virtual screening task.', 'details': str(e)}), 500

    return jsonify({'task_id': task.id}), 202


@app.route('/api/virtual_screening/status/<task_id>', methods=['GET'])
@require_api_token
def get_virtual_screening_status(task_id):
    """Get virtual screening task status with progress info."""
    task_result = AsyncResult(task_id, app=celery_app)
    progress = _load_progress(f"virtual_screening:progress:{task_id}")

    response = {
        'task_id': task_id,
        'state': task_result.state,
        'progress': progress or {}
    }

    if task_result.state == 'FAILURE':
        info = task_result.info
        response['error'] = str(info)

    return jsonify(response)


@app.route('/api/virtual_screening/results/<task_id>', methods=['GET'])
@require_api_token
def download_virtual_screening_results(task_id):
    """Download virtual screening results."""
    return download_results(task_id)


@app.route('/api/lead_optimization/submit', methods=['POST'])
@require_api_token
def submit_lead_optimization():
    """Submit a lead optimization job."""
    logger.info("Received lead optimization submission request.")

    if 'target_config' not in request.files:
        return jsonify({'error': "Request must include 'target_config'."}), 400

    target_file = request.files['target_config']
    if target_file.filename == '':
        return jsonify({'error': 'Target config file is empty.'}), 400

    try:
        target_content = target_file.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'error': 'Failed to decode target_config as UTF-8.'}), 400

    input_compound = request.form.get('input_compound')
    input_file = request.files.get('input_file')

    if not input_compound and not input_file:
        return jsonify({'error': "Either 'input_compound' or 'input_file' is required."}), 400
    if input_compound and input_file:
        return jsonify({'error': "Provide only one of 'input_compound' or 'input_file'."}), 400

    input_file_base64 = None
    input_filename = None
    if input_file:
        if input_file.filename == '':
            return jsonify({'error': 'Input file is empty.'}), 400
        input_file_base64 = base64.b64encode(input_file.read()).decode('ascii')
        input_filename = secure_filename(input_file.filename)

    options = {
        'optimization_strategy': request.form.get('optimization_strategy'),
        'max_candidates': _parse_int(request.form.get('max_candidates')),
        'iterations': _parse_int(request.form.get('iterations')),
        'batch_size': _parse_int(request.form.get('batch_size')),
        'top_k_per_iteration': _parse_int(request.form.get('top_k_per_iteration')),
        'diversity_weight': _parse_float(request.form.get('diversity_weight')),
        'similarity_threshold': _parse_float(request.form.get('similarity_threshold')),
        'max_similarity_threshold': _parse_float(request.form.get('max_similarity_threshold')),
        'diversity_selection_strategy': request.form.get('diversity_selection_strategy'),
        'max_chiral_centers': _parse_int(request.form.get('max_chiral_centers')),
        'generate_report': _parse_bool(request.form.get('generate_report'), False),
        'core_smarts': request.form.get('core_smarts'),
        'exclude_smarts': request.form.get('exclude_smarts'),
        'rgroup_smarts': request.form.get('rgroup_smarts'),
        'variable_smarts': request.form.get('variable_smarts'),
        'variable_const_smarts': request.form.get('variable_const_smarts'),
        'verbosity': _parse_int(request.form.get('verbosity')),
        'task_timeout': _parse_int(request.form.get('task_timeout')),
        'backend': request.form.get('backend')
    }

    if options.get('backend') and options['backend'] not in ['boltz', 'alphafold3']:
        options['backend'] = None

    optimization_args = {
        'target_filename': secure_filename(target_file.filename),
        'target_content': target_content,
        'input_compound': input_compound,
        'input_filename': input_filename,
        'input_file_base64': input_file_base64,
        'options': options
    }

    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        priority = 'default'
    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE

    try:
        task = lead_optimization_task.apply_async(args=[optimization_args], queue=target_queue)
    except Exception as e:
        logger.exception("Failed to dispatch lead optimization task: %s", e)
        return jsonify({'error': 'Failed to dispatch lead optimization task.', 'details': str(e)}), 500

    return jsonify({'task_id': task.id}), 202


@app.route('/api/lead_optimization/status/<task_id>', methods=['GET'])
@require_api_token
def get_lead_optimization_status(task_id):
    """Get lead optimization task status with progress info."""
    task_result = AsyncResult(task_id, app=celery_app)
    progress = _load_progress(f"lead_optimization:progress:{task_id}")

    response = {
        'task_id': task_id,
        'state': task_result.state,
        'progress': progress or {}
    }

    if task_result.state == 'FAILURE':
        info = task_result.info
        response['error'] = str(info)

    return jsonify(response)


@app.route('/api/lead_optimization/results/<task_id>', methods=['GET'])
@require_api_token
def download_lead_optimization_results(task_id):
    """Download lead optimization results."""
    return download_results(task_id)


@app.route('/api/affinity', methods=['POST'])
@require_api_token
def handle_affinity():
    """
    Receives affinity prediction requests, and dispatches Celery tasks.
    """
    logger.info("Received affinity prediction request.")

    if 'input_file' not in request.files:
        logger.error("Missing 'input_file' in affinity prediction request. Client IP: %s", request.remote_addr)
        return jsonify({'error': "Request form must contain a 'input_file' part"}), 400

    input_file = request.files['input_file']
    
    if input_file.filename == '':
        logger.error("No selected file for 'input_file' in affinity prediction request.")
        return jsonify({'error': 'No selected file for input_file'}), 400

    try:
        input_file_content = input_file.read().decode('utf-8')
        logger.debug("Input file successfully read and decoded.")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode input_file as UTF-8. Client IP: {request.remote_addr}")
        return jsonify({'error': "Failed to decode input_file. Ensure it's a valid UTF-8 text file."}), 400
    except IOError as e:
        logger.exception(f"Failed to read input_file from request: {e}. Client IP: {request.remote_addr}")
        return jsonify({'error': f"Failed to read input_file: {e}"}), 400

    ligand_resname = request.form.get('ligand_resname', 'LIG')
    logger.info(f"ligand_resname parameter received: {ligand_resname} for client {request.remote_addr}.")
    
    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning(f"Invalid priority '{priority}' provided by client {request.remote_addr}. Defaulting to 'default'.")
        priority = 'default'

    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE
    logger.info(f"Affinity prediction priority: {priority}, targeting queue: '{target_queue}' for client {request.remote_addr}.")

    affinity_args = {
        'input_file_content': input_file_content,
        'input_filename': secure_filename(input_file.filename),
        'ligand_resname': ligand_resname
    }

    try:
        task = affinity_task.apply_async(args=[affinity_args], queue=target_queue)
        logger.info(f"Affinity task {task.id} dispatched to queue: '{target_queue}'.")
    except Exception as e:
        logger.exception(f"Failed to dispatch Celery task for affinity prediction request from {request.remote_addr}: {e}")
        return jsonify({'error': 'Failed to dispatch affinity prediction task.', 'details': str(e)}), 500
    
    return jsonify({'task_id': task.id}), 202


@app.route('/api/affinity_separate', methods=['POST'])
@require_api_token
def handle_affinity_separate():
    """
    Receives affinity prediction requests with separate protein and ligand files,
    and dispatches Celery tasks.
    """
    logger.info("Received separate affinity prediction request.")

    # Check for required files
    if 'protein_file' not in request.files or 'ligand_file' not in request.files:
        logger.error("Missing required files in separate affinity prediction request. Client IP: %s", request.remote_addr)
        return jsonify({'error': "Request form must contain both 'protein_file' and 'ligand_file' parts"}), 400

    protein_file = request.files['protein_file']
    ligand_file = request.files['ligand_file']
    
    if protein_file.filename == '' or ligand_file.filename == '':
        logger.error("No selected files for separate affinity prediction request.")
        return jsonify({'error': 'Both protein_file and ligand_file must be selected'}), 400

    try:
        # Read protein file
        protein_file_content = protein_file.read().decode('utf-8')
        
        # Read ligand file (might be binary for SDF)
        ligand_file.seek(0)  # Reset file pointer
        try:
            ligand_file_content = ligand_file.read().decode('utf-8')
        except UnicodeDecodeError:
            # For binary SDF files, read as bytes then decode
            ligand_file.seek(0)
            ligand_file_content = ligand_file.read().decode('utf-8', errors='replace')
        
        logger.debug("Protein and ligand files successfully read.")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode files as UTF-8. Client IP: {request.remote_addr}")
        return jsonify({'error': "Failed to decode files. Ensure they are valid text files."}), 400
    except IOError as e:
        logger.exception(f"Failed to read files from request: {e}. Client IP: {request.remote_addr}")
        return jsonify({'error': f"Failed to read files: {e}"}), 400

    # Get optional parameters
    # For separate inputs, ligand_resname is handled automatically by the system
    ligand_resname = request.form.get('ligand_resname', 'LIG')  # Keep for backward compatibility but will be overridden
    output_prefix = request.form.get('output_prefix', 'complex')
    
    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning(f"Invalid priority '{priority}' provided by client {request.remote_addr}. Defaulting to 'default'.")
        priority = 'default'

    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE
    logger.info(f"Separate affinity prediction priority: {priority}, targeting queue: '{target_queue}' for client {request.remote_addr}.")

    affinity_args = {
        'protein_file_content': protein_file_content,
        'ligand_file_content': ligand_file_content,
        'protein_filename': secure_filename(protein_file.filename),
        'ligand_filename': secure_filename(ligand_file.filename),
        'ligand_resname': ligand_resname,
        'output_prefix': output_prefix
    }

    try:
        task = affinity_task.apply_async(args=[affinity_args], queue=target_queue)
        logger.info(f"Separate affinity task {task.id} dispatched to queue: '{target_queue}'.")
    except Exception as e:
        logger.exception(f"Failed to dispatch Celery task for separate affinity prediction request from {request.remote_addr}: {e}")
        return jsonify({'error': 'Failed to dispatch separate affinity prediction task.', 'details': str(e)}), 500
    
    return jsonify({'task_id': task.id}), 202


@app.route('/status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """
    Retrieves the current status and information of a specific Celery task.
    Provides detailed feedback for various task states.
    """
    logger.info(f"Received status request for task ID: {task_id}")
    
    task_result = AsyncResult(task_id, app=celery_app)
    
    response = {
        'task_id': task_id,
        'state': task_result.state,
        'info': {} # Initialize info field
    }
    
    info = task_result.info

    if task_result.state == 'PENDING':
        response['info']['status'] = 'Task is waiting in the queue or the task ID does not exist.'
        logger.info(f"Task {task_id} is PENDING or non-existent.")
    elif task_result.state == 'SUCCESS':
        response['info'] = info if isinstance(info, dict) else {'result': str(info)}
        response['info']['status'] = 'Task completed successfully.'
        logger.info(f"Task {task_id} is SUCCESS.")
    elif task_result.state == 'FAILURE':
        response['info'] = {'exc_type': type(info).__name__, 'exc_message': str(info)} if isinstance(info, Exception) \
                           else (info if isinstance(info, dict) else {'message': str(info)})
        logger.error(f"Task {task_id} is in FAILURE state. Info: {response['info']}")
    elif task_result.state == 'REVOKED':
        response['info']['status'] = 'Task was revoked.'
        logger.warning(f"Task {task_id} was REVOKED.")
    else: # e.g., STARTED, RETRY
        response['info'] = info if isinstance(info, dict) else {'message': str(info)}
        logger.info(f"Task {task_id} is in state: {task_result.state}.")
    
    return jsonify(response)


@app.route('/results/<task_id>', methods=['GET'])
def download_results(task_id):
    """
    Allows downloading the result file for a completed task.
    Includes checks for task completion and path security.
    """
    logger.info(f"Received download request for task ID: {task_id}")
    task_result = AsyncResult(task_id, app=celery_app)
    
    if not task_result.ready():
        logger.warning(f"Attempted to download results for task {task_id} which is not ready. State: {task_result.state}")
        return jsonify({'error': 'Task has not completed yet.', 'state': task_result.state}), 404
            
    result_info = task_result.info
    if not isinstance(result_info, dict) or 'result_file' not in result_info or not result_info['result_file']:
        logger.error(f"Result file information missing or invalid for task {task_id}. Info: {result_info}")
        return jsonify({'error': 'Result file information not found in task metadata or is invalid.'}), 404

    filename = secure_filename(result_info['result_file'])
    directory = app.config['UPLOAD_FOLDER']
    filepath = os.path.join(directory, filename)

    # Critical security check: Ensure the file path is within the allowed directory.
    abs_filepath = os.path.abspath(filepath)
    abs_upload_folder = os.path.abspath(directory)
    if not abs_filepath.startswith(abs_upload_folder):
        logger.error(f"Attempted directory traversal for task {task_id}. Filepath: {filepath}")
        return jsonify({'error': 'Invalid file path detected.'}), 400

    if not os.path.exists(filepath):
        logger.error(f"Result file not found on disk for task {task_id} at path: {filepath}")
        return jsonify({'error': 'Result file not found on disk.'}), 404
    
    logger.info(f"Serving result file {filename} for task {task_id} from {filepath}.")
    return send_from_directory(directory, filename, as_attachment=True)


@app.route('/upload_result/<task_id>', methods=['POST'])
def upload_result_from_worker(task_id):
    """
    Internal endpoint for Celery Workers to upload result files.
    Ensures secure file saving.
    """
    logger.info(f"Received file upload request from worker for task ID: {task_id}")

    if 'file' not in request.files:
        logger.error(f"No file part in the upload request for task {task_id}.")
        return jsonify({'error': 'No file part in the request'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        logger.error(f"No selected file for upload for task {task_id}.")
        return jsonify({'error': 'No selected file'}), 400
    
    try:
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        file.save(save_path)
        logger.info(f"Result file '{filename}' for task {task_id} received and saved to {save_path}.")
        return jsonify({'message': f"File '{filename}' uploaded successfully for task {task_id}"}), 200
    except IOError as e:
        logger.exception(f"Failed to save uploaded file '{filename}' for task {task_id}: {e}")
        return jsonify({'error': f"Failed to save file: {e}"}), 500
    except Exception as e:
        logger.exception(f"An unexpected error occurred during file upload for task {task_id}: {e}")
        return jsonify({'error': f"An unexpected error occurred: {e}"}), 500


# --- Management Endpoints ---

@app.route('/tasks', methods=['GET'])
@require_api_token
def list_tasks():
    """
    Lists all active, reserved, and scheduled tasks across Celery workers.
    Provides introspection into the Celery cluster status.
    """
    logger.info("Received request to list all tasks.")
    inspector = celery_app.control.inspect()
    
    try:
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        
        all_tasks = {
            'active': [task for worker_tasks in active.values() for task in worker_tasks],
            'reserved': [task for worker_tasks in reserved.values() for task in worker_tasks],
            'scheduled': [task for worker_tasks in scheduled.values() for task in worker_tasks],
        }
        logger.info(f"Successfully listed tasks. Active: {len(all_tasks['active'])}, Reserved: {len(all_tasks['reserved'])}, Scheduled: {len(all_tasks['scheduled'])}")
        return jsonify(all_tasks)
    except Exception as e:
        logger.exception(f"Error inspecting Celery workers: {e}. Ensure workers are running and reachable.")
        return jsonify({'error': 'Could not inspect Celery workers. Ensure workers are running and reachable.', 'details': str(e)}), 500


@app.route('/tasks/<task_id>', methods=['DELETE'])
@require_api_token
def terminate_task(task_id):
    """
    Sends a revocation signal to terminate a running or queued task.
    """
    logger.info(f"Received request to terminate task ID: {task_id}")
    try:
        celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM', send_event=True)
        logger.info(f"Revocation request sent for task ID: {task_id}.")
        return jsonify({'status': 'Revocation request sent. Task should terminate shortly.', 'task_id': task_id})
    except Exception as e:
        logger.exception(f"Failed to send revocation request for task {task_id}: {e}")
        return jsonify({'error': 'Failed to send revocation request.', 'details': str(e)}), 500


@app.route('/tasks/<task_id>/move', methods=['POST'])
@require_api_token
def move_task(task_id):
    """
    Moves a task from its current queue to a specified new queue.
    This revokes the old task and re-dispatches it.
    """
    logger.info(f"Received request to move task ID: {task_id}")
    data = request.get_json()
    if not data or 'target_queue' not in data:
        logger.error(f"Invalid request to move task {task_id}: missing 'target_queue'.")
        return jsonify({'error': "Request body must be JSON and contain 'target_queue'."}), 400

    target_queue = data['target_queue']
    valid_queues = [config.HIGH_PRIORITY_QUEUE, config.DEFAULT_QUEUE]
    if target_queue not in valid_queues:
        logger.error(f"Invalid target_queue '{target_queue}' for task {task_id}. Allowed: {valid_queues}")
        return jsonify({'error': f"Invalid 'target_queue'. Must be one of: {', '.join(valid_queues)}."}), 400

    inspector = celery_app.control.inspect()
    try:
        reserved_tasks_by_worker = inspector.reserved() or {}
        task_info = None
        for worker, tasks in reserved_tasks_by_worker.items():
            for task in tasks:
                if task['id'] == task_id:
                    task_info = task
                    break
            if task_info:
                break
        
        if not task_info:
            logger.warning(f"Task {task_id} not found in reserved queue. It may be running, completed, or non-existent.")
            return jsonify({'error': 'Task not found in reserved queue. It may be running, completed, or non-existent.'}), 404

        # Revoke the original task (non-terminating if it hasn't started).
        celery_app.control.revoke(task_id, terminate=False, send_event=True)
        logger.info(f"Revoked original task {task_id} for moving.")

        # Re-dispatch the task with its original arguments to the new queue.
        original_args = task_info.get('args', [])
        original_kwargs = task_info.get('kwargs', {})
        
        new_task = predict_task.apply_async(
            args=original_args,
            kwargs=original_kwargs,
            queue=target_queue
        )
        logger.info(f"Task {task_id} successfully moved to new task ID: {new_task.id} in queue: {target_queue}.")

        return jsonify({
            'status': 'moved',
            'original_task_id': task_id,
            'new_task_id': new_task.id,
            'target_queue': target_queue,
            'message': f"Task {task_id} was moved to a new task {new_task.id} in queue {target_queue}."
        }), 200

    except Exception as e:
        logger.exception(f"Failed to move task {task_id}: {e}")
        return jsonify({'error': 'Failed to move task.', 'details': str(e)}), 500


# --- MSA 缓存管理 API ---

def get_msa_cache_stats():
    """获取MSA缓存统计信息"""
    cache_dir = MSA_CACHE_CONFIG['cache_dir']
    if not os.path.exists(cache_dir):
        return {
            'total_files': 0,
            'total_size_mb': 0,
            'oldest_file': None,
            'newest_file': None
        }
    
    msa_files = glob.glob(os.path.join(cache_dir, "msa_*.a3m"))
    
    if not msa_files:
        return {
            'total_files': 0,
            'total_size_mb': 0,
            'oldest_file': None,
            'newest_file': None
        }
    
    total_size = sum(os.path.getsize(f) for f in msa_files)
    file_times = [(f, os.path.getmtime(f)) for f in msa_files]
    file_times.sort(key=lambda x: x[1])
    
    oldest_file = datetime.fromtimestamp(file_times[0][1])
    newest_file = datetime.fromtimestamp(file_times[-1][1])
    
    return {
        'total_files': len(msa_files),
        'total_size_mb': round(total_size / (1024 * 1024), 2),
        'oldest_file': oldest_file.strftime('%Y-%m-%d %H:%M:%S'),
        'newest_file': newest_file.strftime('%Y-%m-%d %H:%M:%S')
    }

def cleanup_expired_msa_cache():
    """清理过期的MSA缓存文件"""
    cache_dir = MSA_CACHE_CONFIG['cache_dir']
    if not os.path.exists(cache_dir):
        return {'removed_files': 0, 'freed_space_mb': 0}
    
    max_age_seconds = MSA_CACHE_CONFIG['max_age_days'] * 24 * 3600
    current_time = time.time()
    
    msa_files = glob.glob(os.path.join(cache_dir, "msa_*.a3m"))
    removed_files = 0
    freed_space = 0
    
    for file_path in msa_files:
        file_age = current_time - os.path.getmtime(file_path)
        if file_age > max_age_seconds:
            file_size = os.path.getsize(file_path)
            try:
                os.remove(file_path)
                removed_files += 1
                freed_space += file_size
                logger.info(f"清理过期MSA缓存文件: {os.path.basename(file_path)}")
            except Exception as e:
                logger.error(f"清理缓存文件失败 {file_path}: {e}")
    
    return {
        'removed_files': removed_files,
        'freed_space_mb': round(freed_space / (1024 * 1024), 2)
    }

def cleanup_oversized_msa_cache():
    """清理超出大小限制的MSA缓存文件（按访问时间删除最旧的）"""
    cache_dir = MSA_CACHE_CONFIG['cache_dir']
    if not os.path.exists(cache_dir):
        return {'removed_files': 0, 'freed_space_mb': 0}
    
    max_size_bytes = MSA_CACHE_CONFIG['max_size_gb'] * 1024 * 1024 * 1024
    msa_files = glob.glob(os.path.join(cache_dir, "msa_*.a3m"))
    
    if not msa_files:
        return {'removed_files': 0, 'freed_space_mb': 0}
    
    # 计算当前总大小
    file_info = [(f, os.path.getsize(f), os.path.getatime(f)) for f in msa_files]
    current_size = sum(info[1] for info in file_info)
    
    if current_size <= max_size_bytes:
        return {'removed_files': 0, 'freed_space_mb': 0}
    
    # 按访问时间排序（最旧的在前）
    file_info.sort(key=lambda x: x[2])
    
    removed_files = 0
    freed_space = 0
    
    for file_path, file_size, _ in file_info:
        if current_size <= max_size_bytes:
            break
        
        try:
            os.remove(file_path)
            removed_files += 1
            freed_space += file_size
            current_size -= file_size
            logger.info(f"清理超量MSA缓存文件: {os.path.basename(file_path)}")
        except Exception as e:
            logger.error(f"清理缓存文件失败 {file_path}: {e}")
    
    return {
        'removed_files': removed_files,
        'freed_space_mb': round(freed_space / (1024 * 1024), 2)
    }

@app.route('/api/msa/cache/stats', methods=['GET'])
@require_api_token
def get_msa_cache_stats_api():
    """获取MSA缓存统计信息API"""
    try:
        stats = get_msa_cache_stats()
        return jsonify({
            'success': True,
            'data': stats
        }), 200
    except Exception as e:
        logger.exception(f"获取MSA缓存统计失败: {e}")
        return jsonify({
            'error': 'Failed to get MSA cache statistics',
            'details': str(e)
        }), 500

@app.route('/api/msa/cache/cleanup', methods=['POST'])
@require_api_token
def cleanup_msa_cache_api():
    """清理MSA缓存API"""
    try:
        # 获取清理前的统计
        stats_before = get_msa_cache_stats()
        
        # 清理过期文件
        expired_result = cleanup_expired_msa_cache()
        
        # 清理超量文件
        oversized_result = cleanup_oversized_msa_cache()
        
        # 获取清理后的统计
        stats_after = get_msa_cache_stats()
        
        result = {
            'before': stats_before,
            'after': stats_after,
            'expired_cleanup': expired_result,
            'oversized_cleanup': oversized_result,
            'total_removed': expired_result['removed_files'] + oversized_result['removed_files'],
            'total_freed_mb': expired_result['freed_space_mb'] + oversized_result['freed_space_mb']
        }
        
        logger.info(f"MSA缓存清理完成: 删除 {result['total_removed']} 个文件，释放 {result['total_freed_mb']} MB空间")
        
        return jsonify({
            'success': True,
            'data': result
        }), 200
        
    except Exception as e:
        logger.exception(f"MSA缓存清理失败: {e}")
        return jsonify({
            'error': 'Failed to cleanup MSA cache',
            'details': str(e)
        }), 500

@app.route('/api/msa/cache/clear', methods=['POST'])
@require_api_token
def clear_all_msa_cache_api():
    """清空所有MSA缓存API"""
    try:
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        if not os.path.exists(cache_dir):
            return jsonify({
                'success': True,
                'data': {'removed_files': 0, 'freed_space_mb': 0.0}
            }), 200

        total_size = 0
        total_items = 0

        for root, _, files in os.walk(cache_dir):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                try:
                    total_size += os.path.getsize(file_path)
                    total_items += 1
                except OSError as exc:
                    logger.warning(f"计算缓存文件大小失败 {file_path}: {exc}")

        try:
            shutil.rmtree(cache_dir)
            logger.info(f"已删除整个MSA缓存目录: {cache_dir}")
        except Exception as exc:
            logger.exception(f"清空MSA缓存目录失败: {exc}")
            return jsonify({
                'error': 'Failed to clear MSA cache',
                'details': str(exc)
            }), 500

        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            logger.error(f"重建MSA缓存目录失败: {exc}")

        result = {
            'removed_files': total_items,
            'freed_space_mb': round(total_size / (1024 * 1024), 2)
        }

        logger.info(f"MSA缓存清空完成: 删除 {result['removed_files']} 个文件，释放 {result['freed_space_mb']} MB空间")

        return jsonify({
            'success': True,
            'data': result
        }), 200
        
    except Exception as e:
        logger.exception(f"MSA缓存清空失败: {e}")
        return jsonify({
            'error': 'Failed to clear MSA cache',
            'details': str(e)
        }), 500


def _calculate_directory_metrics(target: Path) -> tuple[int, int]:
    total_size = 0
    total_files = 0

    if not target.exists():
        return total_size, total_files

    for root, _, files in os.walk(target):
        for file_name in files:
            file_path = Path(root) / file_name
            try:
                total_size += file_path.stat().st_size
                total_files += 1
            except OSError as exc:
                logger.warning(f"计算路径大小时忽略 {file_path}: {exc}")

    return total_size, total_files


@app.route('/api/colabfold/cache/clear', methods=['POST'])
@require_api_token
def clear_colabfold_cache_api():
    """清理 ColabFold 服务器生成的历史任务缓存。"""

    jobs_dir = Path(config.COLABFOLD_JOBS_DIR).expanduser()
    if not jobs_dir.exists():
        logger.info(f"ColabFold jobs 目录不存在: {jobs_dir}")
        return jsonify({
            'success': True,
            'data': {
                'removed_items': 0,
                'freed_space_mb': 0.0
            }
        }), 200

    total_size, total_files = _calculate_directory_metrics(jobs_dir)

    removed_items = 0
    failures: list[dict[str, str]] = []

    for entry in jobs_dir.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed_items += 1
        except Exception as exc:
            logger.error(f"清理 ColabFold 缓存条目失败 {entry}: {exc}")
            failures.append({
                'path': str(entry),
                'error': str(exc)
            })

    result = {
        'removed_items': removed_items,
        'freed_space_mb': round(total_size / (1024 * 1024), 2),
        'failed_items': failures
    }

    logger.info(
        "ColabFold 缓存清理完成: 删除 %s 个条目，释放 %.2f MB 空间",
        removed_items,
        result['freed_space_mb']
    )

    status_code = 200 if not failures else 207  # Multi-Status style: 部分失败

    return jsonify({
        'success': not failures,
        'data': result
    }), status_code

# --- Task Monitoring API Endpoints ---

@app.route('/monitor/status', methods=['GET'])
@require_api_token
def get_monitor_status():
    """获取任务和GPU状态"""
    try:
        gpu_status = get_gpu_status()
        stuck_tasks = task_monitor.get_stuck_tasks()
        
        # 获取所有正在运行任务的详细信息
        running_tasks = []
        for gpu_id, task_id in gpu_status['in_use'].items():
            task_info = task_monitor._analyze_task(task_id)
            if task_info:
                task_info['gpu_id'] = gpu_id
                running_tasks.append(task_info)
        
        result = {
            'gpu_status': {
                'available_count': gpu_status['available_count'],
                'available': gpu_status['available'],
                'in_use_count': gpu_status['in_use_count'],
                'in_use': gpu_status['in_use']
            },
            'running_tasks': running_tasks,
            'stuck_tasks': stuck_tasks,
            'stuck_count': len(stuck_tasks),
            'timestamp': datetime.now().isoformat()
        }
        
        logger.info(f"任务状态查询: {len(running_tasks)} 个运行中任务, {len(stuck_tasks)} 个卡死任务")
        
        return jsonify({
            'success': True,
            'data': result
        }), 200
        
    except Exception as e:
        logger.exception(f"获取任务状态失败: {e}")
        return jsonify({
            'error': 'Failed to get task status',
            'details': str(e)
        }), 500

@app.route('/monitor/clean', methods=['POST'])
@require_api_token
def clean_stuck_tasks():
    """清理卡死的任务"""
    try:
        force = request.json.get('force', False) if request.json else False
        task_ids = request.json.get('task_ids') if request.json else None
        
        # 如果没有指定task_ids，则清理所有卡死的任务
        if task_ids is None:
            # 先清理已完成但未释放GPU的任务
            clean_results = task_monitor.clean_completed_tasks()
            # 然后清理卡死的任务
            kill_results = task_monitor.kill_stuck_tasks(force=force)
        else:
            clean_results = {'cleaned_gpus': [], 'failed_to_clean': []}
            kill_results = task_monitor.kill_stuck_tasks(task_ids, force=force)
        
        result = {
            'cleaned_completed_tasks': clean_results,
            'killed_stuck_tasks': kill_results,
            'total_cleaned_gpus': len(clean_results['cleaned_gpus']) + len(kill_results['released_gpus']),
            'total_killed_tasks': len(kill_results['killed_tasks'])
        }
        
        logger.info(f"任务清理完成: 清理了 {result['total_cleaned_gpus']} 个GPU, 终止了 {result['total_killed_tasks']} 个任务")
        
        return jsonify({
            'success': True,
            'data': result
        }), 200
        
    except Exception as e:
        logger.exception(f"清理任务失败: {e}")
        return jsonify({
            'error': 'Failed to clean tasks',
            'details': str(e)
        }), 500

@app.route('/monitor/kill-all', methods=['POST'])
@require_api_token  
def kill_all_tasks():
    """强制清理所有任务（紧急情况）"""
    try:
        force = request.json.get('force', True) if request.json else True
        
        # 获取所有正在使用GPU的任务
        gpu_status = get_gpu_status()
        all_task_ids = list(gpu_status['in_use'].values())
        
        if not all_task_ids:
            return jsonify({
                'success': True,
                'data': {
                    'message': '没有找到正在运行的任务',
                    'killed_tasks': [],
                    'released_gpus': []
                }
            }), 200
        
        # 强制清理所有任务
        results = task_monitor.kill_stuck_tasks(all_task_ids, force=force)
        
        logger.warning(f"紧急清理所有任务: 终止了 {len(results['killed_tasks'])} 个任务, 释放了 {len(results['released_gpus'])} 个GPU")
        
        return jsonify({
            'success': True,
            'data': results
        }), 200
        
    except Exception as e:
        logger.exception(f"紧急清理失败: {e}")
        return jsonify({
            'error': 'Failed to kill all tasks',
            'details': str(e)
        }), 500

@app.route('/monitor/health', methods=['GET'])
def health_check():
    """健康检查端点（不需要认证）"""
    try:
        gpu_status = get_gpu_status()
        stuck_tasks = task_monitor.get_stuck_tasks()
        
        is_healthy = len(stuck_tasks) == 0
        
        result = {
            'healthy': is_healthy,
            'gpu_available': gpu_status['available_count'],
            'gpu_in_use': gpu_status['in_use_count'], 
            'stuck_tasks_count': len(stuck_tasks),
            'timestamp': datetime.now().isoformat()
        }
        
        status_code = 200 if is_healthy else 503
        
        return jsonify(result), status_code
        
    except Exception as e:
        logger.exception(f"健康检查失败: {e}")
        return jsonify({
            'healthy': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 503


if __name__ == '__main__':
    # For production, use a WSGI server like Gunicorn/uWSGI instead of app.run(debug=True).
    logger.info("Starting Flask API server...")
    app.run(host='0.0.0.0', port=5000, debug=True)
