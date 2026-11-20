"""结构预测后端注册表。"""
from __future__ import annotations

from backend.runtime.backends.base import StructureBackend
from backend.runtime.backends.esmfold2 import EsmFold2Backend

_REGISTRY: dict[str, StructureBackend] = {}


def structure_backend(name: str) -> StructureBackend:
    key = str(name or "").strip().lower()
    backend = _REGISTRY.get(key)
    if backend is None:
        raise ValueError(f"no migrated structure backend for '{name}'")
    return backend


def register(backend: StructureBackend) -> None:
    _REGISTRY[backend.name] = backend


register(EsmFold2Backend())
