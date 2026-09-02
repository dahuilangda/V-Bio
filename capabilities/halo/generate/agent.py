"""REINVENT-style RL agent update (augmented likelihood + experience replay)."""

from __future__ import annotations

import random

import torch
import torch.nn.functional as F

from halo.generate.prior import SmilesTransformer
from halo.generate.vocab import PAD, SmilesVocab


class ReplayBuffer:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.items: list[tuple[str, float]] = []

    def add(self, pairs: list[tuple[str, float]]) -> None:
        self.items.extend(pairs)
        if len(self.items) > self.capacity:
            self.items = self.items[-self.capacity :]

    def sample(self, n: int, rng: random.Random) -> list[tuple[str, float]]:
        k = min(n, len(self.items))
        return rng.sample(self.items, k)


class AgentUpdater:
    """Updates the agent toward the REINVENT augmented likelihood.

    augmented log-prob target:  A(s) = alpha * logPrior(s) + (1-alpha) * sigma * (R(s) - mean_R)
    loss: MSE( logAgent(s) - A(s) )  (Olivecrona et al. 2017 / REINVENT),
    plus a soft KL(anchor) penalty toward the prior for stability.
    """

    def __init__(self, agent: SmilesTransformer, prior: SmilesTransformer, vocab: SmilesVocab, device="cuda"):
        self.agent = agent
        self.prior = prior
        self.vocab = vocab
        self.device = device
        self.prior.to(device).eval()
        for p in self.prior.parameters():
            p.requires_grad_(False)
        self.opt = torch.optim.Adam(self.agent.parameters(), lr=1e-4)

    def _encode(self, smiles_list: list[str], max_len: int = 160) -> torch.Tensor | None:
        enc = [self.vocab.encode(s)[:max_len] for s in smiles_list]
        enc = [e for e in enc if len(e) >= 2]
        if not enc:
            return None
        L = max(len(e) for e in enc)
        x = torch.full((len(enc), L), self.vocab.stoi[PAD], dtype=torch.long)
        for i, e in enumerate(enc):
            x[i, : len(e)] = torch.tensor(e)
        return x.to(self.device)

    def update(
        self,
        samples: list[tuple[str, float]],
        *,
        alpha: float,
        sigma: float,
        kl_beta: float = 0.05,
        epochs: int = 4,
        batch_size: int = 256,
        replay: ReplayBuffer | None = None,
        replay_fraction: float = 0.25,
        log=None,
    ) -> dict:
        """samples: (smiles, reward) pairs; rewards in [0, 1]."""
        rng = random.Random()
        data = list(samples)
        if replay is not None and len(replay.items) > 0 and replay_fraction > 0:
            extra = replay.sample(int(len(data) * replay_fraction), rng)
            data = data + extra
        if not data:
            return {"loss": float("nan"), "n": 0}
        rewards = torch.tensor([r for _, r in data], dtype=torch.float32, device=self.device)
        mean_r = rewards.mean()
        A = alpha * 0.0  # prior term added per-batch below
        losses = []
        self.agent.train()
        for _ in range(epochs):
            rng.shuffle(data)
            for i in range(0, len(data), batch_size):
                chunk = data[i : i + batch_size]
                if not chunk:
                    continue
                x = self._encode([s for s, _ in chunk])
                if x is None or x.size(0) < 2:
                    continue
                r = torch.tensor([rw for _, rw in chunk], dtype=torch.float32, device=self.device)
                with torch.no_grad():
                    lp_prior = self.prior.log_probs(x)
                lp_agent = self.agent.log_probs(x)
                aug = (1 - alpha) * sigma * (r - r.mean())
                target = alpha * lp_prior + aug
                mse = F.mse_loss(lp_agent, target)
                kl = (lp_agent - lp_prior).mean() ** 2  # signed drift penalty
                loss = mse + kl_beta * kl
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 5.0)
                self.opt.step()
                losses.append(float(loss))
        return {"loss": sum(losses) / max(len(losses), 1), "n": len(data)}
