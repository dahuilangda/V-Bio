from __future__ import annotations

import os
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional

from backend.core.config import RESULTS_BASE_DIR


def discover_cuda_devices() -> List[str]:
    devices: List[str] = []
    try:
        smi_proc = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        smi_proc = None
    if smi_proc and smi_proc.returncode == 0:
        for line in smi_proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("GPU"):
                devices.append(line.split(":")[0].replace("GPU", "").strip())
    return devices


def determine_docker_gpu_arg(visible_devices: Optional[str]) -> str:
    available = discover_cuda_devices()
    if not available:
        raise RuntimeError(
            "当前后端需要 NVIDIA GPU，但当前环境未检测到可用的 CUDA 设备。")

    if not visible_devices:
        return "all"

    tokens = [token.strip() for token in visible_devices.split(',') if token.strip()]
    if not tokens:
        raise RuntimeError("检测到 CUDA_VISIBLE_DEVICES 已设置，但未包含有效设备索引。")

    numeric_tokens = [token for token in tokens if token.isdigit()]
    invalid = [token for token in numeric_tokens if token not in available]
    if invalid:
        raise RuntimeError(
            "请求使用的 GPU 索引在当前机器上不可用: "
            f"{', '.join(invalid)}。可用索引: {', '.join(available)}")

    return f"device={','.join(tokens)}"


def safe_runtime_token(raw: Any) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw or "").strip()).strip("._-")
    if token:
        return token[:72]
    return f"pxm_{int(time.time())}_{random.randint(1000, 9999)}"


def resolve_backend_results_root(backend: str, task_id: Optional[str],
                                 temp_dir: str) -> Path:
    backend_token = str(backend or "unknown").strip().lower() or "unknown"
    runtime_token = safe_runtime_token(
        task_id or os.environ.get("VBIO_TASK_ID") or Path(temp_dir).name)
    base_dir = Path(str(RESULTS_BASE_DIR or "/data/boltz_central_results")).expanduser()
    root = base_dir / backend_token / runtime_token
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_backend_work_root(results_root: Path) -> Path:
    work_root = results_root / "runtime"
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root
