"""Multi-objective reward composition (REINVENT-style weighted product).

Machine reward combines structure-based terms (affinity pIC50, ipSAE,
ligand pLDDT - from the Boltz oracle or the surrogate), property terms
(QED window, SAS, cLogP) and the human preference bonus.
"""

from __future__ import annotations

import math

import numpy as np
from rdkit import Chem
from rdkit import DataStructs
from rdkit.Chem import rdFingerprintGenerator

from halo.score.properties import compute_descriptors, descriptor_vector

_fp_gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def _sigmoid(x, lo, hi):
    """Band-shaped reward: exp decay outside [lo, hi], smooth+dense gradient
    (LLMol-style band shaping replaces sparse product-of-thresholds)."""
    width = max((hi - lo) * 0.15, 0.5)
    return float(1.0 / (1.0 + math.exp((lo - x) / width)) * 1.0 / (1.0 + math.exp((x - hi) / width)))


def _ramp(x, lo, width):
    """Monotone rising ramp (unlike the band above)."""
    return float(1.0 / (1.0 + math.exp((lo - x) / max(width, 1e-6))))


def affinity_term(pic50: float, target_pic50: float = 8.0) -> float:
    """Sigmoid ramp on predicted pIC50."""
    return float(1.0 / (1.0 + math.exp(-(pic50 - target_pic50) * 1.2)))


def pose_evidence(iptm: float, ipsae: float, plddt: float) -> float:
    """Pose-evidence weight in [0,1].

    Boltz-2's affinity head is unreliable when the pose is wrong (Corso et
    al. 2025): ipTM / ipSAE / ligand pLDDT quantify how much the predicted
    complex supports the affinity number. We use this evidence to GATE the
    affinity reward - a high affinity score on an ill-posed complex earns
    almost nothing.
    """
    w_iptm = float(np.clip((iptm - 0.35) / 0.35, 0, 1)) ** 0.5
    w_ipsae = float(np.clip(ipsae / 0.5, 0, 1)) ** 0.5
    w_plddt = float(np.clip((plddt - 45) / 35.0, 0, 1)) ** 0.5
    return float(np.clip(w_iptm * (0.4 + 0.6 * w_ipsae) * (0.5 + 0.5 * w_plddt), 0.0, 1.0))


def pose_quality_term(ipsae: float, plddt: float) -> float:
    """ipSAE higher-better (typically 0..~0.8); ligand pLDDT 0..100."""
    return float(np.clip(ipsae / 0.55, 0, 1) ** 1.5) * float(np.clip((plddt - 45) / 35.0, 0, 1))


def property_terms(desc: dict) -> dict:
    return {
        "qed": _sigmoid(desc["qed"], 0.45, 1.01),
        "sas": _sigmoid(desc["sas"], 0.0, 6.0),
        "clogp": _sigmoid(desc["clogp"], 0.0, 4.5),
        "mw": _sigmoid(desc["mw"], 250.0, 520.0),
    }


