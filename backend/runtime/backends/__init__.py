"""结构预测后端注册表。"""
from __future__ import annotations

from backend.runtime.backends.af3 import Af3Backend
from backend.runtime.backends.boltz import BoltzBackend
from backend.runtime.backends.esmfold2 import EsmFold2Backend
from backend.runtime.backends.mol1 import Mol1Backend
from backend.runtime.backends.base import StructureBackend
from backend.runtime.backends.protenix import ProtenixBackend
from backend.runtime.backends.request import PredictionRequest

_REGISTRY: dict[str, StructureBackend] = {}
_ALIASES = {"protenix2dock": "protenix", "boltz2dock": "boltz"}


def _canonical(name: str) -> str:
    token = str(name or "").strip().lower()
    return _ALIASES.get(token, token)


def structure_backend(name: str) -> StructureBackend:
    backend = _REGISTRY.get(_canonical(name))
    if backend is None:
        raise ValueError(f"no migrated structure backend for '{name}'")
    return backend


def is_structure_backend(name: str) -> bool:
    return _canonical(name) in _REGISTRY


def resolve_ccd_before_gpu(backend: str, yaml_content: str, custom_ccd_molecules) -> dict | None:
    """申请 GPU 前解析 CCD 编码；输入问题在此报错，结果随任务带入执行。"""
    if not is_structure_backend(backend):
        return None
    engine = structure_backend(backend)
    request = PredictionRequest.from_yaml(yaml_content)
    resolution = engine.resolve_ccd_codes(
        request, engine.validate_ccd_input(request, custom_ccd_molecules))
    return {"molecules": resolution.molecules, "remap": resolution.remap}


for _backend in (BoltzBackend(), Af3Backend(), ProtenixBackend(),
                 Mol1Backend(), EsmFold2Backend()):
    _REGISTRY[_backend.name] = _backend
