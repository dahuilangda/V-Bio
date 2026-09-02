"""Group-relative policy optimization for the molecular agent.

Advantages are normalized within groups that share an edit context (same
parent molecule, or same Murcko scaffold for unconditional samples) and a
reward source, so the baseline tracks the local comparison the oracle
actually supports. Old log-probs are frozen once per update, which keeps the
PPO clip a real trust region across epochs. The KL-to-prior term uses the
quadratic bound and stays in the loss: the reward comes from a learned
surrogate (Boltz-2), so the prior anchor guards against reward hacking.
Truncated importance sampling keeps rare-but-good completions from losing
their gradient.
"""

from __future__ import annotations

import random

import torch
import torch.nn.functional as F


class GRPOUpdater:
    def __init__(self, agent, prior, vocab, device="cuda", lr=3e-5,
                 clip_eps=0.2, kl_beta=0.01, group_size=8,
                 ent_coef: float = 0.003,
                 tis_cap: float = 2.0,
                 use_replay: bool = True, replay_capacity: int = 512):
        """tis_cap caps the importance ratio for positive-advantage tokens so
        low-probability (novel) completions keep their gradient instead of
        being starved by vanishing ratios."""
        from halo.generate.rl_components import PrioritizedReplay

        self.replay = PrioritizedReplay(capacity=replay_capacity) if use_replay else None
        self.agent = agent
        self.prior = prior
        self.vocab = vocab
        self.device = device
        self.clip_eps = clip_eps
        self.kl_beta = kl_beta
        self.ent_coef = ent_coef
        self.tis_cap = tis_cap
        self.group_size = group_size
        self.opt = torch.optim.Adam(agent.parameters(), lr=lr)
        self.prior.eval()

    def _encode(self, texts, max_len=256):
        enc = [self.vocab.encode(t)[:max_len] for t in texts]
        enc = [e for e in enc if len(e) >= 3]
        return enc

    def _group_advantage(self, keys, rewards, sources):
        """(r - mu)/max(sd, eps) within (context, reward-source) groups."""
        groups: dict[tuple, list[int]] = {}
        for i, (k, src) in enumerate(zip(keys, sources)):
            groups.setdefault((k, src), []).append(i)
        adv = [0.0] * len(rewards)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            rs = [rewards[i] for i in idxs]
            mu = sum(rs) / len(rs)
            sd = (sum((r - mu) ** 2 for r in rs) / len(rs)) ** 0.5
            if sd < 1e-6:
                continue
            for i in idxs:
                adv[i] = (rewards[i] - mu) / max(sd, 0.1)
        return adv

    @staticmethod
    def _fallback_group(smiles: str) -> str:
        from rdkit import RDLogger
        from rdkit.Chem.Scaffolds import MurckoScaffold

        RDLogger.DisableLog("rdApp.*")
        try:
            return "scaf:" + MurckoScaffold.MurckoScaffoldSmiles(smiles=smiles)
        except Exception:
            return "solo:" + smiles

    def update(self, samples, epochs: int = 2, batch_size: int = 64,
               log=None) -> dict:
        """samples: (text, reward[, group_key[, source[, prompt_ids_len]]]).

        text is SAFE text. prompt_ids_len marks a conditioned trajectory
        (kept-prefix + generation); tokens before it are prompt, not policy
        actions, and are masked out of the policy-gradient/KL/entropy terms -
        this is what lets RL improve the edit operators themselves."""
        norm = []
        for s in samples:
            if not isinstance(s, (tuple, list)) or len(s) < 2:
                continue
            text, r = s[0], float(s[1])
            if not isinstance(text, str) or not text:
                continue
            key = s[2] if len(s) > 2 and s[2] else self._fallback_group(text)
            src = s[3] if len(s) > 3 and s[3] else "mix"
            plen = int(s[4]) if len(s) > 4 and s[4] else 0
            norm.append((text, r, str(key), str(src), plen))
        if self.replay is not None and norm:
            extra = self.replay.sample(min(len(norm) // 4, 64), random.Random(0))
            extra = [(t, r, self._fallback_group(t), "replay", 0) for t, r in extra]
            norm = norm + extra
            # conditioned trajectories must stay paired with their plen
            self.replay.add([(t, r) for t, r, _, _, p in norm if p == 0])
        if len(norm) < 4:
            return {"n": 0}

        texts = [t for t, *_ in norm]
        rewards = [r for _, r, *_ in norm]
        keys = [k for _, _, k, _, _ in norm]
        sources = [s for _, _, _, s, _ in norm]
        plens = [p for _, _, _, _, p in norm]
        enc = []
        keep = []
        for i, t in enumerate(texts):
            e = self.vocab.encode(t)[:256]
            if len(e) >= 3:
                enc.append(e)
                keep.append(i)
        if len(enc) < 4:
            return {"n": 0}
        rewards = [rewards[i] for i in keep]
        keys = [keys[i] for i in keep]
        sources = [sources[i] for i in keep]
        plens = [min(plens[i], len(e) - 2) for i, e in zip(keep, enc)]
        advs = self._group_advantage(keys, rewards, sources)

        pad = self.vocab.stoi["<pad>"]
        x_all = torch.full((len(enc), max(len(e) for e in enc)), pad, dtype=torch.long)
        for r_i, e in enumerate(enc):
            x_all[r_i, : len(e)] = torch.tensor(e)
        x_all = x_all.to(self.device)
        tok_mask_all = (~x_all[:, 1:].eq(pad)).float()
        # prompt masking: column j carries the log-prob of token j+1; the
        # prompt occupies tokens 1..plen, so actions start at column plen
        for r_i, plen in enumerate(plens):
            if plen > 0:
                tok_mask_all[r_i, :plen] = 0.0

        # frozen old log-probs + prior log-probs, once per update
        self.agent.eval()
        with torch.no_grad():
            old_lp = self.agent._token_logprobs(x_all)
            ref_lp = self.prior._token_logprobs(x_all)
        adv_t = torch.tensor(advs, dtype=torch.float32, device=self.device)

        stats = {"loss": [], "kl": [], "clip": [], "ent": []}
        rng = random.Random(0)
        order = list(range(len(enc)))
        self.agent.train()
        for _ in range(max(1, epochs)):
            rng.shuffle(order)
            for b0 in range(0, len(order), batch_size):
                sel = order[b0 : b0 + batch_size]
                if len(sel) < 4:
                    continue
                x = x_all[sel]
                m = tok_mask_all[sel]
                o_lp, r_lp = old_lp[sel], ref_lp[sel]
                new_logits = self.agent.gpt(x[:, :-1]).logits
                lp = F.log_softmax(new_logits.float(), dim=-1)
                tgt = x[:, 1:]
                new_lp = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
                ratio = torch.exp((new_lp - o_lp).clamp(-8.0, 8.0))
                adv = adv_t[sel].unsqueeze(1).expand_as(ratio)
                # cap the ratio for positive advantages: rare-but-good
                # completions keep their gradient; bad ones are still pushed down
                pos = adv > 0
                ratio_tis = torch.where(pos, torch.clamp(ratio, max=self.tis_cap), ratio)
                unclipped = ratio_tis * adv
                clipped = torch.clamp(ratio_tis, 1 - self.clip_eps, 1 + self.clip_eps) * adv
                denom = m.sum().clamp_min(1)
                pg_loss = -(torch.min(unclipped, clipped) * m).sum() / denom
                # quadratic KL bound to the prior (stable, always >= 0)
                delta = (r_lp - new_lp).clamp(-6.0, 6.0)
                kl = (0.5 * delta * delta * m).sum() / denom
                # token entropy bonus (anti-collapse)
                ent = -(lp.exp() * lp).sum(-1)
                ent_b = (ent * m).sum() / denom
                loss = pg_loss + self.kl_beta * kl - self.ent_coef * ent_b
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1.0)
                self.opt.step()
                with torch.no_grad():
                    stats["loss"].append(float(pg_loss))
                    stats["kl"].append(float(kl))
                    stats["clip"].append(float((((ratio - 1).abs() > self.clip_eps).float() * m).sum() / denom))
                    stats["ent"].append(float(ent_b))
        n = len(enc)
        out = {"n": n,
               "pg_loss": sum(stats["loss"]) / max(len(stats["loss"]), 1),
               "kl_to_prior": sum(stats["kl"]) / max(len(stats["kl"]), 1),
               "frac_clipped": sum(stats["clip"]) / max(len(stats["clip"]), 1),
               "entropy": sum(stats["ent"]) / max(len(stats["ent"]), 1)}
        if log and out["n"]:
            log(f"[grpo] n={out['n']} pg={out['pg_loss']:.4f} kl={out['kl_to_prior']:+.2f} "
                f"clip={out['frac_clipped']:.2f} ent={out['entropy']:.2f}")
        return out
