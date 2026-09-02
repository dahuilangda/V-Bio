"""Direct Preference Optimization (DPO) for the molecular LLM.

Standard RLHF recipe adapted to SMILES: human pairwise preferences
(y_w preferred over y_l) directly fine-tune the generative agent against a
frozen reference (the prior) with

    L = -log sigmoid( beta * [ (log pi_a(y_w) - log pi_ref(y_w))
                             - (log pi_a(y_l) - log pi_ref(y_l)) ] )

which replaces the older "preference -> matched-pair rule" carrier with a
pure large-model alignment signal (Rafailov et al. 2023; the standard
post-RLHF alignment step applied here to molecular generation).
"""

from __future__ import annotations

import torch
import torch.nn.functional as F


class DPOUpdater:
    def __init__(self, agent, prior, vocab, device="cuda", beta=1.0, lr=5e-5):
        self.agent = agent
        self.prior = prior
        self.vocab = vocab
        self.device = device
        self.beta = beta
        self.opt = torch.optim.Adam(agent.parameters(), lr=lr)
        self.prior.eval()

    def _encode(self, smiles_list):
        enc = [self.vocab.encode(s)[:192] for s in smiles_list]
        enc = [e for e in enc if len(e) >= 3]
        if not enc:
            return None
        L = max(len(e) for e in enc)
        x = torch.full((len(enc), L), self.vocab.stoi["<pad>"], dtype=torch.long)
        for i, e in enumerate(enc):
            x[i, : len(e)] = torch.tensor(e)
        return x.to(self.device)

    def update(self, pairs: list[tuple[str, str]], epochs: int = 2, log=None) -> dict:
        """pairs: (preferred, dispreferred) SMILES."""
        pairs = [(w, l) for w, l in pairs if isinstance(w, str) and isinstance(l, str)]
        if not pairs:
            return {"n": 0}
        stats = {"acc": [], "loss": []}
        for _ in range(epochs):
            # batch all pairs (each row: [w, l])
            W = self._encode([w for w, _ in pairs])
            L_ = self._encode([l for _, l in pairs])
            if W is None or L_ is None or W.shape[0] < 1 or W.shape != L_.shape:
                # length mismatch from filtering: fall back to per-pair update
                for w, l in pairs:
                    lw = self._encode([w])
                    ll = self._encode([l])
                    if lw is None or ll is None:
                        continue
                    self._step(lw, ll, stats)
                continue
            self._step_batch(W, L_, stats)
        out = {"n": len(pairs),
               "loss": sum(stats["loss"]) / max(len(stats["loss"]), 1),
               "implicit_acc": sum(stats["acc"]) / max(len(stats["acc"]), 1)}
        if log and out["n"]:
            log(f"[dpo] n={out['n']} loss={out['loss']:.3f} acc={out['implicit_acc']:.2f}")
        return out

    def _dpo_loss(self, lw, ll):
        # per-token normalized log-probs: sequence sums would scale the DPO
        # logits with length and saturate the sigmoid
        def norm_lp(model, x):
            lp = model.log_probs(x)
            ntok = (~x[:, 1:].eq(self.vocab.stoi["<pad>"])).float().sum(-1).clamp_min(1)
            return lp / ntok

        with torch.no_grad():
            ref_w, ref_l = norm_lp(self.prior, lw), norm_lp(self.prior, ll)
        pol_w, pol_l = norm_lp(self.agent, lw), norm_lp(self.agent, ll)
        logits = self.beta * ((pol_w - ref_w) - (pol_l - ref_l))
        loss = -F.logsigmoid(logits).mean()
        with torch.no_grad():
            acc = (logits > 0).float().mean()
        return loss, float(acc)

    def _step_batch(self, W, L_, stats):
        loss, acc = self._dpo_loss(W, L_)
        self.opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1.0)
        self.opt.step()
        stats["loss"].append(float(loss.detach()))
        stats["acc"].append(acc)

    def _step(self, lw, ll, stats):
        self._step_batch(lw, ll, stats)
