"""Training loop for the modern (Llama-style) prior.

Differences vs the GPT-2 loop:
  * auxiliary property regression loss (sol/syn/liab) on mean-pooled states
    — multi-task shaping of the representation
  * modality augmentation at load time: a share of plain lines is re-labelled
    <cyc> (head-to-tail) or <bicy> (3-Cys layout) so one prior serves
    linear / cyclic / bicyclic design through the structure token
"""

from __future__ import annotations

import math
import random

import torch
import torch.nn.functional as F

from peplm.models.gpt2 import encode_line
from peplm.models.llama_prior import ModernPrior
from peplm.props.descriptors import compute_props
from peplm.vocab import Vocab, parse_tokens


def modality_augment(train: list[str], rng: random.Random,
                     p_cyc: float = 0.08, p_bicy: float = 0.08) -> list[str]:
    """Re-label structure tokens so the prior learns all three modalities.
    FIM lines are only re-labelled cyclic (their span layout cannot be
    re-anchored post-hoc); bicyclic lines get the first_last Cys layout."""
    from peplm.oracle.peptide_boltz import enforce_bicyclic_cys

    out: list[str] = []
    for line in train:
        parts = line.split()
        if len(parts) < 3 or parts[-2] not in ("<lin>", "<cyc>", "<bicy>"):
            out.append(line)
            continue
        body = parts[-1]
        r = rng.random()
        if r >= p_cyc + p_bicy:
            out.append(line)
            continue
        if r < p_cyc:
            parts[-2] = "<cyc>"
            out.append(" ".join(parts))
            continue
        # bicyclic: enforce layout on the plain residue body
        if "<pre>" in body or "[" in body:
            out.append(line)
            continue
        res = enforce_bicyclic_cys(list(body), [len(body) // 2],
                                   random.Random(0), layout="first_last")
        parts[-2] = "<bicy>"
        parts[-1] = "".join(res)
        out.append(" ".join(parts))
    return out


def _line_props(text: str) -> tuple[float, float, float]:
    body = text.split()[-1]
    toks = [t for t in parse_tokens(body)
            if len(t) == 1 or t.startswith("[")]
    if not toks:
        return (0.5, 0.5, 0.5)
    p = compute_props(toks)
    return (p["solubility"], p["synthesizability"], p["liability"])


def pretrain_modern(
    model: ModernPrior,
    train: list[str],
    val: list[str],
    vocab: Vocab,
    *,
    epochs: int = 2,
    batch_size: int = 384,
    lr: float = 6e-4,
    warmup: float = 0.02,
    device: str = "cuda",
    grad_accum: int = 1,
    aux_weight: float = 0.1,
    log=print,
    max_len: int | None = None,
    save_best: str | None = None,
) -> dict:
    max_len = max_len or model.max_len
    model.to(device)
    # hidden-states retention for the aux head roughly doubles activation
    # memory; gradient checkpointing trades ~30% compute for a large cut
    model.gpt.gradient_checkpointing_enable()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01,
                            betas=(0.9, 0.95))
    steps_per_epoch = max(1, math.ceil(len(train) / (batch_size * grad_accum)))
    total_steps = steps_per_epoch * epochs
    warm = max(10, int(total_steps * warmup))

    def lr_at(step):
        if step < warm:
            return step / warm
        p = (step - warm) / max(1, total_steps - warm)
        return 0.1 + 0.45 * (1 + math.cos(math.pi * min(p, 1.0)))

    rng = random.Random(0)
    best_val, best_state = math.inf, None
    step = 0
    pad = model.pad
    for epoch in range(epochs):
        model.train()
        rng.shuffle(train)
        tot, nb = 0.0, 0
        opt.zero_grad()
        for i in range(0, len(train), batch_size):
            batch = [s for s in train[i:i + batch_size] if s]
            if len(batch) < 2:
                continue
            enc, props = [], []
            for s in batch:
                try:
                    e = encode_line(vocab, s)[:max_len]
                except (ValueError, KeyError):
                    continue
                if len(e) >= 3:
                    enc.append(e)
                    props.append(_line_props(s))
            if len(enc) < 2:
                continue
            L = max(len(e) for e in enc)
            x = torch.full((len(enc), L), pad, dtype=torch.long)
            for r_i, e in enumerate(enc):
                x[r_i, : len(e)] = torch.tensor(e)
            x = x.to(device)
            prop_t = torch.tensor(props, dtype=torch.float32, device=device)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                out = model.gpt(x[:, :-1], attention_mask=(~x[:, :-1].eq(pad)).long(),
                                output_hidden_states=True)
                logits = out.logits
                loss_ce = F.cross_entropy(
                    logits.reshape(-1, logits.size(-1)).float(),
                    x[:, 1:].reshape(-1), ignore_index=pad)
                loss = loss_ce
                if model.aux_props:
                    att = (~x[:, :-1].eq(pad)).long()
                    m = att.unsqueeze(-1).float()
                    pooled = (out.hidden_states[-1] * m).sum(1) / m.sum(1).clamp_min(1.0)
                    pred = model.prop_head(pooled.float())
                    loss = loss + aux_weight * F.mse_loss(pred, prop_t)
            (loss / grad_accum).backward()
            if (i // batch_size + 1) % grad_accum == 0:
                for g in opt.param_groups:
                    g["lr"] = lr * lr_at(step)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                opt.zero_grad()
                step += 1
            tot += float(loss_ce.detach())
            nb += 1
        model.eval()
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            vlosses = []
            for j in range(0, len(val), batch_size):
                vb = val[j:j + batch_size]
                enc = []
                for s in vb:
                    try:
                        e = encode_line(vocab, s)[:max_len]
                    except (ValueError, KeyError):
                        continue
                    if len(e) >= 3:
                        enc.append(e)
                if len(enc) < 2:
                    continue
                L = max(len(e) for e in enc)
                vx = torch.full((len(enc), L), pad, dtype=torch.long)
                for r_i, e in enumerate(enc):
                    vx[r_i, : len(e)] = torch.tensor(e)
                vx = vx.to(device)
                vl = model.gpt(vx[:, :-1],
                               attention_mask=(~vx[:, :-1].eq(pad)).long()).logits
                vlosses.append(float(F.cross_entropy(
                    vl.reshape(-1, vl.size(-1)).float(), vx[:, 1:].reshape(-1),
                    ignore_index=pad)))
        vloss = sum(vlosses) / max(len(vlosses), 1)
        log(f"[tier1-modern] epoch {epoch} train {tot/max(nb,1):.4f} "
            f"val {vloss:.4f} lr {lr*lr_at(step):.2e}")
        if vloss < best_val - 1e-4:
            best_val = vloss
            best_state = {k: v.detach().cpu().clone()
                          for k, v in model.state_dict().items()}
            if save_best:
                cfg = model.gpt.config
                torch.save({
                    "state_dict": best_state,
                    "itos": [vocab.itos[i] for i in range(len(vocab))],
                    "arch": "modern",
                    "config": {"d_model": cfg.hidden_size,
                               "n_layers": cfg.num_hidden_layers,
                               "n_heads": cfg.num_attention_heads,
                               "max_len": cfg.max_position_embeddings},
                }, save_best)
    if best_state is not None:
        model.load_state_dict(best_state)
    return {"best_val_loss": best_val}


def load_modern_prior(path: str, device: str = "cpu") -> tuple[ModernPrior, Vocab]:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    vocab = Vocab(ckpt["itos"])
    cfg = ckpt.get("config") or {}
    model = ModernPrior(vocab, d_model=cfg.get("d_model", 512),
                        n_layers=cfg.get("n_layers", 8),
                        n_heads=cfg.get("n_heads", 8),
                        max_len=cfg.get("max_len", 128))
    model.load_state_dict(ckpt["state_dict"])
    model.to(device)
    model.eval()
    return model, vocab
