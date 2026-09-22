"""Central config shared by the API server and Celery workers."""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]




def _parse_gpu_device_ids(raw_value: str | None) -> list[int] | None:
    """Parse a comma/space separated GPU list from environment variables."""
    if not raw_value:
        return None

    tokens = [token.strip() for token in re.split(r"[\s,]+", raw_value) if token.strip()]
    if not tokens:
        return None

    devices: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        try:
            device = int(token)
        except ValueError:
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



# 1. 基础设施配置

# Redis & Celery
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# 2. Worker & GPU 配置

# Worker 并发
# 结构上传（PDB/CIF/SDF/MOL2）的请求体硬上限；Flask 超限直接 413。
MAX_UPLOAD_BYTES = _parse_int_env("MAX_UPLOAD_BYTES", 64 * 1024 * 1024, minimum=1024*1024)
# 是否向客户端回显内部异常详情（默认关闭，避免泄露内部路径/主机名）。
EXPOSE_ERROR_DETAILS = os.environ.get("EXPOSE_ERROR_DETAILS", "").strip().lower() in {"1", "true", "yes", "on"}

# boltz2score：GPU 等待上限 1h、推理上限 3h；soft 4.5h（优雅处理）
# 加 hard 5h 封顶 worker 槽位，不影响健康任务。
BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS = _parse_int_env(
    "BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS", 4 * 3600 + 1800, minimum=600
)
BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS = _parse_int_env(
    "BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS", 5 * 3600, minimum=1200
)
if BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS <= BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS + 300:
    # 硬限触发在软限之内会在 SoftTimeLimitExceeded 清理（释放 GPU、拆容器）前 SIGKILL。
    raise RuntimeError(
        "BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS must exceed the soft limit by at least 300s "
        f"(soft={BOLTZ2SCORE_TASK_SOFT_TIME_LIMIT_SECONDS}, "
        f"hard={BOLTZ2SCORE_TASK_HARD_TIME_LIMIT_SECONDS})."
    )

# >0 限制并发占用的 GPU 数；<=0 自动使用全部可用 GPU。
MAX_CONCURRENT_TASKS = _parse_int_env("MAX_CONCURRENT_TASKS", -1)

# CPU worker 并发；0 = 自动使用全部 CPU 核心。
CPU_MAX_CONCURRENT_TASKS = _parse_int_env("CPU_MAX_CONCURRENT_TASKS", 0)

# Worker 子进程超时
# 常规单次预测/评分任务默认允许 3 小时。
PREDICTION_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PREDICTION_SUBPROCESS_TIMEOUT_SECONDS",
    3 * 60 * 60,
    minimum=60,
)
# 多肽候选子任务硬超时；<=0 禁用（默认禁用，多任务排队时避免误杀）。
PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS",
    0,
    minimum=0,
)
# 多肽父任务总超时；<=0 禁用。
PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS",
    0,
    minimum=0,
)
# 估算父任务超时预算时每轮并行 wave 预留的秒数。
PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS",
    30 * 60,
    minimum=60,
)
# 多肽父任务总预算的固定缓冲时间（秒）。
PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS = _parse_int_env(
    "PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS",
    30 * 60,
    minimum=0,
)
# 多肽候选子任务等待 GPU 的最长时间；<=0 一直等待（多父任务排队时避免误杀）。
PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS = _parse_int_env(
    "PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS",
    0,
    minimum=0,
)

# GPU 设备选择
# 可用 GPU ID 列表（如 "0,1,3"）；未设置时初始化自动探测。
GPU_DEVICE_IDS = _parse_gpu_device_ids(os.environ.get("GPU_DEVICE_IDS"))


# GPU 资源池 Redis 键（由 gpu_manager.py 读写）
GPU_POOL_NAMESPACE = str(os.environ.get("GPU_POOL_NAMESPACE", "") or "").strip()


def _namespaced_gpu_pool_key(base_key: str) -> str:
    if not GPU_POOL_NAMESPACE:
        return base_key
    return f"{base_key}:{GPU_POOL_NAMESPACE}"


