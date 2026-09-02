"""GRPO for the peptide agent (HALO GRPOUpdater, residue-token adaptation).

Advantages normalized within groups sharing a proposal context (edit parent /
length bucket for de novo) and reward source; frozen old log-probs per update
keep the PPO clip a real trust region; quadratic KL to the Tier-1 prior guards
against surrogate/oracle reward hacking; truncated importance sampling keeps
rare-but-good completions (novel NCAAs!) from losing their gradient.
Conditioned trajectories carry a prompt length so prompt tokens are masked out
of the policy-gradient — RL then improves the edit operator itself.
"""

from __future__ import annotations

import random

import torch
import torch.nn.functional as F

from peplm.vocab import Vocab


class GRPOUpdater:
    def __init__(self, agent, prior, vocab: Vocab, device="cuda", lr=3e-5,
                 clip_eps: float = 0.2, kl_beta: float = 0.02,
                 ent_coef: float = 0.003, tis_cap: float = 2.0,
                 max_len: int = 96):
        self.agent = agent
        self.prior = prior
        self.vocab = vocab
        self.device = device
        self.clip_eps = clip_eps
        self.kl_beta = kl_beta
        self.ent_coef = ent_coef
        self.tis_cap = tis_cap
        self.max_len = max_len
        self.opt = torch.optim.Adam(agent.parameters(), lr=lr)
        self.prior.eval()

    def _group_advantage(self, keys, rewards, sources):
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

    def update(self, samples, epochs: int = 2, batch_size: int = 64,
               log=None) -> dict:
        """samples: (tokens, reward, group_key, source, prompt_len).
        tokens is the full token list (dev tag + structure + residues)."""
        norm = []
        for s in samples:
            if not isinstance(s, (tuple, list)) or len(s) < 2:
                continue
            toks, r = s[0], float(s[1])
            if not toks:
                continue
            key = s[2] if len(s) > 2 and s[2] else "solo"
            src = s[3] if len(s) > 3 and s[3] else "mix"
            plen = int(s[4]) if len(s) > 4 and s[4] else 0
            norm.append((list(toks), r, str(key), str(src), plen))
        if len(norm) < 4:
            return {"n": 0}

        pad = self.vocab.pad
        eos = self.vocab.eos
        bos = self.vocab.bos
        enc = [[bos] + self.vocab.encode_tokens(t)[: self.max_len - 2] + [eos]
               for t, *_ in norm]
        enc = [e for e in enc if len(e) >= 3]
        keep = [i for i, e in enumerate(enc) if len(e) >= 3][: len(enc)]
        if len(enc) < 4:
            return {"n": 0}
        rewards = [norm[i][1] for i in keep]
        keys = [norm[i][2] for i in keep]
        sources = [norm[i][3] for i in keep]
        plens = [min(norm[i][4], len(e) - 2) for i, e in zip(keep, enc)]
        advs = self._group_advantage(keys, rewards, sources)

        x_all = torch.full((len(enc), max(len(e) for e in enc)), pad, dtype=torch.long)
        for r_i, e in enumerate(enc):
            x_all[r_i, : len(e)] = torch.tensor(e)
        x_all = x_all.to(self.device)
        tok_mask = (~x_all[:, 1:].eq(pad)).float()
        for r_i, plen in enumerate(plens):
            if plen > 0:
                tok_mask[r_i, :plen] = 0.0

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
                sel = order[b0: b0 + batch_size]
                if len(sel) < 4:
                    continue
                x = x_all[sel]
                m = tok_mask[sel]
                o_lp, r_lp = old_lp[sel], ref_lp[sel]
                logits = self.agent.gpt(x[:, :-1]).logits
                lp = F.log_softmax(logits.float(), dim=-1)
                tgt = x[:, 1:]
                new_lp = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
                ratio = torch.exp((new_lp - o_lp).clamp(-8.0, 8.0))
                adv = adv_t[sel].unsqueeze(1).expand_as(ratio)
                pos = adv > 0
                ratio_tis = torch.where(pos, torch.clamp(ratio, max=self.tis_cap), ratio)
                unclipped = ratio_tis * adv
                clipped = torch.clamp(ratio_tis, 1 - self.clip_eps,
                                      1 + self.clip_eps) * adv
                denom = m.sum().clamp_min(1)
                pg_loss = -(torch.min(unclipped, clipped) * m).sum() / denom
                delta = (r_lp - new_lp).clamp(-6.0, 6.0)
                kl = (0.5 * delta * delta * m).sum() / denom
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
                    stats["clip"].append(float((((ratio - 1).abs() > self.clip_eps)
                                                .float() * m).sum() / denom))
                    stats["ent"].append(float(ent_b))
        out = {"n": len(enc),
               "pg_loss": sum(stats["loss"]) / max(len(stats["loss"]), 1),
               "kl_to_prior": sum(stats["kl"]) / max(len(stats["kl"]), 1),
               "frac_clipped": sum(stats["clip"]) / max(len(stats["clip"]), 1),
               "entropy": sum(stats["ent"]) / max(len(stats["ent"]), 1)}
        if log and out["n"]:
            log(f"[grpo] n={out['n']} pg={out['pg_loss']:.4f} "
                f"kl={out['kl_to_prior']:+.2f} clip={out['frac_clipped']:.2f} "
                f"ent={out['entropy']:.2f}")
        return out
