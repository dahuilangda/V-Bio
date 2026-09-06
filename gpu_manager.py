import redis
from backend.core import config
import logging
import math
import time
import os
import shutil
import subprocess
from typing import Any

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


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _collect_live_celery_task_ids() -> set[str]:
    """
    Collect currently live Celery task ids from active/reserved/scheduled slots.
    """
    live: set[str] = set()
    try:
        from backend.core.celery_app import celery_app
    except Exception as exc:
        logger.warning("无法导入 celery_app 以检查活跃任务，将跳过 live-task 检查: %s", exc)
        return live

    try:
        inspector = celery_app.control.inspect(timeout=2)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
    except Exception as exc:
        logger.warning("检查 Celery 活跃任务失败，将跳过 live-task 检查: %s", exc)
        return live

    def _append_from_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            request = row.get("request")
            if isinstance(request, dict):
                task_id = str(request.get("id") or "").strip()
                if task_id:
                    live.add(task_id)
            task_id = str(row.get("id") or row.get("task_id") or "").strip()
            if task_id:
                live.add(task_id)

    for payload in (active, reserved, scheduled):
        if not isinstance(payload, dict):
            continue
        for rows in payload.values():
            _append_from_rows(rows)
    return live


def _read_task_state(task_id: str) -> str:
    normalized = str(task_id or "").strip()
    if not normalized:
        return ""
    try:
        from backend.core.celery_app import celery_app
        from celery.result import AsyncResult
        return str(AsyncResult(normalized, app=celery_app).state or "").strip().upper()
    except Exception:
        return ""


def _rebuild_available_gpu_queue(client: redis.Redis, valid: set[int], in_use_raw: dict[str, str]) -> tuple[list[int], list[int]]:
    in_use_ids: set[int] = set()
    for gpu_key in in_use_raw.keys():
        parsed = _to_int(gpu_key)
        if parsed is not None:
            in_use_ids.add(parsed)
    expected_available = sorted([gpu_id for gpu_id in valid if gpu_id not in in_use_ids])
    current_available_raw = client.lrange(config.GPU_POOL_KEY, 0, -1)
    current_available = []
    for item in current_available_raw:
        parsed = _to_int(item)
        if parsed is not None:
            current_available.append(parsed)

    if current_available == expected_available:
        return current_available, expected_available

    pipe = client.pipeline()
    pipe.delete(config.GPU_POOL_KEY)
    if expected_available:
        pipe.rpush(config.GPU_POOL_KEY, *expected_available)
    pipe.execute()
    return current_available, expected_available


