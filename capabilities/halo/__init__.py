"""HALO: Human-in-the-loop Active Lead Optimization.

A trainable closed-loop lead-optimization algorithm that combines
  * a REINVENT-style SMILES generative agent (prior + RL fine-tuning),
  * chemistry-aware matched-pair / R-group moves (V-Bio lead-opt spirit),
  * Boltz2Score as a structure-based scoring oracle (affinity + ipSAE + pLDDT),
  * an uncertainty-aware online surrogate that amortizes oracle cost,
  * human preference feedback that shapes the reward (learning-to-rank).

Benchmarked on the Schrodinger FEP benchmark targets (CDK2, CDK8, ...).
"""

__version__ = "0.1.0"

import sys
from pathlib import Path

# V-Bio repo root (contains capabilities/); used only for sys.path setup.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Module-local assets: trained priors + novelty reference corpus.
RUNS_DIR = Path(__file__).resolve().parent / "runs"

# Patch transformers for the `safe` package BEFORE any halo module lazily
# imports safe (transformers >= 5.x removed symbols safe still imports).
from halo import safe_compat  # noqa: E402,F401  (applies on import)
