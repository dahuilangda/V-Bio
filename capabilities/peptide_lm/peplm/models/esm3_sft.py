"""Masked SFT warm-up for the ESM3 peptide policy (PepMLM protocol on the
ESM3 backbone): grounds the receptor-conditioned distribution before RL,
with LoRA capacity, masked CE on binder tokens, and PepMLM's data format
(binder fully masked at the C-terminus of the receptor)."""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import torch
import torch.nn.functional as F

PEPTIDELM_ROOT = Path(__file__).resolve().parents[2]
PAIRS = PEPTIDELM_ROOT / "models/binder_receptor_pairs.jsonl"

AA20 = set("ACDEFGHIKLMNPQRSTVWY")


def load_pairs(path: Path = PAIRS, max_receptor: int = 450,
               pep_range: tuple = (4, 50)) -> list[tuple[str, str]]:
    pairs = []
    for line in open(path):
        d = json.loads(line)
        pep = (d.get("pep_seq") or d.get("peptide") or "").upper()
        rec = (d.get("rec_seq") or d.get("receptor") or "").upper()
        if (pep and rec and set(pep) <= AA20 and set(rec) <= AA20
                and len(rec) <= max_receptor
                and pep_range[0] <= len(pep) <= pep_range[1]):
            pairs.append((rec, pep))
    return pairs


def sft_warmup(
    policy,
    pairs: list[tuple[str, str]],
    epochs: int = 2,
    lr: float = 1e-5,
    batch: int = 8,
    grad_accum: int = 4,
    log=print,
) -> dict:
    """Masked CE on the binder span; mask probability anneals 0.4->1.0
    over training (partial infilling first, then the full de novo regime
    the policy meets in RL)."""
    dev = policy.device
    opt = torch.optim.AdamW(policy.adapter_parameters(), lr=lr,
                            betas=(0.9, 0.99), weight_decay=0.1)
    vocab = policy.toks.sequence.vocab
    rng = random.Random(0)
    steps_total = math.ceil(len(pairs) / batch) * epochs
    step = 0
    best = math.inf
    for epoch in range(epochs):
        rng.shuffle(pairs)
        opt.zero_grad(set_to_none=True)
        for i0 in range(0, len(pairs), batch):
            rows = pairs[i0:i0 + batch]
            # pad to the longest context in the mini-batch
            enc = [([vocab["<cls>"]]
                    + [vocab[c] for c in rec]
                    + [vocab[c] for c in pep] + [vocab["<eos>"]], len(rec))
                   for rec, pep in rows]
            L = max(len(e[0]) for e in enc)
            pad = vocab["<pad>"]
            batch_t = torch.full((len(enc), L), pad, dtype=torch.long,
                                 device=dev)
            tgt = torch.full((len(enc), L), -100, dtype=torch.long,
                             device=dev)
            attn = torch.zeros((len(enc), L), dtype=torch.bool, device=dev)
            for r, (ids, rec_len) in enumerate(enc):
                pep_start = rec_len + 1          # cls + receptor (no chainbreak)
                pep_len = len(ids) - pep_start - 1
                masked = list(ids)
                # the ENTIRE binder is masked exclusively — no partial
                # visibility (training on it lets the model copy visible
                # neighbors and inflate CE)
                for j in range(pep_len):
                    masked[pep_start + j] = policy.mask_id
                    tgt[r, pep_start + j] = ids[pep_start + j]
                batch_t[r, :len(masked)] = torch.tensor(masked, device=dev)
                attn[r, :len(ids)] = True
            with torch.autocast("cuda", dtype=policy.dtype):
                out = policy.model(sequence_tokens=batch_t,
                                   sequence_id=attn.long())
            logits = out.sequence_logits.float()
            ce = F.cross_entropy(
                logits.reshape(-1, logits.shape[-1]), tgt.reshape(-1),
                ignore_index=-100, reduction="mean")
            (ce / grad_accum).backward()
            if (i0 // batch + 1) % grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(
                    policy.adapter_parameters(), 1.0)
                opt.step()
                opt.zero_grad(set_to_none=True)
            step += 1
            if step % 50 == 0:
                log(f"[esm3-sft] ep{epoch} step {step}/{steps_total} "
                    f"ce={float(ce):.4f} p_mask=1.00")
            best = min(best, float(ce))
    return {"best_ce": best, "steps": step}