def _reconcile_in_use_allocations(client: redis.Redis, valid: set[int]) -> dict[str, Any]:
    """
    Reconcile stale in-use leases:
    - Keep leases owned by live celery tasks.
    - Keep leases with active heartbeat.
    - Reclaim leases for terminal tasks.
    - Reclaim PENDING leases that are not live and have no heartbeat (typical worker-crash orphan).

    Reclaims mirror release_gpu: a stale lease is only dropped (returning the GPU to the
    available pool) once GPU memory is confirmed reclaimed, so a stale lease is never handed
    out dirty to the next task. If memory is still in use the lease is retained and reported
    as held_dirty.
    """
    in_use_raw = client.hgetall(config.GPU_IN_USE_HASH_KEY) or {}
    if not in_use_raw:
        return {"released": [], "kept": {}, "held_dirty": [], "live_tasks": 0}

    live_task_ids = _collect_live_celery_task_ids()
    released: list[tuple[int, str, str]] = []
    held_dirty: list[tuple[int, str, str]] = []
    kept: dict[str, str] = {}

    def _reclaim_if_memory_reclaimed(gpu_id: int, task_id: str, reason: str) -> None:
        # 与 release_gpu 一致：只有确认显存已回收才解除 lease，避免脏卡被重新分配。
        if _wait_gpu_memory_reclaimed(gpu_id, task_id):
            # 条件删除：仅当 in_use[gpu_id] 仍为当前 task 时才解除，避免与并发 release_gpu
            # 双方都通过内存门后重复把 GPU 推回 available（rebuild 也会兜底去重）。
            script = (
                "if redis.call('hget', KEYS[1], ARGV[1]) == ARGV[2] "
                "then return redis.call('hdel', KEYS[1], ARGV[1]) else return 0 end"
            )
            removed = client.eval(script, 1, config.GPU_IN_USE_HASH_KEY, str(gpu_id), task_id)
            if int(removed or 0) > 0:
                released.append((gpu_id, task_id, reason))
            else:
                logger.debug("GPU %s lease 已被并发 release，跳过重复回收。", gpu_id)
        else:
            held_dirty.append((gpu_id, task_id, reason))
            kept[str(gpu_id)] = task_id

    for gpu_key, owner_task_id in in_use_raw.items():
        gpu_id = _to_int(gpu_key)
        task_id = str(owner_task_id or "").strip()
        if gpu_id is None:
            # Invalid hash field, purge defensively.
            client.hdel(config.GPU_IN_USE_HASH_KEY, gpu_key)
            continue
        if gpu_id not in valid:
            # GPU no longer part of valid set.
            client.hdel(config.GPU_IN_USE_HASH_KEY, gpu_key)
            continue
        if not task_id:
            _reclaim_if_memory_reclaimed(gpu_id, "", "empty_owner")
            continue
        if task_id in live_task_ids:
            kept[str(gpu_id)] = task_id
            continue

        has_heartbeat = bool(client.exists(f"task_heartbeat:{task_id}"))
        if has_heartbeat:
            kept[str(gpu_id)] = task_id
            continue

        state = _read_task_state(task_id)
        if state in {"SUCCESS", "FAILURE", "REVOKED"}:
            _reclaim_if_memory_reclaimed(gpu_id, task_id, f"terminal_{state.lower()}")
            continue
        if state in {"PROGRESS", "STARTED", "RECEIVED", "RETRY"}:
            _reclaim_if_memory_reclaimed(gpu_id, task_id, f"{state.lower()}_without_live_or_heartbeat")
            continue
        if state == "PENDING":
            _reclaim_if_memory_reclaimed(gpu_id, task_id, "pending_without_live_or_heartbeat")
            continue

        kept[str(gpu_id)] = task_id

    return {"released": released, "kept": kept, "held_dirty": held_dirty, "live_tasks": len(live_task_ids)}


# acquire 阻塞期间自动回收孤儿 lease 并重建 available，避免 worker 强杀 / NVML 失效等
# 导致 lease 泄漏后永久死锁。复用现有 reconcile 原语，运行时不重置设备集合。
RECONCILE_LOCK_KEY = "boltz_gpu_pool:reconcile_lock"  # 不加 namespace：全局单写者
RECONCILE_LOCK_TTL_SECONDS = 30
RECONCILE_MIN_INTERVAL_SECONDS = RECONCILE_LOCK_TTL_SECONDS


def _try_acquire_reconcile_lock(client: Any, task_id: str) -> bool:
    """NX 单写者守卫；返回 True 时由本调用方执行 reconcile。"""
    try:
        return bool(client.set(RECONCILE_LOCK_KEY, task_id, nx=True, ex=RECONCILE_LOCK_TTL_SECONDS))
    except Exception:
        return True  # Redis 抖动时放行一个调用方，避免阻塞所有 acquire


def _release_reconcile_lock(client: Any, task_id: str) -> None:
    try:
        # 仅在仍持锁时删除，防误删；失败由 TTL 兜底
        client.eval(
            "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
            1, RECONCILE_LOCK_KEY, task_id,
        )
    except Exception:
        pass


def _run_throttled_reconcile(client: Any, task_id: str, last_reconcile: float) -> float:
    """按最小间隔 + NX 锁节流执行一次 reconcile，返回更新后的 last_reconcile 时间戳。"""
    if time.monotonic() - last_reconcile < RECONCILE_MIN_INTERVAL_SECONDS:
        return last_reconcile
    if not _try_acquire_reconcile_lock(client, task_id):
        return last_reconcile
    try:
        reconcile_gpu_pool(client)
        return time.monotonic()
    except Exception as exc:
        logger.warning(f"任务 {task_id}: reconcile 失败，继续 blpop: {exc}")
        return last_reconcile
    finally:
        _release_reconcile_lock(client, task_id)


