"""Reward for the ESM3 peptide RL loop: relative shaping + residue credit.

All terms are normalised within the round so a cold policy always splits
into better/worse halves; per-residue credit drives re-masking and the
GRPO token weights.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from peplm.score.interface import InterfaceMetrics


@dataclass
class QualityBar:
    """Reported acceptance bar (BindCraft-style). Never zeroes reward."""

    ipsae_min: float = 0.30
    binder_plddt_min: float = 70.0
    lowq_plddt_min: float = 60.0
    interface_pae_max: float = 10.0
    # crystal bound-peptide reference (3LNJ D-pep): min interdist 2.52,
    # CA-CA 3.73 -- heavy-atom contacts at 2.5-2.7 A are physical
    min_clash_dist: float = 2.3
    caca_min: float = 3.2

    def passes(self, m: InterfaceMetrics, clash_dist: float,
               caca: float | None = None) -> bool:
        return (m.ipsae >= self.ipsae_min
                and m.binder_plddt >= self.binder_plddt_min
                and m.lowq_plddt >= self.lowq_plddt_min
                and m.min_interface_pae <= self.interface_pae_max
                and clash_dist >= self.min_clash_dist
                and (caca is None or caca >= self.caca_min))


@dataclass
class DiversityMemory:
    """Pre-oracle novelty filter: repeats never reach the docker budget."""

    exact: set = field(default_factory=set)
    buckets: dict = field(default_factory=lambda: defaultdict(int))
    bucket_size: int = 4

    @staticmethod
    def motif_bucket(sequence: str) -> tuple:
        n = len(sequence)
        return (n // 4,
                min(sequence.count("C"), 4),
                sum(sequence.count(a) for a in "FWY") // 2,
                sum(sequence.count(c) for c in "KRDEH") // 3)

    def is_novel(self, sequence: str) -> bool:
        if sequence in self.exact:
            return False
        return self.buckets[self.motif_bucket(sequence)] < self.bucket_size

    def register(self, sequence: str) -> None:
        self.exact.add(sequence)
        self.buckets[self.motif_bucket(sequence)] += 1


def sequence_is_eligible(sequence: str, min_entropy: float = 2.2) -> bool:
    """Reject degenerate sequences before spending an oracle call."""
    if not sequence:
        return False
    longest = max((len(r) for r in re.findall(r"(.)\1*", sequence)), default=0)
    if longest > len(sequence) * 0.6:
        return False
    n = len(sequence)
    freq = Counter(sequence)
    h = -sum((c / n) * math.log2(c / n) for c in freq.values())
    return h >= min_entropy


def _zscore(values: list[float]) -> np.ndarray:
    """Within-round standardisation; constant input yields zeros."""
    a = np.asarray(values, dtype=float)
    sd = a.std()
    if sd < 1e-9:
        return np.zeros_like(a)
    return (a - a.mean()) / sd


def residue_credit(
    m: InterfaceMetrics,
    plddt_weight: float = 0.5,
    floor: float = 0.05,
) -> np.ndarray:
    """Per-residue blame in [floor, 1]: high = this position is the problem.

    Blends per-residue pLDDT and interface PAE, both ranked within the
    peptide so the vector stays informative even when the whole peptide
    is bad in absolute terms.
    """
    n = max(len(m.per_residue_plddt), len(m.per_residue_ipae))
    if n == 0:
        return np.zeros(0)
    blame = np.zeros(n)
    w_sum = 0.0
    if len(m.per_residue_plddt) == n:
        # low pLDDT -> high blame
        blame += plddt_weight * _rank01(-np.asarray(m.per_residue_plddt))
        w_sum += plddt_weight
    if len(m.per_residue_ipae) == n:
        # high interface PAE -> high blame
        blame += (1.0 - plddt_weight) * _rank01(np.asarray(m.per_residue_ipae))
        w_sum += 1.0 - plddt_weight
    # one missing signal: renormalise instead of halving the range
    if w_sum > 0:
        blame /= w_sum
    return np.clip(blame, floor, 1.0)


def _rank01(a: np.ndarray) -> np.ndarray:
    """Rank-normalise to [0, 1]. Ties get arbitrary distinct ranks (the
    ordering is what the credit uses; exact float ties are rare)."""
    if a.size <= 1:
        return np.zeros_like(a)
    order = a.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(a.size, dtype=float)
    return ranks / (a.size - 1)


class RelativeReward:
    """Turns a round's oracle metrics into GRPO reward tuples.

    Reward dimensions map to the updater's group-advantage axes:
      binding    -- interface PAE (dense) blended with ipSAE (sparse)
      confidence -- mean pLDDT blended with the lowest-quartile value
      geometry   -- CA-CA, clash distance and deep-interpenetration
                    pairs, calibrated to the crystal bound-peptide
                    envelope (3LNJ)
    """

    def __init__(self, bar: QualityBar | None = None,
                 memory: DiversityMemory | None = None):
        self.bar = bar or QualityBar()
        self.memory = memory or DiversityMemory()

    def score_round(self, records: list[dict]) -> list[dict]:
        """Annotate each record in place with ``rewards`` and ``credit``.

        Entries carry ``metrics`` (InterfaceMetrics) plus the CIF-audit
        geometry fields; failed docks (``metrics=None``) get the round's
        minimum reward so they still count as negative examples.
        """
        ok = [r for r in records if r.get("metrics") is not None]
        if not ok:
            for r in records:
                r["rewards"] = (0.0, 0.0, 0.0)
                r["credit"] = np.zeros(0)
            return records

        # binding: -PAE, z-scored within the round; the additive clash
        # penalty applies before normalisation so a buried pose loses even
        # at the round's best PAE
        def _binding_pae(r: dict) -> float:
            clash = float(r.get("min_clash_dist") or 0.0)
            burial = 3.0 * max(0.0, 2.2 - clash)
            pairs = 0.02 * float(r.get("pairs_deep") or 0)
            return -r["metrics"].mean_interface_pae - burial - pairs

        z_pae = _zscore([_binding_pae(r) for r in ok])
        z_min_pae = _zscore([-r["metrics"].min_interface_pae for r in ok])
        z_ipsae = _zscore([r["metrics"].ipsae for r in ok])
        z_plddt = _zscore([r["metrics"].binder_plddt for r in ok])
        z_lowq = _zscore([r["metrics"].lowq_plddt for r in ok])
        z_contacts = _zscore([float(r.get("nonlocal_contacts") or 0)
                              for r in ok])
        # backbone integrity is the collapse signal: crystal bound peptides
        # keep CA-CA >= 3.6 while de novo failures crush to 1.8-2.5
        z_caca = _zscore([float(r.get("caca_min") or 0.0) for r in ok])
        z_clash = _zscore([float(r.get("min_clash_dist") or 0.0) for r in ok])
        z_pairs = _zscore([-float(r.get("pairs_deep") or 0) for r in ok])
        # stereochemical integrity: mixed chirality and twisted peptide
        # bonds; missing stereo keys fill with the round's worst measured
        # value, not "perfect"
        def _worst_filled(key):
            vals = [float(r[key]) for r in ok if r.get(key) is not None]
            return max(vals) if vals else 0.0
        chir_fill = _worst_filled("chir_mixed")
        omega_fill = _worst_filled("omega_bad")
        z_chir = _zscore([-float(r.get("chir_mixed", chir_fill) or chir_fill)
                          for r in ok])
        z_omega = _zscore([-float(r.get("omega_bad", omega_fill) or omega_fill)
                           for r in ok])

        for i, r in enumerate(ok):
            m = r["metrics"]
            binding = 0.5 * z_pae[i] + 0.3 * z_min_pae[i] + 0.2 * z_ipsae[i]
            confidence = 0.4 * z_plddt[i] + 0.6 * z_lowq[i]
            geometry = (0.15 * z_contacts[i] + 0.2 * z_caca[i]
                        + 0.2 * z_clash[i] + 0.15 * z_pairs[i]
                        + 0.15 * z_chir[i] + 0.15 * z_omega[i])
            r["rewards"] = (float(binding), float(confidence), float(geometry))
            r["credit"] = residue_credit(m)
            r["passes_bar"] = self.bar.passes(
                m, r.get("min_clash_dist", 0.0), r.get("caca_min"))

        # failed docks: one sd below the worst real candidate -- a definite
        # negative, not a tie
        worst = min(min(r["rewards"]) for r in ok)
        for r in records:
            if r.get("metrics") is None:
                r["rewards"] = (worst - 1.0,) * 3
                r["credit"] = np.zeros(0)
                r["passes_bar"] = False
        return records
