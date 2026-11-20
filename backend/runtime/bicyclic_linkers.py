#!/usr/bin/env python3
"""Bicyclic linker registry -- the single configuration point shared by every
backend (rfd4 generation, mol1 / protenix / alphafold3 verification, and the
ESM3 candidate loop).

A linker is usable only where every consuming dictionary ships its CCD:

- ZBR  1,3,5-tris(bromomethyl)benzene (TBMB), the standard bicyclic
       chemistry; present in mol1 components.cif, alphafold3 CCD, protenix
       CCD cache and biotite (atomworks). Universal.
- SEZ / 29N  protenix-only CCD codes: mol1 raises "no usable entry" and the
       alphafold3-side linker_ccd bundles lack the descriptor category that
       AF3's CCD parser requires. Kept for the protenix-based legacy path;
       selection against other backends fails loudly.
- BS3  symmetric arms (the same atom bonds all three Cys), which cannot
       express three distinct covalent bonds -- not a bicyclic linker here.

Selection is explicit: unknown codes and (linker, backend) mismatches raise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class BicyclicLinker:
    ccd: str
    arm_atoms: Tuple[str, str, str]  # SG-bonding atoms, canonical order
    engines: frozenset
    note: str = ""
    # Whether the code is absent from an engine's standard CCD and therefore
    # needs an injected custom-CCD resource (linker_ccd/<ccd>.cif). Standard
    # codes must NOT be injected: generated bundles lack the descriptor
    # category that AF3's CCD parser requires.
    af3_userccd: bool = False
    protenix_overlay: bool = False


LINKERS: Dict[str, BicyclicLinker] = {
    "ZBR": BicyclicLinker(
        "ZBR", ("C7", "C8", "C9"),
        frozenset({"rfd4", "mol1", "protenix", "protenix2dock", "alphafold3", "boltz"}),
        "1,3,5-tris(bromomethyl)benzene (TBMB)",
    ),
    "SEZ": BicyclicLinker(
        "SEZ", ("CD", "C1", "C2"),
        frozenset({"protenix", "protenix2dock"}),
        "tris-arm benzene core (protenix CCD only)",
        af3_userccd=True, protenix_overlay=True,
    ),
    "29N": BicyclicLinker(
        "29N", ("C16", "C19", "C25"),
        frozenset({"protenix", "protenix2dock"}),
        "tris-arm linker (protenix CCD only)",
        af3_userccd=True, protenix_overlay=True,
    ),
}


def get_linker(ccd: str) -> BicyclicLinker:
    """Look up a linker; unknown codes raise with the supported set."""
    code = str(ccd or "").strip().upper()
    linker = LINKERS.get(code)
    if linker is None:
        raise ValueError(
            f"未知双环 linker CCD：{ccd}（支持：{sorted(LINKERS)}；"
            "BS3 为对称臂，无法表达三条独立共价键，不在双环支持范围）")
    return linker


def arms_for_backend(ccd: str, backend: str) -> List[str]:
    """Arm atoms of a linker for a specific backend; (linker, backend)
    mismatches raise instead of silently producing an unverifiable payload."""
    linker = get_linker(ccd)
    norm = str(backend or "").strip().lower()
    aliases = {"mol-1": "mol1", "protenix-2-dock": "protenix2dock"}
    norm = aliases.get(norm, norm)
    if norm not in linker.engines:
        raise ValueError(
            f"linker {linker.ccd} 不支持后端 {backend}（可用后端："
            f"{sorted(linker.engines)}）；请改用兼容 linker 或后端")
    return list(linker.arm_atoms)


def atom_map() -> Dict[str, List[str]]:
    """{ccd: [arm atoms]} for consumers that only need the legacy shape
    (candidate-yaml bond construction, linker-code detection)."""
    return {ccd: list(linker.arm_atoms) for ccd, linker in LINKERS.items()}