def reconcile_gpu_pool(client: Any = None) -> dict:
    """回收孤儿 lease 并重建 available = valid - in_use。valid 为空时跳过，不抛错。"""
    client = client or get_redis_client()
    valid, _available, _in_use_raw = _read_gpu_pool_state(client)
    if not valid:
        return {"skipped": True, "valid": []}

    reconcile = _reconcile_in_use_allocations(client, valid)
    for key, prefix in (
        ("released", "回收陈旧 GPU 占用"),
        ("held_dirty", "陈旧占用显存未回收，保留 lease"),
    ):
        items = reconcile.get(key) or []
        if items:
            msg = "; ".join(f"gpu={g},task={t or '-'},reason={r}" for g, t, r in items)
            logger.warning("%s: %s", prefix, msg)

    _current_valid, _current_available, in_use_after = _read_gpu_pool_state(client)
    _rebuild_available_gpu_queue(client, valid, in_use_after)
    return {"skipped": False, "valid": sorted(valid), **reconcile}


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
        "正在删除旧键: %s, %s, %s, %s, %s",
        config.GPU_POOL_KEY,
        config.GPU_VALID_SET_KEY,
        config.GPU_IN_USE_HASH_KEY,
        config.GPU_WAITING_NON_PEPTIDE_SET_KEY,
        config.GPU_META_HASH_KEY,
    )
    pipe.delete(
        config.GPU_POOL_KEY,
        config.GPU_VALID_SET_KEY,
        config.GPU_IN_USE_HASH_KEY,
        config.GPU_WAITING_NON_PEPTIDE_SET_KEY,
        config.GPU_META_HASH_KEY,
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

    if devices_to_use:
        # 设备元数据（总显存）在池建立时一次性登记；任务期只读 Redis。
        _write_gpu_meta(client, list(devices_to_use))

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
    current_valid, _current_available, current_in_use = _read_gpu_pool_state(client)

    if not current_valid:
        logger.info("共享 GPU 池当前为空，执行初始化。")
        initialize_gpu_pool(devices_to_use)
        return

    reconcile = _reconcile_in_use_allocations(client, current_valid)
    if reconcile.get("released"):
        released_msgs = [
            f"gpu={gpu_id},task={task_id or '-'},reason={reason}"
            for gpu_id, task_id, reason in reconcile["released"]
        ]
        logger.warning("检测到并回收陈旧 GPU 占用: %s", "; ".join(released_msgs))
    if reconcile.get("held_dirty"):
        dirty_msgs = [
            f"gpu={gpu_id},task={task_id or '-'},reason={reason}"
            for gpu_id, task_id, reason in reconcile["held_dirty"]
        ]
        logger.warning(
            "陈旧 GPU 占用显存未回收，暂保留 lease 避免脏卡被重新分配: %s",
            "; ".join(dirty_msgs),
        )
    current_valid, current_available, current_in_use = _read_gpu_pool_state(client)
    old_available, expected_available = _rebuild_available_gpu_queue(client, current_valid, current_in_use)
    if old_available != expected_available:
        logger.info(
            "已重建 GPU 可用队列: old_available=%s -> new_available=%s",
            old_available,
            expected_available,
        )

    if current_valid == desired:
        logger.info(
            "共享 GPU 池已存在，跳过重置。valid=%s available=%s in_use=%s",
            sorted(current_valid),
            expected_available,
            current_in_use,
        )
        # 每次 worker 启动都补齐缺项，保证池元数据完整（任务期只读元数据）。
        _write_gpu_meta(client, sorted(current_valid))
        return

    if current_in_use:
        logger.warning(
            "共享 GPU 池设备集合与期望值不一致，但当前存在占用，保留现有池避免错误重置。current=%s desired=%s in_use=%s",
            sorted(current_valid),
            sorted(desired),
            current_in_use,
        )
        # 不重置，但元数据仍要补齐：任务期只读元数据，缺项会显式报错。
        _write_gpu_meta(client, sorted(current_valid))
        return

    logger.info(
        "共享 GPU 池设备集合与期望值不一致，且当前无占用，重建池。current=%s desired=%s",
        sorted(current_valid),
        sorted(desired),
    )
    initialize_gpu_pool(devices_to_use)


def acquire_gpu(task_id: str, timeout: int = 3600) -> int:
    """从池中获取一个 GPU。阻塞期间节流 reconcile 自愈；超时抛 TimeoutError。

    拿到池成员后先做池外占用准入检查（acquire_gpu_external_occupancy）：被外部
    计算进程（如绕过 Celery 直接 docker run 的训练任务）占用的 GPU 会被放回队尾
    并换下一块，而不是分配出去导致 CUDA OOM。
    """
    client = get_redis_client()
    pool_key = config.GPU_POOL_KEY
    timeout_seconds = max(0, int(timeout))
    deadline = time.monotonic() + timeout_seconds
    blpop_slice = 30  # 与 RECONCILE_LOCK_TTL 对齐

    logger.info(f"任务 {task_id}: 正在尝试获取 GPU (最长等待 {timeout_seconds}s)...")

    last_reconcile = 0.0
    rejected_gpu_ids: set[int] = set()
    while True:
        last_reconcile = _run_throttled_reconcile(client, task_id, last_reconcile)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            hint = ""
            if rejected_gpu_ids:
                hint = (
                    f"（其中 GPU {sorted(rejected_gpu_ids)} 存在池外计算型占用，"
                    "已被跳过——请清理外部进程或释放对应 GPU）"
                )
            raise TimeoutError(f"任务 {task_id}: 在 {timeout_seconds}s 内未能获取 GPU。{hint}")
        result = client.blpop(pool_key, timeout=int(max(1, math.ceil(min(blpop_slice, remaining)))))
        if result is not None:
            gpu_id = int(result[1])
            admitted, detail = _gpu_admission_check(gpu_id, task_id)
            if not admitted:
                rejected_gpu_ids.add(gpu_id)
                client.rpush(pool_key, gpu_id)
                time.sleep(0.5)
                continue
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


def acquire_gpu_for_peptide_worker(task_id: str, timeout: int = 0, poll_interval: float = 1.0) -> int:
    """
    Fair GPU acquire for peptide candidate workers.
    If any non-peptide tasks are waiting for GPU, peptide workers yield and retry.
    """
    client = get_redis_client()
    timeout_seconds = int(timeout or 0)
    deadline = None if timeout_seconds <= 0 else (time.monotonic() + max(1, timeout_seconds))
    sleep_step = max(0.2, float(poll_interval))
    logger.info(
        "任务 %s: 多肽子任务开始公平获取 GPU (timeout=%s)。",
        task_id,
        "disabled" if deadline is None else f"{timeout_seconds}s",
    )

    last_reconcile = 0.0

    while True:
        if deadline is None:
            remaining = None
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"任务 {task_id}: 多肽子任务在 {timeout_seconds}s 内未能获取 GPU。")

        try:
            waiting_non_peptide = int(client.scard(config.GPU_WAITING_NON_PEPTIDE_SET_KEY) or 0)
        except Exception:
            waiting_non_peptide = 0
        if waiting_non_peptide > 0:
            if remaining is None:
                time.sleep(sleep_step)
            else:
                time.sleep(min(sleep_step, max(0.2, remaining)))
            continue

        if remaining is None:
            blpop_timeout = max(1, int(round(sleep_step)))
        else:
            blpop_timeout = max(1, min(int(remaining), int(round(sleep_step))))
        result = client.blpop(config.GPU_POOL_KEY, timeout=blpop_timeout)
        if result is None:
            # 池空：节流 reconcile 自愈后重试。公平性检查（上方）不受影响。
            last_reconcile = _run_throttled_reconcile(client, task_id, last_reconcile)
            continue

        _, gpu_id_str = result
        gpu_id = int(gpu_id_str)
        admitted, detail = _gpu_admission_check(gpu_id, task_id)
        if not admitted:
            # 池外计算型占用：放回队尾换下一块，等待语义由外层循环与 deadline 保证。
            client.rpush(config.GPU_POOL_KEY, gpu_id)
            time.sleep(min(sleep_step, 0.5))
            continue
        client.hset(config.GPU_IN_USE_HASH_KEY, gpu_id, task_id)
        logger.info(f"✅ 任务 {task_id}: 多肽子任务已获取 GPU {gpu_id}。")
        return gpu_id

