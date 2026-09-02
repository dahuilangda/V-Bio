"""Modern backbone prior: Llama-style decoder (RoPE + SwiGLU + RMSNorm)
with auxiliary property heads.

Architecture (Llama-style decoder), each component with a
literature/engineering basis:
  * rotary position embeddings — length generalization beyond the training
    window (Su et al. 2021); standard in every 2023+ protein/code LLM
  * SwiGLU FFN + RMSNorm, pre-norm — Shazeer 2020 / Zhang & Sennrich
  * auxiliary property regression heads (sol / syn / liability) on the
    mean-pooled final state — multi-task LM pretraining the ESM/ProtTrans
    way: the representation is shaped by the properties we condition on,
    which sharpens tag-conditioned generation
  * dynamic vocabulary extension (resize_token_embeddings) so users can add
    arbitrary non-natural residues at runtime (HF standard mechanism)

The sampling/inference interface is identical to GPT2Prior (sample,
sample_with_prompt with FIM prompts, placement masks, length control,
classifier-free guidance) — the Tier-2 loop is backbone-agnostic.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from peplm.models.gpt2 import PlacementMask  # noqa: F401 (re-export)
from peplm.vocab import Vocab


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

    # ---------------------------------------------------------------- core
    def forward(self, x):
        return self.gpt(x).logits

    def _token_logprobs(self, x: torch.Tensor) -> torch.Tensor:
        att = (~x.eq(self.pad)).long()
        out = self.gpt(x[:, :-1], attention_mask=att[:, :-1], output_hidden_states=True)
        logits = out.logits
        lp = F.log_softmax(logits.float(), dim=-1)
        tgt = x[:, 1:]
        tok = lp.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        return tok * (~tgt.eq(self.pad))

    def _hidden(self, x: torch.Tensor):
        att = (~x.eq(self.pad)).long()
        out = self.gpt(x, attention_mask=att, output_hidden_states=True)
        return out.hidden_states[-1]

    def log_probs(self, x: torch.Tensor) -> torch.Tensor:
        return self._token_logprobs(x).sum(-1)

    # ------------------------------------------------------- dynamic vocab
    def extend_vocab(self, new_tokens: list[str]) -> list[str]:
        """Add arbitrary residue tokens (e.g. user NCAAs '[XYZ]') at runtime.
        New embeddings are initialized at the mean of existing residue
        embeddings (standard HF resize + a sane init for rare tokens)."""
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
    ):
        """Identical contract to GPT2Prior.sample_with_prompt (see that
        docstring): FIM-aware continuation with placement legality, min/max
        length as decoder guarantees, optional classifier-free guidance, and
        the decode-time constraint plan (upgrade 3)."""
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
        out = rows if return_tokens else ["".join(r) for r in rows]
        if return_logprobs:
            norm_lp = (gen_logprob / gen_len.clamp_min(1)).tolist()
            return out, norm_lp
        return out
