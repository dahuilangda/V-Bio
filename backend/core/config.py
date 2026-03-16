# config.py
"""
应用的中心配置文件。

此文件定义了所有组件（API 服务器, Celery Worker）共享的静态配置。
动态逻辑（如基于硬件检测的配置调整）应在相应组件的启动脚本中执行。
"""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = BASE_DIR / "capabilities"


def _resolve_capability_dir(name: str) -> Path:
    return CAPABILITIES_DIR / name


def _parse_gpu_device_ids(raw_value: str | None) -> list[int] | None:
    """Parse a comma/space separated GPU list from environment variables."""
    if not raw_value:
        return None

    # Support comma/space separated values and ignore empty fragments
    tokens = [token.strip() for token in re.split(r"[\s,]+", raw_value) if token.strip()]
    if not tokens:
        return None

    devices: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        try:
            device = int(token)
        except ValueError:
            # Ignore invalid entries but keep parsing the rest
            continue

        if device not in seen:
            seen.add(device)
            devices.append(device)

    return devices or None


def _parse_int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        value = default
    else:
        try:
            value = int(str(raw_value).strip())
        except ValueError:
            value = default
    if minimum is not None and value < minimum:
        return minimum
    return value


def print_config_debug_info():
    """打印当前配置调试信息"""
    print("\n" + "="*60)
    print("V-Bio 配置信息")
    print("="*60)

    config_vars = [
        'REDIS_URL', 'MAX_CONCURRENT_TASKS', 'CPU_MAX_CONCURRENT_TASKS', 'CENTRAL_API_URL',
        'MSA_SERVER_URL', 'MSA_SERVER_TIMEOUT_SECONDS', 'RESULTS_BASE_DIR', 'GPU_WORKER_CAPABILITIES', 'CPU_WORKER_CAPABILITIES',
        'GPU_POOL_NAMESPACE',
        'BOLTZ_API_TOKEN'
    ]

    for var in config_vars:
        value = os.environ.get(var, '未设置')
        if var == 'BOLTZ_API_TOKEN':
            value = '***已设置***' if value and value != '未设置' else '未设置'
        print(f"{var:25}: {value}")

    print("="*60 + "\n")

# ==============================================================================
# 1. 基础设施配置 (Core Infrastructure)
# ==============================================================================

# -- Redis & Celery --
# 用于 Celery 任务队列和结果后端的 Redis 服务地址
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Celery 配置直接复用 Redis 地址
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# ==============================================================================
# 2. Worker & GPU 配置
# ==============================================================================

# -- Worker 并发设置 --
# Worker 可以同时运行的最大并发任务数。
# >0: 限制可并发占用的 GPU 数；<=0: 自动使用全部探测到的可用 GPU。
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", -1))

# CPU worker 并发（独立于 GPU 数量）
# 0 表示自动使用本机全部 CPU 核心。
CPU_MAX_CONCURRENT_TASKS = int(os.environ.get("CPU_MAX_CONCURRENT_TASKS", 0))

# -- Worker 子进程超时 --
# 常规单次预测/评分任务默认允许 3 小时。
PREDICTION_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PREDICTION_SUBPROCESS_TIMEOUT_SECONDS",
    3 * 60 * 60,
    minimum=60,
)
# 多肽候选子任务默认不设置硬超时，避免在多父任务/多子任务排队场景下被误杀。
# >0: 启用硬超时；<=0: 禁用硬超时。
PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS",
    0,
    minimum=0,
)
# 多肽父编排任务默认不设置硬超时。
# >0: 启用父任务总超时上限；<=0: 禁用硬超时。
PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS",
    0,
    minimum=0,
)
# 估算多肽父任务超时预算时，每一轮并行 wave 预留的秒数。
PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS",
    30 * 60,
    minimum=60,
)
# 多肽父任务总预算的固定缓冲时间。
PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS",
    30 * 60,
    minimum=0,
)
# 多肽候选子任务等待 GPU 的最长时间。
# >0: 超过后报错；<=0: 一直等待，适合父任务很多时避免排队超时。
PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS",
    0,
    minimum=0,
)