def _query_gpu_used_memory_mib(gpu_id: int) -> int | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                f"--id={int(gpu_id)}",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning("查询 GPU %s 显存失败: %s", gpu_id, exc)
        return None
    if proc.returncode != 0:
        logger.warning("查询 GPU %s 显存失败: %s", gpu_id, (proc.stderr or "").strip())
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(float(line))
        except ValueError:
            continue
    return None


# 准入判定阈值：与 _wait_gpu_memory_reclaimed 的回收阈值保持同一口径——
# 池内空闲卡经释放回收后计算占用应为 ~0，任何超过该值的计算进程都是池外占用者。
GPU_ADMISSION_COMPUTE_THRESHOLD_MIB = 200
# compute-apps 查询不可用时的回退判据：按总显存占用区分"桌面型"（浏览器等
# 图形进程，几百 MB ~ 2GB，不阻断任务）与"计算型"（训练/推理，远超此值）。
GPU_ADMISSION_DESKTOP_FALLBACK_MIB = 2048
GPU_ADMISSION_FALLBACK_TOTAL_RATIO = 0.20


def _query_gpu_compute_memory_mib(gpu_id: int) -> int | None:
    """Sum of CUDA compute processes' memory on a GPU; None when undetectable.

    `--query-compute-apps` 只列出 CUDA 计算上下文——训练/推理进程都会出现，
    而 Firefox/Chrome 等浏览器的图形（GL/Vulkan）上下文不会出现，因此这是
    区分"计算型占用"与"桌面型占用"的精确信号。
    """
    if not shutil.which("nvidia-smi"):
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                f"--id={int(gpu_id)}",
                "--query-compute-apps=used_memory",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning("查询 GPU %s 计算进程占用失败: %s", gpu_id, exc)
        return None
    if proc.returncode != 0:
        logger.warning("查询 GPU %s 计算进程占用失败: %s", gpu_id, (proc.stderr or "").strip())
        return None
    total = 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += int(float(line))
        except ValueError:
            continue
    return total


