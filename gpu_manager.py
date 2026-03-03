import redis
from backend.core import config
import logging
import time
import os
import shutil
import subprocess

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


def _read_gpu_pool_state(client: redis.Redis) -> tuple[set[int], list[int], dict[str, str]]:
    valid_raw = client.smembers(config.GPU_VALID_SET_KEY)
    available_raw = client.lrange(config.GPU_POOL_KEY, 0, -1)
    in_use_raw = client.hgetall(config.GPU_IN_USE_HASH_KEY)
    valid = {int(item) for item in valid_raw}
    available = [int(item) for item in available_raw]
    return valid, available, in_use_raw

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
    logger.info(
        "正在删除旧键: %s, %s, %s, %s",
        config.GPU_POOL_KEY,
        config.GPU_VALID_SET_KEY,
        config.GPU_IN_USE_HASH_KEY,
        config.GPU_WAITING_NON_PEPTIDE_SET_KEY,
    )
    pipe.delete(
        config.GPU_POOL_KEY,
        config.GPU_VALID_SET_KEY,
        config.GPU_IN_USE_HASH_KEY,
        config.GPU_WAITING_NON_PEPTIDE_SET_KEY,
    )
    
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


def ensure_gpu_pool(devices_to_use: list[int]):
    """
    Ensure a shared GPU pool exists without clobbering active allocations.
    """
    client = get_redis_client()
    desired = {int(device) for device in devices_to_use}
    current_valid, current_available, current_in_use = _read_gpu_pool_state(client)

    if not current_valid:
        logger.info("共享 GPU 池当前为空，执行初始化。")
        initialize_gpu_pool(devices_to_use)
        return

    if current_valid == desired:
        logger.info(
            "共享 GPU 池已存在，跳过重置。valid=%s available=%s in_use=%s",
            sorted(current_valid),
            current_available,
            current_in_use,
        )
        return

    if current_in_use:
        logger.warning(
            "共享 GPU 池设备集合与期望值不一致，但当前存在占用，保留现有池避免错误重置。current=%s desired=%s in_use=%s",
            sorted(current_valid),
            sorted(desired),
            current_in_use,
        )
        return

    logger.info(
        "共享 GPU 池设备集合与期望值不一致，且当前无占用，重建池。current=%s desired=%s",
        sorted(current_valid),
        sorted(desired),
    )
    initialize_gpu_pool(devices_to_use)


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


def register_non_peptide_gpu_waiter(task_id: str) -> None:
    """Register a non-peptide task as waiting for GPU allocation."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return
    client = get_redis_client()
    client.sadd(config.GPU_WAITING_NON_PEPTIDE_SET_KEY, normalized_task_id)


def unregister_non_peptide_gpu_waiter(task_id: str) -> None:
    """Remove a non-peptide task from waiting set after acquire attempt completes."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return
    client = get_redis_client()
    client.srem(config.GPU_WAITING_NON_PEPTIDE_SET_KEY, normalized_task_id)


def get_non_peptide_gpu_waiter_count() -> int:
    client = get_redis_client()
    try:
        return int(client.scard(config.GPU_WAITING_NON_PEPTIDE_SET_KEY) or 0)
    except Exception:
        return 0


