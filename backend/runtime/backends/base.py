"""结构预测后端的抽象接口。

能力（chirality / 约束类型 / 设计模式）由子类声明，输入输出校验
由基类统一执行；子类只实现请求到引擎原生调用的映射。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from backend.runtime.backends.request import PredictionRequest


@dataclass
class BackendRunContext:
    temp_dir: str
    yaml_content: str
    output_archive_path: str
    seed: int = 101
    task_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


class StructureBackend(ABC):
    name: ClassVar[str] = ""

    # 能力全部来自 configs/engine_capabilities.json（单一事实源）；
    # 子类不再手写，避免与矩阵漂移。
    @classmethod
    def supported_constraints(cls) -> frozenset[str]:
        from backend.core.engine_capabilities import engine_constraints
        return engine_constraints(cls.name)

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
        chains = set(lengths) | {e.chain_id for e in request.entities}
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