def _gpu_admission_check(gpu_id: int, task_id: str) -> tuple[bool, str]:
    """Acquire 准入检查：拒绝被池外计算进程占用的 GPU。

    返回 (是否放行, 说明)。判定顺序：
    1. compute-apps 合计（精确信号）：> 200MiB 即存在外部计算进程，拒绝；
       浏览器等桌面图形进程天然不出现在此列表，不会被误杀。
    2. compute-apps 查询不可用时回退到总显存启发式：used > 2GiB 且
       (占比 >= 20% 或总显存未知) 视为计算型占用；少量/桌面占用放行。
    3. 两个查询都失败（NVML 盲区，如 cgroup v2 授权失效）：与释放路径一致，
       乐观放行并告警——不因监控盲区锁死整个池。
    """
    compute_mem = _query_gpu_compute_memory_mib(gpu_id)
    if compute_mem is not None:
        if compute_mem <= GPU_ADMISSION_COMPUTE_THRESHOLD_MIB:
            return True, f"无池外计算占用 (compute={compute_mem}MiB)"
        logger.warning(
            "任务 %s: GPU %s 被池外计算进程占用 (compute=%sMiB > %sMiB)，跳过该卡。",
            task_id,
            gpu_id,
            compute_mem,
            GPU_ADMISSION_COMPUTE_THRESHOLD_MIB,
        )
        return False, f"compute={compute_mem}MiB"

    used = _query_gpu_used_memory_mib(gpu_id)
    if used is None:
        logger.warning(
            "任务 %s: GPU %s 占用状态不可探测（NVML 盲区），按既有策略乐观放行。",
            task_id,
            gpu_id,
        )
        return True, "unknown"
    total = get_gpu_total_memory_mib(gpu_id)
    heavy = used > GPU_ADMISSION_DESKTOP_FALLBACK_MIB and (
        total is None or used >= GPU_ADMISSION_FALLBACK_TOTAL_RATIO * total
    )
    if not heavy:
        return True, f"轻量占用 (used={used}MiB)"
    logger.warning(
        "任务 %s: GPU %s 疑似被池外任务占用 (used=%sMiB, total=%s)，跳过该卡。",
        task_id,
        gpu_id,
        used,
        total,
    )
    return False, f"used={used}MiB"


def _probe_gpu_total_memory_mib(gpu_id: int) -> int | None:
    """Total physical VRAM of a GPU via nvidia-smi; None when undetectable.

    Called ONLY at pool init/ensure time — the per-device total is then kept in
    the GPU_META_HASH_KEY hash so task-time decisions never probe hardware.
    """
    if not shutil.which("nvidia-smi"):
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                f"--id={int(gpu_id)}",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning("查询 GPU %s 总显存失败: %s", gpu_id, exc)
        return None
    if proc.returncode != 0:
        logger.warning("查询 GPU %s 总显存失败: %s", gpu_id, (proc.stderr or "").strip())
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(float(line))
        except ValueError:
            continue
    return None


def _write_gpu_meta(client: Any, devices: list[int]) -> None:
    """Upsert total-VRAM metadata for the given devices (skips already-recorded ones)."""
    devices = sorted({int(d) for d in devices})
    if not devices:
        return
    existing = {int(k) for k in (client.hkeys(config.GPU_META_HASH_KEY) or {})}
    for gpu_id in devices:
        if gpu_id in existing:
            continue
        total_mib = _probe_gpu_total_memory_mib(gpu_id)
        if total_mib is None:
            logger.warning(
                "GPU %s 总显存探测失败，元数据缺项（依赖它的判定将显式报错）。",
                gpu_id,
            )
            continue
        client.hset(config.GPU_META_HASH_KEY, gpu_id, total_mib)


