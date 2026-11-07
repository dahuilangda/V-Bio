"""Engine capability matrix loader.

Single source of truth: configs/engine_capabilities.json at the repo root.
Frontend imports the same file; this module is the backend reader. The file
must exist — a missing matrix is a deployment error, not a fallback case.
"""
from __future__ import annotations

import json
from pathlib import Path

MATRIX_PATH = Path(__file__).resolve().parents[2] / "configs" / "engine_capabilities.json"


def _load() -> dict:
    if not MATRIX_PATH.is_file():
        raise FileNotFoundError(
            f"引擎能力矩阵缺失: {MATRIX_PATH} — 部署不完整，拒绝启动")
    data = json.loads(MATRIX_PATH.read_text())
    engines = data.get("engines")
    if not engines:
        raise ValueError(f"引擎能力矩阵为空: {MATRIX_PATH}")
    return engines


ENGINES = _load()


def get_engine(backend: str) -> dict | None:
    return ENGINES.get(str(backend or "").strip().lower())


def is_active(backend: str) -> bool:
    engine = get_engine(backend)
    return bool(engine) and engine.get("status") == "active"


def active_engine_ids() -> list[str]:
    return sorted(k for k, v in ENGINES.items() if v.get("status") == "active")


def supports(backend: str, workflow: str | None = None,
             feature: str | None = None) -> bool:
    engine = get_engine(backend)
    if not engine or engine.get("status") != "active":
        return False
    if workflow and workflow not in engine.get("workflows", []):
        return False
    if feature and not engine.get("features", {}).get(feature, False):
        return False
    return True
