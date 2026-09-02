"""Tier-1 pretraining loop (HALO pretrain_gpt2 recipe: bf16 + cosine +
warmup + early stopping on validation loss)."""

from __future__ import annotations

import math
import random

import torch
import torch.nn.functional as F

from peplm.models.gpt2 import GPT2Prior, encode_batch
from peplm.vocab import Vocab


def pretrain_gpt2(
    model: GPT2Prior,
    train: list[str],
    val: list[str],
    vocab: Vocab,
    *,
    epochs: int = 3,
    batch_size: int = 384,
    lr: float = 6e-4,
    warmup: float = 0.02,
    device: str = "cuda",
    grad_accum: int = 1,
    log=print,
    max_len: int | None = None,
    save_best: str | None = None,
) -> dict:
    max_len = max_len or model.max_len
    model.to(device)
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
            batch = [s for s in train[i : i + batch_size] if s]
            if len(batch) < 2:
                continue
            x = encode_batch(vocab, batch, max_len, pad).to(device)
            if x.numel() == 0:
                continue
            with torch.autocast("cuda", dtype=torch.bfloat16):
                logits = model.gpt(x[:, :-1]).logits
                loss = F.cross_entropy(
                    logits.reshape(-1, logits.size(-1)).float(),
                    x[:, 1:].reshape(-1), ignore_index=pad,
                )
            (loss / grad_accum).backward()
            if (i // batch_size + 1) % grad_accum == 0:
                for g in opt.param_groups:
                    g["lr"] = lr * lr_at(step)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                opt.zero_grad()
                step += 1
            tot += float(loss.detach())
            nb += 1
        model.eval()
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            vlosses = []
            for j in range(0, len(val), batch_size):
                vb = val[j : j + batch_size]
                if len(vb) < 2:
                    continue
                vx = encode_batch(vocab, vb, max_len, pad).to(device)
                vl = model.gpt(vx[:, :-1]).logits
                vlosses.append(float(F.cross_entropy(
                    vl.reshape(-1, vl.size(-1)).float(), vx[:, 1:].reshape(-1),
                    ignore_index=pad)))
        vloss = sum(vlosses) / max(len(vlosses), 1)
        log(f"[tier1] epoch {epoch} train {tot/max(nb,1):.4f} "
            f"val {vloss:.4f} lr {lr*lr_at(step):.2e}")
        if vloss < best_val - 1e-4:
            best_val = vloss
            best_state = {k: v.detach().cpu().clone()
                          for k, v in model.state_dict().items()}
            if save_best:
                cfg = model.gpt.config
                torch.save({"state_dict": best_state,
                            "itos": [vocab.itos[i] for i in range(len(vocab))],
                            "config": {"d_model": cfg.n_embd,
                                       "n_layers": cfg.n_layer,
                                       "n_heads": cfg.n_head,
                                       "max_len": cfg.n_positions}},
                           save_best)
    if best_state is not None:
        model.load_state_dict(best_state)
    return {"best_val_loss": best_val}


def load_prior(path: str, device: str = "cpu"):
    """Load a prior checkpoint by its declared arch field ('modern' or
    'gpt2'). Checkpoints without an explicit arch are rejected: guessing the
    architecture from tensor shapes has silently loaded the wrong backbone.
    Returns (model, vocab)."""
    ckpt = torch.load(path, map_location=device, weights_only=False)
    arch = ckpt.get("arch")
    if arch == "modern":
        from peplm.models.train_modern import load_modern_prior

        return load_modern_prior(path, device)
    if arch == "gpt2":
        vocab = Vocab(ckpt["itos"])
        cfg = ckpt["config"]
        model = GPT2Prior(vocab, d_model=cfg["d_model"],
                          n_layers=cfg["n_layers"],
                          n_heads=cfg.get("n_heads", 8),
                          max_len=cfg.get("max_len", 96))
        model.load_state_dict(ckpt["state_dict"])
        model.to(device)
        model.eval()
        return model, vocab
    raise ValueError(
        f"checkpoint {path} declares no supported arch (got {arch!r}); "
        "expected 'modern' or 'gpt2'")
