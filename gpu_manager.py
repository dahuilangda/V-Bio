import redis
import config
import logging

# 使用标准日志记录模块
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 使用连接池以实现高效和安全的 Redis 连接
try:
    REDIS_CONNECTION_POOL = redis.ConnectionPool.from_url(config.REDIS_URL, decode_responses=True)
except Exception as e:
    logger.critical(f"无法创建 Redis 连接池，请检查 Redis 服务及配置: {e}")
    raise

def get_redis_client():
    """从共享连接池返回一个 Redis 客户端。"""
    return redis.Redis(connection_pool=REDIS_CONNECTION_POOL)

def initialize_gpu_pool(devices_to_use: list[int]):
    """
    根据给定的设备列表，初始化或重置 Redis 中的 GPU 池。

    Args:
        devices_to_use (list[int]): 要放入池中的 GPU 设备 ID 列表。
    """
    client = get_redis_client()
    logger.info("--- GPU 池初始化 ---")
    
    pipe = client.pipeline()
    
    # 1. 删除旧键，确保状态干净
    logger.info(f"正在删除旧键: {config.GPU_POOL_KEY}, {config.GPU_VALID_SET_KEY}, {config.GPU_IN_USE_HASH_KEY}")
    pipe.delete(config.GPU_POOL_KEY, config.GPU_VALID_SET_KEY, config.GPU_IN_USE_HASH_KEY)
    
    # 2. 将给定的设备 ID 添加到 SET 和 LIST 中
    if devices_to_use:
        logger.info(f"正在将 {devices_to_use} 添加到有效 GPU 集合 '{config.GPU_VALID_SET_KEY}'")
        pipe.sadd(config.GPU_VALID_SET_KEY, *devices_to_use)
        
        logger.info(f"正在将 {devices_to_use} 添加到可用池 '{config.GPU_POOL_KEY}'")
        pipe.rpush(config.GPU_POOL_KEY, *devices_to_use)
    else:
        logger.info("未提供任何设备，将创建一个空的 GPU 池。")

    pipe.execute()

    logger.info("--- 验证 ---")
    valid_gpus = client.scard(config.GPU_VALID_SET_KEY)
    available_gpus = client.llen(config.GPU_POOL_KEY)
    logger.info(f"SET 中的有效 GPU 数量: {valid_gpus}")
    logger.info(f"LIST 中的可用 GPU 数量: {available_gpus}")

    if valid_gpus == len(devices_to_use) and available_gpus == len(devices_to_use):
        logger.info("✅ GPU 池已准备就绪并已通过验证。")
    else:
        logger.warning("⚠️ GPU 池初始化可能失败。")
    logger.info("-----------------------------")


def acquire_gpu(task_id: str, timeout: int = 3600) -> int:
    """
    原子化地从池中获取一个 GPU。
    """
    client = get_redis_client()
    pool_key = config.GPU_POOL_KEY
    
    logger.info(f"任务 {task_id}: 正在尝试获取 GPU (最长等待 {timeout}s)...")
    
    # 阻塞式地从列表左侧弹出一个元素
    result = client.blpop(pool_key, timeout=timeout)
    
    if result is None:
        raise TimeoutError(f"任务 {task_id}: 在 {timeout}s 内未能获取 GPU。")
        
    _, gpu_id_str = result
    gpu_id = int(gpu_id_str)
    
    client.hset(config.GPU_IN_USE_HASH_KEY, gpu_id, task_id)
    
    logger.info(f"✅ 任务 {task_id}: 已获取 GPU {gpu_id}。")
    return gpu_id

def release_gpu(gpu_id: int, task_id: str):
    """
    原子化且安全地将一个 GPU ID 返回到池中。
    """
    client = get_redis_client()
    
    if not client.sismember(config.GPU_VALID_SET_KEY, gpu_id):
        logger.critical(f"严重错误: 任务 {task_id} 尝试释放无效的 GPU ID: {gpu_id}。已忽略。")
        return

    current_owner = client.hget(config.GPU_IN_USE_HASH_KEY, gpu_id)
    if current_owner != task_id:
        logger.error(
            f"错误: 任务 {task_id} 尝试释放 GPU {gpu_id}，但其当前所有者是 "
            f"'{current_owner}'。已忽略以防重复释放或错误释放。"
        )
        return
        
    pipe = client.pipeline()
    pipe.hdel(config.GPU_IN_USE_HASH_KEY, gpu_id)
    pipe.rpush(config.GPU_POOL_KEY, gpu_id)
    pipe.execute()
    
    logger.info(f"✅ 任务 {task_id}: 已将 GPU {gpu_id} 释放回池中。")

