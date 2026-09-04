import os
import sys
import glob
import traceback
import tempfile
import json
import subprocess
import shutil
import logging
import signal
import threading
import time
import re
import base64
import zipfile
import shlex
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from werkzeug.utils import secure_filename

import requests
from celery.exceptions import Ignore
from backend.core import config
from backend.core.celery_app import celery_app
from backend.monitoring.event_transport import publish_task_heartbeat, publish_task_status
from backend.services.common_utils import coerce_bool
from gpu_manager import (
    acquire_gpu,
    release_gpu,
    get_redis_client,
    register_non_peptide_gpu_waiter,
    unregister_non_peptide_gpu_waiter,
)

BASE_DIR = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = BASE_DIR / "capabilities"





def _ensure_repo_root_on_path() -> Path | None:
    """Ensure the repo root (containing capabilities/) is on sys.path."""
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    if CAPABILITIES_DIR.is_dir() and str(CAPABILITIES_DIR) not in sys.path:
        sys.path.insert(0, str(CAPABILITIES_DIR))
    return BASE_DIR

_ensure_repo_root_on_path()


try:
    import psutil
except ImportError:
    psutil = None

# Configure standard logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Subprocess timeout defaults. Peptide parent/candidate workflows support
# disabling hard timeouts entirely so queue contention does not kill long runs.
SUBPROCESS_TIMEOUT = int(getattr(config, "PREDICTION_SUBPROCESS_TIMEOUT_SECONDS", 10800) or 10800)
PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT = int(
    getattr(config, "PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS", 0) or 0
)
PEPTIDE_PARENT_SUBPROCESS_TIMEOUT = int(
    getattr(config, "PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS", 0) or 0
)
PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS = int(
    getattr(config, "PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS", 30 * 60) or (30 * 60)
)
PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS = int(
    getattr(config, "PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS", 30 * 60) or (30 * 60)
)
HEARTBEAT_INTERVAL = 60  # 心跳间隔（秒）
PROGRESS_TTL_SECONDS = 3600
TASK_STATUS_TTL_SECONDS = 24 * 3600
PROGRESS_UPDATE_INTERVAL = 20
MAX_STATUS_DETAILS_CHARS = 4_000
MAX_EXCEPTION_MESSAGE_CHARS = 20_000
MAX_TRACEBACK_CHARS = 40_000
MAX_STDIO_TAIL_CHARS = 12_000
TASK_CANCELLED_KEY_PREFIX = "task_cancelled:"
TASK_CANCELLED_TTL_SECONDS = 14 * 24 * 3600


def _task_cancelled_key(task_id: str) -> str:
    return f"{TASK_CANCELLED_KEY_PREFIX}{str(task_id or '').strip()}"


def _is_task_cancelled(redis_client: Any, task_id: str) -> bool:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return False
    try:
        return bool(redis_client.exists(_task_cancelled_key(normalized_task_id)))
    except Exception as exc:
        logger.warning("Task %s: Failed to read cancellation marker: %s", normalized_task_id, exc)
        return False


def _raise_if_task_cancelled(self: Any, redis_client: Any, task_id: str) -> None:
    if not _is_task_cancelled(redis_client, task_id):
        return
    logger.info("Task %s: Cancellation marker found before execution; acking without GPU work.", task_id)
    raise Ignore()


def _resolve_worker_temp_root() -> str:
    """
    Resolve a host-visible temp root for Docker-outside-of-Docker workflows.
    Keep orchestration staging under RESULTS_BASE_DIR so worker-visible absolute
    paths match the host daemon view.
    """
    raw_root = str(os.environ.get("WORKER_SHARED_TMP_ROOT", "") or "").strip()
    if raw_root:
        return raw_root
    results_base_dir = Path(str(getattr(config, "RESULTS_BASE_DIR", "") or "")).expanduser()
    if not str(results_base_dir).strip():
        results_base_dir = Path('/data/boltz_central_results')
    return str(results_base_dir / '_runtime_tmp')


def _mk_task_temp_dir(prefix: str) -> str:
    temp_root = _resolve_worker_temp_root()
    os.makedirs(temp_root, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=temp_root)


def _revoke_registered_peptide_subtasks(parent_task_id: str, *, terminate: bool = True) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "found": [],
        "revoked": [],
        "failed": [],
    }
    task_token = str(parent_task_id or "").strip()
    if not task_token:
        return result

    try:
        redis_client = get_redis_client()
    except Exception as exc:
        result["failed"].append(f"redis_unavailable:{exc}")
        return result

    key = f"{config.PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX}{task_token}"
    try:
        raw_members = redis_client.smembers(key) or set()
    except Exception as exc:
        result["failed"].append(f"registry_read_failed:{exc}")
        return result

    subtask_ids = [str(item or "").strip() for item in raw_members if str(item or "").strip()]
    result["found"] = subtask_ids
    for subtask_id in subtask_ids:
        try:
            celery_app.control.revoke(
                subtask_id,
                terminate=terminate,
                signal="SIGTERM" if terminate else None,
                send_event=True,
            )
            result["revoked"].append(subtask_id)
        except Exception as exc:
            result["failed"].append(f"{subtask_id}:{exc}")

    try:
        redis_client.delete(key)
    except Exception as exc:
        logger.warning("Failed to clear revoked-subtask registry key %s: %s", key, exc)

    return result


def _acquire_gpu_with_non_peptide_wait_registration(task_id: str, timeout: int = 3600) -> int:
    """
    Register non-peptide waiting intent before blocking on GPU allocation.
    This enables peptide subtask workers to yield and avoid starving regular tasks.
    """
    wait_registered = False
    try:
        register_non_peptide_gpu_waiter(task_id)
        wait_registered = True
    except Exception as exc:
        logger.warning("Task %s: Failed to register non-peptide GPU waiter: %s", task_id, exc)

    try:
        return acquire_gpu(task_id=task_id, timeout=timeout)
    finally:
        if wait_registered:
            try:
                unregister_non_peptide_gpu_waiter(task_id)
            except Exception as exc:
                logger.warning("Task %s: Failed to unregister non-peptide GPU waiter: %s", task_id, exc)

BOLTZ2SCORE_DEFAULT_RECYCLING_STEPS = 20
BOLTZ2SCORE_DEFAULT_SAMPLING_STEPS = 1
BOLTZ2SCORE_DEFAULT_DIFFUSION_SAMPLES = 1
BOLTZ2SCORE_DEFAULT_MAX_PARALLEL_SAMPLES = 1
BOLTZ2SCORE_DEFAULT_STRUCTURE_REFINE = False
BOLTZ2SCORE_DEFAULT_SEED = 42
BOLTZ2SCORE_REFINE_RECYCLING_STEPS = 3
BOLTZ2SCORE_REFINE_SAMPLING_STEPS = 200
BOLTZ2SCORE_REFINE_DIFFUSION_SAMPLES = 5


def _truncate_text(value, limit: int, *, prefer_tail: bool = False) -> str:
    """Return a bounded-length string representation for Redis/Celery metadata."""
    text = "" if value is None else str(value)
    if limit <= 0 or len(text) <= limit:
        return text

    marker = f"\n...[truncated {len(text) - limit} chars]...\n"
    if len(marker) >= limit:
        return text[-limit:] if prefer_tail else text[:limit]

    keep = limit - len(marker)
    if prefer_tail:
        return marker + text[-keep:]
    return text[:keep] + marker


def _format_subprocess_failure(task_label: str, task_id: str, returncode: int, stderr: str, stdout: str) -> str:
    """Build bounded subprocess failure message to avoid oversized backend payloads."""
    stderr_tail = _truncate_text(stderr, MAX_STDIO_TAIL_CHARS, prefer_tail=True)
    stdout_tail = _truncate_text(stdout, MAX_STDIO_TAIL_CHARS, prefer_tail=True)
    return (
        f"Subprocess for {task_label} {task_id} failed with exit code {returncode}.\n"
        f"Stderr (tail):\n{stderr_tail}\n"
        f"Stdout (tail):\n{stdout_tail}"
    )


