"""GPT-2-style fragment-tokenizer prior, pretrained on full ChEMBL36.

Architecture and training follow the standard chemical-language-model recipe
(GPT-2 decoder over SMILES, as in MolGPT / REINVENT4's transformer prior),
upgraded with:

  * a fragment-level vocabulary (frequent ring systems, Murcko frameworks and
    BRICS pieces mined from the corpus - molecules are sequences of drug-like
    fragments, not bare atoms),
  * full-corpus pretraining (1.5M+ ChEMBL36 clean compounds from the V-Bio
    matched-pair database), bf16 + cosine schedule + early stopping,
  * an evaluation suite with MOSES-style metrics (validity, unique@k,
    internal diversity, novelty, FCD via fcd_torch).

The model exposes the same interface the HALO loop expects from a prior:
sample() and log_probs() over the fragment vocab.
"""

from __future__ import annotations

import math
import random

import torch
import torch.nn as nn
import torch.nn.functional as F


class GPT2Prior(nn.Module):
    """HF GPT-2 LM head with a custom (fragment) vocabulary, PAD=left."""

    def __init__(self, vocab, d_model=512, n_layers=8, n_heads=8, dropout=0.0,
                 max_len=256, bos=None, eos=None, pad=None):
        super().__init__()
        from transformers import GPT2Config, GPT2LMHeadModel

        self.vocab = vocab
        self.pad = pad if pad is not None else vocab.stoi.get("<pad>", 0)
        self.bos = bos if bos is not None else vocab.stoi.get("<bos>", 1)
        self.eos = eos if eos is not None else vocab.stoi.get("<eos>", 2)
        cfg = GPT2Config(
            vocab_size=len(vocab), n_positions=max_len, n_embd=d_model,
            n_layer=n_layers, n_head=n_heads, resid_pdrop=dropout,
            embd_pdrop=dropout, attn_pdrop=dropout, bos_token_id=self.bos,
            eos_token_id=self.eos, pad_token_id=self.pad,
        )
        self.gpt = GPT2LMHeadModel(cfg)
        self.max_len = max_len

    def forward(self, x):
        return self.gpt(x).logits

    def _token_logprobs(self, x):
        att = (~x.eq(self.pad)).long()
        logits = self.gpt(x[:, :-1], attention_mask=att[:, :-1]).logits
        lp = F.log_softmax(logits.float(), dim=-1)
        tgt = x[:, 1:]
        tok = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        return tok * (~tgt.eq(self.pad))

    def log_probs(self, x: torch.Tensor) -> torch.Tensor:
        """Sum log-prob of tokens x[:,1:] given x[:,:-1] (PAD excluded)."""
        att = (~x.eq(self.pad)).long()
        logits = self.gpt(x[:, :-1], attention_mask=att[:, :-1]).logits
        tgt = x[:, 1:]
        lp = F.log_softmax(logits.float(), dim=-1)
        tok_lp = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        mask = ~tgt.eq(self.pad)
        return (tok_lp * mask).sum(-1)

    def _prompt_ids(self, prompt_tokens) -> list[int]:
        """String prompts are BPE-encoded by the tokenizer; token lists map
        through stoi directly."""
        if isinstance(prompt_tokens, str):
            tok = getattr(self.vocab, "tok", None)
            if tok is not None:
                return list(tok.encode(prompt_tokens).ids)
            return [self.vocab.stoi[c] for c in prompt_tokens if c in self.vocab.stoi]
        stoi = self.vocab.stoi
        return [stoi[t] for t in prompt_tokens if t in stoi]

    @torch.no_grad()
    def beam_edit(self, prompt_tokens, n: int, device, beam_width: int = 8,
                  max_len=None, temperature: float = 1.0, ban_tokens: list[str] | None = None,
                  alpha_len_norm: float = 0.6):
        """Closure-aware beam search for edit operators.

        A beam completes only when it emits <eos> with every ring number
        paired (the prompt's dangling digits included). Returns the n best
        completions by length-normalized logprob, so the model's own
        confidence ranks the candidates.
        """
        import re

        max_len = max_len or self.max_len - 2
        stoi = self.vocab.stoi
        prompt_ids = self._prompt_ids(prompt_tokens)
        base = [self.bos] + prompt_ids
        banned_ids = {stoi[t] for t in (ban_tokens or []) if t in stoi}

        def digit_events(token_str: str) -> list[str]:
            evs, masked = [], token_str
            for mm in re.finditer(r"%(\d{2,3})", token_str):
                evs.append(mm.group(1))
                masked = masked.replace(mm.group(0), "  ")
            evs += [ch for ch in masked if ch.isdigit()]
            return evs

        def apply_events(open_rings: dict, tok_str: str):
            for d in digit_events(tok_str):
                if open_rings.get(d, 0) > 0:
                    open_rings[d] -= 1
                    if open_rings[d] == 0:
                        del open_rings[d]
                else:
                    open_rings[d] = 1

        seed: dict[str, int] = {}
        for t in prompt_ids:
            tok = self.vocab.itos.get(t, "")
            if not (tok.startswith("<") and tok.endswith(">")):
                for d in digit_events(tok):
                    seed[d] = seed.get(d, 0) + 1
        seed = {d: c for d, c in seed.items() if c % 2 == 1}

        self.eval()
        # beams: (score, ids, open_rings)
        beams = [(0.0, list(base), dict(seed))]
        finished: list[tuple[float, list[int]]] = []
        for _ in range(max(4, max_len - len(prompt_ids))):
            if len(finished) >= max(n, beam_width):
                break
            cands = []
            x = torch.tensor([b[1] for b in beams], dtype=torch.long, device=device)
            logits = self.gpt(x).logits[:, -1].float() / max(temperature, 1e-4)
            lsm = F.log_softmax(logits, -1)
            for bi, (score, ids, rings) in enumerate(beams):
                top_lp, top_idx = torch.topk(lsm[bi], min(24, lsm.size(-1)))
                for lp_v, tid in zip(top_lp.tolist(), top_idx.tolist()):
                    tok = tid
                    if tok in banned_ids and tok != self.eos:
                        continue
                    if tok == self.eos:
                        if sum(rings.values()) == 0:
                            finished.append((score, ids))
                        continue  # unbalanced eos: this branch dies here
                    tok_str = self.vocab.itos.get(tok, "")
                    nrs = dict(rings)
                    if not (tok_str.startswith("<") and tok_str.endswith(">")):
                        apply_events(nrs, tok_str)
                    cands.append((score + lp_v, ids + [tok], nrs))
            if not cands:
                break
            cands.sort(key=lambda c: -c[0])
            beams = cands[:beam_width]
        # length-normalized ranking (GPUS-style alpha)
        def norm(entry):
            s, ids = entry
            gen_len = max(len(ids) - len(base), 1)
            return s / (gen_len ** alpha_len_norm)

        finished.sort(key=lambda c: -norm(c))
        out, scores = [], []
        for s, ids in finished[:n]:
            gen = ids[len(base):]
            out.append(self.vocab.decode([i for i in gen if i not in (self.pad, self.bos)]))
            scores.append(norm((s, ids)))
        return out, scores

    @torch.no_grad()
    def sample(self, n: int, device, temperature=1.0, top_p=0.95, max_len=None,
               chunk: int = 48) -> list[str]:
        out: list[str] = []
        for i in range(0, n, chunk):
            out.extend(self.sample_with_prompt([], min(chunk, n - i), device,
                                               temperature=temperature,
                                               top_p=top_p, max_len=max_len))
        return out

    @torch.no_grad()
    def sample_with_prompt(self, prompt_tokens: list[str], n: int, device,
                           temperature=1.0, top_p=0.95, max_len=None,
                           ban_tokens: list[str] | None = None,
                           include_prompt: bool = True,
                           require_digit_closure: bool = False,
                           canonical_fsm: bool = False,
                           guidance_alpha: float = 0.0,
                           return_logprobs: bool = False):
        """Autoregressive continuation after a prompt (scaffold conditioning).

        ban_tokens forbids special tokens in the generation. With
        require_digit_closure, <eos> stays masked while any SAFE ring number
        is unpaired, blocking truncated-fragment endings. canonical_fsm
        (FG vocab only, digits as separate tokens) masks every digit token
        except {digits closing an open ring} + {the next unused number}:
        numbering determinism becomes a decoder guarantee, not a learned
        behaviour. guidance_alpha > 0 applies classifier-free guidance:
        logits = (1+a) * P(y|prompt) - a * P(y|bare <cont>) - amplifying how
        much the model depends on the visible environment (the unconditional
        head is well-defined because training includes dropped-context
        examples). return_logprobs adds the length-normalized logprob.
        """
        max_len = max_len or self.max_len - 2
        was_training = self.training
        self.eval()
        stoi = self.vocab.stoi
        prompt_ids = self._prompt_ids(prompt_tokens)
        base = [self.bos] + prompt_ids
        x = torch.tensor([base] * n, dtype=torch.long, device=device)
        finished = torch.zeros(n, dtype=torch.bool, device=device)
        banned = torch.tensor([stoi[t] for t in (ban_tokens or []) if t in stoi],
                              dtype=torch.long, device=device)

        import re
        _pct_re = re.compile(r"%(\d{2,3})")

        def _digit_events(token_str: str) -> list[str]:
            """Ring-closure numbers opened/closed by one token."""
            evs = []
            masked = token_str
            for m in re.finditer(r"%(\d{2,3})", token_str):
                evs.append(m.group(1))
                masked = masked.replace(m.group(0), "  ")
            for ch in masked:
                if ch.isdigit():
                    evs.append(ch)
            return evs

        # per-row open-ring multiset (prompt tokens seed the state)
        open_rings: list[dict[str, int]] = []
        if require_digit_closure:
            prompt_events = []
            for t in prompt_ids:
                tok = self.vocab.itos.get(t, "")
                if not (tok.startswith("<") and tok.endswith(">")):
                    prompt_events.extend(_digit_events(tok))
            seed: dict[str, int] = {}
            for d in prompt_events:
                seed[d] = seed.get(d, 0) + 1
            open_rings = [dict(seed) for _ in range(n)]

        gen_logprob = torch.zeros(n, device=device)
        gen_len = torch.zeros(n, device=device)
        eos_id = self.eos

        # canonical FSM state (FG vocab: digits are standalone tokens)
        digit_val = {}          # token id -> integer digit value
        if canonical_fsm:
            import re as _re0

            for t, i in stoi.items():
                if _re0.fullmatch(r"\d", t or ""):
                    digit_val[i] = int(t)
                elif t and t.startswith("%") and t[1:].isdigit():
                    digit_val[i] = int(t[1:])
            prompt_open: list[set[int]] = []
            prompt_used = 0
            for t in prompt_ids:
                v = digit_val.get(t)
                if v is None:
                    continue
                prompt_used = max(prompt_used, v)
            # rings still open in the prompt (appeared odd times)
            from collections import Counter as _Ctr

            pc = _Ctr(digit_val.get(t) for t in prompt_ids if t in digit_val)
            p_open = {v for v, c in pc.items() if c % 2 == 1}
            fsm_open = [set(p_open) for _ in range(n)]
            fsm_used = [prompt_used] * n
        banned_digit_ids = set(digit_val)
        uncond_x = None
        if guidance_alpha > 0:
            bare = [self.bos] + self._prompt_ids("<cont>")
            uncond_x = torch.tensor([bare] * n, dtype=torch.long, device=device)

        for _ in range(max(4, max_len - len(prompt_ids))):
            logits = self.gpt(x).logits[:, -1].float() / max(temperature, 1e-4)
            if uncond_x is not None:
                u_logits = self.gpt(uncond_x).logits[:, -1].float() / max(temperature, 1e-4)
                logits = (1.0 + guidance_alpha) * logits - guidance_alpha * u_logits
            if len(banned):
                logits.index_fill_(1, banned, float("-inf"))
            if canonical_fsm:
                # allowed: close any open ring, or open exactly max_used+1
                for i in range(n):
                    if finished[i]:
                        continue
                    allowed = set(fsm_open[i])
                    nxt = fsm_used[i] + 1
                    allowed.add(nxt)
                    for tid, v in digit_val.items():
                        if v not in allowed:
                            logits[i, tid] = float("-inf")
            if require_digit_closure:
                for i in range(n):
                    if finished[i]:
                        continue
                    # FG vocab: pattern tokens contain '0' placeholders that
                    # would poison the string-based tracker; use the FSM state
                    if canonical_fsm:
                        if fsm_open[i]:
                            logits[i, eos_id] = float("-inf")
                    elif sum(open_rings[i].values()) > 0:
                        logits[i, eos_id] = float("-inf")
            lsm = F.log_softmax(logits, -1)
            sorted_lp, sorted_idx = torch.sort(lsm, descending=True)
            cum = torch.exp(sorted_lp).cumsum(-1)
            keep = cum - torch.exp(sorted_lp) <= top_p
            keep[..., 0] = True
            probs = torch.zeros_like(logits)
            probs.scatter_(-1, sorted_idx, keep.float() * torch.exp(sorted_lp))
            probs = probs / probs.sum(-1, keepdim=True).clamp_min(1e-12)
            probs = torch.nan_to_num(probs, nan=0.0, posinf=0.0, neginf=0.0)
            probs = probs / probs.sum(-1, keepdim=True).clamp_min(1e-12)
            zero_rows = probs.sum(-1) <= 0
            if bool(zero_rows.any()):
                probs[zero_rows] = 1.0 / probs.size(-1)  # uniform fallback
                if canonical_fsm:
                    # the fallback must not resurrect FSM-forbidden digits
                    for i in range(n):
                        if not zero_rows[i] or finished[i]:
                            continue
                        allowed = fsm_open[i] | {fsm_used[i] + 1}
                        for tid, vv in digit_val.items():
                            if vv not in allowed:
                                probs[i, tid] = 0.0
                        probs[i] = probs[i] / probs[i].sum().clamp_min(1e-12)
            nxt = torch.multinomial(probs, 1)
            if canonical_fsm:
                # multinomial can numerically pick zero-probability entries;
                # hard-guard: an illegal digit becomes the best allowed digit
                for i in range(n):
                    if finished[i]:
                        continue
                    v_nxt = digit_val.get(nxt[i, 0].item())
                    if v_nxt is not None:
                        allowed = fsm_open[i] | {fsm_used[i] + 1}
                        if v_nxt not in allowed:
                            best, best_lp = None, -1e9
                            for tid, vv in digit_val.items():
                                if vv in allowed and float(logits[i, tid]) > best_lp:
                                    best, best_lp = tid, float(logits[i, tid])
                            if best is not None:
                                nxt[i, 0] = best
            if return_logprobs:
                chosen_lp = lsm.gather(-1, nxt).squeeze(1)
                active = (~finished) & (~nxt.squeeze(1).eq(eos_id))
                gen_logprob += torch.where(active, chosen_lp, torch.zeros_like(chosen_lp))
                gen_len += active.long()
            if require_digit_closure:
                nxt_list = nxt.squeeze(1).tolist()
                for i in range(n):
                    if finished[i] or nxt_list[i] in (eos_id, self.pad):
                        continue
                    tok = self.vocab.itos.get(nxt_list[i], "")
                    if tok.startswith("<") and tok.endswith(">"):
                        continue
                    for d in _digit_events(tok):
                        cur = open_rings[i].get(d, 0)
                        if cur > 0:      # second occurrence closes the ring
                            open_rings[i][d] = cur - 1
                            if open_rings[i][d] == 0:
                                del open_rings[i][d]
                        else:
                            open_rings[i][d] = 1
            if canonical_fsm:
                nl = nxt.squeeze(1).tolist()
                for i in range(n):
                    if finished[i]:
                        continue
                    v = digit_val.get(nl[i])
                    if v is not None:
                        if v in fsm_open[i]:
                            fsm_open[i].discard(v)
                        else:
                            fsm_open[i].add(v)
                            fsm_used[i] = max(fsm_used[i], v)
            nxt[finished] = self.pad
            x = torch.cat([x, nxt], dim=1)
            if uncond_x is not None:
                uncond_x = torch.cat([uncond_x, nxt], dim=1)
            finished |= nxt.squeeze(1).eq(self.eos)
            if bool(finished.all()):
                break
        if was_training:
            self.train()
        skip_prompt = 1 + len(prompt_ids) if not include_prompt else 1
        out = []
        for row in x:
            ids = []
            for i in row.tolist()[skip_prompt:]:
                if i == self.eos:
                    break
                if i != self.pad and i != self.bos:
                    ids.append(i)
            out.append(self.vocab.decode(ids))
        if return_logprobs:
            norm_lp = (gen_logprob / gen_len.clamp_min(1)).tolist()
            return out, norm_lp
        return out