def get_gpu_status() -> dict:
    """一个用于监控所有 GPU 状态的辅助工具。"""
    client = get_redis_client()
    in_use = client.hgetall(config.GPU_IN_USE_HASH_KEY)
    available = client.lrange(config.GPU_POOL_KEY, 0, -1)
    return {
        "in_use": in_use,
        "available": available,
        "available_count": len(available),
        "in_use_count": len(in_use)
    }

# --- 管理脚本入口 ---
# 仅当直接运行此文件时，才会执行以下代码
if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 2:
        print("请提供一个命令: 'init' 或 'status'")
        sys.exit(1)

    command = sys.argv[1]
    
    if command == 'init':
        # 动态检测逻辑现在位于此处，仅在作为脚本运行时执行
        try:
            import torch  # type: ignore
        except Exception as exc:  # pragma: no cover - 安装环境相关
            torch = None  # type: ignore[assignment]
            logger.warning(f"无法导入 torch，用于 GPU 自动探测: {exc}")

        max_concurrent = config.MAX_CONCURRENT_TASKS
        configured_gpus = config.GPU_DEVICE_IDS or []
        detected_gpus: list[int] = []
        torch_detected_count: int | None = None

        if 'torch' in locals() and torch is not None:
            try:
                if torch.cuda.is_available():
                    torch_detected_count = torch.cuda.device_count()
                    detected_gpus = list(range(torch_detected_count))
                    logger.info(f"检测到 {len(detected_gpus)} 个可用 GPU: {detected_gpus}")
                else:
                    logger.info("torch.cuda 未检测到可用 GPU。")
            except Exception as e:
                logger.warning(f"无法初始化 torch.cuda: {e}")

        available_gpus = []

        if configured_gpus:
            available_gpus = configured_gpus.copy()
            logger.info(f"使用环境变量 GPU_DEVICE_IDS 指定的 GPU 列表: {available_gpus}")

            if torch_detected_count is not None:
                invalid_gpus = [gpu for gpu in available_gpus if not (0 <= gpu < torch_detected_count)]
                if invalid_gpus:
                    logger.warning(f"GPU_DEVICE_IDS 包含无效的 GPU ID，将忽略: {invalid_gpus}")
                available_gpus = [gpu for gpu in available_gpus if 0 <= gpu < torch_detected_count]
                if not available_gpus and detected_gpus:
                    logger.warning("GPU_DEVICE_IDS 中无有效 GPU，将回退到自动检测结果。")
                    available_gpus = detected_gpus.copy()
        else:
            available_gpus = detected_gpus.copy()

        if not available_gpus:
            logger.warning("未检测到可用 GPU，初始化空 GPU 池。")
            final_concurrency = 0
            devices_to_use = []
        else:
            final_concurrency = min(max_concurrent, len(available_gpus))
            if final_concurrency < len(available_gpus):
                logger.info(
                    f"MAX_CONCURRENT_TASKS={max_concurrent} 限制并发，实际使用 {final_concurrency} 块 GPU"
                )
            devices_to_use = available_gpus[:final_concurrency]

        logger.info(f"将使用以下设备初始化 GPU 池: {devices_to_use}")
        initialize_gpu_pool(devices_to_use)

    elif command == 'status':
        status = get_gpu_status()
        print("\n--- GPU Pool Status ---")
        print(f"Available ({status['available_count']}): {status['available']}")
        print(f"In Use ({status['in_use_count']}):")
        if status['in_use']:
            for gpu, task in status['in_use'].items():
                print(f"  - GPU {gpu}: Task {task}")
        else:
            print("  (None)")
        print("-----------------------")
        
    else:
        print(f"未知命令: {command}。可用命令: 'init', 'status'")
