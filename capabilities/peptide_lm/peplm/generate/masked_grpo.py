"""Per-step GRPO for masked-LM policies: per-step ratios on the recorded
trajectory, Dr.GRPO mean-centered advantages with constant loss
normalization, k3 KL to the frozen adapter-disabled base, per-dimension
group advantages, frozen old log-probs across inner updates."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import torch
import torch.nn.functional as F


@dataclass
class MaskedGRPOConfig:
    lr: float = 5e-6
    clip_low: float = 0.2            # Dr.GRPO/DAPO asymmetric clip
    clip_high: float = 0.28
    beta_kl: float = 0.04
    kl_target: float = 0.02          # nats/token deadband centre
    kl_bounds: tuple = (0.004, 0.4)
    entropy_coef: float = 0.003
    max_pep_len: int = 25            # CONSTANT loss normalizer (Dr.GRPO)
    grad_clip: float = 1.0
    inner_updates: int = 4           # μ, with frozen old log-probs
    micro_batch: int = 4             # rollouts per optimizer step
    event_chunk: int = 8             # backward fires per chunk so the
    # graph never spans more than one chunk (full micro-batch graphs OOM
    # a 24 GB card)


@dataclass
class Rollout:
    """One scored trajectory for the updater."""
    states: list = field(default_factory=list)      # [T] x [L] cpu tensors
    revealed: list = field(default_factory=list)    # [(pos, token)]
    old_logprob: list = field(default_factory=list) # [T]
    temps: list = field(default_factory=list)       # [T] sampling temps
    rewards: tuple = (0.0,)                         # (binding, conf, geo)
    group: str = ""
    # per-position credit in [0, 1]: scales each token's share of the
    # advantage toward the positions the oracle blamed; empty = uniform
    credit: list = field(default_factory=list)
    pep_start: int = 0
    # ss8 conditioning recorded at sampling time: replay must forward
    # the same track, or new/old logprobs come from different policies.
    # [L] cpu tensor of ss8 token ids, None when sampling was unconditional.
    ss8: object = None

    def __len__(self):
        return len(self.revealed)

    def token_weight(self, pos: int) -> float:
        """Credit multiplier for the token revealed at absolute ``pos``."""
        if not len(self.credit):
            return 1.0
        j = pos - self.pep_start
        if j < 0 or j >= len(self.credit):
            raise ValueError(
                f"revealed position {pos} outside the peptide span "
                f"[{self.pep_start}, {self.pep_start + len(self.credit)}) "
                "-- credit/pep_start misalignment")
        return float(self.credit[j])


class MaskedGRPOUpdater:
    def __init__(self, policy, config: Optional[MaskedGRPOConfig] = None):
        self.policy = policy
        self.cfg = config or MaskedGRPOConfig()
        self.opt = torch.optim.AdamW(
            policy.adapter_parameters(),
            lr=self.cfg.lr, betas=(0.9, 0.99), weight_decay=0.1)
        self._beta = self.cfg.beta_kl
        self.stats: dict = {}

    # utils
    def _group_advantage(self, rewards: list[float]) -> list[float]:
        """Dr.GRPO: mean-center only. All-equal rewards carry no signal."""
        m = sum(rewards) / len(rewards)
        return [r - m for r in rewards]

    def _adjust_beta(self, mean_kl: float):
        """Deadband controller: x2 above 2x target, x0.5 below half."""
        if mean_kl > 2 * self.cfg.kl_target:
            self._beta = min(self._beta * 2.0, self.cfg.kl_bounds[1])
        elif mean_kl < 0.5 * self.cfg.kl_target:
            self._beta = max(self._beta * 0.5, self.cfg.kl_bounds[0])

    # step
    def update(self, rollouts: list[Rollout], log=print) -> dict:
        """One GRPO update over a generation's scored rollouts.

        Groups split by ``Rollout.group``; each reward dimension gets its
        own group-relative advantage, averaged per rollout. Backward fires
        per event chunk, so peak memory is one chunk's graph.
        """
        live = [r for r in rollouts if len(r.revealed) > 0
                and len(r.rewards) >= 2]
        if len(live) < 4:
            return {"skipped": "fewer than 4 scored rollouts "
                    f"(rewards with <2 dimensions carry no signal)"}

        groups: dict[str, list[int]] = {}
        for i, r in enumerate(live):
            groups.setdefault(r.group, []).append(i)
        n_dim = len(live[0].rewards)
        adv = [0.0] * len(live)
        for d in range(n_dim):
            for idxs in groups.values():
                r_d = [live[i].rewards[d] for i in idxs]
                if all(abs(x - r_d[0]) < 1e-9 for x in r_d):
                    continue  # degenerate dimension carries no signal
                for i, a in zip(idxs, self._group_advantage(r_d)):
                    adv[i] += a / n_dim

        stats = {"loss": 0.0, "kl": 0.0, "entropy": 0.0,
                 "clip_frac": 0.0, "beta": self._beta,
                 "groups": len(groups), "rollouts": len(live)}
        self.policy.model.train()
        # Dr.GRPO constant normalizer, per inner update
        norm = self.cfg.max_pep_len * max(1, self.cfg.micro_batch)
        n_backward = 0
        for _ in range(self.cfg.inner_updates):
            perm = torch.randperm(len(live)).tolist()
            for b0 in range(0, len(live), self.cfg.micro_batch):
                chunk = [live[i] for i in perm[b0:b0 + self.cfg.micro_batch]]
                chunk_adv = [adv[i] for i in perm[b0:b0 + self.cfg.micro_batch]]
                events = self._flatten_events(chunk, chunk_adv)
                if not events:
                    continue
                for ev_chunk in self._chunks(events, self.cfg.event_chunk):
                    loss, kl_sum, ent_sum, clipped, toks = self._chunk_loss(
                        ev_chunk)
                    (loss / norm).backward()   # graph freed per chunk
                    torch.nn.utils.clip_grad_norm_(
                        self.policy.adapter_parameters(), self.cfg.grad_clip)
                    stats["loss"] += float(loss) / norm
                    stats["kl"] += kl_sum / toks
                    stats["entropy"] += ent_sum / toks
                    stats["clip_frac"] += clipped / toks
                    n_backward += 1
                self.opt.step()
                self.opt.zero_grad(set_to_none=True)
        self.policy.model.eval()
        if n_backward:
            for k in ("loss", "kl", "entropy", "clip_frac"):
                stats[k] /= n_backward
        if n_backward:
            # adjust the KL leash only on rounds that produced gradient:
            # signal-free rounds report kl=0 and would loosen the leash on
            # a collapsed policy
            self._adjust_beta(stats["kl"])
        stats["beta"] = self._beta
        self.stats = stats
        log(f"[masked-grpo] {stats}")
        return stats

    @staticmethod
    def _flatten_events(chunk, advs):
        """Per-token events with credit-weighted advantage. Credit is
        blame, so the weight direction depends on the advantage sign: a
        positive advantage reinforces the positions that worked
        (1 - blame), a negative one penalises the blamed positions.
        Weights are renormalised to mean 1 per rollout."""
        events = []
        for r, a in zip(chunk, advs):
            if abs(a) < 1e-9:
                continue
            temps = r.temps or [1.0] * len(r.revealed)
            raw = [r.token_weight(pos) for pos, _ in r.revealed]
            weights = [w if a < 0 else (1.0 - w) for w in raw]
            mean_w = sum(weights) / len(weights) if weights else 0.0
            weights = ([w / mean_w for w in weights] if mean_w > 1e-6
                       else [1.0] * len(weights))
            for state, (pos, tok), old_lp, t_s, w in zip(
                    r.states, r.revealed, r.old_logprob, temps, weights):
                events.append((state, pos, tok, old_lp, t_s, a * w, r.ss8))
        return events

    @staticmethod
    def _chunks(events, size):
        for i in range(0, len(events), size):
            yield events[i:i + size]

    # per-chunk
    def _chunk_loss(self, events: list):
        """PPO-clip surrogate + k3 KL + entropy over ONE event chunk,
        batched into a single policy forward and a single
        adapter-disabled reference forward.
        Returns (loss, kl_sum, entropy_sum, clip_count, token_count).
        """
        pol = self.policy.model
        device = self.policy.device
        batch = torch.stack([ev[0].to(device) for ev in events])  # [E, L]
        ss8_src = events[0][6]
        ss8_batch = (torch.stack([ev[6].to(device) for ev in events])
                     if ss8_src is not None else None)
        pos_t = torch.tensor([ev[1] for ev in events], device=device)
        tok_t = torch.tensor([ev[2] for ev in events], device=device)
        old_t = torch.tensor([ev[3] for ev in events], device=device,
                             dtype=torch.float32)
        temp_t = torch.tensor([ev[4] for ev in events], device=device,
                              dtype=torch.float32).clamp_min(1e-3)
        adv_t = torch.tensor([ev[5] for ev in events], device=device,
                             dtype=torch.float32)

        aa = torch.tensor(self.policy.aa_ids, device=device)

        def _tempered_gather(logits):
            """logits [E, L, V] -> per-event tempered logprob at
            (pos, token) plus the tempered AA-restricted distribution."""
            raw = logits.gather(
                1, pos_t.view(-1, 1, 1).expand(-1, 1, logits.shape[-1])
            ).squeeze(1)                                  # [E, V]
            keep = torch.full_like(raw, float("-inf"))
            keep[:, aa] = raw[:, aa]
            tempered = keep / temp_t.view(-1, 1)
            lp = F.log_softmax(tempered, dim=-1)
            return lp.gather(1, tok_t.view(-1, 1)).squeeze(1), lp

        with torch.autocast("cuda", dtype=self.policy.dtype):
            out = pol(sequence_tokens=batch, ss8_tokens=ss8_batch)
        new_lp, ev_logp = _tempered_gather(out.sequence_logits.float())

        rho = torch.exp((new_lp - old_t).clamp(-8.0, 8.0))
        lo, hi = 1 - self.cfg.clip_low, 1 + self.cfg.clip_high
        surr = torch.minimum(rho * adv_t, rho.clamp(lo, hi) * adv_t)
        clipped = int(((rho < lo) | (rho > hi)).sum().item())

        kl_term = torch.zeros_like(surr)
        kl_sum = 0.0
        if self._beta > 0:
            with pol.disable_adapter(), torch.no_grad(), \
                    torch.autocast("cuda", dtype=self.policy.dtype):
                ref_out = pol(sequence_tokens=batch, ss8_tokens=ss8_batch)
            _, ref_full = _tempered_gather(ref_out.sequence_logits.float())
            ref_lp = ref_full.gather(1, tok_t.view(-1, 1)).squeeze(1)
            d = ref_lp - new_lp
            kl_term = torch.exp(d) - d - 1.0     # k3; grad flows via new_lp
            kl_sum = float(kl_term.sum().item())

        # entropy over the AA-restricted support only: masked entries are
        # -inf and exp(-inf)*(-inf) = NaN
        lp_aa = ev_logp[:, aa]
        entropy = -(lp_aa.exp() * lp_aa).sum(dim=-1)      # [E], tempered
        ent_sum = float(entropy.sum().item())

        loss = (-surr.sum() + self._beta * kl_term.sum()
                - self.cfg.entropy_coef * entropy.sum())
        return loss, kl_sum, ent_sum, clipped, len(events)
