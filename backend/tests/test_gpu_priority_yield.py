"""GPU 池优先级让位与候选取消可见性。

覆盖三块逻辑:
1. 优先级等待登记:high 任务等卡期间登记进 priority_waiting ZSET,取到卡
   (或失败)后注销,死亡条目按 staleness 自愈;
2. 让位判定:default 任务在有存活高优等待方时让位(预检与弹卡后复核同判),
   high 任务自身不受该约束,MSA 软保留判定不受影响;
3. 取消可见性:acquire_gpu_for_peptide_worker 的 cancel_check 回调在每轮
   等待前触发,抛异常立即中止等待——已取消候选的孙进程不再无限等卡。

ensure_image 的镜像存在性预检见 test_docker_ensure_image.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gpu_manager  # noqa: E402
from backend.tests.test_gpu_msa_reservation import CELERY_OWNER, _FakeRedis  # noqa: E402

import time  # noqa: E402


@pytest.fixture()
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


def _patch_acquire_stubs(monkeypatch, fake_redis: _FakeRedis) -> None:
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(gpu_manager, "_run_throttled_reconcile", lambda client, task_id, last: last)
    monkeypatch.setattr(gpu_manager.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gpu_manager, "_gpu_admission_check", lambda gpu_id, task_id: (True, "stub"))


def test_high_priority_waiter_lifecycle(fake_redis, monkeypatch):
    """high 任务等卡前登记、取到卡后注销；登记真实写入 ZSET。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    fake_redis.rpush(gpu_manager.config.GPU_POOL_KEY, 1)

    real_register = gpu_manager.register_priority_waiter
    real_unregister = gpu_manager.unregister_priority_waiter
    observed: list[int] = []

    def _spy_register(client, task_id):
        real_register(client, task_id)
        observed.append(fake_redis.zcard(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY))

    def _spy_unregister(client, task_id):
        real_unregister(client, task_id)
        observed.append(fake_redis.zcard(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY))

    monkeypatch.setattr(gpu_manager, "register_priority_waiter", _spy_register)
    monkeypatch.setattr(gpu_manager, "unregister_priority_waiter", _spy_unregister)

    gpu_manager.acquire_gpu("high-task", timeout=60, priority="high")

    # 登记时 ZSET 非空(真实写入),注销后为空(真实删除)
    assert observed == [1, 0]


def test_high_priority_waiter_unregistered_on_timeout(fake_redis, monkeypatch):
    """等待超时抛 TimeoutError 时同样注销(finally 兜底)。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    monkeypatch.setattr(
        gpu_manager, "msa_reservation_blocks", lambda client, task_id: (True, "stub"))

    with pytest.raises(TimeoutError):
        gpu_manager.acquire_gpu("high-task", timeout=1, priority="high")
    assert fake_redis.zcard(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY) == 0


def test_default_task_yields_to_live_priority_waiter(fake_redis, monkeypatch):
    """default 任务在有存活高优等待方时让位,直到等待方消失。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    fake_redis.rpush(gpu_manager.config.GPU_POOL_KEY, 2)
    fake_redis.zadd(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY, {"high-task": time.time()})

    # 第一轮让位(等待方在),等待方注销后(模拟取到卡)放行
    waiter_states = iter([1, 0, 0])  # 预检(1,让位)→ 弹卡后复核(0)→ …
    monkeypatch.setattr(
        gpu_manager, "count_live_priority_waiters",
        lambda client: next(waiter_states),
    )

    acquired = gpu_manager.acquire_gpu(CELERY_OWNER, timeout=60)
    assert acquired == 2
    assert fake_redis.hashes[gpu_manager.config.GPU_IN_USE_HASH_KEY]["2"] == CELERY_OWNER


def test_high_task_ignores_priority_yield(fake_redis, monkeypatch):
    """high 任务不受优先级让位约束(等待者就是它自己)。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    fake_redis.rpush(gpu_manager.config.GPU_POOL_KEY, 3)
    fake_redis.zadd(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY, {"high-task": time.time()})

    acquired = gpu_manager.acquire_gpu("high-task", timeout=60, priority="high")
    assert acquired == 3


def test_dead_priority_waiter_self_heals(fake_redis):
    """计数只读排除死条目;物理清理由维护周期承担。"""
    fake_redis.zadd(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY, {
        "alive": time.time(),
        "dead": time.time() - gpu_manager.config.GPU_WAITER_STALE_SECONDS - 1,
    })
    assert gpu_manager.count_live_priority_waiters(fake_redis) == 1
    gpu_manager._prune_stale_waiters(fake_redis, gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY)
    assert fake_redis.zcard(gpu_manager.config.GPU_PRIORITY_WAITING_ZSET_KEY) == 1


def test_peptide_cancel_check_aborts_wait(fake_redis, monkeypatch):
    """cancel_check 抛异常立即中止等卡——已取消候选的孙进程不再无限等待。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    monkeypatch.setattr(
        gpu_manager, "msa_reservation_blocks", lambda client, task_id: (True, "stub")
    )  # 池持续让位,迫使循环多轮运转

    calls = {"n": 0}

    def _cancel_after_two():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("任务已取消 (task_cancelled:abcd1234), 中止 GPU 等待")

    with pytest.raises(RuntimeError, match="中止 GPU 等待"):
        gpu_manager.acquire_gpu_for_peptide_worker("cand-1", timeout=0, cancel_check=_cancel_after_two)
    assert calls["n"] == 2
    assert fake_redis.hashes.get(gpu_manager.config.GPU_IN_USE_HASH_KEY, {}) == {}


def test_peptide_cancel_check_runs_before_blpop(fake_redis, monkeypatch):
    """cancel_check 在每轮 BLPOP 前触发(池空时也不漏检)。"""
    _patch_acquire_stubs(monkeypatch, fake_redis)
    monkeypatch.setattr(
        gpu_manager, "msa_reservation_blocks", lambda client, task_id: (False, "")
    )

    calls = {"n": 0}

    def _cancel_immediately():
        calls["n"] += 1
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        gpu_manager.acquire_gpu_for_peptide_worker("cand-2", timeout=0, cancel_check=_cancel_immediately)
    assert calls["n"] == 1