def _sanitize_docker_extra_args(raw_args: list[str]) -> list[str]:
    """Drop malformed -e/--env tokens to avoid breaking docker image resolution."""
    sanitized: list[str] = []
    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token in {"--env", "-e"}:
            if index + 1 >= len(raw_args):
                logger.warning("Ignore malformed docker arg %s without value.", token)
                index += 1
                continue
            value = raw_args[index + 1]
            if "=" not in value:
                logger.warning("Ignore malformed docker env arg %s %s (expect KEY=VALUE).", token, value)
                index += 2
                continue
            sanitized.extend([token, value])
            index += 2
            continue
        sanitized.append(token)
        index += 1
    return sanitized


def _collect_gpu_device_group_ids() -> list[int]:
    """Collect host group ids for NVIDIA device files so docker can read GPU nodes."""
    candidate_nodes = [
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia-uvm"),
        Path("/dev/nvidia-uvm-tools"),
    ]
    candidate_nodes.extend(sorted(Path("/dev").glob("nvidia[0-9]*")))
    if Path("/dev/dri").exists():
        candidate_nodes.extend(sorted(Path("/dev/dri").glob("renderD*")))

    group_ids: list[int] = []
    for node in candidate_nodes:
        try:
            stat_result = node.stat()
        except FileNotFoundError:
            continue
        gid = stat_result.st_gid
        if gid not in group_ids:
            group_ids.append(gid)
    return group_ids


def _make_task_container_name(task_id: str, runtime_label: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(task_id or "").strip()).strip(".-_").lower()
    if not token:
        token = "task"
    runtime_token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(runtime_label or "runtime")).strip(".-_").lower()
    if not runtime_token:
        runtime_token = "runtime"
    return f"vbio-{runtime_token}-{token[:40]}"


def _terminate_task_container(container_name: str) -> None:
    if not container_name:
        return
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception as exc:
        logger.warning("Failed to remove task container %s: %s", container_name, exc)


def _terminate_task_containers_by_task_id(task_id: str) -> None:
    """
    Best-effort cleanup for all runtime containers tied to a task.
    This protects against orphaned `docker run` containers when upper-level subprocesses are killed.
    """
    task_token = str(task_id or "").strip()
    if not task_token:
        return
    try:
        query = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=boltz.task_id={task_token}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        container_ids = [line.strip() for line in str(query.stdout or "").splitlines() if line.strip()]
        for container_id in container_ids:
            _terminate_task_container(container_id)
        if container_ids:
            logger.info(
                "Task %s: Force-cleaned runtime containers by label: %s",
                task_token,
                ", ".join(container_ids),
            )
    except Exception as exc:
        logger.debug("Task %s: Failed to cleanup runtime containers by label: %s", task_token, exc)


def _docker_args_has_flag(args: list[str], flag: str) -> bool:
    normalized = str(flag or "").strip()
    if not normalized:
        return False
    for token in args:
        if token == normalized or token.startswith(f"{normalized}="):
            return True
    return False


