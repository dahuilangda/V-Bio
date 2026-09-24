"""Typed configuration for the backend proposal engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProposerConfig:
    """All proposal-engine knobs in one place; see ESM3Proposer
    docstring for per-field semantics."""
    # length
    peptide_length: int | None = None
    len_range: tuple[int, int] | None = None

    # NCAA
    ncaa_min: int = 0
    ncaa_max: int = 0
    ncaa_pool: list[str] = field(default_factory=list)
    user_residues: list[dict] | None = None
    ncaa_decode_bias: float = 0.5

    # topology
    design_mode: str = "linear"
    cyclic: bool = False
    cys_positions: list[int] = field(default_factory=list)
    cys_layout: dict | None = None
    allow_extra_cys: bool = False

    # constraints
    fixed_residues: list[dict] = field(default_factory=list)
    ss_profile: str | None = None

    # conditioning
    target_sequence: str | None = None
    target_pocket_positions: list[int] | None = None

    # runtime
    device: str = "cpu"
    seed: int = 0
