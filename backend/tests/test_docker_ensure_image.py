"""镜像存在性预检 ensure_image。

缺失的私有 runtime 镜像此前会在 docker run 内触发数分钟的 pull 尝试后以
exit 125 失败(GPU 租约全程空占);预检把失败收敛为亚秒级、带构建指引的
业务错误。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.worker import docker_cmd  # noqa: E402


def _patch_inspect(monkeypatch, returncode: int, stderr: str = "") -> dict:
    calls: dict = {}

    def _run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(docker_cmd.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(docker_cmd.subprocess, "run", _run)
    return calls


def test_missing_image_raises_actionable_error(monkeypatch):
    calls = _patch_inspect(monkeypatch, returncode=1, stderr="Error: No such image: vbio-boltz2-runtime")

    with pytest.raises(RuntimeError, match="vbio-boltz2-runtime.*不存在.*docker build"):
        docker_cmd.ensure_image("vbio-boltz2-runtime")
    assert calls["cmd"][:4] == ["docker", "image", "inspect", "vbio-boltz2-runtime"]
    assert calls["timeout"] == docker_cmd.IMAGE_PRECHECK_TIMEOUT_SECONDS


def test_present_image_passes(monkeypatch):
    calls = _patch_inspect(monkeypatch, returncode=0)
    docker_cmd.ensure_image("vbio-protenix-v2-runtime:2.0.0")  # 不抛即通过
    assert calls["cmd"] == [
        "docker", "image", "inspect", "vbio-protenix-v2-runtime:2.0.0", "--format", "{{.Id}}",
    ]


def test_empty_image_name_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="未配置"):
        docker_cmd.ensure_image("  ")


def test_missing_docker_cli_is_noop(monkeypatch):
    monkeypatch.setattr(docker_cmd.shutil, "which", lambda name: None)
    docker_cmd.ensure_image("anything")  # 交给实际执行路径报错


def test_image_and_python_prechecks(monkeypatch):
    """image_and_python 复用 ensure_image:缺失镜像在容器构造前即失败。"""
    _patch_inspect(monkeypatch, returncode=1, stderr="No such image")
    with pytest.raises(RuntimeError, match="不存在"):
        docker_cmd.image_and_python()
