"""各引擎的标准 CCD 编码集合，以及自定义编码的改名与 yaml 回写。"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

import yaml


def normalize_ccd_code(value: object) -> str:
    return str(value or "").strip().upper().removeprefix("CCD_")


def next_free_z_code(taken: set) -> str:
    seq = 1
    while f"Z{seq:03d}" in taken:
        seq += 1
    code = f"Z{seq:03d}"
    taken.add(code)
    return code


def apply_custom_ccd_code_remap(yaml_content: str, remap: Mapping[str, str]) -> str:
    if not remap:
        return yaml_content
    data = yaml.safe_load(yaml_content) or {}
    for entity in data.get("sequences") or []:
        if not isinstance(entity, dict):
            continue
        for key in ("protein", "proteinChain", "ligand", "ion", "rna", "dna"):
            inner = entity.get(key)
            if not isinstance(inner, dict):
                continue
            if str(inner.get("ccd") or "").upper() in remap:
                inner["ccd"] = remap[str(inner.get("ccd")).upper()]
            for mod in inner.get("modifications") or []:
                if isinstance(mod, dict) and str(mod.get("ccd") or "").upper() in remap:
                    mod["ccd"] = remap[str(mod.get("ccd")).upper()]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class CcdCatalog(ABC):
    """引擎自带的标准 CCD 编码集合。"""

    name: str = ""

    def __init__(self) -> None:
        self._codes: frozenset[str] | None = None

    def codes(self) -> frozenset[str]:
        if self._codes is None:
            self._codes = self.read()
            if not self._codes:
                raise RuntimeError(f"{self.name} 标准 CCD 字典为空")
        return self._codes

    @abstractmethod
    def read(self) -> frozenset[str]:
        raise NotImplementedError

    def missing(self, referenced: Iterable[str]) -> list[str]:
        """引用的编码里引擎不认识的那些。"""
        return sorted(set(referenced) - self.codes())

    def rename_collisions(self, molecules: list[dict]) -> dict[str, str]:
        """自定义编码与标准编码同名时改用空闲 Z###，返回 旧编码 → 新编码。"""
        standard = self.codes()
        taken = set(standard) | {str(m["ccd"]) for m in molecules}
        remap: dict[str, str] = {}
        for item in molecules:
            if item["ccd"] in standard:
                code = item["ccd"]
                item["ccd"] = next_free_z_code(taken)
                remap[code] = item["ccd"]
        return remap


@lru_cache(maxsize=4)
def _mmcif_component_ids(path: str, size: int, mtime_ns: int) -> frozenset:
    with open(path, errors="replace") as handle:
        return frozenset(line[5:].strip().upper() for line in handle if line.startswith("data_"))


class MmcifCcdCatalog(CcdCatalog):
    """读取 components.cif 的 data_ 块；同一文件只解析一次。"""

    def __init__(self, name: str, cif_path) -> None:
        super().__init__()
        self.name = name
        self.path = Path(cif_path)

    def read(self) -> frozenset[str]:
        if not self.path.is_file():
            raise FileNotFoundError(f"标准 CCD components.cif 不存在: {self.path}")
        stat = self.path.stat()
        return _mmcif_component_ids(str(self.path), stat.st_size, stat.st_mtime_ns)


_MARKER = "__VBIO_CCD_CODES__"
_IMAGE_READER = """import sys
{reader}
codes = sorted(set(str(code).strip().upper() for code in codes if str(code).strip()))
print("{marker}" + str(len(codes)))
print("\\n".join(codes))
"""


class ImageCcdCatalog(CcdCatalog):
    """字典只在引擎镜像里能读到：在镜像内执行 reader 取编码。

    reader 是把 codes 赋值为编码可迭代对象的 Python 片段；volumes、env
    与启动该引擎时的挂载、环境变量一致。
    """

    def __init__(self, name: str, image: str, python: str, reader: str,
                 volumes: Iterable[tuple[str, str]] = (),
                 env: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.name = name
        self.image = image
        self.python = python
        self.script = _IMAGE_READER.format(reader=reader, marker=_MARKER)
        self.volumes = tuple(volumes)
        self.env = dict(env or {})

    def read(self) -> frozenset[str]:
        from backend.worker.docker_cmd import ensure_image

        ensure_image(self.image)
        command = ["docker", "run", "--rm", "--entrypoint", self.python]
        for host_path, container_path in self.volumes:
            command += ["--volume", f"{host_path}:{container_path}:ro"]
        for key, value in self.env.items():
            command += ["--env", f"{key}={value}"]
        completed = subprocess.run(
            command + [self.image, "-c", self.script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"{self.name} CCD 字典读取失败 (exit {completed.returncode}): "
                f"{(completed.stderr or '').strip()[-400:]}")
        _, marker, payload = completed.stdout.partition(_MARKER)
        count, _, codes = payload.partition("\n")
        if not marker or not count.isdigit() or int(count) < 1 or len(codes.split()) != int(count):
            raise RuntimeError(f"{self.name} CCD 字典读取不完整")
        return frozenset(codes.split())