def acquire_gpu_for_peptide_worker(task_id: str, timeout: int = 3600, poll_interval: float = 1.0) -> int:
    """
    Fair GPU acquire for peptide candidate workers.
    If any non-peptide tasks are waiting for GPU, peptide workers yield and retry.
    """
    client = get_redis_client()
    deadline = time.monotonic() + max(1, int(timeout))
    sleep_step = max(0.2, float(poll_interval))
    logger.info(f"任务 {task_id}: 多肽子任务开始公平获取 GPU (timeout={timeout}s)。")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"任务 {task_id}: 多肽子任务在 {timeout}s 内未能获取 GPU。")

        try:
            waiting_non_peptide = int(client.scard(config.GPU_WAITING_NON_PEPTIDE_SET_KEY) or 0)
        except Exception:
            waiting_non_peptide = 0
        if waiting_non_peptide > 0:
            time.sleep(min(sleep_step, max(0.2, remaining)))
            continue

        blpop_timeout = max(1, min(int(remaining), int(round(sleep_step))))
        result = client.blpop(config.GPU_POOL_KEY, timeout=blpop_timeout)
        if result is None:
            continue

        _, gpu_id_str = result
        gpu_id = int(gpu_id_str)
        client.hset(config.GPU_IN_USE_HASH_KEY, gpu_id, task_id)
        logger.info(f"✅ 任务 {task_id}: 多肽子任务已获取 GPU {gpu_id}。")
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
        "in_use_count": len(in_use),
        "waiting_non_peptide_count": get_non_peptide_gpu_waiter_count(),
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

        if not detected_gpus:
            try:
                if shutil.which("nvidia-smi"):
                    probe = subprocess.run(
                        ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if probe.returncode == 0:
                        lines = [line.strip() for line in (probe.stdout or "").splitlines() if line.strip()]
                        parsed = []
                        for line in lines:
                            try:
                                parsed.append(int(line))
                            except ValueError:
                                continue
                        if parsed:
                            detected_gpus = sorted(set(parsed))
                            logger.info(f"通过 nvidia-smi 检测到可用 GPU: {detected_gpus}")
                    else:
                        logger.warning(f"nvidia-smi 探测 GPU 失败: {probe.stderr.strip()}")
            except Exception as exc:
                logger.warning(f"使用 nvidia-smi 自动探测 GPU 失败: {exc}")

        if not detected_gpus:
            raw_visible = str(os.environ.get("NVIDIA_VISIBLE_DEVICES", "") or "").strip()
            if raw_visible and raw_visible.lower() not in {"all", "none", "void"}:
                parsed_visible = []
                for token in raw_visible.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    try:
                        parsed_visible.append(int(token))
                    except ValueError:
                        continue
                if parsed_visible:
                    detected_gpus = sorted(set(parsed_visible))
                    logger.info(f"通过 NVIDIA_VISIBLE_DEVICES 推断可用 GPU: {detected_gpus}")

        if not detected_gpus:
            try:
                proc_gpus_dir = "/proc/driver/nvidia/gpus"
                if os.path.isdir(proc_gpus_dir):
                    gpu_entries = [item for item in os.listdir(proc_gpus_dir) if item.strip()]
                    if gpu_entries:
                        detected_gpus = list(range(len(gpu_entries)))
                        logger.info(f"通过 {proc_gpus_dir} 检测到可用 GPU: {detected_gpus}")
            except Exception as exc:
                logger.warning(f"通过 /proc 路径探测 GPU 失败: {exc}")

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
            if max_concurrent <= 0:
                final_concurrency = len(available_gpus)
                logger.info(
                    "MAX_CONCURRENT_TASKS<=0，自动使用全部探测到的 GPU: %s",
                    final_concurrency,
                )
            else:
                final_concurrency = min(max_concurrent, len(available_gpus))
                if final_concurrency < len(available_gpus):
                    logger.info(
                        f"MAX_CONCURRENT_TASKS={max_concurrent} 限制并发，实际使用 {final_concurrency} 块 GPU"
                    )
            devices_to_use = available_gpus[:final_concurrency]

        logger.info(f"将使用以下设备确保 GPU 池就绪: {devices_to_use}")
        ensure_gpu_pool(devices_to_use)

    elif command == 'status':
        status = get_gpu_status()
        print("\n--- GPU Pool Status ---")
        print(f"Available ({status['available_count']}): {status['available']}")
        print(f"In Use ({status['in_use_count']}):")
        print(f"Waiting non-peptide: {status.get('waiting_non_peptide_count', 0)}")
        if status['in_use']:
            for gpu, task in status['in_use'].items():
                print(f"  - GPU {gpu}: Task {task}")
        else:
            print("  (None)")
        print("-----------------------")
        
    else:
        print(f"未知命令: {command}。可用命令: 'init', 'status'")
