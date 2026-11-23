"""结构预测后端的抽象接口。

能力（chirality / 约束类型 / 设计模式）由子类声明，输入输出校验
由基类统一执行；子类只实现请求到引擎原生调用的映射。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Mapping

from backend.core.errors import UserInputError
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.ccd import CcdCatalog, normalize_ccd_code


@dataclass
class BackendRunContext:
    temp_dir: str
    yaml_content: str
    output_archive_path: str
    seed: int = 101
    task_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CcdResolution:
    """molecules：补全后的自定义分子；remap：被改名的自定义编码，旧编码 → 新编码。"""

    molecules: list[dict]
    remap: dict[str, str] = field(default_factory=dict)


def _referenced_ccd_codes(request: PredictionRequest) -> set[str]:
    codes = {normalize_ccd_code(m.ccd) for entity in request.entities for m in entity.modifications}
    codes |= {normalize_ccd_code(c) for entity in request.entities for c in entity.ccd}
    codes.discard("")
    return codes


def _platform_ccd_codes() -> frozenset[str]:
    """平台自带定义的编码：多肽 preset 与双环 linker。"""
    from backend.runtime.bicyclic_linkers import atom_map
    from backend.runtime.peptide_design_backend import PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES

    return frozenset(PEPTIDE_PRESET_CUSTOM_CCD_MOLECULES) | frozenset(atom_map())


class StructureBackend(ABC):
    name: ClassVar[str] = ""
    ccd_code_pattern: ClassVar[str] = r"[A-Z0-9_-]{1,12}"
    injects_referenced_presets: ClassVar[bool] = False
    validates_ccd_mmcif: ClassVar[bool] = False

    # 能力全部来自 configs/engine_capabilities.json（单一事实源）；
    # 子类不再手写，避免与矩阵漂移。
    @classmethod
    def supported_constraints(cls) -> frozenset[str]:
        from backend.core.engine_capabilities import engine_constraints
        return engine_constraints(cls.name)

    @abstractmethod
    def ccd_catalog(self) -> CcdCatalog:
        """引擎自带的标准 CCD 编码集合。"""

    def check_ccd_codes(self, codes: Iterable[str]) -> None:
        accepted = re.compile(self.ccd_code_pattern)
        normalized = {normalize_ccd_code(code) for code in codes}
        rejected = sorted(code for code in normalized if not accepted.fullmatch(code))
        if rejected:
            raise UserInputError(f"{self.name} 不接受的 CCD 编码: {', '.join(rejected)}")

    def validate_ccd_input(self, request: PredictionRequest, custom_ccd_molecules) -> CcdResolution:
        """不读引擎字典的校验，提交侧即可执行。"""
        from backend.runtime.custom_ccd_builder import (
            _build_custom_ccd_bundle,
            _normalize_custom_ccd_molecules,
        )

        self.validate_modifications(request, custom_ccd_molecules)
        molecules = _normalize_custom_ccd_molecules(custom_ccd_molecules or [])
        if self.injects_referenced_presets:
            from backend.runtime.peptide_design_backend import merge_referenced_preset_molecules
            molecules = merge_referenced_preset_molecules(molecules, request.source_yaml)
        molecules = [dict(molecule) for molecule in molecules]
        self.check_ccd_codes(_referenced_ccd_codes(request) | {m["ccd"] for m in molecules})
        for entity in request.entities:
            stacked = sorted(
                position for position, count
                in Counter(m.position for m in entity.modifications).items() if count > 1)
            if stacked:
                raise UserInputError(
                    f"链 {entity.chain_id} 第 {', '.join(map(str, stacked))} 位叠加了多个修饰；"
                    "每个残基位只能有一个修饰，需要叠加时请合并为一个自定义残基。")
        if molecules:
            bundle, _ = _build_custom_ccd_bundle(molecules)
            if self.validates_ccd_mmcif:
                from backend.runtime.ccd_contract import validate_ccd_additions
                validate_ccd_additions(bundle)
        return CcdResolution(molecules=molecules)

    def resolve_ccd_codes(self, request: PredictionRequest,
                          resolution: CcdResolution) -> CcdResolution:
        """对照引擎字典：改名与标准编码同名的自定义编码，确认引用的编码都能解析。"""
        referenced = _referenced_ccd_codes(request)
        if not resolution.molecules and not referenced:
            return resolution
        catalog = self.ccd_catalog()
        molecules = [dict(molecule) for molecule in resolution.molecules]
        remap = catalog.rename_collisions(molecules)
        defined = {str(m["ccd"]) for m in molecules} | set(remap) | _platform_ccd_codes()
        missing = catalog.missing(referenced - defined)
        if missing:
            raise UserInputError(
                f"{self.name} 无法解析 CCD 编码: {', '.join(missing)}。"
                "请将其加入 custom_ccd_molecules，或改用该引擎支持的标准编码。")
        return CcdResolution(molecules=molecules, remap=remap)

    def ccd_resolution(self, request: PredictionRequest, options: Mapping[str, Any]) -> CcdResolution:
        """优先用申请 GPU 前算好的结果（options['ccd_resolution']），否则现算。"""
        prepared = options.get("ccd_resolution")
        if prepared is not None:
            return CcdResolution(molecules=prepared["molecules"], remap=prepared["remap"])
        return self.resolve_ccd_codes(
            request, self.validate_ccd_input(request, options.get("custom_ccd_molecules")))

    def validate(self, request: PredictionRequest, custom_ccd_molecules=None) -> None:
        supported = self.supported_constraints()
        present = set()
        if request.bonds:
            present.add("bond")
        for constraint in request.other_constraints:
            if isinstance(constraint, dict):
                present.update(constraint)
        unsupported = sorted(present - supported)
        if unsupported:
            raise ValueError(
                f"constraints {unsupported} are not supported by the {self.name} backend")
        self.validate_modifications(request, custom_ccd_molecules)

    def validate_modifications(self, request: PredictionRequest,
                               custom_ccd_molecules=None) -> None:
        """修饰能力按矩阵强制：standard_ccd / custom_residue_upload。"""
        from backend.core.engine_capabilities import engine_modification_support
        support = engine_modification_support(self.name)
        has_modifications = any(e.modifications for e in request.entities)
        if has_modifications and not support["standard_ccd"]:
            raise ValueError(
                f"{self.name} backend does not support CCD residue modifications")
        if custom_ccd_molecules and not support["custom_residue_upload"]:
            raise ValueError(
                f"{self.name} backend does not support custom residue uploads")
        lengths = request.sequence_lengths()
        chains = set(lengths) | {
            chain for e in request.entities for chain in (e.ids or [e.chain_id])
        }
        for bond in request.bonds:
            for chain, residue in ((bond.chain1, bond.residue1),
                                   (bond.chain2, bond.residue2)):
                if chain not in chains:
                    raise ValueError(f"bond constraint references unknown chain: {chain}")
                expected = lengths.get(chain)
                if expected and not 1 <= residue <= expected:
                    raise ValueError(
                        f"bond residue {residue} out of range 1..{expected} for chain {chain}")

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        """引擎约束管线钩子：重映射/清洗/域校验。默认原样返回。"""
        return request

    @abstractmethod
    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        """执行预测并写出 output_archive_path。实现应先调用
        self.validate(request) 与 self.post_validate(request)。"""
