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


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    return str(raw_value).strip().lower() in ("1", "true", "yes", "on")



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
# 结构上传（PDB/CIF/SDF/MOL2）的请求体硬上限；Flask 超限直接 413。
MAX_UPLOAD_BYTES = _parse_int_env("MAX_UPLOAD_BYTES", 64 * 1024 * 1024, minimum=1024*1024)
# 客户端是否回显内部异常详情（默认关闭：详情可能包含内部路径/主机名）。完整错误始终进服务端日志。
EXPOSE_ERROR_DETAILS = os.environ.get("EXPOSE_ERROR_DETAILS", "").strip().lower() in {"1", "true", "yes", "on"}

# Celery-level ceilings for boltz2score: GPU wait is capped at 1h and inference at 3h, so a
# soft limit at 4.5h (graceful SoftTimeLimitExceeded handling) and a hard kill at 5h bound the
# worker slot without touching healthy runs.
BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS = _parse_int_env(
    "BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS", 4 * 3600 + 1800, minimum=600
)
BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS = _parse_int_env(
    "BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS", 5 * 3600, minimum=1200
)
if BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS <= BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS + 300:
    # A hard limit that fires at/inside the soft limit SIGKILLs the worker before the graceful
    # SoftTimeLimitExceeded cleanup (GPU release, container teardown) can run.
    raise RuntimeError(
        "BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS must exceed the soft limit by at least 300s "
        f"(soft={BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS}, "
        f"hard={BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS})."
    )

# Worker 可以同时运行的最大并发任务数。
# >0: 限制可并发占用的 GPU 数；<=0: 自动使用全部探测到的可用 GPU。
MAX_CONCURRENT_TASKS = _parse_int_env("MAX_CONCURRENT_TASKS", -1)

# CPU worker 并发（独立于 GPU 数量）
# 0 表示自动使用本机全部 CPU 核心。
CPU_MAX_CONCURRENT_TASKS = _parse_int_env("CPU_MAX_CONCURRENT_TASKS", 0)

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

