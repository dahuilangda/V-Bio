# config.py
import os
import torch

# -- Redis Configuration --
# 用于 Celery 任务队列和结果后端的 Redis 服务地址
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# -- Celery Configuration --
# 直接从 Redis 配置中获取
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# -- GPU & Concurrency Configuration --
# worker 可以同时运行的最大并发任务数
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 4))

# 自动检测当前机器上所有可用的 GPU
AVAILABLE_GPUS = list(range(torch.cuda.device_count()))

# 用于管理可用 GPU ID 的 Redis 列表的键名
GPU_POOL_KEY = "boltz_gpu_pool"

# 检查并发数设置是否超过实际 GPU 数量
if MAX_CONCURRENT_TASKS > len(AVAILABLE_GPUS):
    print(
        f"Warning: MAX_CONCURRENT_TASKS ({MAX_CONCURRENT_TASKS}) is greater than "
        f"the number of available GPUs ({len(AVAILABLE_GPUS)}). "
        f"Concurrency will be limited to {len(AVAILABLE_GPUS)}."
    )
    MAX_CONCURRENT_TASKS = len(AVAILABLE_GPUS)

# 根据最大并发数确定实际要投入使用的 GPU 设备列表
DEVICES_TO_USE = AVAILABLE_GPUS[:MAX_CONCURRENT_TASKS]

# -- Results Configuration --
# 此目录用于主应用服务器，存储从 worker 上传回来的中心化结果
RESULTS_BASE_DIR = os.environ.get("RESULTS_BASE_DIR", "/data/boltz_central_results")

# -- Central API Configuration --
# 中心 API 的 URL，用于接收 worker 上传的结果文件
CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "http://127.0.0.1:5000")

# -- Security Configuration --
# 用于保护管理 API 端点的秘密令牌。
# 在生产环境中，强烈建议通过环境变量设置此值。
# export API_SECRET_TOKEN='your-super-secret-token'
API_SECRET_TOKEN = os.environ.get("API_SECRET_TOKEN", "development-token")