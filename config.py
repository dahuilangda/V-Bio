# config.py
"""
应用的中心配置文件。

此文件定义了所有组件（API 服务器, Celery Worker）共享的静态配置。
动态逻辑（如基于硬件检测的配置调整）应在相应组件的启动脚本中执行。
"""
import os

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
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 4))


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

# -- 中心 API 地址 --
# Worker 将使用此 URL 来上传结果和更新状态
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://localhost:5000")

# -- MSA 服务器地址 --
# ColabFold MSA 服务器的 URL，用于生成多序列比对
# 默认使用 ColabFold 官方服务器，也可以使用本地服务器
MSA_SERVER_URL = os.environ.get("MSA_SERVER_URL", "http://172.17.1.248:8080")


# ==============================================================================
# 4. 安全性配置 (Security)
# ==============================================================================

# -- API 令牌 --
# 用于外部客户端访问受保护的 API 端点
# 在生产环境中，必须通过环境变量设置此值。
# 例如: export API_SECRET_TOKEN='your-super-secret-token'
API_SECRET_TOKEN = os.environ.get("API_SECRET_TOKEN", "development-api-token")