def encode_batch(vocab, smiles_list, max_len, pad_id):
    enc = [vocab.encode(s)[:max_len] for s in smiles_list]
    enc = [e for e in enc if len(e) >= 3]
    L = max(len(e) for e in enc)
    x = torch.full((len(enc), L), pad_id, dtype=torch.long)
    for i, e in enumerate(enc):
        x[i, : len(e)] = torch.tensor(e)
    return x


def pretrain_gpt2(
    model: GPT2Prior,
    train: list[str],
    val: list[str],
    vocab,
    *,
    epochs=2,
    batch_size=384,
    lr=6e-4,
    warmup=0.02,
    device="cuda",
    grad_accum=1,
    log=print,
    max_len=None,
    save_best=None,
):
    """bf16 pretraining with cosine schedule, grad clipping, early stopping."""
    from torch.amp import autocast

    max_len = max_len or model.max_len
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01, betas=(0.9, 0.95))
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
            with autocast("cuda", dtype=torch.bfloat16):
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
        with torch.no_grad(), autocast("cuda", dtype=torch.bfloat16):
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
        log(f"[gpt2-prior] epoch {epoch} train {tot/max(nb,1):.4f} val {vloss:.4f} lr {lr*lr_at(step):.2e}")
        if vloss < best_val - 1e-4:
            best_val = vloss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            if save_best:
                torch.save(best_state, save_best)
    if best_state is not None:
        model.load_state_dict(best_state)
    return {"best_val_loss": best_val}
