"""Peptide reward: pose-gated interface evidence + property bands.

HALO-style weighted geometric mean of band terms; interface credit is
gated by pose evidence so an ill-folded peptide cannot earn it through
a lucky ipSAE.
"""

from __future__ import annotations

import math


import numpy as np

from peplm.candidate import Candidate
from peplm.props.descriptors import compute_props
from peplm.score.learned_props import _load_learned_heads


def _sigmoid(x, lo, hi):
    width = max((hi - lo) * 0.15, 0.5)
    return float(1.0 / (1.0 + math.exp((lo - x) / width))
                * 1.0 / (1.0 + math.exp((x - hi) / width)))


def _ramp(x, lo, width=0.12):
    return float(1.0 / (1.0 + math.exp((lo - x) / max(width, 1e-6))))


def seq_identity(a: str, b: str) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / max(1, len(a))


class PeptideReward:
    def __init__(self, target_iptm: float = 0.65, ncaa_range: tuple = (0, 6),
                 len_range: tuple = (8, 25), seed_sequences: list[str] | None = None,
                 weights: dict | None = None):
        self.target_iptm = target_iptm
        self.ncaa_range = ncaa_range
        self.len_range = len_range
        self.seeds = [s.upper() for s in (seed_sequences or [])]
        self.weights = weights or {
            "interface": 3.0, "iptm": 1.0, "pose": 1.5,
            "sol": 0.8, "syn": 0.8, "liab": 0.8,
            "min_ipae": 0.8, "learned_heads": 0.6, "self_consistency": 0.7,
            "ncaa": 0.4, "len": 0.4, "sim": 0.5,
            # pocket chemistry complementarity: a zero-oracle-cost pull
            # toward side-chain chemistry matching the user pocket
            "chem_comp": 0.9, "binder_alone": 1.2,
        }
        self._learned = None  # lazily loaded PeptideGPT property heads

    def _similarity(self, cand: Candidate) -> float:
        seq = "".join(t for t in cand.residues if len(t) == 1)
        if not self.seeds:
            return 0.0
        return max(seq_identity(seq, s) for s in self.seeds)

    def machine_reward(self, cand: Candidate, surrogate_pred: dict | None = None,
                       surrogate_sigma: float | None = None) -> tuple[float, dict]:
        """surrogate rows pass predicted metrics + risk-averse sigma so
        unverified candidates cannot out-earn oracle-verified ones."""
        m = dict(surrogate_pred) if surrogate_pred else dict(cand.metrics)
        if not cand.props:
            cand.props = compute_props(cand.residues, cyclic=cand.cyclic)
        p = cand.props
        parts: dict = {}

        ipsae = m.get("ipsae_dom")
        lig_max = m.get("ligand_ipsae_max")
        pair = m.get("pair_iptm")
        if ipsae is None and lig_max is not None:
            ipsae = lig_max
        if ipsae is None and pair is not None:
            ipsae = float(pair)
        if ipsae is not None:
            if surrogate_sigma is not None and surrogate_sigma > 0:
                ipsae = float(ipsae) - surrogate_sigma  # risk-averse
            # ramp anchored at 0.20: de novo cyclic binders operate in
            # the 0.2-0.5 ipSAE band during search
            parts["interface"] = _ramp(float(ipsae), 0.20, 0.15)
        iptm = m.get("pair_iptm") if m.get("pair_iptm") is not None else m.get("iptm")
        if iptm is not None:
            parts["iptm"] = _ramp(float(iptm), self.target_iptm - 0.25, 0.15)
        plddt = m.get("binder_avg_plddt")
        if plddt:
            parts["pose"] = _ramp(float(plddt) / 100.0, 0.45, 0.12)

        # Binder-alone determinism (BindCraft): peptide-chain pLDDT from
        # the complex approximates folding alone (correlated, not identical)
        binder_chain_plddt = m.get("binder_chain_plddt")
        if binder_chain_plddt is not None:
            parts["binder_alone"] = _ramp(
                float(binder_chain_plddt) / 100.0, 0.40, 0.20)
        # pose gate: interface credit only when the peptide folds/poses well
        gate = parts.get("pose", 0.5)
        if "interface" in parts:
            parts["interface"] *= (0.35 + 0.65 * gate)

        parts["sol"] = _ramp(p["solubility"], 0.35, 0.15)
        parts["syn"] = _ramp(p["synthesizability"], 0.45, 0.15)
        parts["liab"] = _ramp(p["liability"], 0.60, 0.15)
        sc = m.get("self_consistency")
        if sc is not None:
            parts["self_consistency"] = float(sc)
        cc = m.get("chem_comp")
        if cc is not None:
            # pocket chemistry complementarity in [0, 1], computed by the
            # orchestrator from the user pocket's amino-acid composition
            parts["chem_comp"] = float(cc)
        mp = m.get("min_ipae")
        if mp is not None:
            # SOTA interchain confidence: min target-binder PAE (Angstrom);
            # small = confident interface. Ramp: >=1.0 at <=6 A -> ~0 at >14 A.
            parts["min_ipae"] = float(1.0 / (1.0 + math.exp((float(mp) - 9.0) / 2.0)))
        if self._learned is None:
            self._learned = _load_learned_heads()
        if self._learned is not None:
            lh = self._learned(cand.residues)
            if lh:
                # learned developability shaping: mean over heads with a
                # 0.5 floor -- an informative pull, never a hard gate
                vals = [float(v) for v in lh.values()]
                parts["learned_heads"] = 0.5 + 0.5 * (sum(vals) / len(vals))
        parts["ncaa"] = _sigmoid(p["ncaa_count"], self.ncaa_range[0], self.ncaa_range[1] + 1)
        parts["len"] = _sigmoid(p["length"], self.len_range[0], self.len_range[1] + 1)
        if self.seeds:
            parts["sim"] = _sigmoid(self._similarity(cand), 0.20, 0.90)

        log_r, total_w = 0.0, 0.0
        for k, w in self.weights.items():
            if k in parts:
                log_r += w * math.log(max(parts[k], 1e-3))
                total_w += w
        score = float(np.clip(math.exp(log_r / max(total_w, 1e-6)), 0.0, 1.0))
        return score, parts

    def combine_batch(self, parts_list: list[dict]) -> list[float]:
        """Batch z-norm mixing (HALO): saturated dimensions stop drowning
        the others; blended 30/70 with the raw machine reward by the engine."""
        if not parts_list:
            return []
        keys = [k for k in self.weights if any(k in p for p in parts_list)]
        stats = {}
        for k in keys:
            arr = np.asarray([p.get(k, 0.0) for p in parts_list], float)
            mu, sd = float(arr.mean()), float(arr.std())
            stats[k] = (mu, sd if sd > 1e-6 else 1.0)
        eps, total_w = 1e-3, sum(self.weights[k] for k in keys) or 1.0
        out = []
        for p in parts_list:
            log_r = 0.0
            for k in keys:
                mu, sd = stats[k]
                zn = 1.0 / (1.0 + math.exp(-(p.get(k, 0.0) - mu) / sd))
                log_r += self.weights[k] * math.log(max(zn, eps))
            out.append(float(np.clip(math.exp(log_r / total_w), 0.0, 1.0)))
        return out
