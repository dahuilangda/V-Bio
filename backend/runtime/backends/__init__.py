"""结构预测后端注册表。"""
from __future__ import annotations

from backend.runtime.backends.af3 import Af3Backend
from backend.runtime.backends.boltz import BoltzBackend
from backend.runtime.backends.esmfold2 import EsmFold2Backend
from backend.runtime.backends.mol1 import Mol1Backend
from backend.runtime.backends.base import StructureBackend
from backend.runtime.backends.protenix import ProtenixBackend

_REGISTRY: dict[str, StructureBackend] = {}


def structure_backend(name: str) -> StructureBackend:
    backend = _REGISTRY.get(str(name or "").strip().lower())
    if backend is None:
        raise ValueError(f"no migrated structure backend for '{name}'")
    return backend


for _backend in (BoltzBackend(), Af3Backend(), ProtenixBackend(),
                 Mol1Backend(), EsmFold2Backend()):
    _REGISTRY[_backend.name] = _backend
