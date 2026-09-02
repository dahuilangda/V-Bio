"""Residue-monomer prior (HALO gpt2_prior.py recipe, peptide-adapted).

Same proven training/inference interface as the HALO prior (sample /
sample_with_prompt / log_probs / _token_logprobs), with one peptide-specific
upgrade: position-aware constrained sampling. NCAA placement rules (e.g. PCA
is N-terminal only) and NCAA-count budgets are enforced during decoding by a
per-position token mask — validity becomes a decoder guarantee, not a
rejection-sampling hope.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from peplm.residues import NCAA_PRESETS, NCAA_TOKENS, placement_of
from peplm.vocab import Vocab


class PlacementMask:
    """Per-position allowed-token mask factory for residue sequences.

    Track how many residues were already emitted (special tokens like dev tags
    and <lin>/<cyc> do not count); placement rules then decide which NCAA
    tokens remain legal at the current position. The NCAA pool is the preset
    table plus any user-registered residues (peplm.residues.USER_RESIDUES)."""

    def __init__(self, vocab: Vocab, max_len: int,
                 ncaa_max: int | None = None,
                 banned_ncaa: list[str] | None = None,
                 extra_tokens: list[str] | None = None,
                 pool_tokens: list[str] | None = None):
        """pool_tokens: the explicit user-specified NCAA pool (strict — only
        these bracket tokens are legal). None = the full catalog (preset
        table + user-registered residues), used at training time."""
        from peplm.residues import USER_RESIDUES, placement_lookup

        self.vocab = vocab
        self.max_len = max_len
        self.ncaa_max = ncaa_max
        banned = set(banned_ncaa or [])
        if pool_tokens is not None:
            self.tokens = [t for t in dict.fromkeys(pool_tokens)
                           if t not in banned]
        else:
            self.tokens = [f"[{c}]" for c in NCAA_PRESETS] \
                + [f"[{c}]" for c in USER_RESIDUES] \
                + list(extra_tokens or [])
            self.tokens = [t for t in dict.fromkeys(self.tokens)
                           if t not in banned]
        self._placement = placement_lookup
        self.ncaa_ids = {vocab.stoi[t] for t in self.tokens if t in vocab.stoi}
        # strict pool: bracket tokens NOT in the user pool are banned outright
        self.strict_ban: set[int] = set()
        if pool_tokens is not None:
            allowed = set(self.tokens)
            self.strict_ban = {vocab.stoi[t] for t in vocab.itos.values()
                               if t.startswith("[") and t not in allowed}
            self.strict_ban -= self.ncaa_ids

    def mask(self, emitted_residues: int, ncaa_used: int, total_len_hint: int,
             logits: torch.Tensor):
        """Apply in-place legality mask to a [B, V] logits batch."""
        stoi = self.vocab.stoi
        at_n = emitted_residues == 0
        near_c = total_len_hint and emitted_residues >= total_len_hint - 1
        ncaa_ok = self.ncaa_max is None or ncaa_used < self.ncaa_max
        banned_positions: set[int] = set(self.strict_ban)
        if not ncaa_ok:
            banned_positions |= self.ncaa_ids
        else:
            for t in self.tokens:
                pl = self._placement(t)
                tid = stoi.get(t)
                if tid is None:
                    continue
                if pl == "n_term" and not at_n:
                    banned_positions.add(tid)
                elif pl == "c_term" and not near_c:
                    banned_positions.add(tid)
                elif pl == "terminal" and not (at_n or near_c):
                    banned_positions.add(tid)
        if banned_positions:
            idx = torch.tensor(sorted(banned_positions), dtype=torch.long,
                               device=logits.device)
            logits.index_fill_(1, idx, float("-inf"))


class GPT2Prior(nn.Module):
    """HF GPT-2 LM head over the residue vocabulary, PAD=left."""

    def __init__(self, vocab: Vocab, d_model: int = 512, n_layers: int = 8,
                 n_heads: int = 8, dropout: float = 0.0, max_len: int = 96):
        super().__init__()
        from transformers import GPT2Config, GPT2LMHeadModel

        self.vocab = vocab
        self.pad = vocab.stoi["<pad>"]
        self.bos = vocab.stoi["<bos>"]
        self.eos = vocab.stoi["<eos>"]
        cfg = GPT2Config(
            vocab_size=len(vocab), n_positions=max_len, n_embd=d_model,
            n_layer=n_layers, n_head=n_heads, resid_pdrop=dropout,
            embd_pdrop=dropout, attn_pdrop=dropout,
            bos_token_id=self.bos, eos_token_id=self.eos,
            pad_token_id=self.pad,
        )
        self.gpt = GPT2LMHeadModel(cfg)
        self.max_len = max_len

    # ---------------------------------------------------------------- core
    def forward(self, x):
        return self.gpt(x).logits

    def _token_logprobs(self, x: torch.Tensor) -> torch.Tensor:
        att = (~x.eq(self.pad)).long()
        logits = self.gpt(x[:, :-1], attention_mask=att[:, :-1]).logits
        lp = F.log_softmax(logits.float(), dim=-1)
        tgt = x[:, 1:]
        tok = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        return tok * (~tgt.eq(self.pad))

    def log_probs(self, x: torch.Tensor) -> torch.Tensor:
        """Sum log-prob of tokens x[:,1:] given x[:,:-1] (PAD excluded)."""
        return self._token_logprobs(x).sum(-1)

    # ------------------------------------------------------------ sampling
    @torch.no_grad()
    def sample(self, n: int, device, temperature: float = 1.0, top_p: float = 0.95,
               max_len: int | None = None, chunk: int = 256,
               prompt_tokens: list[str] | None = None, **kw) -> list[str]:
        out: list[str] = []
        for i in range(0, n, chunk):
            out.extend(self.sample_with_prompt(
                prompt_tokens or [], min(chunk, n - i), device,
                temperature=temperature, top_p=top_p, max_len=max_len, **kw))
        return out

    @torch.no_grad()
    def sample_with_prompt(
        self,
        prompt_tokens: list[str],
        n: int,
        device,
        temperature: float = 1.0,
        top_p: float = 0.95,
        max_len: int | None = None,
        ban_tokens: list[str] | None = None,
        placement: PlacementMask | None = None,
        target_len: int | None = None,
        return_tokens: bool = False,
        return_logprobs: bool = False,
        guidance_alpha: float = 0.0,
        uncond_anchor: list[str] | None = None,
        max_res: int | None = None,
        constraints: "ConstraintPlan | None" = None,
    ) -> list[str] | tuple:
        """Autoregressive continuation after a prompt.

        placement applies NCAA legality masks during decoding; target_len (if
        given) both guides terminal placement rules and blocks <eos> until
        min(target_len*0.6, target_len-2) residues are emitted so peptides
        cannot collapse to trivial length. guidance_alpha > 0 applies
        classifier-free guidance: logits = (1+a) * P(y|prompt) - a * P(y|bare
        anchor) — amplifying dependence on the visible conditioning (the dev
        tag). The unconditional head is well-defined because pretraining
        includes tag-dropped examples whose prefix is the anchor (HALO's CFG
        recipe). return_tokens gives the token list (needed by the loop;
        string join would be identical but tokens keep structure tokens like
        <cyc> explicit)."""
        max_len = max_len or self.max_len - 2
        was_training = self.training
        self.eval()
        stoi = self.vocab.stoi
        prompt_ids = [stoi[t] for t in prompt_tokens if t in stoi]
        base = [self.bos] + prompt_ids
        x = torch.tensor([base] * n, dtype=torch.long, device=device)
        finished = torch.zeros(n, dtype=torch.bool, device=device)
        banned = torch.tensor([stoi[t] for t in (ban_tokens or []) if t in stoi],
                              dtype=torch.long, device=device)
        # count residues already in the prompt for placement tracking
        res_emitted = [sum(1 for t in prompt_tokens
                           if len(t) == 1 or t.startswith("["))] * n
        ncaa_used = [sum(1 for t in prompt_tokens if t.startswith("["))] * n
        gen_logprob = torch.zeros(n, device=device)
        gen_len = torch.zeros(n, device=device)
        eos_id = self.eos
        min_res = max(3, int(target_len * 0.6) - 2) if target_len else 0
        # hard length control: past max_res the only legal token is <eos>
        # (the prior's natural spread is 8-45; a design window must be a
        # decoder guarantee, not a post-hoc filter that starves the pool)
        if max_res is None and target_len:
            max_res = int(round(target_len * 1.3))
        uncond_x = None
        if guidance_alpha > 0:
            anchor = uncond_anchor or ["<lin>"]
            bare = [self.bos] + [stoi[t] for t in anchor if t in stoi]
            uncond_x = torch.tensor([bare] * n, dtype=torch.long, device=device)

        for _ in range(max(4, max_len - len(prompt_ids))):
            logits = self.gpt(x).logits[:, -1].float() / max(temperature, 1e-4)
            if uncond_x is not None:
                u_logits = self.gpt(uncond_x).logits[:, -1].float() / max(temperature, 1e-4)
                logits = (1.0 + guidance_alpha) * logits - guidance_alpha * u_logits
            if len(banned):
                logits.index_fill_(1, banned, float("-inf"))
            if constraints is not None:
                # decode-time constraint plan (fixed residues / anchors /
                # NCAA pool + length bounds) — one place, no post-hoc repair
                hint = target_len if target_len else 10 ** 6
                for i in range(n):
                    if finished[i]:
                        continue
                    constraints.apply(logits[i], res_emitted[i],
                                      ncaa_used[i], hint)
            else:
                if placement is not None:
                    hint = target_len if target_len else 10 ** 6
                    for i in range(n):
                        if finished[i]:
                            continue
                        placement.mask(res_emitted[i], ncaa_used[i], hint, logits[i:i + 1])
                # block <eos> before a minimum peptide length
                if min_res:
                    short = torch.tensor([r < min_res for r in res_emitted],
                                         device=device)
                    logits[short & (~finished), eos_id] = float("-inf")
                if max_res:
                    long_rows = torch.tensor([r >= max_res for r in res_emitted],
                                              device=device)
                    if bool(long_rows.any()):
                        keep = torch.full_like(logits[0], float("-inf"))
                        keep[eos_id] = 0.0
                        logits[long_rows & (~finished)] = keep.unsqueeze(0)
            lsm = F.log_softmax(logits, -1)
            sorted_lp, sorted_idx = torch.sort(lsm, descending=True)
            cum = torch.exp(sorted_lp).cumsum(-1)
            keep = cum - torch.exp(sorted_lp) <= top_p
            keep[..., 0] = True
            probs = torch.zeros_like(logits)
            probs.scatter_(-1, sorted_idx, keep.float() * torch.exp(sorted_lp))
            probs = probs / probs.sum(-1, keepdim=True).clamp_min(1e-12)
            probs = torch.nan_to_num(probs, nan=0.0, posinf=0.0, neginf=0.0)
            if bool((probs.sum(-1) <= 0).any()):
                dead = probs.sum(-1) <= 0
                probs[dead] = 1.0 / probs.size(-1)
            nxt = torch.multinomial(probs, 1)
            if return_logprobs:
                chosen_lp = lsm.gather(-1, nxt).squeeze(1)
                active = (~finished) & (~nxt.squeeze(1).eq(eos_id))
                gen_logprob += torch.where(active, chosen_lp, torch.zeros_like(chosen_lp))
                gen_len += active.long()
            nxt_list = nxt.squeeze(1).tolist()
            for i in range(n):
                if finished[i]:
                    continue
                tok = self.vocab.itos.get(nxt_list[i], "")
                if len(tok) == 1 and tok.isalpha() and tok.isupper():
                    res_emitted[i] += 1
                elif tok.startswith("["):
                    res_emitted[i] += 1
                    ncaa_used[i] += 1
            nxt[finished] = self.pad
            x = torch.cat([x, nxt], dim=1)
            if uncond_x is not None:
                uncond_x = torch.cat([uncond_x, nxt], dim=1)
            finished |= nxt.squeeze(1).eq(eos_id)
            if bool(finished.all()):
                break
        if was_training:
            self.train()
        rows: list[list[str]] = []
        for row in x:
            toks: list[str] = []
            for i in row.tolist()[len(base):]:
                if i == self.eos:
                    break
                if i not in (self.pad, self.bos):
                    toks.append(self.vocab.itos[i])
            rows.append(toks)
        if return_tokens:
            out: list = rows
        else:
            out = ["".join(r) for r in rows]
        if return_logprobs:
            norm_lp = (gen_logprob / gen_len.clamp_min(1)).tolist()
            return out, norm_lp
        return out


def encode_line(vocab, text: str) -> list[int]:
    """Canonical training encoding.

    Line grammar: ``tag+ L-bucket <lin>|<cyc> BODY`` where BODY is either
    a plain residue string (NCAA brackets allowed) or an FIM body
    ``<pre> P <suf> S <mid> M``. All whitespace-separated parts except the
    last are single tokens; the last part is regex-parsed. Wrapped with
    <bos>/<eos> so the model learns termination (the missing-eos bug)."""
    from peplm.vocab import parse_tokens

    parts = text.split()
    if not parts:
        return []
    toks: list[str] = [p for p in parts[:-1] if p in vocab.stoi]
    toks.extend(parse_tokens(parts[-1]))
    return [vocab.bos] + vocab.encode_tokens(toks) + [vocab.eos]


def encode_batch(vocab: Vocab, texts: list[str], max_len: int, pad_id: int):
    enc = []
    for s in texts:
        try:
            e = encode_line(vocab, s)[:max_len]
        except (ValueError, KeyError):
            continue  # unknown characters must never kill a training run
        if len(e) >= 3:
            enc.append(e)
    if not enc:
        return torch.zeros((0, 3), dtype=torch.long)
    L = max(len(e) for e in enc)
    x = torch.full((len(enc), L), pad_id, dtype=torch.long)
    for i, e in enumerate(enc):
        x[i, : len(e)] = torch.tensor(e)
    return x
