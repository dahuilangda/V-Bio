"""Regression tests for the NVML-blind docker fallback in GPU admission.

Long-running GPU worker containers lose NVML initialization after host cgroup
reloads ("Failed to initialize NVML: Unknown Error") while CUDA still works.
Before the fallback, `_gpu_admission_check` optimistically admitted every GPU
in that state — handing tasks a GPU already filled by an out-of-pool training
job, which then died with CUDA OOM at model load. These tests pin the fallback
semantics:

- compute-apps query gets the docker probe when local nvidia-smi fails;
- admission rejects a GPU the probe reports as occupied;
- admission still admits when the probe reports a quiet GPU;
- the release path (`_wait_gpu_memory_reclaimed` / `_query_gpu_used_memory_mib`
  default) never triggers docker probes — the probe sees whole-GPU usage
  including foreign processes, so enabling it there would strand leases.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gpu_manager  # noqa: E402


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def blind_local_nvidia_smi(monkeypatch):
    """Make every local nvidia-smi invocation fail like an NVML-blind container."""
    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "nvidia-smi":
            return _FakeProc(255, "", "Failed to initialize NVML: Unknown Error")
        return subprocess.run(argv, *args, **kwargs)

    monkeypatch.setattr(gpu_manager.subprocess, "run", fake_run)


def test_compute_query_falls_back_to_docker_probe(monkeypatch, blind_local_nvidia_smi):
    monkeypatch.setattr(
        gpu_manager, "_nvidia_smi_query_via_docker",
        lambda gpu_id, query: "23898\n",
    )
    assert gpu_manager._query_gpu_compute_memory_mib(0) == 23898


def test_compute_query_returns_none_when_both_paths_blind(monkeypatch, blind_local_nvidia_smi):
    monkeypatch.setattr(
        gpu_manager, "_nvidia_smi_query_via_docker", lambda gpu_id, query: None)
    assert gpu_manager._query_gpu_compute_memory_mib(0) is None


def test_admission_rejects_occupied_gpu(monkeypatch, blind_local_nvidia_smi):
    monkeypatch.setattr(
        gpu_manager, "_nvidia_smi_query_via_docker",
        lambda gpu_id, query: "23578\n",
    )
    admitted, detail = gpu_manager._gpu_admission_check(0, "t-test")
    assert admitted is False
    assert "23578" in detail


def test_admission_admits_quiet_gpu(monkeypatch, blind_local_nvidia_smi):
    monkeypatch.setattr(
        gpu_manager, "_nvidia_smi_query_via_docker",
        lambda gpu_id, query: "12\n",
    )
    admitted, detail = gpu_manager._gpu_admission_check(1, "t-test")
    assert admitted is True
    assert "12" in detail


def test_release_path_never_docker_probes(monkeypatch, blind_local_nvidia_smi):
    """_query_gpu_used_memory_mib default (release path) must stay blind-tolerant.

    The docker probe counts foreign processes' memory as well; using it in the
    release path would make _wait_gpu_memory_reclaimed retain leases forever on
    GPUs that host any out-of-pool process (real reclaim threshold never met).
    """
    calls = []
    monkeypatch.setattr(
        gpu_manager, "_nvidia_smi_query_via_docker",
        lambda gpu_id, query: calls.append(gpu_id) or "23578\n",
    )
    assert gpu_manager._query_gpu_used_memory_mib(0) is None
    assert calls == []
