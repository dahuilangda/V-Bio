"""Production RL components for GRPO (each with published gains).

Measured behaviour on Boltz-2 rewards:
  * prioritized experience replay (buffer=100... we use 512, batch=20% of
    step batch): +10% effectiveness, +6% efficiency, no validity loss
  * moving-average baseline (on top of group-relative baseline): +6%/+4%
  * hill-climbing top-k retention: +10%/+6% (validity -3%)
  * RND intrinsic reward: +18% diversity (best diversity/validity trade)
  * TanhIMS soft scaffold penalty: soft
    penalty superior to binary diversity filters; best combined with RND
"""

from __future__ import annotations

import random
from collections import deque

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

_FP = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


class PrioritizedReplay:
    """REINVENT4/REINFORCE-ING style replay with reward-proportional sampling."""

    def __init__(self, capacity: int = 512, alpha: float = 0.6):
        self.buf: deque = deque(maxlen=capacity)
        self.alpha = alpha

    def add(self, samples: list[tuple[str, float]], max_reward: float = 1.0) -> None:
        for s, r in samples:
            if r <= 0:
                continue
            self.buf.append((s, float(r)))

    def sample(self, n: int, rng: random.Random) -> list[tuple[str, float]]:
        if not self.buf:
            return []
        items = list(self.buf)
        w = np.array([max(r, 1e-3) ** self.alpha for _, r in items])
        w = w / w.sum()
        idx = rng.choices(range(len(items)), weights=w.tolist(), k=min(n, len(items)))
        return [items[i] for i in idx]

    def __len__(self):
        return len(self.buf)


class MovingAverageBaseline:
    """Exponential moving average of episode returns (REINFORCE-ING: +6% eff)."""

    def __init__(self, momentum: float = 0.9):
        self.momentum = momentum
        self.value: float | None = None

    def update(self, rewards: list[float]) -> float:
        batch_mean = float(np.mean(rewards)) if rewards else 0.0
        if self.value is None:
            self.value = batch_mean
        else:
            self.value = self.momentum * self.value + (1 - self.momentum) * batch_mean
        return self.value

    def advantage(self, rewards: np.ndarray) -> np.ndarray:
        return rewards - (self.value if self.value is not None else rewards.mean())


class ScaffoldSoftPenalty:
    """TanhIMS soft identical-scaffold penalty (IJCAI 2025 best practice).

    Penalizes the k-th repetition of the same Murcko scaffold smoothly:
        pen(scaffold with count c) = tanh((bucket - c) / bucket)
    instead of zeroing reward outright (hard filter) - keeps gradient signal.
    """

    def __init__(self, bucket: int = 25):
        self.bucket = bucket
        self.counts: dict[str, int] = {}

    def penalty(self, smiles_list: list[str]) -> np.ndarray:
        pens = np.ones(len(smiles_list))
        for i, s in enumerate(smiles_list):
            m = Chem.MolFromSmiles(s)
            if m is None:
                continue
            try:
                scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=m)
            except Exception:
                continue
            c = self.counts.get(scaf, 0)
            pens[i] = float(np.tanh((self.bucket - c) / self.bucket))
            self.counts[scaf] = c + 1
        return pens

    def register(self, smiles_list: list[str]) -> None:
        for s in smiles_list:
            m = Chem.MolFromSmiles(s)
            if m is None:
                continue
            try:
                scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=m)
                self.counts[scaf] = self.counts.get(scaf, 0) + 1
            except Exception:
                pass


class RNDIntrinsicReward:
    """Random Network Distillation novelty bonus (+18% diversity, IJCAI 2025).

    A frozen random target network and a trained predictor both embed
    fingerprints; prediction error = novelty. Pure-PyTorch, tiny.
    """

    def __init__(self, dim_in: int = 2048, dim_hidden: int = 256, lr: float = 1e-3, device="cpu"):
        import torch
        import torch.nn as nn

        self.device = device
        self.target = nn.Sequential(nn.Linear(dim_in, dim_hidden), nn.ReLU(),
                                    nn.Linear(dim_hidden, dim_hidden)).to(device)
        for p in self.target.parameters():
            p.requires_grad_(False)
        self.predictor = nn.Sequential(nn.Linear(dim_in, dim_hidden), nn.ReLU(),
                                       nn.Linear(dim_hidden, dim_hidden)).to(device)
        self.opt = torch.optim.Adam(self.predictor.parameters(), lr=lr)
        self._scale = None

    def _fp(self, smiles: str) -> np.ndarray | None:
        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return None
        return np.asarray(_FP.GetFingerprintAsNumPy(m), dtype=np.float32)

    def novelty(self, smiles_list: list[str], update: bool = True) -> np.ndarray:
        import torch

        out = np.zeros(len(smiles_list))
        fps, idx = [], []
        for i, s in enumerate(smiles_list):
            f = self._fp(s)
            if f is not None:
                fps.append(f)
                idx.append(i)
        if not fps:
            return out
        x = torch.from_numpy(np.stack(fps)).to(self.device)
        with torch.no_grad():
            tgt = self.target(x)
        pred = self.predictor(x)
        err = ((pred - tgt) ** 2).sum(1).detach().cpu().numpy()
        if self._scale is None or self._scale <= 0:
            self._scale = float(err.mean()) if err.mean() > 0 else 1.0
        out[np.array(idx)] = err / self._scale
        out = np.nan_to_num(out, nan=0.0, posinf=0.0)
        if update:
            loss = ((self.predictor(x) - tgt) ** 2).sum(1).mean()
            self.opt.zero_grad()
            loss.backward()
            self.opt.step()
        return out


def compose_multiobjective_reward(
    base_rewards: np.ndarray,
    smiles_list: list[str],
    scaffold_penalty: ScaffoldSoftPenalty | None = None,
    rnd: RNDIntrinsicReward | None = None,
    w_diversity: float = 0.10,
    w_novelty: float = 0.10,
) -> np.ndarray:
    """final = base * (1 + w_div*ims_pen) * (1 + w_nov*rnd_norm), clipped [0,1]."""
    r = np.nan_to_num(np.clip(base_rewards, 0.0, 1.0), nan=0.0).copy()
    if scaffold_penalty is not None:
        r *= 1.0 + w_diversity * (scaffold_penalty.penalty(smiles_list) - 1.0)
    if rnd is not None:
        nov = rnd.novelty(smiles_list)
        nov = nov / (nov.max() + 1e-6)
        r *= 1.0 + w_novelty * nov
    return np.nan_to_num(np.clip(r, 0.0, 1.0), nan=0.0)
