"""Shared docker command skeleton for GPU task containers.

Common parts of every task-container invocation: naming/labels, nvidia
runtime, shm sizing, GPU pinning, repo mount, and env basics. Engine-specific
builders (image, extra mounts, entry) compose on top.
"""

from __future__ import annotations

import shlex
from typing import Optional

from backend.core import config
from backend.worker import tasks as _tasks

REPO_MOUNT_CONTAINER = "/workspace/vbio"


def build_task_docker_skeleton(
    *,
    task_id: str,
    gpu_id: int,
    runtime_label: str,
    base_dir: Optional[str] = None,
    shm_size: str = "16g",
) -> tuple[list[str], str]:
    """Return (partial command, container_name) with common flags applied.

    The caller appends engine-specific volumes/env, the image, and the argv.
    """
    container_name = _tasks._make_task_container_name(task_id, runtime_label)
    raw_extra = shlex.split(str(getattr(config, "PROTENIX_DOCKER_EXTRA_ARGS", "") or ""))
    extra_args = _tasks._sanitize_docker_extra_args(raw_extra)
    runtime_overridden = any(tok == "--runtime" for tok in extra_args)

    command = ["docker", "run", "--rm", "--name", container_name]
    command.extend(["--label", f"boltz.task_id={task_id}"])
    command.extend(["--label", f"boltz.runtime={runtime_label}"])
    if not runtime_overridden:
        command.extend(["--runtime", "nvidia"])
    if (shm_size
            and not _tasks._docker_args_has_flag(extra_args, "--shm-size")
            and not _tasks._docker_args_has_flag(extra_args, "--ipc")):
        command.extend(["--shm-size", shm_size])
    command.extend(["--gpus", f"device={int(gpu_id)}"])

    repo = base_dir or (getattr(config, "BASE_DIR", None) or "/data/V-Bio")
    command.extend([
        "--volume", f"{repo}:{REPO_MOUNT_CONTAINER}:ro",
        "--workdir", REPO_MOUNT_CONTAINER,
        "--env", f"BOLTZ_TASK_ID={task_id}",
    ])
    return command, container_name


def protenix_runtime_mounts(command: list[str]) -> list[str]:
    """Append the standard protenix runtime mounts/env (model, caches, shm)."""
    command.extend([
        "--volume", f"{config.PROTENIX_MODEL_DIR}:/workspace/model:ro",
        "--volume", f"{config.PROTENIX_COMMON_CACHE_DIR}:/cache/common:ro",
        # rw：protenix2dock 回写共享 MSA 缓存
        "--volume", "/data/boltz_msa_cache:/data/msa_cache",
        "--volume", "/dev/shm:/dev/shm",
        "--env", "PYTHONPATH=/workspace/vbio/vendor/protenix-source",
        "--env", "PROTENIX_ROOT_DIR=/cache",
    ])
    # Writable whole-module cache: Protenix model construction takes ~80 s per
    # task (random init immediately overwritten by the checkpoint); the
    # pickled module (protenix/model/module_cache) loads in seconds.
    module_cache_dir = str(getattr(config, "PROTENIX_MODULE_CACHE_DIR", "") or "").strip()
    if module_cache_dir:
        container_dir = "/cache/module_cache"
        command.extend([
            "--volume", f"{module_cache_dir}:{container_dir}",
            "--env", f"PROTENIX_MODULE_CACHE_DIR={container_dir}",
        ])
    return command


def image_and_python(image: Optional[str] = None) -> tuple[str, str]:
    """Resolve (image, python_bin) with validation."""
    resolved = str(image or getattr(config, "PROTENIX_DOCKER_IMAGE", "") or "").strip()
    if not resolved:
        raise RuntimeError("PROTENIX_DOCKER_IMAGE 未配置，无法启动 Docker 任务。")
    python_bin = str(getattr(
        config, "PROTENIX_PYTHON_BIN",
        "/usr/local/micromamba/envs/protenix/bin/python",
    ))
    return resolved, python_bin
