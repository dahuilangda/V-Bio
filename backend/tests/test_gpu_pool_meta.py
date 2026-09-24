"""GPU pool metadata: total VRAM recorded at init, read at task time.

The pool metadata hash is the only source the runtime consults — there is no
task-time hardware probing and no fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gpu_manager  # noqa: E402


class _StubRedis:
    """Minimal hash client standing in for the pool metadata store."""

    def __init__(self, initial: dict[str, str] | None = None):
        self.hash: dict[str, str] = dict(initial or {})
        self.hset_calls: list[tuple[str, str]] = []

    def hkeys(self, key: str) -> list[str]:
        assert key == gpu_manager.config.GPU_META_HASH_KEY
        return list(self.hash.keys())

    def hset(self, key: str, field, value) -> int:
        assert key == gpu_manager.config.GPU_META_HASH_KEY
        self.hash[str(field)] = str(value)
        self.hset_calls.append((str(field), str(value)))
        return 1

    def hget(self, key: str, field: str):
        assert key == gpu_manager.config.GPU_META_HASH_KEY
        return self.hash.get(str(field))


def test_write_gpu_meta_records_probed_devices(monkeypatch):
    stub = _StubRedis()
    probes = iter([81920, 24564])
    monkeypatch.setattr(gpu_manager, "_probe_gpu_total_memory_mib", lambda gpu_id: next(probes))
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: stub)

    gpu_manager._write_gpu_meta(stub, [0, 1])
    assert stub.hash == {"0": "81920", "1": "24564"}


def test_write_gpu_meta_upserts_only_missing_devices(monkeypatch):
    stub = _StubRedis(initial={"0": "81920"})
    probe_calls: list[int] = []

    def probe(gpu_id: int) -> int:
        probe_calls.append(gpu_id)
        return 24564

    monkeypatch.setattr(gpu_manager, "_probe_gpu_total_memory_mib", probe)
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: stub)

    gpu_manager._write_gpu_meta(stub, [0, 1])
    # GPU 0 already had metadata — never re-probed, never overwritten.
    assert probe_calls == [1]
    assert stub.hash == {"0": "81920", "1": "24564"}


def test_write_gpu_meta_probe_failure_leaves_device_absent(monkeypatch):
    stub = _StubRedis()
    monkeypatch.setattr(gpu_manager, "_probe_gpu_total_memory_mib", lambda gpu_id: None)
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: stub)

    gpu_manager._write_gpu_meta(stub, [2])
    assert stub.hash == {}
    assert gpu_manager.get_gpu_total_memory_mib(2) is None


def test_get_gpu_total_memory_mib_parses_recorded_value(monkeypatch):
    stub = _StubRedis(initial={"5": "47989"})
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: stub)
    assert gpu_manager.get_gpu_total_memory_mib(5) == 47989


def test_get_gpu_total_memory_mib_missing_device_returns_none(monkeypatch):
    stub = _StubRedis()
    monkeypatch.setattr(gpu_manager, "get_redis_client", lambda: stub)
    assert gpu_manager.get_gpu_total_memory_mib(9) is None
