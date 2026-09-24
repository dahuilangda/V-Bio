"""protenix2dock low_vram resolution: explicit request wins, else pool metadata."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core import config  # noqa: E402
from backend.worker import protenix2dock_task as p2d  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("True", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("1", True),
        ("on", True),
        ("off", False),
        ("maybe", None),
        ("", None),
        (object(), None),
    ],
)
def test_coerce_opt_bool(raw, expected):
    assert p2d._coerce_opt_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("false", False),
        ("true", True),
    ],
)
def test_explicit_request_beats_metadata(raw, expected, monkeypatch):
    # Even a tiny GPU must not override an explicit client decision — and an
    # explicit request never touches the pool metadata at all.
    monkeypatch.setattr(
        p2d, "get_gpu_total_memory_mib",
        lambda gpu_id: (_ for _ in ()).throw(AssertionError("metadata must not be read")),
    )
    assert p2d._resolve_low_vram({"low_vram": raw}) is expected


def test_default_off_regardless_of_gpu(monkeypatch):
    # Policy (2026-09-04): low_vram ONLY on explicit request. No silent
    # auto-degradation by GPU size — an OOM on an undersized card surfaces
    # loudly instead.
    monkeypatch.setattr(p2d, "get_gpu_total_memory_mib", lambda gpu_id: 81920)
    assert p2d._resolve_low_vram({}) is False
    monkeypatch.setattr(p2d, "get_gpu_total_memory_mib", lambda gpu_id: 24564)
    assert p2d._resolve_low_vram({}) is False


def test_auto_threshold_configurable(monkeypatch):
    # threshold no longer drives the default; keep the explicit-request path
    assert p2d._resolve_low_vram({"low_vram": True}) is True
    assert p2d._resolve_low_vram({"low_vram": False}) is False


def test_metadata_no_longer_consulted(monkeypatch):
    # The pool-metadata lookup was part of the removed auto path; the
    # resolver must not depend on it (and must not raise when missing).
    monkeypatch.setattr(p2d, "get_gpu_total_memory_mib", lambda gpu_id: None)
    assert p2d._resolve_low_vram({}) is False
