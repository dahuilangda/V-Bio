"""Shared docker command skeleton for GPU task containers: naming/labels,
nvidia runtime, shm sizing, GPU pinning, repo mount, env basics. Engine
builders compose on top."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Optional

from backend.core import config
from backend.worker import tasks as _tasks

REPO_MOUNT_CONTAINER = "/workspace/vbio"

# 私有 runtime 镜像不会存在于任何公共 registry;docker run 遇到缺失镜像会先尝试
# pull(数秒到数分钟,registry 不可达时更久),期间 GPU 租约被空占,最终报出的
# exit 125 对用户不可读。预检把这类失败收敛为亚秒级、带构建指引的业务错误。
IMAGE_PRECHECK_TIMEOUT_SECONDS = 30


def readable_docker_failure_diagnosis(return_code: int, tail_text: str) -> str:
    """把 docker run 失败的常见根因翻译成用户可读的诊断行(空串=未知)。"""
    tail = (tail_text or "").lower()
    checks = [
        ("no such image", "运行时镜像不存在(可能已被删除;请重建或导入镜像)"),
        ("pull access denied", "镜像不可拉取:私有镜像未构建/未导入,或 registry 凭据缺失"),
        ("unknown runtime", "docker daemon 缺少 nvidia runtime 配置(/etc/docker/daemon.json)"),
        ("no space left on device", "磁盘空间不足:请清理 /data 下的 docker 镜像、结果与缓存"),
        ("cuda out of memory", "GPU 显存不足(CUDA OOM):可能与其他任务或池外进程争用"),
        ("conflict. the container name", "容器名冲突:同任务旧容器仍在,稍后重试或手动清理"),
        ("device requested from driver", "GPU 设备不可用:驱动与容器运行时配置异常"),
    ]
    for needle, message in checks:
        if needle in tail:
            return f"诊断: {message}"
    if return_code == 137:
        return "诊断: 容器被 SIGKILL(OOM 或被外部终止)"
    if return_code == 125:
        return "诊断: docker 自身错误(配置/参数/镜像问题,见 Last output)"
    return ""



def ensure_image(image: str, *, timeout_seconds: int = IMAGE_PRECHECK_TIMEOUT_SECONDS) -> None:
    """Fail fast when the runtime image is absent locally.

    Raises RuntimeError with an actionable message; never pulls (runtime images
    are built/loaded from this repository, not registries).
    """
    resolved = str(image or "").strip()
    if not resolved:
        raise RuntimeError("运行时镜像名未配置，无法启动 Docker 任务。")
    if not shutil.which("docker"):
        return  # 无 docker CLI 的环境交给实际执行路径报错
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", resolved, "--format", "{{.Id}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=max(5, int(timeout_seconds)), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"镜像存在性预检超时({timeout_seconds}s): docker daemon 无响应, "
            f"任务未启动容器。原始异常: {exc}"
        ) from exc
    if probe.returncode == 0:
        return
    raise RuntimeError(
        f"运行时镜像 `{resolved}` 不存在，任务未启动容器、未消耗 GPU 时间。"
        f"请在 GPU 主机构建或导入该镜像（参考 docs/deployment/model-services.md："
        f"docker build -f deploy/docker/<runtime>.Dockerfile -t {resolved} .，"
        f"或 docker load < 导出包）。原始报错：{(probe.stderr or '').strip()[:200]}"
    )


def build_task_docker_skeleton(
    *,
    task_id: str,
    gpu_id: int,
    runtime_label: str,
    base_dir: Optional[str] = None,
    shm_size: str = "16g",
) -> tuple[list[str], str]:
    """Return (partial command, container_name) with common flags applied;
    the caller appends image, engine volumes/env, and argv."""
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
    if not config.PROTENIX_MODEL_DIR:
        raise ValueError(
            "PROTENIX_MODEL_DIR is not configured; refusing to build a "
            "container with a literal 'None:/workspace/model:ro' volume")
    command.extend([
        "--volume", f"{config.PROTENIX_MODEL_DIR}:/workspace/model:ro",
        "--volume", f"{config.PROTENIX_COMMON_CACHE_DIR}:/cache/common:ro",
        # rw：protenix2dock 回写共享 MSA 缓存
        "--volume", f"{config.BOLTZ_MSA_CACHE_DIR}:/data/msa_cache",
        "--volume", "/dev/shm:/dev/shm",
        "--env", f"PYTHONPATH={REPO_MOUNT_CONTAINER}/vendor/protenix-source",
        "--env", "PROTENIX_ROOT_DIR=/cache",
    ])
    # Writable whole-module cache: model construction takes ~80 s per task;
    # the pickled module loads in seconds.
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
    ensure_image(resolved)
    python_bin = str(getattr(
        config, "PROTENIX_PYTHON_BIN",
        "/usr/local/micromamba/envs/protenix/bin/python",
    ))
    return resolved, python_bin
