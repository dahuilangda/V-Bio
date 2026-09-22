"""PeptideLM: LM peptide design for V-Bio.

Tier 1: property/modality/SS-conditioned causal prior over residue-monomer
sequences. Tier 2: receptor-conditioned design (PepMLM-650M) plus the
closed loop (Boltz-2 / Protenix oracle, GRPO, surrogate gating).
"""

from peplm.__version__ import __version__

__all__ = ["__version__"]
