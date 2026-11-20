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
    supported_constraints: ClassVar[frozenset[str]] = frozenset()
    supported_chirality: ClassVar[frozenset[str]] = frozenset({"l"})

    def validate(self, request: PredictionRequest) -> None:
        present = set()
        if request.bonds:
            present.add("bond")
        for constraint in request.other_constraints:
            if isinstance(constraint, dict):
                present.update(constraint)
        unsupported = sorted(present - set(self.supported_constraints))
        if unsupported:
            raise ValueError(
                f"constraints {unsupported} are not supported by the {self.name} backend")
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

    @abstractmethod
    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        """执行预测并写出 output_archive_path。"""
