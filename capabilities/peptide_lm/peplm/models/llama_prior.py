"""Modern backbone prior: Llama-style decoder (RoPE + SwiGLU + RMSNorm)
with auxiliary property heads and an additive per-residue SS3 track
(``ss_track``, an nn.Embedding(4, d) summed onto the residue embedding;
ids 0 = free position, 1/2/3 = H/E/L, zero-initialized so pre-track
checkpoints load with identity behaviour).

SS-track alignment: position i predicts token i+1, so position i's
embedding carries the SS label for token i+1 — the track is consumed
left-shifted everywhere (`x[:, :-1]` pairs with `ss_ids[:, 1:]` in
scoring; the sampler appends `track[r + 1]` before emitting residue r+1).
This is the only supported alignment.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from peplm.vocab import PlacementMask, Vocab


class PropertyHeads(nn.Module):
    """3-way regression (solubility, synthesizability, liability) from a
    pre-pooled hidden state."""

    def __init__(self, hidden: int):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(hidden, hidden), nn.GELU(),
                                 nn.Linear(hidden, 3))

    def forward(self, pooled: torch.Tensor) -> torch.Tensor:
        return self.net(pooled)


class ModernPrior(nn.Module):
    """Llama-style causal LM over the residue vocabulary (PAD=left)."""

    def __init__(self, vocab: Vocab, d_model: int = 512, n_layers: int = 8,
                 n_heads: int = 8, dropout: float = 0.0, max_len: int = 128,
                 rope_theta: float = 10000.0, aux_props: bool = True):
        super().__init__()
        from transformers import LlamaConfig, LlamaForCausalLM

        self.vocab = vocab
        self.pad = vocab.stoi["<pad>"]
        self.bos = vocab.stoi["<bos>"]
        self.eos = vocab.stoi["<eos>"]
        cfg = LlamaConfig(
            vocab_size=len(vocab),
            hidden_size=d_model,
            intermediate_size=int(d_model * 2.75),  # SwiGLU 8/3 rule
            num_hidden_layers=n_layers,
            num_attention_heads=n_heads,
            num_key_value_heads=n_heads,
            max_position_embeddings=max_len,
            rms_norm_eps=1e-5,
            pad_token_id=self.pad,
            bos_token_id=self.bos,
            eos_token_id=self.eos,
            rope_theta=rope_theta,
            attention_dropout=dropout,
        )
        self.gpt = LlamaForCausalLM(cfg)  # `self.gpt` keeps the GPT2Prior API
        self.max_len = max_len
        self.aux_props = aux_props
        if aux_props:
            self.prop_head = PropertyHeads(d_model)
        self.ss_track = nn.Embedding(4, d_model)
        nn.init.zeros_(self.ss_track.weight)

    # core
    def _embed(self, x: torch.Tensor, ss_ids: torch.Tensor) -> torch.Tensor:
        """Residue embeddings plus the additive SS track (id 0 adds zero
        for untrained/absent conditioning)."""
        return self.gpt.get_input_embeddings()(x) + self.ss_track(ss_ids)

    def forward(self, x, ss_ids: torch.Tensor | None = None):
        if ss_ids is None:
            return self.gpt(x).logits
        return self.gpt(inputs_embeds=self._embed(x, ss_ids)).logits

    def _token_logprobs(self, x: torch.Tensor,
                        ss_ids: torch.Tensor | None = None) -> torch.Tensor:
        """Per-token log-probs of x[1:] given x[:-1] (PAD excluded), with
        optional additive SS-track conditioning. With a track, `x[:, :-1]`
        pairs with `ss_ids[:, 1:]` (left-shift), matching the sampler's
        alignment."""
        att = (~x.eq(self.pad)).long()
        if ss_ids is not None:
            logits = self.gpt(inputs_embeds=self._embed(x[:, :-1], ss_ids[:, 1:]),
                              attention_mask=att[:, :-1]).logits
        else:
            logits = self.gpt(x[:, :-1], attention_mask=att[:, :-1]).logits
        lp = F.log_softmax(logits.float(), dim=-1)
        tgt = x[:, 1:]
        tok = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        return tok * (~tgt.eq(self.pad))

    def extend_vocab(self, new_tokens: list[str]) -> list[str]:
        """Add residue tokens (e.g. user NCAAs '[XYZ]') at runtime. New
        embeddings are initialized at the mean of existing residue
        embeddings."""
        import torch as _t

        added = []
        for tok in new_tokens:
            if tok not in self.vocab.stoi:
                added.append(tok)
        if not added:
            return []
        old_itos = [self.vocab.itos[i] for i in range(len(self.vocab))]
        itos = old_itos + added
        self.vocab = Vocab(itos)
        self.pad = self.vocab.stoi["<pad>"]
        self.bos = self.vocab.stoi["<bos>"]
        self.eos = self.vocab.stoi["<eos>"]
        old = self.gpt.get_input_embeddings().weight.data
        self.gpt.resize_token_embeddings(len(itos))
        emb = self.gpt.get_input_embeddings().weight.data
        res_ids = [old_itos.index(t) for t in old_itos
                   if len(t) == 1 or (t.startswith("[") and t.endswith("]"))]
        mean_init = old[res_ids].mean(0)
        for tok in added:
            emb[self.vocab.stoi[tok]] = mean_init + 0.02 * _t.randn_like(mean_init)
        return added

    # sampling
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
        ss_track: list[int] | None = None,
    ):
        """Autoregressive continuation after a prompt.

        FIM-aware (prompt tokens may include <pre>/<suf>/<mid>) with
        placement legality / decode-time constraint plans, min/max length
        as decoder guarantees, optional classifier-free guidance
        (logits = (1+a) * P(y|prompt) - a * P(y|bare anchor)), and the
        additive SS track. return_tokens gives the token list (structure
        tokens like <cyc> stay explicit).

        ss_track[j] applies to the j-th EMITTED residue of the de novo
        stream (indexed by the emitted-residue counter, so the FIM route,
        which counts flank residues, must not pass it — de novo prompts
        only, by contract)."""
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
        res_emitted = [sum(1 for t in prompt_tokens
                           if len(t) == 1 or t.startswith("["))] * n
        ncaa_used = [sum(1 for t in prompt_tokens if t.startswith("["))] * n
        gen_logprob = torch.zeros(n, device=device)
        gen_len = torch.zeros(n, device=device)
        eos_id = self.eos
        min_res = max(3, int(target_len * 0.6) - 2) if target_len else 0
        # hard length control: past max_res the only legal token is <eos>
        # (a design window must be a decoder guarantee, not a post-hoc
        # filter)
        if max_res is None and target_len:
            max_res = int(round(target_len * 1.3))
        uncond_x = None
        if guidance_alpha > 0:
            anchor = uncond_anchor or ["<lin>"]
            bare = [self.bos] + [stoi[t] for t in anchor if t in stoi]
            uncond_x = torch.tensor([bare] * n, dtype=torch.long, device=device)

        # aligned with x = [bos] + prompt_ids: prompt positions carry id 0
        ss_rows = None
        track: list[int] = []
        if ss_track is not None:
            ss_rows = torch.zeros(
                n, 1 + len(prompt_ids), dtype=torch.long, device=device)
            track = [int(v) for v in ss_track]

        for _ in range(max(4, max_len - len(prompt_ids))):
            if ss_rows is not None:
                logits = self.gpt(
                    inputs_embeds=self._embed(x, ss_rows)
                ).logits[:, -1].float() / max(temperature, 1e-4)
            else:
                logits = self.gpt(x).logits[:, -1].float() / max(temperature, 1e-4)
            if uncond_x is not None:
                u_logits = self.gpt(uncond_x).logits[:, -1].float() / max(temperature, 1e-4)
                logits = (1.0 + guidance_alpha) * logits - guidance_alpha * u_logits
            if len(banned):
                logits.index_fill_(1, banned, float("-inf"))
            if constraints is not None:
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
            if ss_rows is not None:
                # left-shift: the SS appended at position j is the label
                # for the residue about to be generated (r+1), not the one
                # just emitted (r)
                ss_next = torch.tensor(
                    [track[r + 1] if r + 1 < len(track) else 0 for r in res_emitted],
                    dtype=torch.long, device=device).clamp(0, 3)
                ss_rows = torch.cat([ss_rows, ss_next.unsqueeze(1)], dim=1)
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
        out = rows if return_tokens else ["".join(r) for r in rows]
        if return_logprobs:
            norm_lp = (gen_logprob / gen_len.clamp_min(1)).tolist()
            return out, norm_lp
        return out
