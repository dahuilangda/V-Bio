import os
import json
import logging
import glob
import time
import uuid
import hashlib
import shutil
import subprocess
import io
import math
import re
import zipfile
import psutil
import signal
import base64
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from typing import Dict, List, Optional
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from celery.result import AsyncResult
import config
from celery_app import celery_app
from tasks import (
    predict_task,
    affinity_task,
    boltz2score_task,
    protenix2score_task,
    virtual_screening_task,
    lead_optimization_task,
)
from gpu_manager import get_redis_client, release_gpu, get_gpu_status
from affinity_preview import AffinityPreviewError, build_affinity_preview

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

DOCKER_CMD_TIMEOUT_SECONDS = 20

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
        seen_pids = set()
        
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'cpu_percent']):
                try:
                    cmd_tokens = proc.info['cmdline'] or []
                    cmdline = ' '.join(cmd_tokens)
                    if task_id in cmdline or f"boltz_task_{task_id}" in cmdline:
                        pid = proc.info['pid']
                        if pid in seen_pids:
                            continue
                        seen_pids.add(pid)
                        processes.append({
                            'pid': pid,
                            'name': proc.info['name'],
                            'cmd_tokens': cmd_tokens,
                            'cmdline': cmdline,
                            'create_time': datetime.fromtimestamp(proc.info['create_time']).isoformat(),
                            'cpu_percent': proc.cpu_percent()
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            process_data = self.redis_client.get(f"task_process:{task_id}")
            if process_data:
                try:
                    raw = process_data.decode('utf-8') if isinstance(process_data, (bytes, bytearray)) else process_data
                    parsed = json.loads(raw)
                    pid = int(parsed.get('pid', 0))
                    if pid > 0 and pid not in seen_pids and psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        cmd_tokens = proc.cmdline()
                        processes.append({
                            'pid': pid,
                            'name': proc.name(),
                            'cmd_tokens': cmd_tokens,
                            'cmdline': ' '.join(cmd_tokens),
                            'create_time': datetime.fromtimestamp(proc.create_time()).isoformat(),
                            'cpu_percent': proc.cpu_percent()
                        })
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"查找进程时出错: {e}")
            
        return processes

    @staticmethod
    def _task_container_name(task_id: str) -> str:
        token = ''.join(ch.lower() if ch.isalnum() else '-' for ch in task_id.strip())
        token = token.strip('-')
        if not token:
            token = hashlib.sha1(task_id.encode('utf-8')).hexdigest()[:12]
        return f"boltz-af3-{token[:48]}"

    @staticmethod
    def _docker_available() -> bool:
        return shutil.which("docker") is not None

    def _run_docker_command(self, args: List[str], timeout: int = DOCKER_CMD_TIMEOUT_SECONDS) -> str:
        result = subprocess.run(
            ["docker", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"docker {' '.join(args)} failed ({result.returncode}): {stderr}")
        return (result.stdout or "").strip()

    def _inspect_container(self, container_id: str) -> Optional[Dict]:
        try:
            raw = self._run_docker_command(["inspect", container_id], timeout=DOCKER_CMD_TIMEOUT_SECONDS)
            payload = json.loads(raw)
            if not payload:
                return None
            entry = payload[0]
            state = entry.get('State') or {}
            labels = ((entry.get('Config') or {}).get('Labels') or {})
            mounts = entry.get('Mounts') or []
            return {
                'id': str(entry.get('Id') or container_id),
                'name': str(entry.get('Name') or '').lstrip('/'),
                'running': bool(state.get('Running')),
                'status': str(state.get('Status') or ''),
                'labels': labels if isinstance(labels, dict) else {},
                'mount_sources': [str(item.get('Source') or '') for item in mounts if isinstance(item, dict)]
            }
        except Exception:
            return None

    def _discover_task_containers(self, task_id: str) -> Dict:
        result = {
            'docker_available': self._docker_available(),
            'containers': [],
            'errors': []
        }
        if not result['docker_available']:
            return result

        container_name = self._task_container_name(task_id)
        try:
            all_ids_raw = self._run_docker_command(["ps", "-a", "-q"], timeout=DOCKER_CMD_TIMEOUT_SECONDS)
        except Exception as e:
            result['errors'].append(str(e))
            return result

        all_ids = [line.strip() for line in all_ids_raw.splitlines() if line.strip()]
        for container_id in all_ids:
            details = self._inspect_container(container_id)
            if not details:
                continue
            labels = details.get('labels') or {}
            mount_sources = details.get('mount_sources') or []
            name = str(details.get('name') or '')
            label_task_id = str(labels.get('boltz.task_id') or '').strip()
            label_match = label_task_id == task_id
            mount_match = any(task_id in source for source in mount_sources)
            name_match = task_id in name or container_name in name
            if label_match or mount_match or name_match:
                result['containers'].append({
                    'id': details['id'],
                    'name': name,
                    'running': bool(details.get('running')),
                    'status': str(details.get('status') or ''),
                    'label_task_id': label_task_id
                })
        return result

    def _detect_task_backend(self, task_processes: List[Dict]) -> Optional[str]:
        for proc in task_processes:
            cmd_tokens = proc.get('cmd_tokens') or []
            if not isinstance(cmd_tokens, list):
                continue
            for index, token in enumerate(cmd_tokens):
                if os.path.basename(str(token)) != 'run_single_prediction.py':
                    continue
                args_file_path = cmd_tokens[index + 1] if index + 1 < len(cmd_tokens) else ''
                if not args_file_path or not os.path.isfile(args_file_path):
                    continue
                try:
                    with open(args_file_path, 'r', encoding='utf-8') as f:
                        args_payload = json.load(f)
                    backend = str(args_payload.get('backend') or '').strip().lower()
                    if backend:
                        return backend
                except Exception:
                    continue
        return None

    def _terminate_process_tree(self, pid: int, force: bool = False) -> bool:
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except Exception:
            return False

        try:
            descendants = root.children(recursive=True)
        except Exception:
            descendants = []

        target_by_pid = {}
        for proc in descendants + [root]:
            target_by_pid[proc.pid] = proc
        targets = list(target_by_pid.values())

        for proc in targets:
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
            except psutil.NoSuchProcess:
                continue
            except Exception:
                return False

        _, alive = psutil.wait_procs(targets, timeout=8)
        if alive:
            for proc in alive:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    continue
                except Exception:
                    return False
            _, alive_after = psutil.wait_procs(alive, timeout=5)
            if alive_after:
                return False
        return True

    def _release_gpu_for_task(self, task_id: str) -> List[str]:
        released = []
        gpu_status = get_gpu_status()
        for gpu_id, owner_task_id in gpu_status['in_use'].items():
            if owner_task_id != task_id:
                continue
            try:
                release_gpu(int(gpu_id), task_id)
                released.append(str(gpu_id))
                logger.info(f"已释放GPU {gpu_id} (任务 {task_id})")
            except Exception as e:
                logger.error(f"释放GPU {gpu_id} 时出错: {e}")
        return released

    def terminate_task_runtime(self, task_id: str, force: bool = False) -> Dict:
        result = {
            'task_id': task_id,
            'celery_revoked': False,
            'backend': None,
            'processes_found': [],
            'processes_terminated': [],
            'processes_failed': [],
            'containers_found': [],
            'containers_removed': [],
            'containers_failed': [],
            'released_gpus': [],
            'remaining_processes': [],
            'remaining_containers': [],
            'ok': False,
            'errors': []
        }

        task_processes = self._find_task_processes(task_id)
        result['processes_found'] = [proc['pid'] for proc in task_processes]
        result['backend'] = self._detect_task_backend(task_processes)

        try:
            celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM', send_event=True)
            result['celery_revoked'] = True
        except Exception as e:
            result['errors'].append(f"Failed to revoke Celery task: {e}")

        for proc in task_processes:
            pid = proc.get('pid')
            if not isinstance(pid, int):
                continue
            if self._terminate_process_tree(pid, force=force):
                result['processes_terminated'].append(pid)
            else:
                result['processes_failed'].append(pid)

        containers_snapshot = self._discover_task_containers(task_id)
        containers = containers_snapshot.get('containers') or []
        result['containers_found'] = [container.get('id') for container in containers]
        if containers_snapshot.get('errors'):
            result['errors'].extend(containers_snapshot['errors'])

        if not containers_snapshot.get('docker_available'):
            if result['backend'] in ('alphafold3', 'protenix'):
                result['errors'].append(
                    "Docker CLI unavailable; cannot guarantee container termination for the selected backend."
                )
        else:
            for container in containers:
                container_id = str(container.get('id') or '').strip()
                if not container_id:
                    continue
                try:
                    self._run_docker_command(["rm", "-f", container_id], timeout=DOCKER_CMD_TIMEOUT_SECONDS)
                    result['containers_removed'].append(container_id)
                except Exception as e:
                    result['containers_failed'].append(container_id)
                    result['errors'].append(str(e))

        try:
            self.redis_client.delete(f"task_start:{task_id}")
            self.redis_client.delete(f"task_update:{task_id}")
            self.redis_client.delete(f"task_heartbeat:{task_id}")
            self.redis_client.delete(f"task_status:{task_id}")
            self.redis_client.delete(f"task_process:{task_id}")
        except Exception as e:
            result['errors'].append(f"Failed to cleanup task redis keys: {e}")

        result['released_gpus'] = self._release_gpu_for_task(task_id)

        remaining_processes = self._find_task_processes(task_id)
        result['remaining_processes'] = [proc.get('pid') for proc in remaining_processes]

        remaining_container_snapshot = self._discover_task_containers(task_id)
        if remaining_container_snapshot.get('errors'):
            result['errors'].extend(remaining_container_snapshot['errors'])
        remaining_containers = remaining_container_snapshot.get('containers') or []
        running_containers = [container for container in remaining_containers if container.get('running')]
        result['remaining_containers'] = [container.get('id') for container in remaining_containers]

        has_active_runtime = bool(result['remaining_processes'] or running_containers)
        has_failures = bool(result['processes_failed'] or result['containers_failed'] or result['errors'])
        result['ok'] = bool(result['celery_revoked']) and not has_active_runtime and not has_failures

        return result
    
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
                termination = self.terminate_task_runtime(task_id, force=force)
                success = bool(termination.get('ok'))
                if success:
                    results['killed_tasks'].append(task_id)
                    for gpu_id in termination.get('released_gpus', []):
                        if gpu_id not in results['released_gpus']:
                            results['released_gpus'].append(gpu_id)
                else:
                    results['failed_to_kill'].append(task_id)
                    logger.error(f"终止任务 {task_id} 失败: {termination}")
            except Exception as e:
                logger.error(f"清理任务 {task_id} 时出错: {e}")
                results['failed_to_kill'].append(task_id)
        
        return results
    
    def _kill_single_task(self, task_id: str, force: bool = False) -> bool:
        """清理单个任务"""
        try:
            termination = self.terminate_task_runtime(task_id, force=force)
            return bool(termination.get('ok'))
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

# Browser clients (VBio) call this API directly, while legacy frontend uses server-side requests.
# Enable permissive CORS by default so both localhost and remote host:port frontends can submit tasks.
_cors_origins_raw = os.environ.get("BOLTZ_CORS_ALLOW_ORIGINS", "*").strip()
if _cors_origins_raw == "*":
    _cors_origin_allowlist = None
else:
    _cors_origin_allowlist = {item.strip() for item in _cors_origins_raw.split(",") if item.strip()}

def _resolve_cors_origin() -> str:
    origin = request.headers.get("Origin")
    if _cors_origin_allowlist is None:
        return origin or "*"
    if origin and origin in _cors_origin_allowlist:
        return origin
    # Fall back to first configured origin to keep browser behavior deterministic.
    return next(iter(_cors_origin_allowlist), "")

def _apply_cors_headers(response):
    origin = _resolve_cors_origin()
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept, X-API-Token, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "86400"
    vary = response.headers.get("Vary")
    response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
    return response

@app.before_request
def handle_cors_preflight():
    if request.method == "OPTIONS":
        return _apply_cors_headers(app.make_response(("", 204)))
    return None

@app.after_request
def add_cors_headers(response):
    return _apply_cors_headers(response)

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

def _find_result_archive(task_id: str) -> Optional[str]:
    """Best-effort lookup for a task's result archive on disk."""
    base_dir = app.config.get('UPLOAD_FOLDER')
    if not base_dir or not os.path.isdir(base_dir):
        return None

    candidates = [
        f"{task_id}_results.zip",
        f"{task_id}_affinity_results.zip",
        f"{task_id}_virtual_screening_results.zip",
        f"{task_id}_lead_optimization_results.zip",
    ]
    for name in candidates:
        candidate_path = os.path.join(base_dir, name)
        if os.path.exists(candidate_path):
            return name

    try:
        matches = glob.glob(os.path.join(base_dir, f"{task_id}_*.zip"))
        if matches:
            newest = max(matches, key=os.path.getmtime)
            return os.path.basename(newest)
    except Exception as exc:
        logger.warning(f"Failed to scan results directory for task {task_id}: {exc}")

    return None


def _resolve_result_archive_path(task_id: str) -> tuple[str, str]:
    """Resolve and validate the on-disk archive path for a task."""
    task_result = AsyncResult(task_id, app=celery_app)

    result_info = None
    if not task_result.ready():
        archive_name = _find_result_archive(task_id)
        if not archive_name:
            raise FileNotFoundError(f"Task has not completed yet. State: {task_result.state}")
        logger.info(f"Serving result archive for task {task_id} without Celery readiness: {archive_name}")
        result_info = {'result_file': archive_name}
    else:
        result_info = task_result.info
        if not isinstance(result_info, dict) or 'result_file' not in result_info or not result_info['result_file']:
            archive_name = _find_result_archive(task_id)
            if not archive_name:
                raise FileNotFoundError("Result file information not found in task metadata or on disk.")
            logger.info(f"Recovered result archive for task {task_id} from disk: {archive_name}")
            result_info = {'result_file': archive_name}

    filename = secure_filename(result_info['result_file'])
    directory = app.config['UPLOAD_FOLDER']
    filepath = os.path.join(directory, filename)

    abs_filepath = os.path.abspath(filepath)
    abs_upload_folder = os.path.abspath(directory)
    if not abs_filepath.startswith(abs_upload_folder):
        raise PermissionError(f"Invalid file path outside upload folder: {filepath}")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Result file not found on disk: {filepath}")

    return filename, filepath


def _choose_preferred_path(candidates: list[str]) -> Optional[str]:
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (1 if "seed-" in item.lower() else 0, len(item))
    )[0]


def _choose_best_boltz_structure_file(names: list[str]) -> Optional[str]:
    candidates: list[tuple[int, int, str]] = []
    for name in names:
        lower = name.lower()
        if not re.search(r"\.(cif|mmcif|pdb)$", lower):
            continue
        if "af3/output/" in lower:
            continue
        score = 100
        if lower.endswith(".cif"):
            score -= 5
        if "model_0" in lower or "ranked_0" in lower:
            score -= 20
        elif "model_" in lower or "ranked_" in lower:
            score -= 5
        candidates.append((score, len(name), name))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _to_finite_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _boltz_confidence_heuristic_score(path: str) -> int:
    lower = path.lower()
    score = 100
    if "confidence_" in lower:
        score -= 5
    if "model_0" in lower or "ranked_0" in lower:
        score -= 20
    elif "model_" in lower or "ranked_" in lower:
        score -= 5
    return score


def _resolve_boltz_structure_for_confidence(names: list[str], confidence_path: str) -> Optional[str]:
    base = os.path.basename(confidence_path)
    lower = base.lower()
    if not lower.startswith("confidence_") or not lower.endswith(".json"):
        return None
    structure_stem = base[len("confidence_"):-len(".json")]
    if not structure_stem.strip():
        return None

    confidence_dir = os.path.dirname(confidence_path)

    def _with_dir(file_name: str) -> str:
        return os.path.join(confidence_dir, file_name) if confidence_dir else file_name

    candidates = [
        _with_dir(f"{structure_stem}.cif"),
        _with_dir(f"{structure_stem}.mmcif"),
        _with_dir(f"{structure_stem}.pdb"),
    ]
    return next((item for item in candidates if item in names), None)


def _choose_best_boltz_files(src_zip: zipfile.ZipFile, names: list[str]) -> tuple[Optional[str], Optional[str]]:
    confidence_candidates = [
        name for name in names
        if name.lower().endswith(".json")
        and "confidence" in name.lower()
        and "af3/output/" not in name.lower()
    ]
    if not confidence_candidates:
        return _choose_best_boltz_structure_file(names), None

    scored: list[tuple[int, float, int, float, int, float, int, int, str]] = []
    for name in confidence_candidates:
        payload = None
        try:
            parsed = json.loads(src_zip.read(name))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = None

        confidence_score = _to_finite_float(payload.get("confidence_score")) if payload else None
        complex_plddt = _to_finite_float(payload.get("complex_plddt")) if payload else None
        iptm = _to_finite_float(payload.get("iptm")) if payload else None
        heuristic = _boltz_confidence_heuristic_score(name)
        scored.append(
            (
                1 if confidence_score is not None else 0,
                confidence_score if confidence_score is not None else float("-inf"),
                1 if complex_plddt is not None else 0,
                complex_plddt if complex_plddt is not None else float("-inf"),
                1 if iptm is not None else 0,
                iptm if iptm is not None else float("-inf"),
                -heuristic,
                -len(name),
                name,
            )
        )

    scored.sort(reverse=True)
    selected_confidence = scored[0][8] if scored else None
    matched_structure = (
        _resolve_boltz_structure_for_confidence(names, selected_confidence)
        if selected_confidence
        else None
    )
    selected_structure = matched_structure or _choose_best_boltz_structure_file(names)
    return selected_structure, selected_confidence


def _choose_best_af3_structure_file(names: list[str]) -> Optional[str]:
    candidates: list[tuple[int, int, str]] = []
    for name in names:
        lower = name.lower()
        if not re.search(r"\.(cif|mmcif|pdb)$", lower):
            continue
        if "af3/output/" not in lower:
            continue
        score = 100
        if lower.endswith(".cif"):
            score -= 5
        if os.path.basename(lower) == "boltz_af3_model.cif":
            score -= 30
        if "/model.cif" in lower or lower.endswith("model.cif"):
            score -= 8
        if "seed-" in lower:
            score += 8
        else:
            score -= 6
        if "predicted" in lower:
            score -= 1
        if "model" in lower:
            score -= 1
        candidates.append((score, len(name), name))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


_PROTENIX_SUMMARY_SAMPLE_RE = re.compile(r"_summary_confidence_sample_(\d+)\.json$", re.IGNORECASE)


def _choose_best_protenix_files(src_zip: zipfile.ZipFile, names: list[str]) -> tuple[Optional[str], Optional[str]]:
    canonical_structure_candidates = [
        "protenix/output/protenix_model_0.cif",
        "protenix/output/protenix_model_0.mmcif",
        "protenix/output/protenix_model_0.pdb",
    ]
    canonical_confidence = "protenix/output/confidence_protenix_model_0.json"
    structure = next((item for item in canonical_structure_candidates if item in names), None)
    if structure and canonical_confidence in names:
        return structure, canonical_confidence

    summary_candidates = [
        item
        for item in names
        if item.lower().startswith("protenix/output/")
        and item.lower().endswith(".json")
        and _PROTENIX_SUMMARY_SAMPLE_RE.search(os.path.basename(item))
    ]
    if not summary_candidates:
        return None, None

    scored: list[tuple[float, int, str]] = []
    for item in summary_candidates:
        try:
            payload = json.loads(src_zip.read(item))
            score = float(payload.get("ranking_score"))
        except Exception:
            continue
        if not math.isfinite(score):
            continue
        scored.append((score, -len(item), item))
    if not scored:
        raise RuntimeError("Protenix summary confidence files are present but ranking_score is invalid.")
    scored.sort(reverse=True)
    selected_summary = scored[0][2]
    sample_match = _PROTENIX_SUMMARY_SAMPLE_RE.search(os.path.basename(selected_summary))
    if not sample_match:
        raise RuntimeError(f"Unable to parse Protenix summary sample index from: {selected_summary}")
    sample_index = sample_match.group(1)
    summary_dir = os.path.dirname(selected_summary)
    summary_base = os.path.basename(selected_summary)
    structure_base = re.sub(
        r"_summary_confidence_sample_\d+\.json$",
        f"_sample_{sample_index}",
        summary_base,
        flags=re.IGNORECASE,
    )
    if structure_base == summary_base:
        raise RuntimeError(f"Unable to derive Protenix sample structure name from: {selected_summary}")

    structure_candidates = [
        os.path.join(summary_dir, f"{structure_base}.cif"),
        os.path.join(summary_dir, f"{structure_base}.mmcif"),
        os.path.join(summary_dir, f"{structure_base}.pdb"),
    ]
    structure = next((item for item in structure_candidates if item in names), None)
    if not structure:
        raise RuntimeError(
            f"Unable to locate Protenix structure for summary '{selected_summary}' "
            f"(expected sample index {sample_index})."
        )
    return structure, selected_summary


def _build_view_archive_bytes(source_zip_path: str) -> bytes:
    """Create a minimal view archive for UI rendering from a full result archive."""
    with zipfile.ZipFile(source_zip_path, "r") as src_zip:
        names = [name for name in src_zip.namelist() if not name.endswith("/")]
        lower_names = [name.lower() for name in names]
        is_af3 = any("af3/output/" in name for name in lower_names)
        is_protenix = any("protenix/output/" in name for name in lower_names)

        include: list[str] = []
        if is_af3:
            structure = _choose_best_af3_structure_file(names)
            if structure:
                include.append(structure)
            summary_candidates = [
                name for name in names
                if name.lower().endswith(".json")
                and "af3/output/" in name.lower()
                and "summary_confidences" in name.lower()
            ]
            confidences_candidates = [
                name for name in names
                if name.lower().endswith(".json")
                and "af3/output/" in name.lower()
                and os.path.basename(name).lower() == "confidences.json"
            ]
            summary = _choose_preferred_path(summary_candidates)
            confidences = _choose_preferred_path(confidences_candidates)
            if summary:
                include.append(summary)
            if confidences:
                include.append(confidences)
        elif is_protenix:
            structure, confidence = _choose_best_protenix_files(src_zip, names)
            if structure:
                include.append(structure)
            if confidence:
                include.append(confidence)
            protenix_metrics = next(
                (
                    name
                    for name in names
                    if name.lower() == "protenix/output/protenix2score_metrics.json"
                ),
                None,
            )
            if protenix_metrics:
                include.append(protenix_metrics)
            # Keep both canonical and legacy affinity payload locations for
            # deterministic ligand-chain context in affinity task views.
            affinity_candidates = [
                name
                for name in names
                if name.lower().endswith(".json")
                and os.path.basename(name).lower() == "affinity_data.json"
            ]
            protenix_affinity = _choose_preferred_path(affinity_candidates)
            if protenix_affinity:
                include.append(protenix_affinity)
        else:
            structure, confidence = _choose_best_boltz_files(src_zip, names)
            if structure:
                include.append(structure)
            if confidence:
                include.append(confidence)
            affinity_candidates = [
                name for name in names
                if name.lower().endswith(".json") and "affinity" in name.lower()
            ]
            if affinity_candidates:
                include.append(sorted(affinity_candidates, key=lambda item: len(item))[0])

        if not include:
            # If we cannot classify the archive type, keep original behavior.
            raise RuntimeError("Unable to build view archive: no renderable files found.")

        include_unique: list[str] = []
        seen = set()
        for item in include:
            if item in seen:
                continue
            seen.add(item)
            include_unique.append(item)

        out_buffer = io.BytesIO()
        with zipfile.ZipFile(out_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for member_name in include_unique:
                try:
                    payload = src_zip.read(member_name)
                except KeyError:
                    continue
                out_zip.writestr(member_name, payload)
        out_buffer.seek(0)
        return out_buffer.getvalue()


def _build_or_get_view_archive(source_zip_path: str) -> str:
    """Build a cached view archive and return the cached file path."""
    src_stat = os.stat(source_zip_path)
    cache_schema_version = "view-v4-boltz-confidence-ranking"
    cache_seed = f"{cache_schema_version}|{source_zip_path}|{int(src_stat.st_mtime_ns)}|{src_stat.st_size}"
    cache_key = hashlib.sha256(cache_seed.encode("utf-8")).hexdigest()[:24]
    cache_dir = Path("/tmp/boltz_result_view_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{cache_key}.zip"
    if cache_path.exists():
        return str(cache_path)

    data = _build_view_archive_bytes(source_zip_path)
    temp_path = cache_dir / f"{cache_key}.tmp"
    temp_path.write_bytes(data)
    os.replace(str(temp_path), str(cache_path))
    return str(cache_path)

def _get_tracker_status(task_id: str) -> tuple[Optional[Dict], Optional[str]]:
    """Fetch status/heartbeat from Redis when Celery state is unavailable."""
    try:
        redis_client = get_redis_client()
        status_raw = redis_client.get(f"task_status:{task_id}")
        heartbeat = redis_client.get(f"task_heartbeat:{task_id}")
        status_data = json.loads(status_raw) if status_raw else None
        return status_data, heartbeat
    except Exception as exc:
        logger.warning(f"Failed to fetch tracker status for task {task_id}: {exc}")
        return None, None

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
    if backend not in ['boltz', 'alphafold3', 'protenix']:
        logger.warning(f"Invalid backend '{backend}' provided by client {request.remote_addr}. Defaulting to 'boltz'.")
        backend = 'boltz'
    logger.info(f"backend parameter received: {backend} for client {request.remote_addr}.")
    
    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning(f"Invalid priority '{priority}' provided by client {request.remote_addr}. Defaulting to 'default'.")
        priority = 'default'

    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE
    logger.info(f"Prediction priority: {priority}, targeting queue: '{target_queue}' for client {request.remote_addr}.")

    seed_value = _parse_int(request.form.get('seed'))
    template_inputs = []
    template_meta_raw = request.form.get('template_meta')
    template_meta = []
    if template_meta_raw:
        try:
            template_meta = json.loads(template_meta_raw)
        except json.JSONDecodeError:
            logger.warning("Invalid template_meta JSON provided; ignoring template metadata.")

    meta_map = {
        entry.get("file_name"): entry
        for entry in template_meta
        if isinstance(entry, dict) and entry.get("file_name")
    }

    template_files = request.files.getlist("template_files")
    for uploaded in template_files:
        if not uploaded or not uploaded.filename:
            continue
        filename = uploaded.filename
        content_bytes = uploaded.read()
        meta = meta_map.get(filename, {})
        fmt = meta.get("format")
        if not fmt:
            lower_name = filename.lower()
            fmt = "pdb" if lower_name.endswith(".pdb") else "cif"
        template_inputs.append({
            "file_name": filename,
            "format": fmt,
            "template_chain_id": meta.get("template_chain_id"),
            "target_chain_ids": meta.get("target_chain_ids", []),
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    predict_args = {
        'yaml_content': yaml_content,
        'use_msa_server': use_msa_server,
        'model_name': model_name,
        'backend': backend,
        'seed': seed_value,
    }
    if template_inputs:
        predict_args['template_inputs'] = template_inputs

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

    if options.get('backend') and options['backend'] not in ['boltz', 'alphafold3', 'protenix']:
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


@app.route('/api/affinity/preview', methods=['POST'])
@require_api_token
def preview_affinity_complex():
    """Build a temporary target(+optional ligand) structure for Mol* preview and ligand metadata."""
    logger.info("Received affinity preview request.")

    if 'protein_file' not in request.files:
        return jsonify({'error': "Request form must contain 'protein_file' part"}), 400

    protein_file = request.files['protein_file']
    ligand_file = request.files.get('ligand_file')

    if protein_file.filename == '':
        return jsonify({'error': 'protein_file must be selected'}), 400

    try:
        protein_text = protein_file.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'error': 'Failed to decode protein_file as UTF-8 text.'}), 400
    except IOError as e:
        logger.exception("Failed to read protein_file for affinity preview: %s", e)
        return jsonify({'error': f'Failed to read protein_file: {e}'}), 400

    ligand_text = ""
    ligand_filename = ""
    if ligand_file is not None and ligand_file.filename != '':
        try:
            ligand_file.seek(0)
            try:
                ligand_text = ligand_file.read().decode('utf-8')
            except UnicodeDecodeError:
                ligand_file.seek(0)
                ligand_text = ligand_file.read().decode('utf-8', errors='replace')
            ligand_filename = secure_filename(ligand_file.filename)
        except IOError as e:
            logger.exception("Failed to read ligand_file for affinity preview: %s", e)
            return jsonify({'error': f'Failed to read ligand_file: {e}'}), 400

    protein_filename = secure_filename(protein_file.filename)

    try:
        preview = build_affinity_preview(
            protein_text=protein_text,
            protein_filename=protein_filename,
            ligand_text=ligand_text,
            ligand_filename=ligand_filename,
        )
    except AffinityPreviewError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to build affinity preview: %s", e)
        return jsonify({'error': 'Failed to generate affinity preview.', 'details': str(e)}), 500

    return jsonify(
        {
            'structure_text': preview.structure_text,
            'structure_format': preview.structure_format,
            'structure_name': preview.structure_name,
            'target_structure_text': preview.target_structure_text,
            'target_structure_format': preview.target_structure_format,
            'ligand_structure_text': preview.ligand_structure_text,
            'ligand_structure_format': preview.ligand_structure_format,
            'ligand_smiles': preview.ligand_smiles,
            'target_chain_ids': preview.target_chain_ids,
            'ligand_chain_id': preview.ligand_chain_id,
            'has_ligand': preview.has_ligand,
            'ligand_is_small_molecule': preview.ligand_is_small_molecule,
            'supports_activity': preview.supports_activity,
            'protein_filename': protein_filename,
            'ligand_filename': ligand_filename,
        }
    )


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


@app.route('/api/boltz2score', methods=['POST'])
@require_api_token
def handle_boltz2score():
    """
    Receives Boltz2Score requests (confidence; optional affinity) and dispatches Celery tasks.
    Supports complex input or separate protein/ligand inputs.
    """
    logger.info("Received Boltz2Score request.")

    target_chain = request.form.get('target_chain')
    ligand_chain = request.form.get('ligand_chain')
    requested_recycling_steps = _parse_int(request.form.get('recycling_steps'))
    requested_sampling_steps = _parse_int(request.form.get('sampling_steps'))
    requested_diffusion_samples = _parse_int(request.form.get('diffusion_samples'))
    requested_max_parallel_samples = _parse_int(request.form.get('max_parallel_samples'))
    requested_seed = _parse_int(request.form.get('seed'))
    requested_structure_refine = _parse_bool(request.form.get('structure_refine'), False)
    requested_use_msa_server = _parse_bool(request.form.get('use_msa_server'), False)
    ligand_smiles_map = {}
    ligand_smiles_map_raw = request.form.get('ligand_smiles_map')
    if ligand_smiles_map_raw:
        try:
            parsed = json.loads(ligand_smiles_map_raw)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        continue
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        ligand_smiles_map[key] = value
        except Exception as e:
            logger.error("Invalid ligand_smiles_map JSON from %s: %s", request.remote_addr, e)
            return jsonify({'error': "Invalid 'ligand_smiles_map' JSON format."}), 400
    score_args = {}

    if 'input_file' in request.files:
        input_file = request.files['input_file']
        if input_file.filename == '':
            logger.error("No selected file for 'input_file' in Boltz2Score request.")
            return jsonify({'error': 'No selected file for input_file'}), 400

        try:
            input_file_content = input_file.read().decode('utf-8')
            logger.debug("Boltz2Score input file successfully read and decoded.")
        except UnicodeDecodeError:
            logger.error(f"Failed to decode input_file as UTF-8. Client IP: {request.remote_addr}")
            return jsonify({'error': "Failed to decode input_file. Ensure it's a valid UTF-8 text file."}), 400
        except IOError as e:
            logger.exception(f"Failed to read input_file from request: {e}. Client IP: {request.remote_addr}")
            return jsonify({'error': f"Failed to read input_file: {e}"}), 400

        score_args = {
            'input_file_content': input_file_content,
            'input_filename': secure_filename(input_file.filename),
            'target_chain': target_chain,
            'ligand_chain': ligand_chain,
        }
        if ligand_smiles_map:
            score_args['ligand_smiles_map'] = ligand_smiles_map
        score_args['affinity_refine'] = _parse_bool(request.form.get('affinity_refine'), False)
        score_args['enable_affinity'] = _parse_bool(request.form.get('enable_affinity'), False)
        score_args['auto_enable_affinity'] = _parse_bool(request.form.get('auto_enable_affinity'), False)
    elif (
        'protein_file' in request.files
        or 'ligand_file' in request.files
        or request.form.get('ligand_smiles')
    ):
        if 'protein_file' not in request.files:
            logger.error("Missing protein_file in Boltz2Score separate-input request. Client IP: %s", request.remote_addr)
            return jsonify({'error': "Request form must contain 'protein_file'."}), 400

        protein_file = request.files['protein_file']
        ligand_smiles = (request.form.get('ligand_smiles') or '').strip()
        ligand_file = request.files.get('ligand_file')
        has_ligand_file = ligand_file is not None and ligand_file.filename != ''
        has_ligand_smiles = bool(ligand_smiles)

        if protein_file.filename == '':
            logger.error("No selected protein file for Boltz2Score separate-input request.")
            return jsonify({'error': 'protein_file must be selected'}), 400

        if not has_ligand_file and not has_ligand_smiles:
            logger.error("Missing ligand input in Boltz2Score separate-input request.")
            return jsonify({'error': "Provide either 'ligand_file' or non-empty 'ligand_smiles'."}), 400

        try:
            protein_file_content = protein_file.read().decode('utf-8')
            logger.debug("Boltz2Score protein input successfully read and decoded.")
        except UnicodeDecodeError:
            logger.error(f"Failed to decode protein_file as UTF-8. Client IP: {request.remote_addr}")
            return jsonify({'error': "Failed to decode protein_file. Ensure it's a valid text file."}), 400
        except IOError as e:
            logger.exception(f"Failed to read protein_file from request: {e}. Client IP: {request.remote_addr}")
            return jsonify({'error': f"Failed to read protein_file: {e}"}), 400

        output_prefix = request.form.get('output_prefix', 'complex')
        score_args = {
            'protein_file_content': protein_file_content,
            'protein_filename': secure_filename(protein_file.filename),
            'output_prefix': output_prefix,
        }

        if has_ligand_file:
            try:
                ligand_file.seek(0)
                try:
                    ligand_file_content = ligand_file.read().decode('utf-8')
                except UnicodeDecodeError:
                    ligand_file.seek(0)
                    ligand_file_content = ligand_file.read().decode('utf-8', errors='replace')
                logger.debug("Boltz2Score ligand file successfully read and decoded.")
            except IOError as e:
                logger.exception(f"Failed to read ligand_file from request: {e}. Client IP: {request.remote_addr}")
                return jsonify({'error': f"Failed to read ligand_file: {e}"}), 400

            score_args.update({
                'ligand_file_content': ligand_file_content,
                'ligand_filename': secure_filename(ligand_file.filename),
            })
        else:
            score_args.update({
                'ligand_smiles': ligand_smiles,
                'ligand_filename': secure_filename(request.form.get('ligand_filename', 'ligand_from_smiles.sdf')),
            })
        if ligand_smiles_map:
            score_args['ligand_smiles_map'] = ligand_smiles_map

        score_args['affinity_refine'] = _parse_bool(request.form.get('affinity_refine'), False)
        score_args['enable_affinity'] = _parse_bool(request.form.get('enable_affinity'), False)
        score_args['auto_enable_affinity'] = _parse_bool(request.form.get('auto_enable_affinity'), False)
        if target_chain:
            score_args['target_chain'] = target_chain
        if ligand_chain:
            score_args['ligand_chain'] = ligand_chain
    else:
        logger.error("Missing input for Boltz2Score request. Client IP: %s", request.remote_addr)
        return jsonify({
            'error': "Request form must contain 'input_file' or 'protein_file' with ('ligand_file' or 'ligand_smiles')."
        }), 400

    if requested_recycling_steps is not None:
        score_args['recycling_steps'] = requested_recycling_steps
    if requested_sampling_steps is not None:
        score_args['sampling_steps'] = requested_sampling_steps
    if requested_diffusion_samples is not None:
        score_args['diffusion_samples'] = requested_diffusion_samples
    if requested_max_parallel_samples is not None:
        score_args['max_parallel_samples'] = requested_max_parallel_samples
    if requested_seed is not None:
        score_args['seed'] = requested_seed
    score_args['structure_refine'] = requested_structure_refine
    score_args['use_msa_server'] = requested_use_msa_server

    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning(f"Invalid priority '{priority}' provided by client {request.remote_addr}. Defaulting to 'default'.")
        priority = 'default'

    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE
    logger.info(f"Boltz2Score priority: {priority}, targeting queue: '{target_queue}' for client {request.remote_addr}.")

    try:
        task = boltz2score_task.apply_async(args=[score_args], queue=target_queue)
        logger.info(f"Boltz2Score task {task.id} dispatched to queue: '{target_queue}'.")
        if isinstance(score_args.get('ligand_smiles_map'), dict) and score_args['ligand_smiles_map']:
            logger.info(
                "Boltz2Score task %s received ligand_smiles_map keys: %s",
                task.id,
                sorted(score_args['ligand_smiles_map'].keys()),
            )
    except Exception as e:
        logger.exception(f"Failed to dispatch Boltz2Score task from {request.remote_addr}: {e}")
        return jsonify({'error': 'Failed to dispatch Boltz2Score task.', 'details': str(e)}), 500

    return jsonify({'task_id': task.id}), 202


@app.route('/api/protenix2score', methods=['POST'])
@require_api_token
def handle_protenix2score():
    """
    Receives Protenix2Score requests and dispatches Celery tasks.
    Supports confidence scoring and optional Boltzina affinity estimation.
    """
    logger.info("Received Protenix2Score request.")

    if 'input_file' not in request.files:
        logger.error("Missing input_file in Protenix2Score request. Client IP: %s", request.remote_addr)
        return jsonify({'error': "Request form must contain 'input_file'."}), 400

    input_file = request.files['input_file']
    if input_file.filename == '':
        logger.error("No selected file for input_file in Protenix2Score request.")
        return jsonify({'error': 'No selected file for input_file'}), 400

    try:
        input_file_content = input_file.read().decode('utf-8')
    except UnicodeDecodeError:
        logger.error("Failed to decode Protenix2Score input_file as UTF-8. Client IP: %s", request.remote_addr)
        return jsonify({'error': "Failed to decode input_file. Ensure it's a valid UTF-8 text file."}), 400
    except IOError as e:
        logger.exception("Failed to read Protenix2Score input_file: %s", e)
        return jsonify({'error': f"Failed to read input_file: {e}"}), 400

    priority = request.form.get('priority', 'default').lower()
    if priority not in ['high', 'default']:
        logger.warning("Invalid priority '%s' for Protenix2Score, defaulting to default.", priority)
        priority = 'default'
    target_queue = config.HIGH_PRIORITY_QUEUE if priority == 'high' else config.DEFAULT_QUEUE

    target_chain = request.form.get('target_chain')
    ligand_chain = request.form.get('ligand_chain')
    ligand_smiles_map = {}
    ligand_smiles_map_raw = request.form.get('ligand_smiles_map')
    if ligand_smiles_map_raw:
        try:
            parsed = json.loads(ligand_smiles_map_raw)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        continue
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        ligand_smiles_map[key] = value
        except Exception as e:
            logger.error("Invalid ligand_smiles_map JSON from %s: %s", request.remote_addr, e)
            return jsonify({'error': "Invalid 'ligand_smiles_map' JSON format."}), 400

    score_args = {
        'input_file_content': input_file_content,
        'input_filename': secure_filename(input_file.filename),
    }

    score_args['affinity_refine'] = _parse_bool(request.form.get('affinity_refine'), False)
    score_args['enable_affinity'] = _parse_bool(request.form.get('enable_affinity'), False)
    score_args['auto_enable_affinity'] = _parse_bool(request.form.get('auto_enable_affinity'), False)
    score_args['use_msa'] = _parse_bool(request.form.get('use_msa'), True)
    score_args['use_template'] = _parse_bool(request.form.get('use_template'), False)
    logger.info(
        "Protenix2Score options: use_msa=%s, use_template=%s, enable_affinity=%s",
        score_args['use_msa'],
        score_args['use_template'],
        score_args['enable_affinity'],
    )
    if target_chain:
        score_args['target_chain'] = target_chain
    if ligand_chain:
        score_args['ligand_chain'] = ligand_chain
    if ligand_smiles_map:
        score_args['ligand_smiles_map'] = ligand_smiles_map

    seed = _parse_int(request.form.get('seed'))
    if seed is not None:
        score_args['seed'] = seed

    try:
        task = protenix2score_task.apply_async(args=[score_args], queue=target_queue)
        logger.info("Protenix2Score task %s dispatched to queue '%s'.", task.id, target_queue)
    except Exception as e:
        logger.exception("Failed to dispatch Protenix2Score task from %s: %s", request.remote_addr, e)
        return jsonify({'error': 'Failed to dispatch Protenix2Score task.', 'details': str(e)}), 500

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
        archive_name = _find_result_archive(task_id)
        if archive_name:
            response['state'] = 'SUCCESS'
            response['info'] = {
                'status': 'Task completed (result file found on server).',
                'result_file': archive_name
            }
            logger.info(f"Task {task_id} marked SUCCESS via result archive '{archive_name}'.")
        else:
            tracker_status, heartbeat = _get_tracker_status(task_id)
            if tracker_status or heartbeat:
                response['state'] = 'PROGRESS'
                status_message = (tracker_status or {}).get('details') or (tracker_status or {}).get('status') or 'Task is running'
                response['info'] = {
                    'status': status_message,
                    'message': status_message,
                    'tracker': tracker_status or {},
                    'heartbeat': heartbeat
                }
                logger.info(f"Task {task_id} is running per tracker; Celery state PENDING.")
            else:
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
        runtime_processes = task_monitor._find_task_processes(task_id)
        runtime_container_snapshot = task_monitor._discover_task_containers(task_id)
        runtime_containers = runtime_container_snapshot.get('containers') or []
        running_containers = [container for container in runtime_containers if container.get('running')]
        if runtime_processes or running_containers:
            response['state'] = 'PROGRESS'
            response['info'] = {
                'status': 'Termination in progress; runtime still active.',
                'message': 'Task revoke acknowledged but runtime process/container is still active.',
                'process_count': len(runtime_processes),
                'container_count': len(running_containers)
            }
            logger.warning(f"Task {task_id} marked REVOKED but runtime is still active.")
        else:
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
    try:
        filename, filepath = _resolve_result_archive_path(task_id)
    except FileNotFoundError as exc:
        task_result = AsyncResult(task_id, app=celery_app)
        logger.warning(f"Failed to resolve results for task {task_id}: {exc}")
        return jsonify({'error': str(exc), 'state': task_result.state}), 404
    except PermissionError as exc:
        logger.error(f"Invalid result path for task {task_id}: {exc}")
        return jsonify({'error': 'Invalid file path detected.'}), 400

    directory = app.config['UPLOAD_FOLDER']
    logger.info(f"Serving full result file {filename} for task {task_id} from {filepath}.")
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        conditional=False,
        etag=False,
        max_age=0
    )


@app.route('/results/<task_id>/view', methods=['GET'])
def download_results_view(task_id):
    """Serve a minimized result archive for UI hydration and structure viewing."""
    logger.info(f"Received view download request for task ID: {task_id}")
    try:
        _, filepath = _resolve_result_archive_path(task_id)
    except FileNotFoundError as exc:
        task_result = AsyncResult(task_id, app=celery_app)
        logger.warning(f"Failed to resolve view results for task {task_id}: {exc}")
        return jsonify({'error': str(exc), 'state': task_result.state}), 404
    except PermissionError as exc:
        logger.error(f"Invalid view result path for task {task_id}: {exc}")
        return jsonify({'error': 'Invalid file path detected.'}), 400

    try:
        view_path = _build_or_get_view_archive(filepath)
    except Exception as exc:
        logger.warning(
            "Failed to build view archive for task %s from %s: %s",
            task_id,
            filepath,
            exc,
        )
        return jsonify({'error': f'Failed to build view archive: {exc}'}), 500

    download_name = f"{task_id}_view_results.zip"
    logger.info(f"Serving view result archive for task {task_id}: {view_path}")
    return send_file(
        view_path,
        as_attachment=True,
        download_name=download_name,
        conditional=False,
        etag=False,
        max_age=0,
        mimetype="application/zip",
    )


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
        temp_save_path = f"{save_path}.upload-{uuid.uuid4().hex}.tmp"
        file.save(temp_save_path)
        os.replace(temp_save_path, save_path)
        lower_name = filename.lower()
        should_prebuild_view = (
            lower_name.endswith(".zip")
            and "virtual_screening" not in lower_name
            and "lead_optimization" not in lower_name
        )
        if should_prebuild_view:
            try:
                _build_or_get_view_archive(save_path)
            except Exception as view_exc:
                logger.warning(
                    "Failed to prebuild view archive for %s (task %s): %s",
                    filename,
                    task_id,
                    view_exc,
                )
        logger.info(f"Result file '{filename}' for task {task_id} received and saved to {save_path}.")
        return jsonify({'message': f"File '{filename}' uploaded successfully for task {task_id}"}), 200
    except IOError as e:
        try:
            if 'temp_save_path' in locals() and os.path.exists(temp_save_path):
                os.remove(temp_save_path)
        except Exception:
            pass
        logger.exception(f"Failed to save uploaded file '{filename}' for task {task_id}: {e}")
        return jsonify({'error': f"Failed to save file: {e}"}), 500
    except Exception as e:
        try:
            if 'temp_save_path' in locals() and os.path.exists(temp_save_path):
                os.remove(temp_save_path)
        except Exception:
            pass
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
    Terminates a running or queued task and verifies runtime processes/containers are stopped.
    """
    logger.info(f"Received request to terminate task ID: {task_id}")
    try:
        termination = task_monitor.terminate_task_runtime(task_id, force=True)
        if not termination.get('ok'):
            logger.error(f"Task {task_id} runtime termination failed: {termination}")
            return jsonify({
                'status': 'Task termination failed; runtime is still active.',
                'task_id': task_id,
                'terminated': False,
                'details': termination
            }), 409

        logger.info(f"Task {task_id} runtime terminated successfully.")
        return jsonify({
            'status': 'Task terminated successfully.',
            'task_id': task_id,
            'terminated': True,
            'details': termination
        }), 200
    except Exception as e:
        logger.exception(f"Failed to terminate task {task_id}: {e}")
        return jsonify({'error': 'Failed to terminate task runtime.', 'details': str(e)}), 500


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