# -- GPU 设备选择 --
# 通过环境变量 GPU_DEVICE_IDS 指定可用的 GPU ID 列表（例如："0,1,3"）。
# 如果未设置，则在初始化时自动探测所有可用 GPU。
GPU_DEVICE_IDS = _parse_gpu_device_ids(os.environ.get("GPU_DEVICE_IDS"))


# -- GPU 资源池 Redis 键 --
# 这些键由 GPU 管理器 (gpu_manager.py) 使用，以安全地追踪和分配 GPU 资源。
GPU_POOL_NAMESPACE = str(os.environ.get("GPU_POOL_NAMESPACE", "") or "").strip()


def _namespaced_gpu_pool_key(base_key: str) -> str:
    if not GPU_POOL_NAMESPACE:
        return base_key
    return f"{base_key}:{GPU_POOL_NAMESPACE}"


# 用于管理可用 GPU ID 的 Redis 列表
GPU_POOL_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:available")
# 用于存储所有有效 GPU ID 的 Redis 集合（防止无效 GPU ID 被释放）
GPU_VALID_SET_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:valid_gpus")
# 用于追踪任务与 GPU 占用关系的 Redis 哈希
GPU_IN_USE_HASH_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:in_use")
# 用于追踪等待 GPU 的“非多肽子任务”集合（公平调度：普通任务优先于多肽子任务续跑）
GPU_WAITING_NON_PEPTIDE_SET_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:waiting_non_peptide")
# 用于追踪多肽父任务 -> 子任务 Celery IDs 的注册表前缀
PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX = "boltz_peptide_subtasks:"


# ==============================================================================
# 3. 应用及 API 设置
# ==============================================================================

# -- 结果存储 --
# 用于主 API 服务器存储从 Worker 上传回来的中心化结果文件的目录
RESULTS_BASE_DIR = os.environ.get("RESULTS_BASE_DIR", "/data/boltz_central_results")

# -- Lead Optimization 输出目录 --
# 控制 lead optimization 的本地输出落盘位置（任务完成后会打包上传）
LEAD_OPTIMIZATION_OUTPUT_DIR = os.environ.get(
    "LEAD_OPTIMIZATION_OUTPUT_DIR",
    "/data/boltz_lead_optimization_results"
)
LEAD_OPT_MMP_QUERY_CACHE_DIR = os.environ.get(
    "LEAD_OPT_MMP_QUERY_CACHE_DIR",
    str(_resolve_capability_dir("lead_optimization") / "data" / "mmp_query_cache"),
)

# -- 中心 API 地址 --
# Worker 将使用此 URL 来上传结果和更新状态
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://localhost:5000")

# -- MSA 服务器地址 --
# ColabFold MSA 服务器的 URL，用于生成多序列比对
# 默认值仅用于本机联调；生产部署请显式写入 .env
MSA_SERVER_URL = os.environ.get("MSA_SERVER_URL", "http://localhost:8080")
MSA_SERVER_MODE = os.environ.get("MSA_SERVER_MODE", "colabfold")
MSA_SERVER_TIMEOUT_SECONDS = _parse_int_env("MSA_SERVER_TIMEOUT_SECONDS", 1800, minimum=60)

# ColabFold 服务器缓存目录（用于清理历史任务）
COLABFOLD_JOBS_DIR = os.environ.get(
    "COLABFOLD_JOBS_DIR",
    "/data/colabfold/jobs",
)


# ==============================================================================
# 4. 安全性配置 (Security)
# ==============================================================================

# -- Boltz API 令牌 --
# 用于外部客户端访问受保护的 API 端点和连接到外部 Boltz 服务
# 在生产环境中，必须通过环境变量设置此值。
# 例如在 .env 中设置: BOLTZ_API_TOKEN=your-super-secret-token
BOLTZ_API_TOKEN = os.environ.get("BOLTZ_API_TOKEN", "development-api-token")

# ==============================================================================
# 5. Boltz2 Docker 集成
# ==============================================================================

