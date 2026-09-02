"""SMILES generative model: small transformer prior + REINVENT-style RL agent.

The prior p(s) is pretrained on drug-like SMILES (ChEMBL subset + benchmark
ligands). The agent pi(s) starts as a copy and is fine-tuned each loop round
with the REINVENT augmented-likelihood objective so that it drifts toward
high-reward chemistry while staying anchored to the prior.
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from halo.generate.vocab import BOS, EOS, PAD, SmilesVocab


class SmilesTransformer(nn.Module):
    def __init__(self, vocab: SmilesVocab, d_model=256, n_layers=4, n_heads=8, dropout=0.1, max_len=160):
        super().__init__()
        self.vocab = vocab
        n_vocab = len(vocab)
        self.pad = vocab.stoi[PAD]
        self.embed = nn.Embedding(n_vocab, d_model, padding_idx=self.pad)
        self.pos = nn.Embedding(max_len, d_model)
        self.drop = nn.Dropout(dropout)
        layer = nn.TransformerEncoderLayer(
            d_model, n_heads, dim_feedforward=4 * d_model, dropout=dropout,
            batch_first=True, norm_first=True, activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(layer, n_layers)
        self.head = nn.Linear(d_model, n_vocab)
        self.max_len = max_len

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T) int64 tokens
        T = x.size(1)
        pos = torch.arange(T, device=x.device).unsqueeze(0)
        h = self.drop(self.embed(x) + self.pos(pos))
        mask = nn.Transformer.generate_square_subsequent_mask(T, device=x.device)
        pad_mask = x.eq(self.pad)
        h = self.encoder(h, mask=mask, src_key_padding_mask=pad_mask, is_causal=True)
        return self.head(h)

    def log_probs(self, x: torch.Tensor) -> torch.Tensor:
        """Sequence log-probabilities sum over tokens (input shifted target)."""
        logits = self.forward(x[:, :-1])
        tgt = x[:, 1:]
        lp = F.log_softmax(logits, dim=-1)
        tok_lp = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)  # (B, T-1)
        pad_mask = ~tgt.eq(self.pad)
        return (tok_lp * pad_mask).sum(-1)

    @torch.no_grad()
    def sample(self, n: int, device, temperature=1.0, top_p=0.95, max_len=None) -> list[str]:
        return self.sample_with_prompt([], n, device, temperature=temperature,
                                       top_p=top_p, max_len=max_len)

    @torch.no_grad()
    def sample_with_prompt(self, prompt_tokens: list[str], n: int, device,
                           temperature=1.0, top_p=0.95, max_len=None) -> list[str]:
        """Continuation after a token prefix (scaffold conditioning)."""
        max_len = max_len or self.max_len
        was_training = self.training
        self.eval()
        stoi = self.vocab.stoi
        prompt_ids = [stoi[t] for t in prompt_tokens if t in stoi]
        base = [stoi[BOS]] + prompt_ids
        x = torch.tensor([base] * n, dtype=torch.long, device=device)
        finished = torch.zeros(n, dtype=torch.bool, device=device)
        for _ in range(max(4, max_len - len(prompt_ids))):
            logits = self.forward(x)[:, -1] / max(temperature, 1e-4)
            sorted_lp, sorted_idx = torch.sort(F.log_softmax(logits, -1), descending=True)
            cum = torch.exp(sorted_lp).cumsum(-1)
            keep = cum - torch.exp(sorted_lp) <= top_p
            keep[..., 0] = True
            probs = torch.full_like(logits, 0.0)
            gathered = keep.float() * torch.exp(sorted_lp)
            probs.scatter_(-1, sorted_idx, gathered)
            probs = probs / probs.sum(-1, keepdim=True).clamp_min(1e-12)
            nxt = torch.multinomial(probs, 1)
            nxt[finished] = self.pad
            x = torch.cat([x, nxt], dim=1)
            finished |= nxt.squeeze(1).eq(stoi[EOS])
            if bool(finished.all()):
                break
        if was_training:
            self.train()
        out = []
        for row in x:
            ids = []
            for i in row.tolist()[1:]:
                if i == stoi[EOS]:
                    break
                if i not in (self.pad, stoi[BOS]):
                    ids.append(i)
            out.append(self.vocab.decode(ids))
        return out



def pretrain_prior(
    model: SmilesTransformer,
    smiles: list[str],
    vocab: SmilesVocab,
    *,
    epochs=20,
    batch_size=256,
    lr=3e-4,
    val_fraction=0.05,
    device="cuda",
    log=print,
    max_len=160,
) -> dict:
    """Teacher-forced MLE with early stopping on validation perplexity."""
    import random

    rng = random.Random(0)
    smiles = list(smiles)
    rng.shuffle(smiles)
    n_val = max(8, int(len(smiles) * val_fraction))
    val, train = smiles[:n_val], smiles[n_val:]

    def make_batch(items):
        enc = [vocab.encode(s)[:max_len] for s in items]
        L = max(len(e) for e in enc)
        x = torch.full((len(enc), L), vocab.stoi[PAD], dtype=torch.long)
        for i, e in enumerate(enc):
            x[i, : len(e)] = torch.tensor(e)
        return x

    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-2)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))
    best_val, best_state, patience, bad = math.inf, None, 10, 0
    history = []
    for epoch in range(epochs):
        model.train()
        rng.shuffle(train)
        tot, nb = 0.0, 0
        for i in range(0, len(train), batch_size):
            batch = train[i : i + batch_size]
            if not batch:
                continue
            x = make_batch(batch).to(device)
            logits = model(x[:, :-1])
            loss = F.cross_entropy(
                logits.reshape(-1, logits.size(-1)), x[:, 1:].reshape(-1), ignore_index=vocab.stoi[PAD]
            )
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            tot += float(loss.detach())
            nb += 1
        sched.step()
        model.eval()
        with torch.no_grad():
            vx = make_batch(val).to(device)
            vlogits = model(vx[:, :-1])
            vloss = float(
                F.cross_entropy(vlogits.reshape(-1, vlogits.size(-1)), vx[:, 1:].reshape(-1), ignore_index=vocab.stoi[PAD])
            )
        history.append({"epoch": epoch, "train_loss": tot / max(nb, 1), "val_loss": vloss})
        if log and epoch % 2 == 0:
            log(f"[prior] epoch {epoch:3d} train {tot/max(nb,1):.4f} val {vloss:.4f}")
        if vloss < best_val - 1e-4:
            best_val, bad = vloss, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    return {"best_val_loss": best_val, "history": history}
