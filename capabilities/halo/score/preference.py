"""Human preference model: online Bradley-Terry learning-to-rank reward shaping.

The human provides pairwise preferences ("this candidate is better than that
one") and molecule-level accept/reject judgements during batch review. These
are converted into pairwise comparisons and a linear preference score

    f_pref(x) = w . descriptives(x)

is fit online (interpretable weights - chemists can inspect what the model
learned). The preference bonus enters the final reward with a confidence
weight lambda that grows with the amount of feedback.
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
from rdkit import Chem

from halo.score.properties import descriptor_vector


class PreferenceModel:
    def __init__(self, lr=0.05, epochs=200, l2=1e-3, dim=None):
        self.dim = dim or 14
        self.lr = lr
        self.epochs = epochs
        self.l2 = l2
        self.w = torch.zeros(self.dim, dtype=torch.float64, requires_grad=False)
        self.pairs: list[tuple[np.ndarray, np.ndarray]] = []
        self.fitted = False

    # ---- data collection ----------------------------------------------------
    def add_pair(self, feats_a: np.ndarray, feats_b: np.ndarray) -> None:
        """Preference: a is BETTER than b."""
        self.pairs.append((np.asarray(feats_a, dtype=np.float64), np.asarray(feats_b, dtype=np.float64)))

    def add_pair_from_smiles(self, smi_a: str, smi_b: str) -> bool:
        if not isinstance(smi_a, str) or not isinstance(smi_b, str):
            return False
        try:
            ma, mb = Chem.MolFromSmiles(smi_a), Chem.MolFromSmiles(smi_b)
        except Exception:
            return False
        if ma is None or mb is None:
            return False
        self.add_pair(descriptor_vector(ma), descriptor_vector(mb))
        return True

    @property
    def n_pairs(self) -> int:
        return len(self.pairs)

    # ---- training -----------------------------------------------------------
    def fit(self) -> dict:
        if not self.pairs:
            return {"n": 0}
        w = torch.zeros(self.dim, dtype=torch.float64)
        w.requires_grad_(True)
        A = torch.from_numpy(np.stack([p[0] for p in self.pairs]))
        B = torch.from_numpy(np.stack([p[1] for p in self.pairs]))
        # feature standardization
        F_all = torch.cat([A, B])
        mu, sd = F_all.mean(0), F_all.std(0).clamp_min(1e-6)
        opt = torch.optim.LBFGS([w], lr=self.lr, max_iter=self.epochs, line_search_fn="strong_wolfe")

        def closure():
            opt.zero_grad()
            logits = ((A - mu) / sd) @ w - ((B - mu) / sd) @ w
            loss = torch.nn.functional.binary_cross_entropy_with_logits(
                logits, torch.ones_like(logits)
            ) + self.l2 * (w ** 2).sum()
            loss.backward()
            return loss

        opt.step(closure)
        self.w = w.detach()
        self._mu, self._sd = mu.numpy(), sd.numpy()
        self.fitted = True
        with torch.no_grad():
            acc = float((torch.sign(((A - mu) / sd) @ w - ((B - mu) / sd) @ w) > 0).float().mean())
        return {"n": len(self.pairs), "train_acc": acc}

    # ---- scoring ------------------------------------------------------------
    def score(self, feats: np.ndarray) -> float:
        """Raw preference score f(x); 0 when unfitted."""
        if not self.fitted:
            return 0.0
        z = (np.asarray(feats, dtype=np.float64) - self._mu) / self._sd
        return float(z @ self.w.numpy())

    def score_smiles(self, smiles: str) -> float:
        m = Chem.MolFromSmiles(smiles)
        return self.score(descriptor_vector(m)) if m else 0.0

    def confidence(self, max_pairs: int = 400) -> float:
        """Mixing weight for the preference bonus in the final reward."""
        return min(1.0, self.n_pairs / max_pairs)

    def interpret(self, keys: tuple) -> dict:
        """Standardized weights per descriptor (what the human seems to want)."""
        if not self.fitted:
            return {}
        z = self.w.numpy() / self._sd
        order = np.argsort(-np.abs(z))
        return {keys[i]: float(z[i]) for i in order}

    def save(self, path):
        np.savez(str(path), w=self.w.numpy(), mu=getattr(self, "_mu", np.zeros(self.dim)),
                 sd=getattr(self, "_sd", np.ones(self.dim)), pairs=np.array([[a, b] for a, b in self.pairs]))

    def load(self, path):
        d = np.load(str(path))
        self.w = torch.from_numpy(d["w"])
        self._mu, self._sd = d["mu"], d["sd"]
        self.pairs = [(a, b) for a, b in d["pairs"]] if d["pairs"].size else []
        self.fitted = bool(np.abs(d["w"]).sum() > 0)
