from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

import psutil

from backend.core import config
from backend.core.celery_app import celery_app
from gpu_manager import get_gpu_status, get_redis_client, release_gpu

TASK_CANCELLED_KEY_PREFIX = 'task_cancelled:'
TASK_CANCELLED_TTL_SECONDS = 14 * 24 * 3600


class TaskMonitor:
    """任务监控和清理工具。"""

    def __init__(self, logger, docker_cmd_timeout_seconds: int = 20):
        self.logger = logger
        self.docker_cmd_timeout_seconds = docker_cmd_timeout_seconds
        self.redis_client = get_redis_client()

    
    
    def _find_task_processes(self, task_id: str) -> List[Dict]:
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
                            'cpu_percent': proc.cpu_percent(),
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
                            'cpu_percent': proc.cpu_percent(),
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # PID vanished/restricted is expected; corrupt task_process blobs
                    # propagate to the outer handler.
                    pass
        except Exception as exc:
            self.logger.error('查找进程时出错: %s', exc)

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
        return shutil.which('docker') is not None

    def _run_docker_command(self, args: List[str], timeout: Optional[int] = None) -> str:
        actual_timeout = timeout if timeout is not None else self.docker_cmd_timeout_seconds
        result = subprocess.run(
            ['docker', *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=actual_timeout,
        )
        if result.returncode != 0:
            stderr = (result.stderr or '').strip()
            raise RuntimeError(f"docker {' '.join(args)} failed ({result.returncode}): {stderr}")
        return (result.stdout or '').strip()

    def _inspect_container(self, container_id: str) -> Optional[Dict]:
        try:
            raw = self._run_docker_command(['inspect', container_id], timeout=self.docker_cmd_timeout_seconds)
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
                'mount_sources': [str(item.get('Source') or '') for item in mounts if isinstance(item, dict)],
            }
        except Exception as exc:
            # "No such container" is expected; anything else (daemon down,
            # malformed output) gets logged.
            if 'No such container' not in str(exc):
                self.logger.warning('docker inspect failed for %s: %s', container_id, exc)
            return None

    def _discover_task_containers(self, task_id: str) -> Dict:
        result = {'docker_available': self._docker_available(), 'containers': [], 'errors': []}
        if not result['docker_available']:
            return result

        container_name = self._task_container_name(task_id)
        try:
            all_ids_raw = self._run_docker_command(['ps', '-a', '-q'], timeout=self.docker_cmd_timeout_seconds)
        except Exception as exc:
            result['errors'].append(str(exc))
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
                    'label_task_id': label_task_id,
                })
        return result

    def _detect_task_backend(self, task_processes: List[Dict]) -> Optional[str]:
        for proc in task_processes:
            cmd_tokens = proc.get('cmd_tokens') or []
            if not isinstance(cmd_tokens, list):
                continue
            for index, token in enumerate(cmd_tokens):
                token_text = str(token)
                is_script = os.path.basename(token_text) == 'run_single_prediction.py'
                is_module = token_text == "backend.runtime.run_single_prediction" and index > 0 and str(cmd_tokens[index - 1]) == "-m"
                if not is_script and not is_module:
                    continue
                args_file_path = cmd_tokens[index + 1] if index + 1 < len(cmd_tokens) else ''
                if not args_file_path or not os.path.isfile(args_file_path):
                    continue
                try:
                    with open(args_file_path, 'r', encoding='utf-8') as fh:
                        args_payload = json.load(fh)
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

        target_by_pid = {proc.pid: proc for proc in descendants + [root]}
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
                self.logger.info('已释放GPU %s (任务 %s)', gpu_id, task_id)
            except Exception as exc:
                self.logger.error('释放GPU %s 时出错: %s', gpu_id, exc)
        return released

    def _revoke_peptide_subtasks(self, parent_task_id: str, force: bool = False) -> Dict[str, List[str]]:
        result = {
            'found': [],
            'revoked': [],
            'failed': [],
        }
        key = f"{config.PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX}{str(parent_task_id or '').strip()}"
        if not parent_task_id:
            return result
        try:
            task_ids = self.redis_client.smembers(key) or set()
        except Exception as exc:
            result['failed'].append(f"read_registry_failed:{exc}")
            return result

        normalized_ids = [str(item or '').strip() for item in task_ids if str(item or '').strip()]
        result['found'] = normalized_ids
        for subtask_id in normalized_ids:
            try:
                if force:
                    celery_app.control.revoke(subtask_id, terminate=True, signal='SIGTERM', send_event=True)
                else:
                    celery_app.control.revoke(subtask_id, terminate=False, send_event=True)
                result['revoked'].append(subtask_id)
            except Exception as exc:
                result['failed'].append(f"{subtask_id}:{exc}")
        try:
            self.redis_client.delete(key)
        except Exception:
            pass
        return result

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
            'peptide_subtasks_found': [],
            'peptide_subtasks_revoked': [],
            'peptide_subtasks_failed': [],
            'ok': False,
            'errors': [],
        }

        try:
            self.redis_client.setex(
                f'{TASK_CANCELLED_KEY_PREFIX}{task_id}',
                TASK_CANCELLED_TTL_SECONDS,
                'cancelled',
            )
        except Exception as exc:
            result['errors'].append(f'Failed to write cancellation marker: {exc}')

        task_processes = self._find_task_processes(task_id)
        result['processes_found'] = [proc['pid'] for proc in task_processes]
        result['backend'] = self._detect_task_backend(task_processes)

        try:
            celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM', send_event=True)
            result['celery_revoked'] = True
        except Exception as exc:
            result['errors'].append(f'Failed to revoke Celery task: {exc}')

        peptide_revocation = self._revoke_peptide_subtasks(task_id, force=force)
        result['peptide_subtasks_found'] = peptide_revocation.get('found') or []
        result['peptide_subtasks_revoked'] = peptide_revocation.get('revoked') or []
        result['peptide_subtasks_failed'] = peptide_revocation.get('failed') or []

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
            if result['backend'] in ('alphafold3', 'protenix', 'nesso'):
                result['errors'].append('Docker CLI unavailable; cannot guarantee container termination for the selected backend.')
        else:
            for container in containers:
                container_id = str(container.get('id') or '').strip()
                if not container_id:
                    continue
                try:
                    self._run_docker_command(['rm', '-f', container_id], timeout=self.docker_cmd_timeout_seconds)
                    result['containers_removed'].append(container_id)
                except Exception as exc:
                    result['containers_failed'].append(container_id)
                    result['errors'].append(str(exc))

        try:

            self.redis_client.delete(f'task_heartbeat:{task_id}')
            self.redis_client.delete(f'task_status:{task_id}')
            self.redis_client.delete(f'task_process:{task_id}')
        except Exception as exc:
            result['errors'].append(f'Failed to cleanup task redis keys: {exc}')

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

    
    