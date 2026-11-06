"""mol1/molkdock 能力映射与后端拦截的回归测试。

背景：mol1 接入时出现过三处断点（分发分支缺失、子进程白名单漏 mol1、
worker 队列未订阅），capability_from_prediction_backend 的 mol1dock 遗留
映射也曾是死分支。这里钉死映射与拦截行为。
"""
from __future__ import annotations

import pytest

from backend.scheduling.capability_router import capability_from_prediction_backend


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("mol1", "mol1"),
        ("mol-1", "mol1"),
        ("mol1dock", "mol1"),  # 已下线引擎的历史任务行按 mol1 归队
        ("mol-1-dock", "mol1"),
        ("protenix", "protenix"),
        ("protenix2dock", "protenix2dock"),
        ("未知引擎", "boltz2"),
    ],
)
def test_capability_from_prediction_backend(backend, expected):
    assert capability_from_prediction_backend(backend) == expected