def get_gpu_total_memory_mib(gpu_id: int) -> int | None:
    """Recorded total VRAM of a pool GPU; None when the pool metadata lacks it."""
    client = get_redis_client()
    raw = client.hget(config.GPU_META_HASH_KEY, int(gpu_id))
    if raw is None:
        return None
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _wait_gpu_memory_reclaimed(
    gpu_id: int,
    task_id: str,
    *,
    threshold_mib: int = 200,
    timeout_seconds: float = 90.0,
    poll_seconds: float = 1.0,
    nvml_grace_seconds: float = 5.0,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    nvml_grace_deadline = time.monotonic() + max(0.0, float(nvml_grace_seconds))
    nvml_unavailable = False
    last_used: int | None = None
    while True:
        used = _query_gpu_used_memory_mib(gpu_id)
        if used is None:
            # NVML 不可用（如 cgroup v2 容器授权失效）。保守保留（return False）会让 release_gpu /
            # reconcile 全局死锁。调用方已校验 owner / 判定 task 死亡，driver 会回收死进程显存，
            # 故短暂等待后乐观释放。
            if not nvml_unavailable:
                nvml_unavailable = True
                logger.warning("任务 %s: NVML 不可用，无法查询 GPU %s 显存，短暂等待后乐观释放。", task_id, gpu_id)
            if time.monotonic() >= nvml_grace_deadline:
                logger.warning("任务 %s: NVML 持续不可用，乐观释放 GPU %s。", task_id, gpu_id)
                return True
            time.sleep(min(max(0.2, float(poll_seconds)), 0.5))
            continue
        nvml_unavailable = False
        last_used = used
        if used <= int(threshold_mib):
            logger.info(
                "✅ 任务 %s: GPU %s 显存已确认回收 (used=%sMiB)。",
                task_id,
                gpu_id,
                used,
            )
            return True
        if time.monotonic() >= deadline:
            logger.critical(
                "严重: 任务 %s 释放 GPU %s 时显存在超时内未回收 "
                "(used=%sMiB > threshold=%sMiB)，保留 in-use lease，避免脏 GPU 被重新分配。",
                task_id,
                gpu_id,
                last_used,
                threshold_mib,
            )
            return False
        time.sleep(max(0.2, float(poll_seconds)))


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

    if not _wait_gpu_memory_reclaimed(gpu_id, task_id):
        return
        
    pipe = client.pipeline()
    pipe.hdel(config.GPU_IN_USE_HASH_KEY, gpu_id)
    pipe.rpush(config.GPU_POOL_KEY, gpu_id)
    pipe.execute()
    
    logger.info(f"✅ 任务 {task_id}: 已将 GPU {gpu_id} 释放回池中。")

def get_gpu_status() -> dict:
    """一个用于监控所有 GPU 状态的辅助工具。"""
    client = get_redis_client()
    valid_raw = client.smembers(config.GPU_VALID_SET_KEY)
    valid = sorted([int(item) for item in valid_raw])
    in_use = client.hgetall(config.GPU_IN_USE_HASH_KEY)
    available = client.lrange(config.GPU_POOL_KEY, 0, -1)
    return {
        "valid": valid,
        "valid_count": len(valid),
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
        max_concurrent = config.MAX_CONCURRENT_TASKS
        configured_gpus = config.GPU_DEVICE_IDS or []
        detected_gpus: list[int] = []
        torch_detected_count: int | None = None

        # 1) 优先使用 nvidia-smi（容器内最稳定，且不依赖 Python torch 包）
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

        # 2) 仅在 nvidia-smi 未得到结果时，回退到 torch 探测
        if not detected_gpus:
            try:
                import torch  # type: ignore

                if torch.cuda.is_available():
                    torch_detected_count = torch.cuda.device_count()
                    detected_gpus = list(range(torch_detected_count))
                    logger.info(f"通过 torch.cuda 检测到可用 GPU: {detected_gpus}")
                else:
                    logger.info("torch.cuda 未检测到可用 GPU。")
            except Exception as exc:  # pragma: no cover - 安装环境相关
                logger.info(f"torch 不可用，跳过 torch GPU 探测: {exc}")

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
        print(f"Valid ({status.get('valid_count', 0)}): {status.get('valid', [])}")
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