class RewardFunction:
    """Weighted product of terms; human preference bonus mixed in linearly."""

    def __init__(
        self,
        target_pic50: float = 8.0,
        pref_lambda_max: float = 0.30,
        seed_smiles: list[str] | None = None,
        use_pose_gate: bool = True,
        novelty_index=None,
    ):
        self.target_pic50 = target_pic50
        self.pref_lambda_max = pref_lambda_max
        self.use_pose_gate = use_pose_gate
        self.novelty_index = novelty_index
        self.pref_model = None  # set by engine
        self.weights = {
            "affinity": 3.0,
            "pose": 2.0,
            "qed": 0.5,
            "sas": 0.5,
            "clogp": 0.5,
            "mw": 0.5,
            "similarity": 1.0,
        }
        if novelty_index is not None:
            self.weights["novelty"] = 0.5
        self._seed_fps = None
        if seed_smiles:
            mols = [Chem.MolFromSmiles(s) for s in seed_smiles]
            self._seed_fps = [_fp_gen.GetFingerprint(m) for m in mols if m is not None]

    def max_tanimoto_to_seeds(self, smiles: str) -> float:
        if not self._seed_fps:
            return 0.0
        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return 0.0
        fp = _fp_gen.GetFingerprint(m)
        return float(max(DataStructs.TanimotoSimilarity(fp, s) for s in self._seed_fps))

    def similarity_term(self, smiles: str, band=(0.2, 0.9)) -> float:
        return _sigmoid(self.max_tanimoto_to_seeds(smiles), band[0], band[1])

    def machine_reward(self, mol_or_smiles, affinity_pic50: float, ipsae: float, plddt: float,
                       similarity_band=(0.2, 0.9), iptm: float | None = None,
                       conservative_sigma: float | None = None,
                       surrogate_uncertainty: tuple | None = None) -> tuple[float, dict]:
        mol = mol_or_smiles if isinstance(mol_or_smiles, Chem.Mol) else Chem.MolFromSmiles(mol_or_smiles)
        parts = {}
        if mol is None:
            return 0.0, parts
        desc = compute_descriptors(mol)
        smiles = Chem.MolToSmiles(mol)
        eff_pic50 = affinity_pic50
        if conservative_sigma is not None and conservative_sigma > 0:
            eff_pic50 = affinity_pic50 - conservative_sigma  # risk-averse surrogate score
        parts["affinity_raw"] = affinity_term(affinity_pic50, self.target_pic50)
        if surrogate_uncertainty is not None:
            # surrogate row: pose evidence from pessimistic ipsae/plddt and a
            # discounted iptm prior (no measured value exists) - removes the
            # systematic gate advantage unverified molecules had over
            # oracle-verified ones
            s_ips, s_pl = surrogate_uncertainty
            ipsae = float(ipsae or 0) - 0.5 * float(s_ips or 0)
            plddt = float(plddt or 0) - 0.5 * float(s_pl or 0)
            iptm = 0.45
        evidence = pose_evidence(iptm if iptm is not None else 0.6, ipsae, plddt) if self.use_pose_gate else 1.0
        parts["pose_evidence"] = evidence
        # pose-gated affinity: untrustworthy complexes do not earn affinity credit
        parts["affinity"] = evidence * affinity_term(eff_pic50, self.target_pic50)
        parts["pose"] = pose_quality_term(ipsae, plddt)
        parts.update(property_terms(desc))
        parts["similarity"] = self.similarity_term(smiles, similarity_band)
        if self.novelty_index is not None:
            # 1 - max Tanimoto to ChEMBL; exact copies score ~0
            nov = self.novelty_index.novelty(smiles)
            parts["novelty"] = _ramp(nov, 0.35, 0.15)
        log_r = 0.0
        eps = 1e-3
        for k, w in self.weights.items():
            if k in parts:
                log_r += w * math.log(max(parts[k], eps))
        machine = math.exp(log_r / max(sum(self.weights.values()), 1e-6))
        return float(np.clip(machine, 0.0, 1.0)), parts

    def combine_batch(self, parts_list: list[dict]) -> list[float]:
        """Combine per-molecule reward parts across the pool: each dimension is
        z-scored within the batch and squashed to (0,1) before the weighted
        geometric mean, so saturated dimensions do not drown the others."""
        if not parts_list:
            return []
        keys = [k for k in self.weights if any(k in p for p in parts_list)]
        stats = {}
        for k in keys:
            vals = [p.get(k, 0.0) for p in parts_list]
            arr = np.asarray(vals, dtype=float)
            mu, sd = float(arr.mean()), float(arr.std())
            stats[k] = (mu, sd if sd > 1e-6 else 1.0)
        eps = 1e-3
        total_w = sum(self.weights[k] for k in keys) or 1.0
        out = []
        for p in parts_list:
            log_r = 0.0
            for k in keys:
                mu, sd = stats[k]
                zn = 1.0 / (1.0 + math.exp(-(p.get(k, 0.0) - mu) / sd))  # (0,1)
                log_r += self.weights[k] * math.log(max(zn, eps))
            out.append(float(np.clip(math.exp(log_r / total_w), 0.0, 1.0)))
        return out

    def final_reward(self, machine: float, smiles: str, pref_conf: float = 0.0) -> tuple[float, float]:
        """Mix machine reward with the (confidence-weighted) human preference bonus."""
        bonus = 0.0
        if self.pref_model is not None and self.pref_model.fitted:
            f = self.pref_model.score_smiles(smiles)
            bonus = float(np.clip(0.5 + f / 4.0, 0.0, 1.0))  # squashed to [0,1]
        lam = self.pref_lambda_max * pref_conf
        final = (1 - lam) * machine + lam * bonus
        return float(final), bonus
