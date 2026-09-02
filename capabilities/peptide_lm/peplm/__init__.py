"""PeptideLM: two-tier language-model peptide design.

Tier 1: property-conditioned GPT-2 prior over residue-monomer sequences.
Tier 2: target-conditioned closed loop (Boltz-2 oracle, GRPO, surrogate gating).
"""

__version__ = "0.1.0"