def _build_gpu_docker_python_command(
    *,
    task_id: str,
    gpu_id: int,
    task_temp_dir: str,
    runtime_label: str,
    python_entry: list[str],
) -> tuple[list[str], str]:
    image = str(getattr(config, "BOLTZ2_DOCKER_IMAGE", "") or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法启动 Docker 任务。")

    raw_extra_args = shlex.split(str(getattr(config, "BOLTZ2_DOCKER_EXTRA_ARGS", "") or ""))
    extra_args = _sanitize_docker_extra_args(raw_extra_args)
    runtime_overridden = any(token == "--runtime" for token in extra_args)
    shm_size = str(getattr(config, "BOLTZ2_DOCKER_SHM_SIZE", "16g") or "").strip()

    container_name = _make_task_container_name(task_id, runtime_label)
    command = ["docker", "run", "--rm"]
    command.extend(["--name", container_name])
    command.extend(["--label", f"boltz.task_id={task_id}"])
    command.extend(["--label", f"boltz.runtime={runtime_label}"])
    if not runtime_overridden:
        command.extend(["--runtime", "nvidia"])
    if shm_size and not _docker_args_has_flag(extra_args, "--shm-size") and not _docker_args_has_flag(extra_args, "--ipc"):
        command.extend(["--shm-size", shm_size])
    command.extend(["--gpus", f"device={int(gpu_id)}"])
    command.extend(["--volume", f"{task_temp_dir}:{task_temp_dir}"])
    command.extend(["--volume", f"{BASE_DIR}:/workspace/vbio:ro"])
    command.extend(["--workdir", "/workspace/vbio"])
    command.extend(["--env", "PYTHONPATH=/workspace/vbio"])
    command.extend(["--env", f"BOLTZ_TASK_ID={task_id}"])
    msa_server_url = str(getattr(config, "MSA_SERVER_URL", "") or "").strip()
    if msa_server_url:
        command.extend(["--env", f"MSA_SERVER_URL={msa_server_url}"])

    host_cache_dir = str(getattr(config, "BOLTZ2_HOST_CACHE_DIR", "") or "").strip()
    container_cache_dir = str(getattr(config, "BOLTZ2_CONTAINER_CACHE_DIR", "/root/.boltz") or "/root/.boltz").strip() or "/root/.boltz"
    if host_cache_dir:
        os.makedirs(host_cache_dir, exist_ok=True)
        command.extend(["--volume", f"{host_cache_dir}:{container_cache_dir}"])
        command.extend(["--env", f"BOLTZ_CACHE={container_cache_dir}"])

    # MSA sequence cache: mount a host directory from the big /data partition so
    # task containers stop growing their writable layers with ~MB-sized .a3m
    # files (the old default /tmp/boltz_msa_cache accumulated 7+ GB per host).
    msa_cache_host = str(getattr(config, "BOLTZ_MSA_CACHE_DIR", "") or "").strip()
    if msa_cache_host:
        try:
            os.makedirs(msa_cache_host, exist_ok=True)
            command.extend(["--volume", f"{msa_cache_host}:{msa_cache_host}"])
            command.extend(["--env", f"BOLTZ_MSA_CACHE_DIR={msa_cache_host}"])
        except OSError:
            logger.warning("Task %s: could not prepare MSA cache dir %s; using container default.", task_id, msa_cache_host)

    command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for gid in _collect_gpu_device_group_ids():
        command.extend(["--group-add", str(gid)])

    command.extend(extra_args)
    command.append(image)
    command.extend(python_entry)
    return command, container_name


def _should_skip_large_result_file(file_name: str) -> bool:
    """Filter heavy intermediate confidence arrays not needed by the UI.

    The Boltz writer emits full PAE/PDE matrices per diffusion sample as
    ``pae_<record>_model_<n>.npz`` / ``pde_...npz`` (~4 MB per sample, ~2/3 of
    the archive). Every consumer downstream (frontend bundle parser, /view
    archive builder, excel export) reads PAE/PDE summaries from the confidence
    JSON, never from these arrays — skipping them cuts a 16-sample dock archive
    from ~78 MB to ~10 MB. ``plddt_*.npz`` stay: they are a few KB each.
    Historical ``*_data_`` prefixes are kept for archives produced by older
    writers.
    """
    lower = file_name.lower()
    if not lower.endswith(".npz"):
        return False
    return (
        lower.startswith("pae_")
        or lower.startswith("pde_")
        or lower.startswith("pae_data_")
        or lower.startswith("pde_data_")
        or lower.startswith("plddt_data_")
    )


def _build_failure_meta(error: Exception) -> dict:
    """Create bounded Celery FAILURE meta payload."""
    return {
        'exc_type': type(error).__name__,
        'exc_message': _truncate_text(error, MAX_EXCEPTION_MESSAGE_CHARS),
        'traceback': _truncate_text(traceback.format_exc(), MAX_TRACEBACK_CHARS),
    }


def _coerce_positive_int(value: Any, default: int, min_value: int = 1) -> int:
    """Parse int-like value with lower bound, otherwise return default."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else default


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
        # TaskMonitor._analyze_task reads task_start/task_update to detect stuck tasks
        # (max duration / no-progress); nothing wrote them, so both checks were dead.
        try:
            self.redis_client.setex(f"task_start:{self.task_id}", 86400, datetime.now().isoformat())
            self.redis_client.setex(f"task_update:{self.task_id}", 86400, datetime.now().isoformat())
        except Exception as e:
            logger.warning(f"Task {self.task_id}: Failed to record task start timestamps: {e}")
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_worker, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"Task {self.task_id}: Started heartbeat monitoring")
    
    def stop_heartbeat(self, *, clear_status: bool = False):
        """停止心跳线程.

        By default, keep task_status for a while so API /status can still infer
        terminal FAILURE/SUCCESS when Celery backend state is temporarily stale.
        """
        self._stop_heartbeat = True
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        try:
            self.redis_client.delete(self.heartbeat_key)
            self.redis_client.delete(self.process_key)
            if clear_status:
                self.redis_client.delete(self.status_key)
        except Exception as e:
            logger.warning(f"Failed to cleanup Redis keys for task {self.task_id}: {e}")
    
    def _heartbeat_worker(self):
        """心跳工作线程。

        Resilient loop: a single Redis blip used to kill the heartbeat permanently while
        the task kept running for hours — every liveness check (monitor, /status) then
        saw a live task as dead. Retry with capped backoff; only a stop signal or a
        sustained outage (consecutive failures) ends the thread.
        """
        consecutive_failures = 0
        while not self._stop_heartbeat:
            try:
                current_time = datetime.now().isoformat()
                self.redis_client.setex(self.heartbeat_key, HEARTBEAT_INTERVAL * 2, current_time)
                publish_task_heartbeat(self.redis_client, task_id=self.task_id)
                consecutive_failures = 0
                time.sleep(HEARTBEAT_INTERVAL)
            except Exception as e:
                consecutive_failures += 1
                logger.error(
                    f"Heartbeat error for task {self.task_id} (consecutive failures: {consecutive_failures}): {e}"
                )
                if self._stop_heartbeat or consecutive_failures >= 30:
                    break
                time.sleep(min(HEARTBEAT_INTERVAL, 5 * consecutive_failures))
    
    def update_status(self, status, details=None, payload: Optional[dict] = None):
        """更新任务状态"""
        try:
            status_data = {
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "details": _truncate_text(details, MAX_STATUS_DETAILS_CHARS)
            }
            if isinstance(payload, dict) and payload:
                status_data["payload"] = payload
            self.redis_client.setex(self.status_key, TASK_STATUS_TTL_SECONDS, json.dumps(status_data))
            # Refresh the no-progress clock TaskMonitor uses for stuck detection.
            self.redis_client.setex(f"task_update:{self.task_id}", 86400, status_data["timestamp"])
            publish_task_status(
                self.redis_client,
                task_id=self.task_id,
                status=status,
                details_text=status_data["details"],
                details=payload,
            )
            logger.info(f"Task {self.task_id}: Status updated to {status}")
        except Exception as e:
            logger.error(f"Failed to update status for task {self.task_id}: {e}")
    
    def register_process(self, pid):
        """注册进程ID"""
        try:
            pgid = None
            try:
                pgid = os.getpgid(pid)
            except Exception:
                pgid = None
            process_data = {
                "pid": pid,
                "pgid": pgid,
                "start_time": datetime.now().isoformat()
            }
            self.redis_client.setex(self.process_key, 3600, json.dumps(process_data))
            logger.info(f"Task {self.task_id}: Registered process {pid}")
        except Exception as e:
            logger.error(f"Failed to register process for task {self.task_id}: {e}")



def _read_json_record(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            parsed = json.load(f)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _read_int_metric(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _read_float_metric(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not (parsed == parsed and parsed not in (float('inf'), float('-inf'))):
        return None
    return parsed


def _normalize_peptide_gpu_ids(raw_gpu_ids: Any) -> list[int]:
    if isinstance(raw_gpu_ids, int):
        return [raw_gpu_ids] if raw_gpu_ids >= 0 else []

    values: list[Any]
    if isinstance(raw_gpu_ids, str):
        values = [token for token in re.split(r"[\s,]+", raw_gpu_ids) if token]
    elif isinstance(raw_gpu_ids, (list, tuple, set)):
        values = list(raw_gpu_ids)
    else:
        return []

    normalized: list[int] = []
    seen: set[int] = set()
    for item in values:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed < 0 or parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return normalized


def _gpu_pool_device_count() -> int:
    """Authoritative GPU-pool size (valid devices registered at pool init).

    No config/env fallback tiers: the pool is what actually schedules
    candidate subtasks, so its own registry is the single source of truth.
    A missing/empty pool is a hard error, not a guess.
    """
    from gpu_manager import get_gpu_status as get_gpu_status_fn

    return int((get_gpu_status_fn() or {}).get("valid_count") or 0)


def _collect_peptide_design_setup_meta(predict_args: dict) -> dict:
    options = predict_args.get('peptide_design_options')
    if not isinstance(options, dict):
        return {}

    setup = {}
    mode = str(options.get('peptideDesignMode') or options.get('peptide_design_mode') or '').strip().lower()
    if mode:
        setup['design_mode'] = mode

    binder_length = _read_int_metric(options.get('peptideBinderLength', options.get('peptide_binder_length')))
    if binder_length is not None:
        setup['binder_length'] = binder_length

    iterations = _read_int_metric(options.get('peptideIterations', options.get('peptide_iterations')))
    if iterations is not None:
        setup['iterations'] = iterations
        setup['total_generations'] = iterations

    population_size = _read_int_metric(options.get('peptidePopulationSize', options.get('peptide_population_size')))
    if population_size is not None:
        setup['population_size'] = population_size

    elite_size = _read_int_metric(options.get('peptideEliteSize', options.get('peptide_elite_size')))
    if elite_size is not None:
        setup['elite_size'] = elite_size

    mutation_rate = _read_float_metric(options.get('peptideMutationRate', options.get('peptide_mutation_rate')))
    if mutation_rate is not None:
        setup['mutation_rate'] = mutation_rate

    return setup


def _normalize_gpu_ids_for_meta(gpu_value: Any) -> list[int]:
    if isinstance(gpu_value, int):
        return [gpu_value]
    if isinstance(gpu_value, (list, tuple)):
        normalized: list[int] = []
        for item in gpu_value:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            normalized.append(parsed)
        return normalized
    return []


def _build_peptide_runtime_meta(predict_args: dict, gpu_info: Any) -> dict:
    gpu_ids = _normalize_gpu_ids_for_meta(gpu_info)
    primary_gpu = gpu_ids[0] if gpu_ids else -1
    if len(gpu_ids) > 1:
        status_text = f"Running peptide design on GPUs {', '.join(str(item) for item in gpu_ids)}"
    elif primary_gpu >= 0:
        status_text = f"Running prediction on GPU {primary_gpu}"
    else:
        status_text = "Running peptide design"

    runtime_meta = {
        'status': status_text
    }
    if gpu_ids:
        runtime_meta['gpu_ids'] = gpu_ids
        runtime_meta['gpu_id'] = primary_gpu

    setup_payload = _collect_peptide_design_setup_meta(predict_args)
    progress_path = str(predict_args.get('peptide_progress_path') or '').strip()
    progress_payload = _read_json_record(progress_path)
    peptide_progress_raw = progress_payload.get('peptide_design') if isinstance(progress_payload, dict) else {}
    peptide_progress = peptide_progress_raw if isinstance(peptide_progress_raw, dict) else {}

    status_override = str(
        peptide_progress.get('current_status')
        or peptide_progress.get('status_message')
        or ''
    ).strip()
    if status_override:
        runtime_meta['status'] = status_override

    merged_peptide = {
        **setup_payload,
        **peptide_progress
    }
    if merged_peptide:
        runtime_meta['peptide_design'] = merged_peptide

    progress_meta = {}
    for key in (
        'current_generation',
        'generation',
        'total_generations',
        'completed_tasks',
        'pending_tasks',
        'total_tasks',
        'best_score',
        'progress_percent',
        'current_status',
        'status_message',
        'generation_total_tasks',
        'generation_completed_tasks',
        'generation_running_tasks',
        'generation_queued_tasks',
        'elapsed_seconds',
        'estimated_remaining_seconds',
        'estimated_completion_time',
        'candidates_evaluated',
        'adaptive_mutation_rate',
        'stagnant_generations',
        'current_best_sequences',
        'best_sequences',
        'candidates',
        'candidate_count',
    ):
        if key in peptide_progress:
            progress_meta[key] = peptide_progress[key]

    if progress_meta:
        runtime_meta['progress'] = progress_meta

    options = predict_args.get('peptide_design_options')
    if isinstance(options, dict) and options:
        runtime_meta['request'] = {
            'options': options
        }

    return runtime_meta


def _normalize_prediction_workflow(raw_workflow: Any) -> str:
    workflow = str(raw_workflow or '').strip().lower()
    if workflow in {'peptide', 'peptide_designer', 'designer'}:
        return 'peptide_design'
    if workflow == 'peptide_design':
        return workflow
    return 'prediction'


def _resolve_peptide_parent_subprocess_timeout(predict_args: dict) -> int:
    options = predict_args.get('peptide_design_options')
    if not isinstance(options, dict):
        options = {}

    iterations = _read_int_metric(options.get('peptideIterations', options.get('peptide_iterations')))
    population_size = _read_int_metric(options.get('peptidePopulationSize', options.get('peptide_population_size')))
    safe_iterations = max(1, iterations if isinstance(iterations, int) else 12)
    safe_population = max(1, population_size if isinstance(population_size, int) else 16)
    total_candidates = safe_iterations * safe_population

    # 候选子任务全量入队，由 GPU 池调度：波次只取决于池的设备数这一个权威值。
    pool_devices = _gpu_pool_device_count()
    if pool_devices <= 0:
        raise RuntimeError(
            "GPU pool has no valid devices; cannot schedule peptide design candidate subtasks."
        )
    wave_count = safe_iterations * max(1, -(-safe_population // pool_devices))
    per_wave_timeout = max(
        60,
        PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS,
        PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT,
    )
    estimated_timeout = (
        wave_count * per_wave_timeout
        + max(0, PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS)
    )
    if PEPTIDE_PARENT_SUBPROCESS_TIMEOUT <= 0:
        logger.info(
            "Resolved peptide parent subprocess timeout: disabled "
            "(estimated=%ss iterations=%s population=%s total_candidates=%s pool_devices=%s per_wave=%ss buffer=%ss)",
            estimated_timeout,
            safe_iterations,
            safe_population,
            total_candidates,
            pool_devices,
            per_wave_timeout,
            PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS,
        )
        return 0

    effective_timeout = max(SUBPROCESS_TIMEOUT, min(estimated_timeout, PEPTIDE_PARENT_SUBPROCESS_TIMEOUT))

    logger.info(
        "Resolved peptide parent subprocess timeout: effective=%ss estimated=%ss cap=%ss "
        "(iterations=%s population=%s total_candidates=%s pool_devices=%s per_wave=%ss buffer=%ss)",
        effective_timeout,
        estimated_timeout,
        PEPTIDE_PARENT_SUBPROCESS_TIMEOUT,
        safe_iterations,
        safe_population,
        total_candidates,
        pool_devices,
        per_wave_timeout,
        PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS,
    )

    if estimated_timeout > PEPTIDE_PARENT_SUBPROCESS_TIMEOUT:
        logger.warning(
            "Peptide parent timeout estimate (%ss) exceeds configured cap (%ss). "
            "Using capped timeout. iterations=%s population=%s pool_devices=%s total_candidates=%s",
            estimated_timeout,
            PEPTIDE_PARENT_SUBPROCESS_TIMEOUT,
            safe_iterations,
            safe_population,
            pool_devices,
            total_candidates,
        )

    return effective_timeout


def _communicate_with_optional_timeout(process: subprocess.Popen, timeout_seconds: int) -> tuple[str, str]:
    if int(timeout_seconds or 0) <= 0:
        return process.communicate()
    return process.communicate(timeout=timeout_seconds)




def _write_smiles_to_sdf(smiles: str, out_path: str) -> None:
    """Build a 3D ligand from SMILES and write SDF for Boltz2Score separate mode."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    smiles = (smiles or "").strip()
    if not smiles:
        raise ValueError("ligand_smiles is empty.")

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("Invalid ligand_smiles; RDKit failed to parse.")

    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = 0xB07A
    status = AllChem.EmbedMolecule(mol, params)
    if status != 0:
        status = AllChem.EmbedMolecule(mol, randomSeed=0xB07A, useRandomCoords=True)
    if status != 0:
        raise ValueError("Failed to generate 3D conformer from ligand_smiles.")

    try:
        AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
    except Exception:
        try:
            AllChem.UFFOptimizeMolecule(mol, maxIters=500)
        except Exception:
            pass

    mol.SetProp("_Name", "LIG")
    writer = Chem.SDWriter(out_path)
    try:
        writer.write(mol)
    finally:
        writer.close()


def _trim_sdf_to_first_valid_molecule(path: str) -> bool:
    """Keep only the first valid molecule in an SDF file.

    Returns True when the file was rewritten because multiple valid molecules
    were present; otherwise returns False.
    """
    normalized_path = str(path or "").strip()
    if not normalized_path or not normalized_path.lower().endswith(".sdf"):
        return False

    from rdkit import Chem

    supplier = Chem.SDMolSupplier(normalized_path, removeHs=False)
    first_valid = None
    valid_count = 0
    for mol in supplier:
        if mol is None:
            continue
        valid_count += 1
        if first_valid is None:
            first_valid = Chem.Mol(mol)
        if valid_count >= 2:
            break

    if first_valid is None or valid_count < 2:
        return False

    writer = Chem.SDWriter(normalized_path)
    try:
        writer.write(first_valid)
    finally:
        writer.close()
    return True


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


def _extract_protein_chain_ids_from_structure(structure_path: str) -> list[str]:
    """Extract protein chain IDs from PDB/mmCIF without modifying coordinates."""
    path = Path(structure_path)
    suffix = path.suffix.lower()
    if suffix in {".pdb", ".ent"}:
        return _extract_protein_chain_ids_from_pdb(str(path))

    if suffix not in {".cif", ".mmcif"}:
        return []

    try:
        import gemmi

        structure = gemmi.read_structure(str(path))
        chain_ids: set[str] = set()
        for model in structure:
            for chain in model:
                for residue in chain:
                    if residue.het_flag == "A":
                        chain_id = (chain.name or "").strip()
                        if chain_id:
                            chain_ids.add(chain_id)
                        break
        return sorted(chain_ids)
    except Exception:
        return []


RESULT_UPLOAD_ATTEMPTS = 3
RESULT_UPLOAD_BACKOFF_SECONDS = 5.0


class ResultUploadError(RuntimeError):
    """All result-upload attempts failed. The local archive is then the ONLY copy of a
    finished GPU run's output — callers must let the task fail loudly, never silently."""


def upload_result_to_central_api(task_id: str, local_file_path: str, filename: str) -> dict:
    """
    Uploads a local file to the centralized API server.

    This upload is the only delivery path for a finished GPU run's results: a single
    transient 5xx or network blip used to convert hours of compute into a FAILURE with
    the local temp dir wiped. Transient failures (5xx, connection/read errors) retry
    with exponential backoff; 4xx responses are permanent and fail immediately. A 2xx
    with a non-JSON body counts as success (the response text is only bookkeeping).
    """
    upload_url = f"{config.CENTRAL_API_URL}/upload_result/{task_id}"
    headers = {'X-API-Token': config.BOLTZ_API_TOKEN}
    logger.info(f"Task {task_id}: Starting upload from '{local_file_path}' to '{upload_url}'.")

    last_error: Optional[Exception] = None
    for attempt in range(1, RESULT_UPLOAD_ATTEMPTS + 1):
        try:
            with open(local_file_path, 'rb') as f:
                files = {'file': (filename, f)}
                response = requests.post(
                    upload_url,
                    files=files,
                    headers=headers,
                    timeout=(10, 300)  # (connection timeout, read timeout)
                )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {'status_code': response.status_code, 'text': (response.text or '')[:200]}
            logger.info(
                f"Task {task_id}: Results uploaded successfully (attempt {attempt}). Server response: {payload}"
            )
            return payload
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if 400 <= status_code < 500:
                # Client-side rejection (bad request/auth) — retrying cannot fix it.
                raise ResultUploadError(
                    f"Upload rejected with HTTP {status_code} for task {task_id}: {exc}"
                ) from exc
            last_error = exc
        except requests.exceptions.RequestException as exc:
            last_error = exc
        if attempt < RESULT_UPLOAD_ATTEMPTS:
            delay = RESULT_UPLOAD_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                f"Task {task_id}: Upload attempt {attempt}/{RESULT_UPLOAD_ATTEMPTS} failed "
                f"({last_error}); retrying in {delay:.0f}s."
            )
            time.sleep(delay)
    raise ResultUploadError(
        f"All {RESULT_UPLOAD_ATTEMPTS} upload attempts failed for task {task_id}: {last_error}"
    ) from last_error

