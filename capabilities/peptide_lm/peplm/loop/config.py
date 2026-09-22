"""Closed-loop configuration (dataclass, CLI-overridable)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopConfig:
    n_rounds: int = 8
    n_agent: int = 32                 # de novo samples per round
    n_edit: int = 24                  # structure-guided edits per round
    n_mut: int = 16                   # point-mutation moves per round
    oracle_budget: int = 16           # real Boltz calls per round (surrogate-gated)
    len_range: tuple = (8, 25)
    ncaa_range: tuple = (0, 6)
    cyclic: bool = False
    dev_floor: float = 0.35           # hard filter: developability >= floor
    temperature: float = 1.0
    rl_lr: float = 3e-5
    rl_epochs: int = 2
    kl_beta: float = 0.02
    use_surrogate: bool = True
    acquisition_kappa: float = 0.35
    exploit_fraction: float = 0.6
    elite_size: int = 12
    seed: int = 0
    device: str = "cuda"
    gpus: tuple = (0, 1, 2, 3)
    # lead optimization: keep the seed peptide as a permanent edit anchor
    # and score it in round 1 (its pLDDT powers the structure-guided editor)
    anchor_seed: bool = False
    # bicyclic: exactly 3 Cys bonded to a tri-functional linker CCD;
    # cys_positions are 0-based interior anchors (the terminal residue is
    # always the third Cys)
    design_mode: str = "linear"          # linear | cyclic | bicyclic
    cys_positions: tuple = ()            # e.g. (2, 7, 14) — 0-based anchors
    # length-function anchor layout ({"mode": "ring"|"ratio", ...});
    # supersedes cys_positions, keeps manual topologies valid across
    # adaptive lengths
    cys_layout: dict | None = None
    allow_extra_cys: bool = False        # keep non-anchor Cys unlinked
    linker_ccd: str = "SEZ"
    bicyclic_layout: str = "first_last"  # first_last | interior_terminal
    # user-defined residues ({ccd, smiles, base, placement}, ...),
    # registered at loop start (vocab + placement pool + oracle CCD cache)
    user_residues: tuple = ()
    # user-allowed NCAA tokens only; empty pool (default) = natural-only
    # design, no NCAA tokens anywhere
    ncaa_pool: tuple = ()
    # fixed positions ({'position': 1-based int, 'residue': str}, ...);
    # every operator protects them
    fixed_residues: tuple = ()
    # ranking metric for "best": composite (ipsae-led) or pure interface;
    # both are logged in scored.jsonl
    best_metric: str = "composite"       # composite | ipSAE
    # decode-time constraint tuning
    ncaa_decode_bias: float = 0.5        # soft logit bias toward pool tokens
    # cross-backend re-fold of the top-k each round (0 = off)
    consistency_topk: int = 8