# -- 任务列表 Excel 异步导出 --
# 导出任务通过 Celery 队列在 CPU worker 上执行，产物与作业状态分别落盘/落在 Redis。
EXPORTS_BASE_DIR = os.environ.get(
    "EXPORTS_BASE_DIR",
    os.path.join(RESULTS_BASE_DIR, "exports"),
)
# Redis 中导出作业记录的存活时间（秒）；到期后状态查询返回 404，产物文件按文件 TTL 清理
EXPORT_JOB_TTL_SECONDS = _parse_int_env("EXPORT_JOB_TTL_SECONDS", 48 * 3600, minimum=3600)
# 导出产物文件在磁盘上的保留时间（秒）；每次启动新导出时顺带清理过期文件
EXPORT_FILE_TTL_SECONDS = _parse_int_env("EXPORT_FILE_TTL_SECONDS", 48 * 3600, minimum=3600)
# 单次导出允许的最大任务行数（防止超大 payload 拖垮队列）；生产项目实测可达 1.3 万行
EXPORT_MAX_TASK_ROWS = _parse_int_env("EXPORT_MAX_TASK_ROWS", 50000, minimum=1)
# 提交导出请求时允许的最大 JSON body（字节）。
# 与 MAX_UPLOAD_BYTES 对齐：生产实测 1.3 万行含配体 pLDDT 的导出约 40-52MB，
# 32MB 会先于行数上限拒绝合法导出。
EXPORT_REQUEST_MAX_BYTES = _parse_int_env("EXPORT_REQUEST_MAX_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024)

# -- Lead Optimization 输出目录 --
# 控制 lead optimization 的本地输出落盘位置（任务完成后会打包上传）
LEAD_OPTIMIZATION_OUTPUT_DIR = os.environ.get(
    "LEAD_OPTIMIZATION_OUTPUT_DIR",
    "/data/boltz_lead_optimization_results"
)

# -- 任务结果保留与定期清理 --
# 超过保留期的任务结果文件（结果 zip、<backend>/<task_id>/ 中间结果树、
# lead optimization 输出、泄漏的运行时临时目录）会被自动删除。到期判据为文件
# mtime（zip 上传完成/目录最后写入时刻）；运行或排队中的任务不受影响。
RESULTS_RETENTION_DAYS = _parse_int_env("RESULTS_RETENTION_DAYS", 90, minimum=1)
# 结果清理开关：由 monitor 服务按下方间隔周期执行文件系统 GC
RESULTS_CLEANUP_ENABLED = _env_bool("RESULTS_CLEANUP_ENABLED", True)
# 结果清理的执行间隔（秒）：monitor 启动后会先执行一轮，之后按此周期复查
RESULTS_CLEANUP_INTERVAL_SECONDS = _parse_int_env("RESULTS_CLEANUP_INTERVAL_SECONDS", 6 * 3600, minimum=300)

# -- MSA 缓存保留期 --
# MSA(a3m) 序列缓存可整体重建，超过保留期的缓存文件由同一个 monitor 清理
# 周期删除（默认与任务结果一致为 90 天）；缓存目录见 BOLTZ_MSA_CACHE_DIR。
MSA_CACHE_RETENTION_DAYS = _parse_int_env("MSA_CACHE_RETENTION_DAYS", 90, minimum=1)

# -- 中心 API 地址 --
# Worker 将使用此 URL 来上传结果和更新状态
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://localhost:5000")

# -- MSA 服务器地址 --
# ColabFold MSA 服务器的 URL，用于生成多序列比对
# 默认值仅用于本机联调；生产部署请显式写入 .env
MSA_SERVER_URL = os.environ.get("MSA_SERVER_URL", "http://localhost:8080")
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
BOLTZ_MSA_CACHE_DIR = os.environ.get("BOLTZ_MSA_CACHE_DIR", "/data/boltz_msa_cache")  # MSA sequence cache on the /data partition
BOLTZ2_DOCKER_EXTRA_ARGS = os.environ.get("BOLTZ2_DOCKER_EXTRA_ARGS", "")
BOLTZ2_DOCKER_SHM_SIZE = os.environ.get("BOLTZ2_DOCKER_SHM_SIZE", "16g")
BOLTZ2_HOST_CACHE_DIR = os.environ.get("BOLTZ2_HOST_CACHE_DIR", "")
BOLTZ2_CONTAINER_CACHE_DIR = os.environ.get("BOLTZ2_CONTAINER_CACHE_DIR", "/root/.boltz")

# ==============================================================================
# 6. AlphaFold3 Docker 集成
# ==============================================================================

ALPHAFOLD3_DOCKER_IMAGE = os.environ.get("ALPHAFOLD3_DOCKER_IMAGE", "jurgjn/alphafold3:v3.0.2")
ALPHAFOLD3_MODEL_DIR = os.environ.get("ALPHAFOLD3_MODEL_DIR")
ALPHAFOLD3_DATABASE_DIR = os.environ.get("ALPHAFOLD3_DATABASE_DIR")
ALPHAFOLD3_DOCKER_EXTRA_ARGS = os.environ.get("ALPHAFOLD3_DOCKER_EXTRA_ARGS", "")

# ==============================================================================
# 7. Protenix Docker 集成
# ==============================================================================

PROTENIX_DOCKER_IMAGE = os.environ.get(
    "PROTENIX_DOCKER_IMAGE",
    "vbio-protenix-v2-runtime:2.0.0"
)
PROTENIX_MODEL_DIR = os.environ.get("PROTENIX_MODEL_DIR")
PROTENIX_MODEL_NAME = os.environ.get("PROTENIX_MODEL_NAME", "protenix-v2")
PROTENIX_SOURCE_DIR = os.environ.get(
    "PROTENIX_SOURCE_DIR",
    "/data/V-Bio/vendor/protenix-source"
)
PROTENIX_DOCKER_EXTRA_ARGS = os.environ.get("PROTENIX_DOCKER_EXTRA_ARGS", "--entrypoint=")
PROTENIX_INFER_EXTRA_ARGS = os.environ.get("PROTENIX_INFER_EXTRA_ARGS", "")
PROTENIX_PYTHON_BIN = os.environ.get("PROTENIX_PYTHON_BIN", "/usr/local/micromamba/envs/protenix/bin/python")
PROTENIX_USE_HOST_USER = os.environ.get("PROTENIX_USE_HOST_USER", "false")
PROTENIX_CONTAINER_APP_DIR = os.environ.get("PROTENIX_CONTAINER_APP_DIR", "/app")
PROTENIX_CONTAINER_MODEL_DIR = os.environ.get("PROTENIX_CONTAINER_MODEL_DIR", "/workspace/model")
PROTENIX_CONTAINER_CHECKPOINT_PATH = os.environ.get("PROTENIX_CONTAINER_CHECKPOINT_PATH", "")
PROTENIX_COMMON_CACHE_DIR = os.environ.get(
    "PROTENIX_COMMON_CACHE_DIR",
    "/data/protenix/common_cache",
)
# Writable host dir for the whole-module pickle cache (protenix model
# construction is ~80 s per task without it).  Empty disables the cache.
PROTENIX_MODULE_CACHE_DIR = os.environ.get(
    "PROTENIX_MODULE_CACHE_DIR",
    "/data/protenix/module_cache",
)

# ==============================================================================
# 8. Nesso Docker 集成
# ==============================================================================

NESSO_DOCKER_IMAGE = os.environ.get("NESSO_DOCKER_IMAGE", "vbio-nesso-runtime:1.0.0")
NESSO_DOCKER_EXTRA_ARGS = os.environ.get("NESSO_DOCKER_EXTRA_ARGS", "")
NESSO_HOST_CACHE_DIR = os.environ.get("NESSO_HOST_CACHE_DIR", "/data/nesso_cache")
NESSO_CONTAINER_CACHE_DIR = os.environ.get("NESSO_CONTAINER_CACHE_DIR", "/workspace/nesso-cache")
NESSO_MODEL_REVISION = os.environ.get("NESSO_MODEL_REVISION", "v1.0.0")
NESSO_NO_KERNELS = os.environ.get("NESSO_NO_KERNELS", "true")
NESSO_RECYCLING_STEPS = _parse_int_env("NESSO_RECYCLING_STEPS", 5, minimum=0)
NESSO_NUM_WORKERS = _parse_int_env("NESSO_NUM_WORKERS", 2, minimum=1)
NESSO_PRECISION = os.environ.get("NESSO_PRECISION", "bf16-mixed")
