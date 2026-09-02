#!/usr/bin/env python
"""Tier-1 pretraining: corpus load + NCAA augmentation + GPT-2 training + eval.

Usage:
  PY scripts/train_tier1.py --data runs/data --out runs/prior --device cuda:0
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from peplm.models.gpt2 import GPT2Prior
from peplm.models.train import pretrain_gpt2
from peplm.props.descriptors import compute_props, dev_tag_for
from peplm.residues import NCAA_TOKENS, placement_of
from peplm.vocab import DEFAULT_VOCAB, parse_tokens


def load_lines(data_dir: str, max_train: int | None = None) -> tuple[list[str], list[str]]:
    d = Path(data_dir)
    train, val = [], []
    for name in ("uniref_train.txt", "pdb_train.txt"):
        p = d / name
        if p.exists():
            train.extend(l.strip() for l in p.read_text().splitlines() if l.strip())
    p = d / "uniref_val.txt"
    if p.exists():
        val.extend(l.strip() for l in p.read_text().splitlines() if l.strip())

    # defensive: every token must be in the vocabulary (one bad line must
    # never kill a multi-hour training run); grammar: tokens... BODY(last)
    def ok(line: str) -> bool:
        parts = line.split()
        if len(parts) < 2:
            return False
        try:
            body = parse_tokens(parts[-1])
        except ValueError:
            return False
        return (all(t in DEFAULT_VOCAB.stoi for t in body)
                and all(p_ in DEFAULT_VOCAB.stoi for p_ in parts[:-1]))

    n0 = len(train)
    train = [l for l in train if ok(l)]
    val = [l for l in val if ok(l)]
    if len(train) < n0:
        print(f"[tier1] filtered {n0 - len(train)} invalid lines")
    rng = random.Random(0)
    rng.shuffle(train)
    if max_train:
        train = train[:max_train]
    if not val:
        rng.shuffle(train)
        cut = max(1000, len(train) // 100)
        val, train = train[:cut], train[cut:]
    return train, val


def augment_with_ncaa(train: list[str], fraction: float = 0.03,
                      seed: int = 7) -> list[str]:
    """NCAA warm-start on a slice of the corpus. Operates on the BODY (last
    whitespace part); FIM lines are left untouched (their <pre>/<suf>/<mid>
    spans must stay consistent)."""
    rng = random.Random(seed)
    out = []
    for line in train:
        out.append(line)
        if rng.random() >= fraction:
            continue
        parts = line.split()
        body = parts[-1]
        if "<pre>" in body or "[" in body:
            continue
        toks = parse_tokens(body)
        idxs = [i for i, t in enumerate(toks) if len(t) == 1]
        if not idxs:
            continue
        k = rng.randint(1, min(2, len(idxs)))
        legal = [t for t in NCAA_TOKENS if placement_of(t) == "any"]
        for i in rng.sample(idxs, k):
            toks[i] = rng.choice(legal)
        from peplm.data.build_corpus import props_of, tag_prefix, bucket_tag

        sol, syn, liab = props_of(toks)
        cuts = {"sol": (0.40, 0.55), "syn": (0.70, 0.85), "liab": (0.75, 0.90)}
        out.append(" ".join(tag_prefix(sol, syn, liab, cuts)
                            + [bucket_tag(len(toks)), "<lin>"])
                   + " " + "".join(toks))
    return out


@torch.no_grad()
def evaluate(model, vocab, device, n: int = 400, seed: int = 3) -> dict:
    """Generation quality + conditioning separation + FIM span recovery."""
    from peplm.props.descriptors import compute_props
    from peplm.vocab import parse_tokens

    rng = random.Random(seed)
    stats = {}
    hi_prompt = ["<sol_h>", "<syn_h>", "<liab_h>", "<L15>", "<lin>"]
    lo_prompt = ["<sol_l>", "<syn_l>", "<liab_l>", "<L15>", "<lin>"]
    ban = ["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>", "<syn_m>", "<syn_l>",
           "<liab_h>", "<liab_m>", "<liab_l>", "<dev_hi>", "<dev_md>",
           "<dev_lo>", "<cont>", "<mask>", "<pre>", "<suf>", "<mid>",
           "<lin>", "<cyc>"] + [f"<L{5*k}>" for k in range(1, 10)]
    samples_by_tag = {}
    for name, prompt in (("hi", hi_prompt), ("lo", lo_prompt)):
        toks_list = model.sample_with_prompt(
            prompt, n, device, temperature=1.0, top_p=0.95,
            return_tokens=True, ban_tokens=ban)
        seqs = [[x for x in t if not x.startswith("<")] for t in toks_list if t]
        devs = [compute_props(s)["developability"] for s in seqs if s]
        samples_by_tag[name] = seqs
        stats[f"n_{name}"] = len(seqs)
        stats[f"mean_dev_{name}"] = sum(devs) / max(len(devs), 1)
    hi, lo = samples_by_tag["hi"], samples_by_tag["lo"]
    uniq = len({"".join(t) for t in hi})
    stats["unique_frac"] = uniq / max(len(hi), 1)
    lens = [len(t) for t in hi]
    stats["mean_len"] = sum(lens) / max(len(lens), 1)

    def div(seqs, k=64):
        import itertools

        pairs = list(itertools.combinations(seqs[:k], 2))
        if not pairs:
            return 0.0
        d = 0.0
        for a, b in pairs:
            L = min(len(a), len(b))
            if L:
                d += sum(x != y for x, y in zip(a, b)) / L
        return d / len(pairs)

    stats["internal_diversity"] = div(hi)
    stats["tag_separation"] = stats["mean_dev_hi"] - stats["mean_dev_lo"]

    # FIM span recovery (ProtFIM-style): mask the middle 6 residues of
    # natural peptides; measure (a) greedy exact/near recovery and
    # (b) teacher-forced per-token accuracy of the true middle
    probes = [t for t in lo + hi if len(t) >= 16][:120]
    n_tok = n_span = 0
    tok_hit = span_hit = near_hit = 0
    for seq in probes:
        w = 6
        a = (len(seq) - w) // 2
        P, M, S = seq[:a], seq[a:a + w], seq[a + w:]
        prompt = (["<sol_h>", "<syn_h>", "<liab_h>",
                   f"<L{min(max((len(seq)//5)*5,5),45)}>", "<lin>",
                   "<pre>"] + P + ["<suf>"] + S + ["<mid>"])
        # teacher-forced accuracy over the middle span
        ids = torch.tensor([[vocab.bos] + vocab.encode_tokens(prompt + M)],
                           device=device)
        with torch.no_grad():
            lp = torch.log_softmax(
                model.gpt(ids[:, :-1]).logits.float(), -1)[0]
        for j in range(len(prompt), len(prompt) + len(M)):
            n_tok += 1
            if int(lp[j - 1].argmax()) == int(ids[0, j]):
                tok_hit += 1
        # greedy fill (variable length, sampled until <eos>)
        fill = []
        gid = torch.tensor([[vocab.bos] + vocab.encode_tokens(prompt)],
                           device=device)
        for _ in range(w + 2):
            with torch.no_grad():
                logits = model.gpt(gid).logits[:, -1].float()
            logits[vocab.stoi["<pad>"]] = float("-inf")
            for b in ban:
                logits[0, vocab.stoi[b]] = float("-inf")
            nxt = int(torch.argmax(logits, -1))
            if nxt == vocab.eos:
                break
            fill.append(vocab.itos[nxt])
            gid = torch.cat([gid, torch.tensor([[nxt]], device=device)], 1)
        n_span += 1
        span_hit += int(fill[:w] == M)
        near_hit += int(sum(g == m for g, m in zip(fill, M)) >= w // 2)
    stats["fim_token_acc"] = tok_hit / max(n_tok, 1)
    stats["fim_span_recovery"] = span_hit / max(n_span, 1)
    stats["fim_near_recovery"] = near_hit / max(n_span, 1)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="runs/data")
    ap.add_argument("--out", default="models")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch_size", type=int, default=384)
    ap.add_argument("--d_model", type=int, default=512)
    ap.add_argument("--n_layers", type=int, default=8)
    ap.add_argument("--max_train", type=int, default=None)
    ap.add_argument("--lr", type=float, default=6e-4)
    ap.add_argument("--ncaa_aug", type=float, default=0.10,
                    help="fraction of corpus lines augmented with NCAAs")
    ap.add_argument("--arch", choices=["gpt2", "modern"], default="modern",
                    help="modern = Llama-style RoPE/SwiGLU/RMSNorm + aux "
                         "property heads + modality augmentation")
    ap.add_argument("--skip_eval", action="store_true")
    args = ap.parse_args()

    train, val = load_lines(args.data, args.max_train)
    train = augment_with_ncaa(train, fraction=args.ncaa_aug)
    print(f"[tier1] {len(train)} train (incl. NCAA aug) / {len(val)} val lines")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.arch == "modern":
        from peplm.models.llama_prior import ModernPrior
        from peplm.models.train_modern import modality_augment, pretrain_modern

        train = modality_augment(train, random.Random(11))
        model = ModernPrior(DEFAULT_VOCAB, d_model=args.d_model,
                            n_layers=args.n_layers, n_heads=8, max_len=96)
        print(f"[tier1-modern] params: "
              f"{sum(p.numel() for p in model.parameters())/1e6:.1f}M")
        # hidden-states retention for the aux head costs memory: quarter the
        # batch, keep the effective batch via grad accumulation
        eff_bs = 128
        info = pretrain_modern(model, train, val, DEFAULT_VOCAB,
                               epochs=args.epochs, batch_size=eff_bs,
                               grad_accum=3,
                               lr=args.lr, device=args.device,
                               save_best=str(out / "prior.pt"), log=print)
    else:
        model = GPT2Prior(DEFAULT_VOCAB, d_model=args.d_model,
                          n_layers=args.n_layers, n_heads=8, max_len=96)
        print(f"[tier1] params: {sum(p.numel() for p in model.parameters())/1e6:.1f}M")
        info = pretrain_gpt2(model, train, val, DEFAULT_VOCAB,
                             epochs=args.epochs, batch_size=args.batch_size,
                             lr=args.lr, device=args.device,
                             save_best=str(out / "prior.pt"),
                             log=print)
    print("[tier1] done:", info)
    if not args.skip_eval:
        model.eval().to(args.device)
        stats = evaluate(model, DEFAULT_VOCAB, args.device)
        print("[tier1-eval]", stats)
        (out / "eval.json").write_text(__import__("json").dumps(stats, indent=1))


if __name__ == "__main__":
    main()
