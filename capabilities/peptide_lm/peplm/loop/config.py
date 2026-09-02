"""Closed-loop configuration (dataclass, CLI-overridable)."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # lead-optimization setting: keep the provided seed peptide as a permanent
    # edit anchor and score it in round 1 (its per-residue pLDDT powers the
    # structure-guided editor; HALO's reference-ligand analogue)
    anchor_seed: bool = False
    # bicyclic design (V-Bio production protocol): exactly 3 Cys bonded to a
    # tri-functional linker CCD; cys_positions are 0-based interior anchors
    # (the terminal residue is always the third Cys)
    design_mode: str = "linear"          # linear | cyclic | bicyclic
    cys_positions: tuple = ()            # e.g. (2, 7, 14) — 0-based anchors
    allow_extra_cys: bool = False        # keep non-anchor Cys unlinked
    linker_ccd: str = "SEZ"
    bicyclic_layout: str = "first_last"  # first_last | interior_terminal
    # user-defined non-natural residues: ({ccd, smiles, base, placement}, ...)
    # — arbitrary amino acids beyond the 18 presets; registered at loop start
    # (vocab extension + placement pool + oracle CCD cache)
    user_residues: tuple = ()
    # NON-NATURAL AMINO ACIDS ARE USER-SPECIFIED ONLY: CCD codes the user
    # allowed (chosen from the preset catalog and/or user_residues). Empty
    # pool (default) = pure natural design, no NCAA tokens anywhere.
    ncaa_pool: tuple = ()
    # fixed-position residue constraints (production peptideSequenceMask
    # semantics): ({'position': 1-based int, 'residue': 'F' | '[AIB]'}, ...)
    # — the user pins specific amino acids at specific positions; every
    # operator (agent/FIM/mutate/NCAA-repair/layout) protects them
    fixed_residues: tuple = ()
    # primary ranking metric for "best": production composite (0.58 ipsae-led)
    # or pure interface (ipsae_dom, falls back to pair ipTM). Both are always
    # logged in scored.jsonl.
    best_metric: str = "composite"       # composite | ipSAE
    # decode-time constraint tuning (upgrade 3)
    ncaa_decode_bias: float = 0.5        # soft logit bias toward pool tokens
    # cross-backend self-consistency (upgrade 1): re-fold the top-k with an
    # independent predictor each round (0 = off)
    consistency_topk: int = 8
