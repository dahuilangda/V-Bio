"""平台 yaml 约束的统一解析与校验。

所有后端共用：解析结果为类型化对象，引擎侧再映射为各自原生格式。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BondAtom:
    chain: str
    residue: int
    atom: str


@dataclass(frozen=True)
class BondConstraint:
    atom1: BondAtom
    atom2: BondAtom


def parse_bond_constraints(constraints: list) -> list[BondConstraint]:
    bonds = []
    for constraint in constraints or []:
        spec = constraint.get("bond") if isinstance(constraint, dict) else None
        if not spec:
            continue
        ends = []
        for raw in (spec.get("atom1"), spec.get("atom2")):
            chain, residue, atom = str(raw[0]).strip(), int(raw[1]), str(raw[2]).strip()
            ends.append(BondAtom(chain=chain, residue=residue, atom=atom))
        bonds.append(BondConstraint(atom1=ends[0], atom2=ends[1]))
    return bonds


def headtail_bond(chain: str, sequence_length: int) -> BondConstraint:
    return BondConstraint(
        atom1=BondAtom(chain=chain, residue=1, atom="N"),
        atom2=BondAtom(chain=chain, residue=sequence_length, atom="C"),
    )


def constraint_kinds(constraints: list) -> set[str]:
    return {
        kind
        for constraint in constraints or []
        if isinstance(constraint, dict)
        for kind in constraint
    }


def validate_bond_chains(bonds: list[BondConstraint], known_chains: set[str]) -> None:
    unknown = sorted({a.chain for b in bonds for a in (b.atom1, b.atom2)
                      if a.chain not in known_chains})
    if unknown:
        raise ValueError(f"bond constraint references unknown chains: {unknown}")
