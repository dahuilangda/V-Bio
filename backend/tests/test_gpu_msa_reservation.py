"""MSA 软保留与租约最短租龄保护。

覆盖三块逻辑（均在 gpu_manager.py）：

1. 租约最短租龄：MSA 租约（owner 形如 ``colabfold_msa:<pid>:<epoch>``）在心跳建立
   前的窗口内不得被 reconcile 回收，否则会产生双租约；超龄且心跳消失的照常回收。
2. 长任务持有者统计与 MSA 等待方统计：owner 分类、陈旧等待条目的自愈清理。
3. 软保留让位判定与 acquire 循环中的让位行为：存在存活 MSA 等待方且长任务持卡
   达到上限时，新的 acquire 不弹卡、等待后重试；MSA 无等待方时零开销放行。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gpu_manager  # noqa: E402


CELERY_OWNER = "b3f1c9a2d4e5467fa0b8c3d2e1f0a9b8"
ESM3_OWNER = "esm3-service-4242"


def _msa_owner(age_seconds: float = 0.0) -> str:
    return f"colabfold_msa:179:{int(time.time() - age_seconds)}"


class _FakeRedis:
    """gpu_manager 所用 Redis 子集的内存实现。

    eval 仅实现本模块实际使用的两段 Lua：条件 HDEL（reclaim/release 路径）与
    条件 HSET 认领（acquire 路径），其余脚本一律抛错以防测试悄悄走错分支。
    """

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.keys: dict[str, str] = {}

    # --- kv ---
    def exists(self, key: str) -> int:
        return 1 if key in self.keys else 0

    # --- list ---
    def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])[start : end + 1 if end >= 0 else None]

    def rpush(self, key: str, *values) -> int:
        self.lists.setdefault(key, []).extend(str(v) for v in values)
        return len(self.lists[key])

    def blpop(self, key: str, timeout: int = 0):
        queue = self.lists.get(key)
        if not queue:
            return None
        return (key, queue.pop(0))

    # --- hash ---
    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(str(field))

    def hdel(self, key: str, field: str) -> int:
        table = self.hashes.setdefault(key, {})
        return 1 if table.pop(str(field), None) is not None else 0

    def hset(self, key: str, field, value) -> int:
        self.hashes.setdefault(key, {})[str(field)] = str(value)
        return 1

    # --- zset ---
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        table = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            table[member] = float(score)
        return len(mapping)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zcount(self, key: str, low, high) -> int:
        table = self.zsets.get(key, {})
        cutoff = float(str(low).lstrip("("))
        return sum(1 for score in table.values() if score > cutoff)

    def zrem(self, key: str, *members) -> int:
        table = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if table.pop(str(member), None) is not None:
                removed += 1
        return removed

    def zremrangebyscore(self, key: str, low: str, high) -> int:
        table = self.zsets.get(key)
        if not table:
            return 0
        cutoff = float(str(high).lstrip("("))
        stale = [member for member, score in table.items() if score <= cutoff]
        for member in stale:
            del table[member]
        return len(stale)

    # --- lua ---
    def eval(self, script: str, numkeys: int, *args):
        if "hget" in script and "hdel" in script:
            key, field, value = args[0], args[1], args[2]
            if self.hashes.get(key, {}).get(str(field)) == str(value):
                self.hashes[key].pop(str(field))
                return 1
            return 0
        if "HEXISTS" in script and "HSET" in script:
            in_use_key, _pool_key, gpu_id, owner = args[:4]
            table = self.hashes.setdefault(in_use_key, {})
            if str(gpu_id) in table:
                return 0
            table[str(gpu_id)] = str(owner)
            return 1
        raise AssertionError(f"unexpected lua script in test: {script[:80]}")


@pytest.fixture()
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture()
def no_celery(monkeypatch):
    monkeypatch.setattr(gpu_manager, "_collect_live_celery_task_ids", lambda: set())
    monkeypatch.setattr(gpu_manager, "_read_task_state", lambda task_id: "PENDING")


# ---------------------------------------------------------------------------
# 1. 租约最短租龄
# ---------------------------------------------------------------------------


def test_msa_owner_lease_age_parses_epoch_and_rejects_other_formats():
    assert gpu_manager._msa_owner_lease_age_seconds(_msa_owner(age_seconds=10)) == pytest.approx(10, abs=2)
    assert gpu_manager._msa_owner_lease_age_seconds(CELERY_OWNER) is None
    assert gpu_manager._msa_owner_lease_age_seconds(ESM3_OWNER) is None
    assert gpu_manager._msa_owner_lease_age_seconds("") is None


def test_reconcile_keeps_fresh_msa_lease_without_heartbeat(fake_redis, no_celery, monkeypatch):
    """心跳未建立的 MSA 新租约不得被回收（双租约窗口保护）。"""
    owner = _msa_owner(age_seconds=5)
    fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY] = {"0": owner}

    def _fail(gpu_id, task_id):
        raise AssertionError("fresh lease must not reach the memory-reclaim gate")

    monkeypatch.setattr(gpu_manager, "_wait_gpu_memory_reclaimed", _fail)

    result = gpu_manager._reconcile_in_use_allocations(fake_redis, {0})
    assert result["released"] == []
    assert result["kept"] == {"0": owner}


def test_reconcile_reclaims_stale_msa_lease_without_heartbeat(fake_redis, no_celery, monkeypatch):
    owner = _msa_owner(age_seconds=600)
    fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY] = {"0": owner}
    monkeypatch.setattr(gpu_manager, "_wait_gpu_memory_reclaimed", lambda gpu_id, task_id: True)

    result = gpu_manager._reconcile_in_use_allocations(fake_redis, {0})
    assert result["released"] == [(0, owner, "pending_without_live_or_heartbeat")]
    assert result["kept"] == {}


def test_reconcile_still_reclaims_dead_celery_task(fake_redis, no_celery, monkeypatch):
    """最短租龄只保护 MSA owner；celery 任务的心跳消失仍按原语义回收。"""
    fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY] = {"1": CELERY_OWNER}
    monkeypatch.setattr(gpu_manager, "_wait_gpu_memory_reclaimed", lambda gpu_id, task_id: True)

    result = gpu_manager._reconcile_in_use_allocations(fake_redis, {1})
    assert len(result["released"]) == 1


# ---------------------------------------------------------------------------
# 2. 持有者 / 等待方统计
# ---------------------------------------------------------------------------


def test_count_long_task_lease_holders_excludes_msa_owners(fake_redis):
    fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY] = {
        "0": CELERY_OWNER,
        "1": _msa_owner(),
        "2": ESM3_OWNER,
    }
    assert gpu_manager.count_long_task_lease_holders(fake_redis) == 2


def test_count_live_msa_waiters_excludes_stale_entries(fake_redis):
    """计数只读排除死条目;物理清理由维护周期的 _prune_stale_waiters 承担。"""
    now = time.time()
    stale_cutoff = now - gpu_manager.config.GPU_WAITER_STALE_SECONDS - 1
    fake_redis.zadd(gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY, {
        "colabfold_msa:1:100": now - 10,
        "colabfold_msa:2:200": stale_cutoff,
    })
    assert gpu_manager.count_live_msa_waiters(fake_redis) == 1
    gpu_manager._prune_stale_waiters(fake_redis, gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY)
    assert fake_redis.zcard(gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY) == 1


# ---------------------------------------------------------------------------
# 3. 软保留判定与 acquire 让位
# ---------------------------------------------------------------------------


def test_reservation_blocks_only_past_holder_limit(fake_redis, monkeypatch):
    fake_redis.zadd(gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY, {"colabfold_msa:1:100": time.time()})
    monkeypatch.setattr(gpu_manager, "count_long_task_lease_holders", lambda client: 3)
    monkeypatch.setattr(gpu_manager, "count_pool_gpus", lambda client: 4)

    blocked, detail = gpu_manager.msa_reservation_blocks(fake_redis, CELERY_OWNER)
    assert blocked and "msa_waiters=1" in detail

    monkeypatch.setattr(gpu_manager, "count_long_task_lease_holders", lambda client: 2)
    assert gpu_manager.msa_reservation_blocks(fake_redis, CELERY_OWNER) == (False, "")


def test_reservation_limit_clamped_to_small_pools(fake_redis, monkeypatch):
    """小池(<=上限的卡数)按 valid-1 截断: 2 卡池上限为 1, 持 2 卡即让位,
    否则软保留在小池上永不触发——恰是最需要保 MSA 的场景。"""
    fake_redis.zadd(gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY, {"colabfold_msa:1:100": time.time()})
    monkeypatch.setattr(gpu_manager, "count_long_task_lease_holders", lambda client: 2)
    monkeypatch.setattr(gpu_manager, "count_pool_gpus", lambda client: 2)

    blocked, detail = gpu_manager.msa_reservation_blocks(fake_redis, CELERY_OWNER)
    assert blocked and "limit=1" in detail


def test_reservation_disabled_yields_no_block(fake_redis, monkeypatch):
    fake_redis.zadd(gpu_manager.config.GPU_MSA_WAITING_ZSET_KEY, {"colabfold_msa:1:100": time.time()})
    monkeypatch.setattr(gpu_manager, "count_long_task_lease_holders", lambda client: 4)
    monkeypatch.setattr(gpu_manager.config, "GPU_MSA_RESERVE_ENABLED", False)
    assert gpu_manager.msa_reservation_blocks(fake_redis, CELERY_OWNER) == (False, "")


def test_acquire_gpu_yields_then_acquires(fake_redis, monkeypatch):
    """让位期间不弹卡；保留解除后正常认领并归还首个可用 GPU。"""
    fake_redis.rpush(gpu_manager.config.GPU_POOL_KEY, 2)

    gate_states = iter([True, True, False, False])
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        gpu_manager, "msa_reservation_blocks", lambda client, task_id: (next(gate_states), "stub")
    )
    monkeypatch.setattr(gpu_manager, "_run_throttled_reconcile", lambda client, task_id, last: last)
    monkeypatch.setattr(gpu_manager.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        gpu_manager, "_gpu_admission_check", lambda gpu_id, task_id: (True, "stub")
    )

    acquired = gpu_manager.acquire_gpu(CELERY_OWNER, timeout=60)
    assert acquired == 2
    assert fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY]["2"] == CELERY_OWNER


def test_acquire_gpu_rechecks_reservation_after_pop(fake_redis, monkeypatch):
    """弹卡后复核：预检通过但弹卡时闸门已转阻塞，卡放回队尾而非认领。

    覆盖闸门检查与 BLPOP 之间的登记窗口——没有该复核，MSA 等待方会被人已
    在 BLPOP 中排队的长任务插队，软保留失效。
    """
    fake_redis.rpush(gpu_manager.config.GPU_POOL_KEY, 3)
    rpush_calls: list[tuple] = []
    original_rpush = fake_redis.rpush

    def _spy_rpush(key, *values):
        rpush_calls.append((key, values))
        return original_rpush(key, *values)

    fake_redis.rpush = _spy_rpush
    gate_states = iter([False, True, False, False])
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        gpu_manager, "msa_reservation_blocks", lambda client, task_id: (next(gate_states), "stub")
    )
    monkeypatch.setattr(gpu_manager, "_run_throttled_reconcile", lambda client, task_id, last: last)
    monkeypatch.setattr(gpu_manager.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        gpu_manager, "_gpu_admission_check", lambda gpu_id, task_id: (True, "stub")
    )

    acquired = gpu_manager.acquire_gpu(CELERY_OWNER, timeout=60)
    assert acquired == 3
    assert rpush_calls, "card must be pushed back when reservation turns active after pop"
    assert fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY]["3"] == CELERY_OWNER


def test_acquire_gpu_timeout_reports_timeout(fake_redis, monkeypatch):
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(gpu_manager, "msa_reservation_blocks", lambda client, task_id: (False, ""))
    monkeypatch.setattr(gpu_manager.time, "sleep", lambda seconds: None)
    # blpop 永远为空且 reconcile 短路 → 循环由 deadline 终止
    monkeypatch.setattr(gpu_manager, "_run_throttled_reconcile", lambda client, task_id, last: last)

    with pytest.raises(TimeoutError):
        gpu_manager.acquire_gpu(CELERY_OWNER, timeout=1)
