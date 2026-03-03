from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from typing import List

from backend.core import config
from backend.scheduling.capability_router import build_worker_queue_list
from gpu_manager import get_gpu_status


LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - Entrypoint - %(levelname)s - %(message)s",
    )


def _exec_or_die(cmd: List[str], env: dict | None = None) -> None:
    LOGGER.info("Exec: %s", " ".join(cmd))
    os.execvpe(cmd[0], cmd, env if env is not None else os.environ.copy())


def _resolve_gunicorn_workers() -> int:
    raw = os.environ.get("GUNICORN_WORKERS") or os.environ.get("MAX_CONCURRENT_TASKS") or "4"
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = 1
    if value <= 0:
        value = 1
    return value


def _detect_cpu_cores() -> int:
    cores = os.cpu_count() or 1
    if cores <= 0:
        return 1
    return cores


def _resolve_cpu_worker_concurrency(cli_concurrency: str | None) -> int:
    detected_cores = _detect_cpu_cores()
    raw = (
        cli_concurrency
        if cli_concurrency
        else os.environ.get("CPU_MAX_CONCURRENT_TASKS")
        or os.environ.get("MMP_CELERY_CONCURRENCY")
        or "0"
    )
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = 0

    if value <= 0:
        value = detected_cores
    value = min(value, detected_cores)
    return max(1, value)


def _resolve_worker_queues(worker_type: str, raw_capabilities: str, include_high_priority: bool) -> str:
    queues = build_worker_queue_list(
        config_module=config,
        worker_type=worker_type,
        raw_capabilities=raw_capabilities,
        include_high_priority=include_high_priority,
    )
    if not queues:
        raise RuntimeError(f"No queues resolved for worker_type={worker_type}, raw_capabilities={raw_capabilities!r}")
    return ",".join(queues)


def run_api() -> None:
    workers = _resolve_gunicorn_workers()
    _exec_or_die(
        [
            "gunicorn",
            "--workers",
            str(workers),
            "--bind",
            "0.0.0.0:5000",
            "--timeout",
            "120",
            "backend.app:app",
        ]
    )


def run_monitor() -> None:
    from backend.monitoring.monitor_daemon import main as monitor_main

    monitor_main()


def run_gpu_worker() -> None:
    LOGGER.info("Initializing GPU pool...")
    subprocess.run([sys.executable, "-m", "gpu_manager", "init"], check=True)

    raw_capabilities = str(os.environ.get("GPU_WORKER_CAPABILITIES") or os.environ.get("WORKER_CAPABILITIES") or "")
    queue_list = _resolve_worker_queues("gpu", raw_capabilities, include_high_priority=True)
    gpu_status = get_gpu_status()
    valid_count = int(gpu_status.get("valid_count", 0) or 0)
    available_count = int(gpu_status.get("available_count", 0) or 0)
    in_use_count = int(gpu_status.get("in_use_count", 0) or 0)
    if valid_count <= 0:
        raise RuntimeError(
            "GPU pool has zero capacity (valid_count=0). Refusing to start worker without explicit GPU capacity."
        )
    concurrency = valid_count
    LOGGER.info(
        "Resolved GPU worker concurrency=%s from pool capacity (valid=%s, available=%s, in_use=%s).",
        concurrency,
        valid_count,
        available_count,
        in_use_count,
    )

    _exec_or_die(
        [
            "celery",
            "-A",
            "backend.core.celery_app",
            "worker",
            "-n",
            "gpu@%h",
            "-l",
            "info",
            "--concurrency",
            str(concurrency),
            "-Q",
            queue_list,
            "--max-tasks-per-child",
            "1",
        ]
    )


def run_cpu_worker(cli_concurrency: str | None) -> None:
    raw_capabilities = str(os.environ.get("CPU_WORKER_CAPABILITIES") or "")
    queue_list = _resolve_worker_queues("cpu", raw_capabilities, include_high_priority=False)
    concurrency = _resolve_cpu_worker_concurrency(cli_concurrency)
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""

    _exec_or_die(
        [
            "celery",
            "-A",
            "backend.core.celery_app",
            "worker",
            "-l",
            "info",
            "-n",
            "cpu@%h",
            "--pool=prefork",
            "--concurrency",
            str(concurrency),
            "-Q",
            queue_list,
            "--prefetch-multiplier=1",
            "--max-tasks-per-child",
            "20",
        ],
        env=env,
    )


def main() -> None:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Container runtime entrypoint for V-Bio backend services")
    parser.add_argument("mode", choices=["api", "monitor", "gpu-worker", "cpu-worker"])
    parser.add_argument("--cpu-concurrency", default=None, help="Override CPU worker concurrency")
    args = parser.parse_args()

    if args.mode == "api":
        run_api()
    elif args.mode == "monitor":
        run_monitor()
    elif args.mode == "gpu-worker":
        run_gpu_worker()
    elif args.mode == "cpu-worker":
        run_cpu_worker(args.cpu_concurrency)


if __name__ == "__main__":
    main()
