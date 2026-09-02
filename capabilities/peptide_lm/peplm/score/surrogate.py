"""Surrogate model for oracle-budget gating (HALO pattern).

Bagged gradient-boosted trees over cheap features (composition + property
oracles) predict the interface metric and binder pLDDT; UCB (mu + kappa*sigma
across the bag) decides which candidates earn real Boltz calls.
"""

from __future__ import annotations

import numpy as np

from peplm.candidate import Candidate
from peplm.props.descriptors import compute_props
from peplm.residues import NCAA_TOKENS, NATURAL_AA
from peplm.vocab import residue_tokens

FEATURE_KEYS = ["length", "ncaa_count", "solubility", "synthesizability",
                "liability", "net_charge", "mw", "hydrophobic_ratio",
                "aromatic_ratio", "cyclic"]
TARGET_KEYS = ["interface", "plddt"]


def candidate_features(cand: Candidate) -> np.ndarray:
    if not cand.props:
        cand.props = compute_props(cand.residues, cyclic=cand.cyclic)
    p = cand.props
    comp = np.zeros(len(NATURAL_AA) + len(NCAA_TOKENS))
    order = {t: i for i, t in enumerate(list(NATURAL_AA) + NCAA_TOKENS)}
    res = residue_tokens(cand.tokens)
    for t in res:
        if t in order:
            comp[order[t]] += 1
    comp /= max(len(res), 1)
    feats = [p[k] if not isinstance(p[k], bool) else float(p[k])
             for k in FEATURE_KEYS]
    return np.concatenate([comp, np.asarray(feats, float)])


def _interface_value(metrics: dict) -> float | None:
    for k in ("ipsae_dom", "ligand_ipsae_max", "pair_iptm"):
        v = metrics.get(k)
        if v is not None:
            return float(v)
    return None


class Surrogate:
    def __init__(self, n_models: int = 5, seed: int = 0):
        from sklearn.ensemble import GradientBoostingRegressor

        self.models = {k: [GradientBoostingRegressor(
            n_estimators=120, max_depth=3, subsample=0.8,
            random_state=seed + i) for i in range(n_models)]
            for k in TARGET_KEYS}
        self.fitted = False
        self._mean = np.zeros(len(TARGET_KEYS))
        self.X: list[np.ndarray] = []
        self.y = {k: [] for k in TARGET_KEYS}

    @property
    def n_obs(self) -> int:
        return len(self.X)

    def add_observations(self, candidates: list[Candidate]):
        for cand in candidates:
            m = cand.metrics
            iv = _interface_value(m)
            plddt = m.get("binder_avg_plddt")
            if iv is None and not plddt:
                continue
            self.X.append(candidate_features(cand))
            if iv is not None:
                self.y["interface"].append(iv)
            if plddt is not None:
                self.y["plddt"].append(float(plddt) / 100.0)
        if len(self.X) >= 12:
            self.fit()

    def fit(self) -> dict:
        X = np.stack(self.X)
        for k in TARGET_KEYS:
            if len(self.y[k]) >= 6:
                for m in self.models[k]:
                    m.fit(X, np.asarray(self.y[k]))
                self.fitted = True
        if self.fitted:
            preds = np.stack([np.asarray(self.y[k] or [0.0]) for k in TARGET_KEYS], 1)
            self._mean = preds.mean(0)
        return {"n": self.n_obs, "fitted": self.fitted}

    def predict(self, candidates: list[Candidate]):
        X = np.stack([candidate_features(c) for c in candidates])
        if not self.fitted:
            mu = np.tile(self._mean, (len(candidates), 1))
            sig = np.ones((len(candidates), len(TARGET_KEYS)))
            return mu, sig
        mus, sigs = [], []
        for k in TARGET_KEYS:
            P = np.stack([m.predict(X) for m in self.models[k]], 1)
            mus.append(P.mean(1))
            sigs.append(P.std(1))
        return np.stack(mus, 1), np.stack(sigs, 1)
