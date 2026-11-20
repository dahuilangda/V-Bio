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



def active_engine_ids() -> list[str]:
    return sorted(k for k, v in ENGINES.items() if v.get("status") == "active")



def engine_constraints(backend: str) -> frozenset[str]:
    """引擎声明的约束类型键集合；未知引擎返回空集。"""
    engine = get_engine(backend)
    return frozenset(engine.get("constraints") or []) if engine else frozenset()


def engine_constraint_quality(backend: str, constraint: str) -> str | None:
    """约束实现质量：hard（可保证环闭合/键长）、soft（先验性引导）、
    native（引擎原生条件）。未声明返回 None。"""
    engine = get_engine(backend)
    if not engine:
        return None
    constraints = engine.get("constraints") or {}
    if isinstance(constraints, dict):
        return constraints.get(constraint)
    return constraint in constraints or None


def engine_supports_constraint(backend: str, constraint: str) -> bool:
    return constraint in engine_constraints(backend)


def engine_design_modes(backend: str) -> frozenset[str]:
    engine = get_engine(backend)
    return frozenset(engine.get("design_modes") or []) if engine else frozenset()


def engine_chirality(backend: str) -> frozenset[str]:
    """引擎声明的手性支持（l/d）；未知引擎返回空集。"""
    engine = get_engine(backend)
    return frozenset(engine.get("chirality") or []) if engine else frozenset()


def engine_supports_chirality(backend: str, chirality: str) -> bool:
    return chirality in engine_chirality(backend)


def engine_modification_support(backend: str) -> dict:
    """引擎修饰能力：standard_ccd / custom_residue_upload；未知引擎全 False。"""
    engine = get_engine(backend)
    support = (engine or {}).get("modification_support") or {}
    return {
        "standard_ccd": bool(support.get("standard_ccd")),
        "custom_residue_upload": bool(support.get("custom_residue_upload")),
    }