BOLTZ2_DOCKER_IMAGE = os.environ.get("BOLTZ2_DOCKER_IMAGE", "vbio-boltz2-runtime")  # Shared by boltz2/boltz2score/affinity runtime
BOLTZ2_DOCKER_EXTRA_ARGS = os.environ.get("BOLTZ2_DOCKER_EXTRA_ARGS", "")
BOLTZ2_DOCKER_SHM_SIZE = os.environ.get("BOLTZ2_DOCKER_SHM_SIZE", "16g")
BOLTZ2_HOST_CACHE_DIR = os.environ.get("BOLTZ2_HOST_CACHE_DIR", "")
BOLTZ2_CONTAINER_CACHE_DIR = os.environ.get("BOLTZ2_CONTAINER_CACHE_DIR", "/root/.boltz")

# ==============================================================================
# 6. AlphaFold3 Docker 集成
# ==============================================================================

ALPHAFOLD3_DOCKER_IMAGE = os.environ.get("ALPHAFOLD3_DOCKER_IMAGE", "cford38/alphafold3")
ALPHAFOLD3_MODEL_DIR = os.environ.get("ALPHAFOLD3_MODEL_DIR")
ALPHAFOLD3_DATABASE_DIR = os.environ.get("ALPHAFOLD3_DATABASE_DIR")
ALPHAFOLD3_DOCKER_EXTRA_ARGS = os.environ.get("ALPHAFOLD3_DOCKER_EXTRA_ARGS", "")

# ==============================================================================
# 7. Protenix Docker 集成
# ==============================================================================

PROTENIX_DOCKER_IMAGE = os.environ.get(
    "PROTENIX_DOCKER_IMAGE",
    "ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4"
)
PROTENIX_MODEL_DIR = os.environ.get("PROTENIX_MODEL_DIR")
PROTENIX_MODEL_NAME = os.environ.get("PROTENIX_MODEL_NAME", "protenix_base_20250630_v1.0.0")
PROTENIX_SOURCE_DIR = os.environ.get(
    "PROTENIX_SOURCE_DIR",
    "/data/protenix"
)
PROTENIX_DOCKER_EXTRA_ARGS = os.environ.get("PROTENIX_DOCKER_EXTRA_ARGS", "")
PROTENIX_INFER_EXTRA_ARGS = os.environ.get("PROTENIX_INFER_EXTRA_ARGS", "")
PROTENIX_PYTHON_BIN = os.environ.get("PROTENIX_PYTHON_BIN", "python3")
PROTENIX_USE_HOST_USER = os.environ.get("PROTENIX_USE_HOST_USER", "false")
PROTENIX_CONTAINER_APP_DIR = os.environ.get("PROTENIX_CONTAINER_APP_DIR", "/app")
PROTENIX_CONTAINER_MODEL_DIR = os.environ.get("PROTENIX_CONTAINER_MODEL_DIR", "/workspace/model")
PROTENIX_CONTAINER_CHECKPOINT_PATH = os.environ.get("PROTENIX_CONTAINER_CHECKPOINT_PATH", "")
PROTENIX_COMMON_CACHE_DIR = os.environ.get(
    "PROTENIX_COMMON_CACHE_DIR",
    "/data/protenix/common_cache",
)

# ==============================================================================
# 8. PocketXMol Docker 集成
# ==============================================================================

POCKETXMOL_ROOT_DIR = os.environ.get(
    "POCKETXMOL_ROOT_DIR",
    str(_resolve_capability_dir("pocketxmol"))
)
POCKETXMOL_DOCKER_IMAGE = os.environ.get("POCKETXMOL_DOCKER_IMAGE", "pocketxmol:cu128")
POCKETXMOL_CONFIG_MODEL = os.environ.get("POCKETXMOL_CONFIG_MODEL", "configs/sample/pxm.yml")
POCKETXMOL_OUTPUT_DIR = os.environ.get("POCKETXMOL_OUTPUT_DIR", "outputs_leadopt_runtime")
POCKETXMOL_DEVICE = os.environ.get("POCKETXMOL_DEVICE", "cuda:0")
POCKETXMOL_BATCH_SIZE = int(os.environ.get("POCKETXMOL_BATCH_SIZE", "50"))