# 可用 GPU ID 列表
GPU_POOL_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:available")
# 有效 GPU ID 集合（防止无效 ID 被释放）
GPU_VALID_SET_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:valid_gpus")
# 任务 -> GPU 占用哈希
GPU_IN_USE_HASH_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:in_use")
# 等待 GPU 的非多肽子任务集合（公平调度：普通任务优先于多肽子任务续跑）
GPU_WAITING_NON_PEPTIDE_SET_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:waiting_non_peptide")
# GPU 设备元数据（gpu_id -> total MiB）；池初始化时写入，运行期只读
GPU_META_HASH_KEY = _namespaced_gpu_pool_key("boltz_gpu_pool:meta")
# 多肽父任务 -> 子任务 Celery ID 注册表前缀
PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX = "boltz_peptide_subtasks:"


# 3. 应用及 API 设置

# 结果存储
RESULTS_BASE_DIR = os.environ.get("RESULTS_BASE_DIR", "/data/boltz_central_results")

# 任务列表 Excel 异步导出（Celery 队列执行，产物落盘、状态存 Redis）
EXPORTS_BASE_DIR = os.environ.get(
    "EXPORTS_BASE_DIR",
    os.path.join(RESULTS_BASE_DIR, "exports"),
)
# 导出作业记录 TTL（秒）；到期后状态查询 404。
EXPORT_JOB_TTL_SECONDS = _parse_int_env("EXPORT_JOB_TTL_SECONDS", 48 * 3600, minimum=3600)
# 导出产物保留期（秒）；新导出启动时顺带清理过期文件。
EXPORT_FILE_TTL_SECONDS = _parse_int_env("EXPORT_FILE_TTL_SECONDS", 48 * 3600, minimum=3600)
# 单次导出最大任务行数；生产实测可达 1.3 万行。
EXPORT_MAX_TASK_ROWS = _parse_int_env("EXPORT_MAX_TASK_ROWS", 50000, minimum=1)
# 导出请求 JSON body 上限；与 MAX_UPLOAD_BYTES 对齐（1.3 万行导出约 40-52MB）。
EXPORT_REQUEST_MAX_BYTES = _parse_int_env("EXPORT_REQUEST_MAX_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024)

# Lead Optimization 输出目录（任务完成后打包上传）
LEAD_OPTIMIZATION_OUTPUT_DIR = os.environ.get(
    "LEAD_OPTIMIZATION_OUTPUT_DIR",
    "/data/boltz_lead_optimization_results"
)

# 任务结果保留与定期清理
# 超期结果按 mtime 判定删除；运行/排队中的任务不受影响。
RESULTS_RETENTION_DAYS = _parse_int_env("RESULTS_RETENTION_DAYS", 90, minimum=1)
# 由 monitor 按下方间隔周期执行文件系统 GC。
RESULTS_CLEANUP_ENABLED = _env_bool("RESULTS_CLEANUP_ENABLED", True)
# 清理间隔（秒）；monitor 启动即先跑一轮。
RESULTS_CLEANUP_INTERVAL_SECONDS = _parse_int_env("RESULTS_CLEANUP_INTERVAL_SECONDS", 6 * 3600, minimum=300)

# MSA 缓存保留期（缓存可整体重建，超期由 monitor 周期删除）
MSA_CACHE_RETENTION_DAYS = _parse_int_env("MSA_CACHE_RETENTION_DAYS", 90, minimum=1)

# 中心 API 地址（Worker 用它上传结果、更新状态）
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://localhost:5000")

# ColabFold MSA 服务地址；默认值仅本机联调，生产需显式配置。
MSA_SERVER_URL = os.environ.get("MSA_SERVER_URL", "http://localhost:8080")
MSA_SERVER_TIMEOUT_SECONDS = _parse_int_env("MSA_SERVER_TIMEOUT_SECONDS", 1800, minimum=60)

# ColabFold 服务器缓存目录（用于清理历史任务）
COLABFOLD_JOBS_DIR = os.environ.get(
    "COLABFOLD_JOBS_DIR",
    "/data/colabfold/jobs",
)


# 4. 安全性配置

# 生产环境必须通过环境变量显式设置。
BOLTZ_API_TOKEN = os.environ.get("BOLTZ_API_TOKEN", "development-api-token")

# 5. Boltz2 Docker 集成

BOLTZ2_DOCKER_IMAGE = os.environ.get("BOLTZ2_DOCKER_IMAGE", "vbio-boltz2-runtime")  # Shared by boltz2/boltz2score/affinity runtime
BOLTZ_MSA_CACHE_DIR = os.environ.get("BOLTZ_MSA_CACHE_DIR", "/data/boltz_msa_cache")  # MSA sequence cache on the /data partition
BOLTZ2_DOCKER_EXTRA_ARGS = os.environ.get("BOLTZ2_DOCKER_EXTRA_ARGS", "")
BOLTZ2_DOCKER_SHM_SIZE = os.environ.get("BOLTZ2_DOCKER_SHM_SIZE", "16g")
BOLTZ2_HOST_CACHE_DIR = os.environ.get("BOLTZ2_HOST_CACHE_DIR", "")
BOLTZ2_CONTAINER_CACHE_DIR = os.environ.get("BOLTZ2_CONTAINER_CACHE_DIR", "/root/.boltz")

# 6. AlphaFold3 Docker 集成

ALPHAFOLD3_DOCKER_IMAGE = os.environ.get("ALPHAFOLD3_DOCKER_IMAGE", "jurgjn/alphafold3:v3.0.2")
ALPHAFOLD3_MODEL_DIR = os.environ.get("ALPHAFOLD3_MODEL_DIR")
ALPHAFOLD3_DATABASE_DIR = os.environ.get("ALPHAFOLD3_DATABASE_DIR")
ALPHAFOLD3_DOCKER_EXTRA_ARGS = os.environ.get("ALPHAFOLD3_DOCKER_EXTRA_ARGS", "")

# 7. Protenix Docker 集成

PROTENIX_DOCKER_IMAGE = os.environ.get(
    "PROTENIX_DOCKER_IMAGE",
    "vbio-protenix-v2-runtime:2.0.0"
)
PROTENIX_MODEL_DIR = os.environ.get("PROTENIX_MODEL_DIR")
# Affinity-head checkpoint (read-only mount; empty = head disabled).
PROTENIX2DOCK_AFFINITY_CKPT = os.environ.get("PROTENIX2DOCK_AFFINITY_CKPT", "")
# Per-tenant daily /predict admission quota; <= 0 disables the gate.
TENANT_MAX_DAILY = int(os.environ.get("TENANT_MAX_DAILY", "50"))

# Task-completion email notifications (optional notify_email at submit).
FRONTEND_URL = os.environ.get("FRONTEND_URL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_SENDER_NAME = os.environ.get("SMTP_SENDER_NAME", "V-Bio")
# Task-email batching window in seconds; 0 = one immediate email per task.
NOTIFY_DIGEST_WINDOW_SECONDS = int(os.environ.get("NOTIFY_DIGEST_WINDOW_SECONDS", "900") or 0)
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
# Whole-module pickle cache dir (model construction ~80 s without it); empty disables.
PROTENIX_MODULE_CACHE_DIR = os.environ.get(
    "PROTENIX_MODULE_CACHE_DIR",
    "/data/protenix/module_cache",
)

# 8. Nesso Docker 集成

NESSO_DOCKER_IMAGE = os.environ.get("NESSO_DOCKER_IMAGE", "vbio-nesso-runtime:1.0.0")
NESSO_DOCKER_EXTRA_ARGS = os.environ.get("NESSO_DOCKER_EXTRA_ARGS", "")
NESSO_HOST_CACHE_DIR = os.environ.get("NESSO_HOST_CACHE_DIR", "/data/nesso_cache")
NESSO_CONTAINER_CACHE_DIR = os.environ.get("NESSO_CONTAINER_CACHE_DIR", "/workspace/nesso-cache")
NESSO_MODEL_REVISION = os.environ.get("NESSO_MODEL_REVISION", "v1.0.0")
NESSO_NO_KERNELS = os.environ.get("NESSO_NO_KERNELS", "true")
NESSO_RECYCLING_STEPS = _parse_int_env("NESSO_RECYCLING_STEPS", 5, minimum=0)
NESSO_NUM_WORKERS = _parse_int_env("NESSO_NUM_WORKERS", 2, minimum=1)
NESSO_PRECISION = os.environ.get("NESSO_PRECISION", "bf16-mixed")
