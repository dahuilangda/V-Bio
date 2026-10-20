from __future__ import annotations

import argparse
import json
import logging
import os
import socket
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
        or "0"
    )
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = 0

    if value <= 0:
        # cap prefork children: 128 forks of a heavy module invite silent
        # child deaths (observed: long tasks redelivered via acks_late and
        # restarted from generation 1); 32 saturates the CPU-bound work
        # anyway since candidate subtasks run on GPU workers
        value = min(detected_cores, 32)
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
    # Loopback by default (F1): the SPA reaches the runtime through the authenticated
    # management gateway; binding 0.0.0.0 exposed the unauthenticated runtime to the network.
    # Set VBIO_RUNTIME_BIND=0.0.0.0 only for deployments where the gateway runs elsewhere.
    bind_host = str(os.environ.get("VBIO_RUNTIME_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    _exec_or_die(
        [
            "gunicorn",
            "--workers",
            str(workers),
            "--bind",
            f"{bind_host}:5000",
            "--timeout",
            "120",
            "backend.app:app",
        ]
    )


def run_monitor() -> None:
    from backend.monitoring.monitor_collector import main as monitor_main

    monitor_main()


def _preflight_docker_capabilities(logger, raw_capabilities: str) -> None:
    """启动预检:nvidia runtime 与 capability 所需镜像的存在性。

    只告警不阻断——配置退化时 worker 崩溃循环会让任务无人消费;缺失镜像的
    任务提交后由 ensure_image 提供可读报错并快速失败。
    """
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False,
        )
        runtimes = set(json.loads(info.stdout or "{}").keys()) if info.returncode == 0 else set()
        if "nvidia" not in runtimes:
            logger.error(
                "docker daemon 缺少 nvidia runtime —— GPU 容器将全部失败!"
                " 请在 /etc/docker/daemon.json 配置 runtimes.nvidia 后重启 docker。"
            )
    except Exception as exc:
        logger.warning("docker runtime 预检失败(不阻断): %s", exc)

    capability_images = {
        "boltz2": getattr(config, "BOLTZ2_DOCKER_IMAGE", ""),
        "boltz2score": getattr(config, "BOLTZ2_DOCKER_IMAGE", ""),
        "affinity": getattr(config, "BOLTZ2_DOCKER_IMAGE", ""),
        "protenix": getattr(config, "PROTENIX_DOCKER_IMAGE", ""),
        "protenix2dock": getattr(config, "PROTENIX_DOCKER_IMAGE", ""),
        "alphafold3": getattr(config, "ALPHAFOLD3_DOCKER_IMAGE", ""),
        "nesso": getattr(config, "NESSO_DOCKER_IMAGE", ""),
    }
    for cap in (s.strip() for s in raw_capabilities.split(",") if s.strip()):
        image = str(capability_images.get(cap) or "").strip()
        if not image:
            continue
        try:
            probe = subprocess.run(
                ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False,
            )
        except Exception as exc:
            logger.warning("镜像预检失败 (capability=%s): %s", cap, exc)
            continue
        if probe.returncode != 0:
            logger.error(
                "运行时镜像缺失: capability=%s image=%s —— 该类任务提交后将快速失败,"
                " 请构建/导入镜像(参考 docs/deployment/model-services.md)。",
                cap, image,
            )


def run_gpu_worker() -> None:
    LOGGER.info("Initializing GPU pool...")
    subprocess.run([sys.executable, "-m", "gpu_manager", "init"], check=True)

    raw_capabilities = str(os.environ.get("GPU_WORKER_CAPABILITIES") or os.environ.get("WORKER_CAPABILITIES") or "")
    queue_list = _resolve_worker_queues("gpu", raw_capabilities, include_high_priority=True)
    _preflight_docker_capabilities(LOGGER, raw_capabilities)
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

    # 常驻池自愈：周期 reconcile 清理孤儿租约 + 孤儿任务容器巡检。以独立进程运行，
    # 因为本进程随后 execvpe 为 celery，线程无法存活。放在全部校验之后——
    # 校验失败提前退出时不能遗留无人管的维护进程。
    LOGGER.info("Starting GPU pool maintenance process...")
    subprocess.Popen([sys.executable, "-m", "gpu_manager", "maintain"])

    env = os.environ.copy()
    env["CELERY_WORKER_NAME"] = f"gpu@{socket.gethostname()}"
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
            "--prefetch-multiplier",
            "1",
            "--max-tasks-per-child",
            "50",
        ],
        env=env,
    )


def run_cpu_worker(cli_concurrency: str | None) -> None:
    raw_capabilities = str(os.environ.get("CPU_WORKER_CAPABILITIES") or "")
    queue_list = _resolve_worker_queues("cpu", raw_capabilities, include_high_priority=False)
    concurrency = _resolve_cpu_worker_concurrency(cli_concurrency)
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["CELERY_WORKER_NAME"] = f"cpu@{socket.gethostname()}"

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