@celery_app.task(bind=True)
def predict_task(self, predict_args: dict):
    """
    Celery task responsible for launching an isolated subprocess to perform computation.
    This task includes GPU management, subprocess timeout control, progress tracking, and authenticated upload.
    """
    gpu_id = -1
    allocated_gpu_ids: list[int] = []
    reported_gpu_id = -1
    reported_gpu_ids: list[int] = []
    task_id = self.request.id
    task_temp_dir = None 
    tracker = None
    subprocess_timeout = SUBPROCESS_TIMEOUT

    try:
        # 初始化进度跟踪器
        redis_client = get_redis_client()
        _raise_if_task_cancelled(self, redis_client, task_id)
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing task")
        _raise_if_task_cancelled(self, redis_client, task_id)
        normalized_workflow = _normalize_prediction_workflow(predict_args.get('workflow'))
        is_peptide_design = normalized_workflow == 'peptide_design'
        low_vram = coerce_bool(predict_args.get("low_vram"), False)
        if is_peptide_design:
            subprocess_timeout = _resolve_peptide_parent_subprocess_timeout(predict_args)
        if is_peptide_design:
            logger.info(
                f"Task {task_id}: Peptide workflow detected. "
                "Using per-candidate GPU allocation to avoid long GPU reservation."
            )
            self.update_state(
                state='PROGRESS',
                meta={'status': 'Scheduling peptide candidate subtasks (GPU allocated per candidate)'}
            )
            tracker.update_status("preparing", "Scheduling peptide candidate subtasks")
        else:
            logger.info(f"Task {task_id}: Attempting to acquire GPU.")
            tracker.update_status("acquiring_gpu", "Waiting for GPU allocation")
            _raise_if_task_cancelled(self, redis_client, task_id)
            gpu_id = _acquire_gpu_with_non_peptide_wait_registration(task_id=task_id, timeout=3600)
            _raise_if_task_cancelled(self, redis_client, task_id)
            allocated_gpu_ids = [gpu_id]
            reported_gpu_id = gpu_id
            reported_gpu_ids = list(allocated_gpu_ids)
            self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting computation.'})
            logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
            tracker.update_status("gpu_acquired", f"Using GPU {gpu_id}")
        
        task_temp_dir = _mk_task_temp_dir(prefix=f"boltz_task_{task_id}_")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        predict_args['output_archive_path'] = output_archive_path
        predict_args['task_id'] = task_id
        if is_peptide_design:
            predict_args['peptide_progress_path'] = os.path.join(task_temp_dir, "peptide_progress.json")

        args_file_path = os.path.join(task_temp_dir, 'args.json')
        with open(args_file_path, 'w') as f:
            json.dump(predict_args, f)
        logger.info(f"Task {task_id}: Arguments saved to '{args_file_path}'.")
        tracker.update_status("preparing", "Setting up temporary workspace")

        proc_env = os.environ.copy()
        if is_peptide_design:
            # Parent peptide workflow only orchestrates candidate workers.
            proc_env.pop("CUDA_VISIBLE_DEVICES", None)
            proc_env.pop("BOLTZ_ASSIGNED_GPU_ID", None)
        else:
            proc_env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
            # Expose the scheduler-assigned host GPU index for nested docker runtimes
            # so they do not fall back to GPU 0.
            proc_env["BOLTZ_ASSIGNED_GPU_ID"] = str(gpu_id)
        proc_env["BOLTZ_TASK_ID"] = task_id
        _raise_if_task_cancelled(self, redis_client, task_id)
        
        command = [
            sys.executable,
            "-m",
            "backend.runtime.run_single_prediction",
            args_file_path 
        ]

        if is_peptide_design:
            logger.info(
                f"Task {task_id}: Running peptide orchestration subprocess. "
                f"Subprocess timeout: {'disabled' if subprocess_timeout <= 0 else f'{subprocess_timeout}s'}. "
                f"Command: {' '.join(command)}"
            )
            initial_runtime_meta = _build_peptide_runtime_meta(predict_args, allocated_gpu_ids)
            initial_runtime_meta['subprocess_timeout_seconds'] = subprocess_timeout
            self.update_state(state='PROGRESS', meta=initial_runtime_meta)
            tracker.update_status(
                "running",
                str(initial_runtime_meta.get('status') or "Executing peptide candidate subtasks"),
                payload=initial_runtime_meta
            )
        else:
            gpu_log_text = str(gpu_id)
            logger.info(
                f"Task {task_id}: Running prediction on GPU {gpu_log_text}. "
                f"Subprocess timeout: {subprocess_timeout}s. Command: {' '.join(command)}"
            )
            self.update_state(state='PROGRESS', meta={'status': f'Running prediction on GPU {gpu_id}'})
            tracker.update_status("running", f"Executing prediction with GPU {gpu_id}")
        
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=proc_env,
            start_new_session=True
        )
        
        # 注册进程ID用于监控
        tracker.register_process(process.pid)

        progress_stop_event = threading.Event()
        progress_thread = None
        if is_peptide_design:
            def _emit_peptide_progress() -> None:
                last_meta = None
                while not progress_stop_event.wait(PROGRESS_UPDATE_INTERVAL):
                    try:
                        runtime_meta = _build_peptide_runtime_meta(predict_args, allocated_gpu_ids)
                        if runtime_meta == last_meta:
                            continue
                        status_text = str(runtime_meta.get('status') or '').strip()
                        if status_text:
                            tracker.update_status("running", status_text, payload=runtime_meta)
                        try:
                            # the progress thread runs outside the task
                            # request context, so update_state cannot infer
                            # the task id — pass it explicitly
                            self.update_state(
                                state='PROGRESS', meta=runtime_meta,
                                task_id=task_id)
                        except Exception as state_exc:
                            logger.warning(
                                "Task %s: Failed to update Celery peptide progress state: %s",
                                task_id,
                                state_exc,
                            )
                        last_meta = runtime_meta
                    except Exception as progress_exc:
                        logger.warning(
                            "Task %s: Failed to emit peptide progress update: %s",
                            task_id,
                            progress_exc,
                        )

            progress_thread = threading.Thread(
                target=_emit_peptide_progress,
                daemon=True,
                name=f"peptide-progress-{task_id[:8]}"
            )
            progress_thread.start()

        stdout = ''
        stderr = ''
        try:
            stdout, stderr = _communicate_with_optional_timeout(process, subprocess_timeout)
        except subprocess.TimeoutExpired as e:
            process.kill()
            stdout, stderr = process.communicate()
            revoked_summary = None
            if is_peptide_design:
                revoked_summary = _revoke_registered_peptide_subtasks(task_id, terminate=True)
            error_message = (
                f"Subprocess for task {task_id} timed out after {subprocess_timeout} seconds.\n"
                f"Stderr (tail):\n{_truncate_text(stderr, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}\n"
                f"Stdout (tail):\n{_truncate_text(stdout, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}"
            )
            if revoked_summary and (
                revoked_summary.get("found")
                or revoked_summary.get("revoked")
                or revoked_summary.get("failed")
            ):
                error_message = (
                    f"{error_message}\n"
                    f"Revoked peptide subtasks: {json.dumps(revoked_summary, ensure_ascii=False)}"
                )
            logger.error(error_message)
            tracker.update_status("timeout", f"Process timeout after {subprocess_timeout}s")
            raise TimeoutError(error_message) from e
        finally:
            progress_stop_event.set()
            if progress_thread and progress_thread.is_alive():
                progress_thread.join(timeout=5)

        if process.returncode != 0:
            error_message = _format_subprocess_failure("task", task_id, process.returncode, stderr, stdout)
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

        if allocated_gpu_ids:
            released = sorted(set(allocated_gpu_ids))
            for allocated_gpu in released:
                release_gpu(gpu_id=allocated_gpu, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPUs {released} before result upload.")
            allocated_gpu_ids = []
            gpu_id = -1
        elif gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPU {gpu_id} before result upload.")
            gpu_id = -1

        design_runtime_meta = {}
        if is_peptide_design:
            try:
                parsed_progress = _read_json_record(str(predict_args.get('peptide_progress_path') or '').strip())
                if isinstance(parsed_progress, dict):
                    design_runtime_meta = parsed_progress
            except Exception as progress_exc:
                logger.warning(f"Task {task_id}: Failed to read peptide progress metadata: {progress_exc}")
        
        upload_response = upload_result_to_central_api(task_id, output_archive_path, os.path.basename(output_archive_path))
        
        final_meta = {
            'status': 'Complete', 
            'gpu_id': reported_gpu_id,
            'gpu_ids': reported_gpu_ids,
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path) 
        }
        if design_runtime_meta:
            final_meta.update(design_runtime_meta)
        self.update_state(state='SUCCESS', meta=final_meta)
        logger.info(f"Task {task_id}: Prediction completed and results uploaded successfully. Final status: SUCCESS.")
        tracker.update_status("completed", "Task completed successfully")
        return final_meta

    except Ignore:
        raise
    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        if tracker:
            tracker.update_status("failed", _truncate_text(e, MAX_STATUS_DETAILS_CHARS))
        self.update_state(state='FAILURE', meta=_build_failure_meta(e))
        raise e

    finally:
        _terminate_task_containers_by_task_id(task_id)

        if allocated_gpu_ids:
            released = set()
            for allocated_gpu in allocated_gpu_ids:
                if allocated_gpu in released:
                    continue
                released.add(allocated_gpu)
                release_gpu(gpu_id=allocated_gpu, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPUs {sorted(released)}.")
        elif gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPU {gpu_id}.")
        
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            logger.info(f"Task {task_id}: Cleaned up temporary directory '{task_temp_dir}'.")

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")


@celery_app.task(bind=True, name="tasks.peptide_candidate_worker_task")
def peptide_candidate_worker_task(self, worker_payload: dict):
    """
    Execute one peptide-design candidate as an independent Celery task.
    GPU allocation is handled inside backend.runtime.run_single_prediction worker mode.
    """
    task_id = self.request.id
    if not isinstance(worker_payload, dict):
        raise ValueError("peptide_candidate_worker_task requires a dict payload.")

    candidate_dir = str(worker_payload.get("candidate_dir") or "").strip()
    if not candidate_dir:
        raise ValueError("peptide_candidate_worker_task requires candidate_dir.")
    os.makedirs(candidate_dir, exist_ok=True)

    archive_path = str(worker_payload.get("archive_path") or "").strip()
    if not archive_path:
        archive_path = os.path.join(candidate_dir, "result.zip")

    args_path = str(worker_payload.get("worker_args_path") or "").strip()
    if not args_path:
        args_path = os.path.join(candidate_dir, "worker_args.json")

    candidate_predict_args = worker_payload.get("predict_args", {}) or {}

    payload = {
        "__peptide_candidate_worker__": True,
        "temp_dir": candidate_dir,
        "yaml_content": worker_payload.get("candidate_yaml"),
        "output_archive_path": archive_path,
        "predict_args": candidate_predict_args,
        "model_name": worker_payload.get("model_name"),
        "backend": str(worker_payload.get("backend") or "boltz"),
        "__peptide_worker_acquire_gpu__": True,
        "__peptide_worker_task_id__": str(task_id),
    }
    with open(args_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    proc_env = os.environ.copy()
    proc_env.pop("CUDA_VISIBLE_DEVICES", None)
    proc_env["BOLTZ_TASK_ID"] = str(task_id)
    proc_env["BOLTZ_PEPTIDE_WORKER_TASK_ID"] = str(task_id)

    command = [sys.executable, "-m", "backend.runtime.run_single_prediction", args_path]
    self.update_state(state="PROGRESS", meta={"status": "Peptide candidate task started"})
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=proc_env,
        start_new_session=True,
    )

    try:
        stdout, stderr = _communicate_with_optional_timeout(process, PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise TimeoutError(
            f"Peptide candidate task {task_id} timed out after {PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT}s.\n"
            f"Stderr (tail):\n{_truncate_text(stderr, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}\n"
            f"Stdout (tail):\n{_truncate_text(stdout, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}"
        ) from exc

    if process.returncode != 0:
        raise RuntimeError(_format_subprocess_failure("peptide candidate task", task_id, process.returncode, stderr, stdout))

    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Peptide candidate task {task_id} completed without archive: {archive_path}")

    self.update_state(state="SUCCESS", meta={"status": "Complete", "archive_path": archive_path})
    return {
        "task_id": task_id,
        "archive_path": archive_path,
        "candidate_dir": candidate_dir,
    }


    

@celery_app.task(
    bind=True,
    soft_time_limit=config.BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=config.BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS,
)
def boltz2score_task(self, score_args: dict):
    """
    Celery task for running Boltz2Score (confidence; optional affinity).
    """
    gpu_id = -1
    reported_gpu_id = -1
    task_id = self.request.id
    task_temp_dir = None
    tracker = None

    try:
        redis_client = get_redis_client()
        tracker = TaskProgressTracker(task_id, redis_client)
        tracker.start_heartbeat()
        tracker.update_status("starting", "Initializing Boltz2Score task")

        # Fail before GPU allocation for a dock request that could never run: dock builds the
        # ligand from SMILES, and an empty SMILES would only fail deep inside the container
        # after the GPU has been reserved.
        if str(score_args.get('mode') or 'dock').strip().lower() == 'dock' and not str(score_args.get('ligand_smiles') or '').strip():
            raise RuntimeError("dock mode requires a non-empty ligand_smiles.")

        logger.info(f"Task {task_id}: Attempting to acquire GPU for Boltz2Score.")
        _raise_if_task_cancelled(self, redis_client, task_id)
        tracker.update_status("acquiring_gpu", "Waiting for GPU allocation")

        gpu_id = _acquire_gpu_with_non_peptide_wait_registration(task_id=task_id, timeout=3600)
        reported_gpu_id = gpu_id
        _raise_if_task_cancelled(self, redis_client, task_id)
        self.update_state(state='PROGRESS', meta={'status': f'Acquired GPU {gpu_id}. Starting Boltz2Score.'})
        logger.info(f"Task {task_id}: Acquired GPU {gpu_id}. Creating temporary directory.")
        tracker.update_status("gpu_acquired", f"Using GPU {gpu_id}")

        task_temp_dir = _mk_task_temp_dir(prefix=f"boltz2score_task_{task_id}_")
        input_filename = None
        input_file_path = None
        extra_archive_files = []
        inputs_dir = None
        using_separate_inputs = False

        has_protein_input = 'protein_file_content' in score_args
        has_ligand_file_input = 'ligand_file_content' in score_args
        has_ligand_smiles_input = bool((score_args.get('ligand_smiles') or '').strip())
        staging_mode = str(score_args.get('mode') or 'dock').strip().lower()

        if has_protein_input and (has_ligand_file_input or has_ligand_smiles_input):
            using_separate_inputs = True
            protein_filename = secure_filename(score_args['protein_filename']) or 'protein.pdb'
            ligand_filename = secure_filename(score_args.get('ligand_filename') or "ligand.sdf")

            protein_file_path = os.path.join(task_temp_dir, protein_filename)
            with open(protein_file_path, 'w', encoding='utf-8') as f:
                f.write(score_args['protein_file_content'])

            ligand_file_path = os.path.join(task_temp_dir, ligand_filename)
            if staging_mode == 'dock':
                # dock mode: the capability generates the 3-D conformer from
                # SMILES itself; no ligand file is staged.
                ligand_file_path = None
            elif has_ligand_file_input:
                with open(ligand_file_path, 'w', encoding='utf-8') as f:
                    f.write(score_args['ligand_file_content'])
                if _trim_sdf_to_first_valid_molecule(ligand_file_path):
                    logger.info(
                        "Task %s: detected multi-molecule ligand SDF; keeping only the first valid ligand entry.",
                        task_id,
                    )
            else:
                if not ligand_filename.lower().endswith('.sdf'):
                    ligand_filename = f"{Path(ligand_filename).stem or 'ligand'}.sdf"
                    ligand_file_path = os.path.join(task_temp_dir, ligand_filename)
                _write_smiles_to_sdf(score_args['ligand_smiles'], ligand_file_path)
                logger.info(
                    "Task %s: Generated ligand SDF from SMILES for Boltz2Score separate mode.",
                    task_id,
                )

            detected_target_chain = None
            if not score_args.get('target_chain'):
                detected_chain_ids = _extract_protein_chain_ids_from_structure(protein_file_path)
                if detected_chain_ids:
                    detected_target_chain = ",".join(detected_chain_ids)

            input_file_path = None
            input_filename = None

            inputs_dir = os.path.join(task_temp_dir, "inputs")
            os.makedirs(inputs_dir, exist_ok=True)
            shutil.copyfile(protein_file_path, os.path.join(inputs_dir, protein_filename))
            if ligand_file_path:
                shutil.copyfile(ligand_file_path, os.path.join(inputs_dir, ligand_filename))
                extra_archive_files.extend([
                    os.path.join(inputs_dir, protein_filename),
                    os.path.join(inputs_dir, ligand_filename),
                ])
            else:
                extra_archive_files.append(os.path.join(inputs_dir, protein_filename))

        else:
            input_filename = secure_filename(score_args['input_filename']) or 'input.cif'
            input_file_path = os.path.join(task_temp_dir, input_filename)
            with open(input_file_path, 'w', encoding='utf-8') as f:
                f.write(score_args['input_file_content'])

        output_dir = os.path.join(task_temp_dir, "output")
        work_dir = os.path.join(task_temp_dir, "work")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        structure_refine = coerce_bool(
            score_args.get('structure_refine'),
            BOLTZ2SCORE_DEFAULT_STRUCTURE_REFINE,
        )
        requested_mode = str(score_args.get('mode') or 'dock').strip().lower()
        if requested_mode not in {'score', 'pose', 'refine', 'interface', 'dock'}:
            # The route validates modes at submission; an unknown mode here means route/worker
            # drift or a task enqueued outside the API. Running it as 'score' would silently
            # change the requested computation, so fail the task with the reason instead.
            raise ValueError(
                f"Unsupported boltz2score mode {requested_mode!r}; "
                "expected one of score/pose/refine/interface/dock."
            )
        compute_ipsae = coerce_bool(score_args.get('compute_ipsae'), False)
        use_msa_server = coerce_bool(
            score_args.get('use_msa_server'),
            True,
        )
        msa_server_url = str(getattr(config, "MSA_SERVER_URL", "") or "").strip()
        if not msa_server_url:
            raise RuntimeError("MSA_SERVER_URL 未配置，无法执行 boltz2score 任务。")
        if not use_msa_server:
            logger.info("Task %s: force use_msa_server=True for boltz2score.", task_id)
        use_msa_server = True
        default_recycling_steps = (
            BOLTZ2SCORE_REFINE_RECYCLING_STEPS
            if structure_refine
            else BOLTZ2SCORE_DEFAULT_RECYCLING_STEPS
        )
        default_sampling_steps = (
            BOLTZ2SCORE_REFINE_SAMPLING_STEPS
            if structure_refine
            else BOLTZ2SCORE_DEFAULT_SAMPLING_STEPS
        )
        default_diffusion_samples = (
            BOLTZ2SCORE_REFINE_DIFFUSION_SAMPLES
            if structure_refine
            else BOLTZ2SCORE_DEFAULT_DIFFUSION_SAMPLES
        )

        # 非 score 模式（pose/refine/interface/dock）的扩散参数由 capabilities
        # 内部的 MODE_CONFIGS 提供（上游 benchmark 验证配置，见
        # capabilities/boltz2score/core/flexible_optimization.py）。这里的
        # score-only 默认 sampling_steps=1 一旦透传，会覆盖各模式默认并触发
        # Karras sigma 调度除零（steps/(N-1)=0/0 → sigma 表全 NaN → SVD
        # error code 3）；因此这些模式只在用户显式指定时才覆盖这三个参数。
        defer_diffusion_defaults = requested_mode != 'score'

        def _resolve_diffusion_arg(key: str, default: Any) -> Any:
            if defer_diffusion_defaults and score_args.get(key) is None:
                return None
            return _coerce_positive_int(score_args.get(key), default)

        recycling_steps = _resolve_diffusion_arg('recycling_steps', default_recycling_steps)
        sampling_steps = _resolve_diffusion_arg('sampling_steps', default_sampling_steps)
        diffusion_samples = _resolve_diffusion_arg('diffusion_samples', default_diffusion_samples)
        max_parallel_samples = _coerce_positive_int(
            score_args.get('max_parallel_samples'),
            BOLTZ2SCORE_DEFAULT_MAX_PARALLEL_SAMPLES,
        )
        raw_seed = score_args.get('seed')
        seed = BOLTZ2SCORE_DEFAULT_SEED
        if isinstance(raw_seed, int):
            seed = max(0, raw_seed)
        elif isinstance(raw_seed, str):
            seed_text = raw_seed.strip()
            if seed_text:
                try:
                    seed = max(0, int(seed_text))
                except ValueError:
                    logger.warning(
                        "Task %s: ignoring invalid Boltz2Score seed %r, using default seed=%d",
                        task_id,
                        raw_seed,
                        BOLTZ2SCORE_DEFAULT_SEED,
                    )

        boltz2score_entry = [
            "python",
            "/workspace/vbio/capabilities/boltz2score/boltz2score.py",
            "--output_dir", output_dir,
            "--work_dir", work_dir,
            "--accelerator", "gpu",
            "--devices", "1",
            "--num_workers", "0",
            "--mode", requested_mode,
        ]
        # 扩散参数为 None（dock 未显式指定）时不传，让 capabilities 落到
        # dock_default 的验证配置，而不是被 CLI 的 score-only 默认覆盖。
        if recycling_steps is not None:
            boltz2score_entry.extend(["--recycling_steps", str(recycling_steps)])
        if sampling_steps is not None:
            boltz2score_entry.extend(["--sampling_steps", str(sampling_steps)])
        if diffusion_samples is not None:
            boltz2score_entry.extend(["--diffusion_samples", str(diffusion_samples)])
        boltz2score_entry.extend([
            "--max_parallel_samples", str(max_parallel_samples),
        ])
        if structure_refine:
            boltz2score_entry.append("--structure_refine")
        if use_msa_server:
            boltz2score_entry.extend(["--use_msa_server", "--msa_server_url", msa_server_url])
        if using_separate_inputs:
            boltz2score_entry.extend(["--protein_file", protein_file_path])
            if requested_mode == 'dock':
                ligand_smiles_arg = str(score_args.get('ligand_smiles') or '').strip()
                boltz2score_entry.extend(["--ligand_smiles", ligand_smiles_arg])
                if 'center_x' in score_args:
                    boltz2score_entry.extend([
                        "--center_x", str(float(score_args['center_x'])),
                        "--center_y", str(float(score_args['center_y'])),
                        "--center_z", str(float(score_args['center_z'])),
                    ])
                if 'size_x' in score_args:
                    boltz2score_entry.extend([
                        "--size_x", str(float(score_args['size_x'])),
                        "--size_y", str(float(score_args['size_y'])),
                        "--size_z", str(float(score_args['size_z'])),
                    ])
                if score_args.get('pocket_residues'):
                    boltz2score_entry.extend(["--pocket_residues", str(score_args['pocket_residues'])])
                if score_args.get('pocket_ligand_content'):
                    pocket_ligand_filename = secure_filename(
                        score_args.get('pocket_ligand_filename') or 'pocket_ligand.pdb'
                    )
                    pocket_ligand_path = os.path.join(task_temp_dir, pocket_ligand_filename)
                    with open(pocket_ligand_path, 'w', encoding='utf-8') as f:
                        f.write(score_args['pocket_ligand_content'])
                    boltz2score_entry.extend(["--pocket_ligand", pocket_ligand_path])
                    extra_archive_files.append(pocket_ligand_path)
            else:
                boltz2score_entry.extend(["--ligand_file", ligand_file_path])
        else:
            boltz2score_entry.extend(["--input", input_file_path])
        boltz2score_entry.extend(["--seed", str(seed)])
        if compute_ipsae:
            boltz2score_entry.append("--compute_ipsae")
        if coerce_bool(score_args.get('compute_interactions'), True):
            boltz2score_entry.append("--compute_interactions")

        target_chain = score_args.get('target_chain') or (detected_target_chain if using_separate_inputs else None)
        ligand_chain = score_args.get('ligand_chain')
        if using_separate_inputs:
            target_chain = target_chain or "A"
            ligand_chain = ligand_chain or "L"
        if target_chain:
            boltz2score_entry.extend(["--target_chain", target_chain])
        if ligand_chain:
            boltz2score_entry.extend(["--ligand_chain", ligand_chain])
        if score_args.get('affinity_refine'):
            boltz2score_entry.append("--affinity_refine")
        if score_args.get('enable_affinity'):
            boltz2score_entry.append("--enable_affinity")
        ligand_smiles_map = score_args.get('ligand_smiles_map')
        if isinstance(ligand_smiles_map, dict) and ligand_smiles_map:
            boltz2score_entry.extend(["--ligand_smiles_map", json.dumps(ligand_smiles_map, ensure_ascii=False)])
            logger.info(
                "Task %s: applying ligand_smiles_map keys: %s",
                task_id,
                sorted(ligand_smiles_map.keys()),
            )

        tracker.update_status("running", "Executing Boltz2Score subprocess")
        logger.info(
            "Task %s: Boltz2Score settings: mode=%s, compute_ipsae=%s, structure_refine=%s, use_msa_server=%s, "
            "recycling_steps=%d, sampling_steps=%d, diffusion_samples=%d, max_parallel_samples=%d, seed=%s, msa_server_url=%s",
            task_id,
            requested_mode,
            compute_ipsae,
            structure_refine,
            use_msa_server,
            recycling_steps,
            sampling_steps,
            diffusion_samples,
            max_parallel_samples,
            seed,
            msa_server_url,
        )
        command, task_container_name = _build_gpu_docker_python_command(
            task_id=task_id,
            gpu_id=gpu_id,
            task_temp_dir=task_temp_dir,
            runtime_label="boltz2score",
            python_entry=boltz2score_entry,
        )
        _terminate_task_container(task_container_name)
        logger.info(
            "Task %s: Running Boltz2Score. Docker command: %s",
            task_id,
            " ".join(shlex.quote(part) for part in command),
        )
        _raise_if_task_cancelled(self, redis_client, task_id)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(BASE_DIR),
            start_new_session=True,
        )

        tracker.register_process(process.pid)

        try:
            stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            process.kill()
            stdout, stderr = process.communicate()
            _terminate_task_container(task_container_name)
            error_message = (
                f"Subprocess for Boltz2Score task {task_id} timed out after {SUBPROCESS_TIMEOUT} seconds.\n"
                f"Stderr (tail):\n{_truncate_text(stderr, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}\n"
                f"Stdout (tail):\n{_truncate_text(stdout, MAX_STDIO_TAIL_CHARS, prefer_tail=True)}"
            )
            logger.error(error_message)
            tracker.update_status("timeout", f"Process timeout after {SUBPROCESS_TIMEOUT}s")
            raise TimeoutError(error_message) from e

        if process.returncode != 0:
            error_message = _format_subprocess_failure("Boltz2Score task", task_id, process.returncode, stderr, stdout)
            logger.error(error_message)
            tracker.update_status("failed", f"Process failed with exit code {process.returncode}")
            raise RuntimeError(error_message)

        tracker.update_status("processing_output", "Packaging Boltz2Score results")

        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        with zipfile.ZipFile(output_archive_path, 'w') as zipf:
            for root, _, files in os.walk(output_dir):
                rel_root = os.path.relpath(root, output_dir)
                if rel_root == os.path.join("affinity", "work") or rel_root.startswith(
                    os.path.join("affinity", "work") + os.sep
                ):
                    continue
                for file in files:
                    if _should_skip_large_result_file(file):
                        continue
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

        self.update_state(state='PROGRESS', meta={'status': f'Uploading results for task {task_id}'})
        tracker.update_status("uploading", "Uploading results to central API")

        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPU {gpu_id} before result upload.")
            gpu_id = -1

        upload_response = upload_result_to_central_api(task_id, output_archive_path, os.path.basename(output_archive_path))

        final_meta = {
            'status': 'Complete',
            'gpu_id': reported_gpu_id,
            'upload_info': upload_response,
            'result_file': os.path.basename(output_archive_path)
        }
        self.update_state(state='SUCCESS', meta=final_meta)
        tracker.update_status("completed", "Task completed successfully")
        logger.info(f"Task {task_id}: Boltz2Score completed and results uploaded successfully.")
        return final_meta

    except Ignore:
        # Cancellation checkpoints raise Ignore — it must propagate untouched, else the task
        # reads as FAILURE in Redis and the /status API shows a cancelled task as failed.
        raise

    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        if tracker:
            tracker.update_status("failed", _truncate_text(e, MAX_STATUS_DETAILS_CHARS))
        self.update_state(state='FAILURE', meta=_build_failure_meta(e))
        raise e

    finally:
        _terminate_task_containers_by_task_id(task_id)

        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            logger.info(f"Task {task_id}: Released GPU {gpu_id}.")

        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
            logger.info(f"Task {task_id}: Cleaned up temporary directory '{task_temp_dir}'.")

        if tracker:
            tracker.stop_heartbeat()
            logger.info(f"Task {task_id}: Cleanup completed")



# Register sibling task modules so Celery workers (whose include list is this
# module only) know every task defined outside tasks.py.
from backend.worker.export_tasks_excel import export_tasks_excel_task  # noqa: E402,F401
from backend.worker import protenix2dock_task  # noqa: E402,F401
from backend.worker import affinity_train_task  # noqa: E402,F401
from backend.worker import lead_opt_halo_task  # noqa: E402,F401
