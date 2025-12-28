# config.py
"""
应用的中心配置文件。

此文件定义了所有组件（API 服务器, Celery Worker）共享的静态配置。
动态逻辑（如基于硬件检测的配置调整）应在相应组件的启动脚本中执行。
"""
import os
import re
from pathlib import Path
from dotenv import load_dotenv

# 尝试加载 .env 文件
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file, override=False)


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

def print_config_debug_info():
    """打印当前配置调试信息"""
    print("\n" + "="*60)
    print("Boltz-WebUI 配置信息")
    print("="*60)

    config_vars = [
        'REDIS_URL', 'MAX_CONCURRENT_TASKS', 'CENTRAL_API_URL',
        'MSA_SERVER_URL', 'RESULTS_BASE_DIR', 'BOLTZ_API_TOKEN'
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

# -- Celery 队列名称 --
# 定义任务队列，用于 API 服务器和 Worker 区分任务优先级
# 高优先级队列
HIGH_PRIORITY_QUEUE = 'high_priority'
# 默认队列
DEFAULT_QUEUE = 'default'

# ==============================================================================
# 2. Worker & GPU 配置
# ==============================================================================

# -- Worker 并发设置 --
# Worker 可以同时运行的最大并发任务数。
# 这是一个“期望值”，实际的并发数应在 Worker 启动时根据可用 GPU 动态调整。
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 2))

# -- GPU 设备选择 --
# 通过环境变量 GPU_DEVICE_IDS 指定可用的 GPU ID 列表（例如："0,1,3"）。
# 如果未设置，则在初始化时自动探测所有可用 GPU。
GPU_DEVICE_IDS = _parse_gpu_device_ids(os.environ.get("GPU_DEVICE_IDS"))


# -- GPU 资源池 Redis 键 --
# 这些键由 GPU 管理器 (gpu_manager.py) 使用，以安全地追踪和分配 GPU 资源。
# 用于管理可用 GPU ID 的 Redis 列表
GPU_POOL_KEY = "boltz_gpu_pool:available"
# 用于存储所有有效 GPU ID 的 Redis 集合（防止无效 GPU ID 被释放）
GPU_VALID_SET_KEY = "boltz_gpu_pool:valid_gpus"
# 用于追踪任务与 GPU 占用关系的 Redis 哈希
GPU_IN_USE_HASH_KEY = "boltz_gpu_pool:in_use"


# ==============================================================================
# 3. 应用及 API 设置
# ==============================================================================

# -- 结果存储 --
# 用于主 API 服务器存储从 Worker 上传回来的中心化结果文件的目录
RESULTS_BASE_DIR = os.environ.get("RESULTS_BASE_DIR", "/data/boltz_central_results")

# -- 虚拟筛选/Lead Optimization 输出目录 --
# 控制虚拟筛选与 lead optimization 的本地输出落盘位置（任务完成后会打包上传）
VIRTUAL_SCREENING_OUTPUT_DIR = os.environ.get(
    "VIRTUAL_SCREENING_OUTPUT_DIR",
    "/data/boltz_virtual_screening_results"
)
LEAD_OPTIMIZATION_OUTPUT_DIR = os.environ.get(
    "LEAD_OPTIMIZATION_OUTPUT_DIR",
    "/data/boltz_lead_optimization_results"
)

# -- 中心 API 地址 --
# Worker 将使用此 URL 来上传结果和更新状态
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://localhost:5000")

# -- MSA 服务器地址 --
# ColabFold MSA 服务器的 URL，用于生成多序列比对
# 默认使用 ColabFold 官方服务器，也可以使用本地服务器
MSA_SERVER_URL = os.environ.get("MSA_SERVER_URL", "http://172.17.1.248:8080")
MSA_SERVER_MODE = os.environ.get("MSA_SERVER_MODE", "colabfold")

# ColabFold 服务器缓存目录（用于清理历史任务）
COLABFOLD_JOBS_DIR = os.environ.get(
    "COLABFOLD_JOBS_DIR",
    str(Path(__file__).parent / "colabfold_server" / "jobs")
)


# ==============================================================================
# 4. 安全性配置 (Security)
# ==============================================================================

# -- Boltz API 令牌 --
# 用于外部客户端访问受保护的 API 端点和连接到外部 Boltz 服务
# 在生产环境中，必须通过环境变量设置此值。
# 例如: export BOLTZ_API_TOKEN='your-super-secret-token'
BOLTZ_API_TOKEN = os.environ.get("BOLTZ_API_TOKEN", "development-api-token")

# ==============================================================================
# 5. AlphaFold3 Docker 集成
# ==============================================================================

ALPHAFOLD3_DOCKER_IMAGE = os.environ.get("ALPHAFOLD3_DOCKER_IMAGE", "alphafold3")
ALPHAFOLD3_MODEL_DIR = os.environ.get("ALPHAFOLD3_MODEL_DIR")
ALPHAFOLD3_DATABASE_DIR = os.environ.get("ALPHAFOLD3_DATABASE_DIR")
ALPHAFOLD3_DOCKER_EXTRA_ARGS = os.environ.get("ALPHAFOLD3_DOCKER_EXTRA_ARGS", "")
