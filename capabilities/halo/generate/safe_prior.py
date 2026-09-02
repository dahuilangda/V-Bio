"""SAFE representation: vocabulary, pretraining and sampling helpers.

Why SAFE (literature-validated):
  * fragments are separated by '.' and attachment points are encoded as ring
    closure numbers -> any fragment-boundary truncation decodes to a valid
    molecule and PRESERVES the scaffold by construction (~100% vs our
    measured 10% with raw-SMILES prefix continuation);
  * scaffold decoration benchmarks (SAFE-GPT, Digital Discovery 2024) reach
    validity 1.0 with this exact recipe.

Pipeline: SAFE corpus (1.7M) -> BPE vocabulary (HF tokenizers, SAFE-style
~1.1k merges) -> GPT-2 multitask training
  T1 unconditional SAFE strings
  T2 prefix continuation truncated at FRAGMENT boundaries only
  T3 core-masked hop translation (scaffold fragments -> <core>)
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import torch
import torch.nn.functional as F

HOP, CORE, CONT = "<hop>", "<core>", "<cont>"


def safe_lib_decode(frag: str):
    import safe as _s
    return _s.decode(frag, ignore_errors=True)
SPECIALS = ["<pad>", "<bos>", "<eos>", HOP, CORE, CONT]


def train_bpe(safe_strings: list[str], vocab_size: int = 1180, save_path: Path | None = None):
    from tokenizers import Tokenizer, models, pre_tokenizers, trainers

    tok = Tokenizer(models.BPE(unk_token=None))
    # SAFE strings are '.'-separated fragments; split on dots and whitespace,
    # let BPE learn sub-fragment tokens (matches the SAFE-GPT recipe)
    tok.pre_tokenizer = pre_tokenizers.Split(pattern=r"(\.)", behavior="isolated")
    trainer = trainers.BpeTrainer(
        vocab_size=vocab_size, special_tokens=SPECIALS,
        initial_alphabet=list("()#=%+-[]\\/1234567890@."),
        show_progress=False,
    )
    tok.train_from_iterator(safe_strings, trainer)
    if save_path:
        tok.save(str(save_path))
    return tok


class SafeVocab:
    """Adapter exposing the SmilesVocab-like interface for SAFE+BPE."""

    def __init__(self, tokenizer):
        self.tok = tokenizer
        self.stoi = {}
        for i in range(tokenizer.get_vocab_size()):
            self.id_to_token = None
        vocab = tokenizer.get_vocab()
        self.stoi = dict(vocab)
        self.itos = {v: k for k, v in vocab.items()}

    def __len__(self):
        return self.tok.get_vocab_size()

    def encode_text(self, text: str) -> list[int]:
        ids = self.tok.encode(text).ids
        return [self.stoi["<bos>"]] + ids + [self.stoi["<eos>"]]

    def decode_ids(self, ids: list[int]) -> str:
        toks = [self.itos[i] for i in ids if i in self.itos]
        return "".join(t for t in toks if not (t.startswith("<") and t.endswith(">")))

    def decode(self, ids: list[int]) -> str:
        return self.decode_ids(ids)

    def encode(self, text: str) -> list[int]:
        return self.encode_text(text)

    def save(self, path):
        self.tok.save(str(path))

    @classmethod
    def load(cls, path):
        from tokenizers import Tokenizer

        return cls(Tokenizer.from_file(str(path)))


def fragment_prefix(safe_str: str, frac: float, rng: random.Random) -> tuple[str, int]:
    """Truncate a SAFE string at a FRAGMENT boundary (never inside one)."""
    frags = safe_str.split(".")
    k = max(1, int(len(frags) * frac))
    k = min(k, len(frags) - 1) if len(frags) > 1 else 1
    return ".".join(frags[:k]), k


def mask_core_fragments(safe_str: str, scaffold_frags: set[str]) -> str | None:
    """Replace fragments that belong to the Murcko scaffold with <core>."""
    frags = safe_str.split(".")
    out, masked = [], False
    for f in frags:
        if f in scaffold_frags:
            if out and out[-1] == CORE:
                continue
            out.append(CORE)
            masked = True
        else:
            out.append(f)
    return ".".join(out) if masked else None



def train(model, vocab: SafeVocab, items, *, epochs=1, batch_size=256, lr=4e-4,
             device="cuda", save_path=None, log=print, grad_accum=2, attach_weight=1.0,
             bf16: bool = True):
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01, betas=(0.9, 0.95))
    pad = vocab.stoi["<pad>"]
    n_val = max(500, len(items) // 200)
    val, train = items[:n_val], items[n_val:]
    import math

    steps = math.ceil(len(train) / (batch_size * grad_accum)) * epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=max(steps, 10), pct_start=0.04)

    # attachment decisions (digit-carrying tokens) are the bottleneck of
    # edit correctness; precompute their ids once for loss upweighting
    _w_vec = None

    def _attach_w(attach_weight):
        nonlocal _w_vec
        if _w_vec is None:
            ids = sorted(vocab.stoi.values())
            import numpy as _np

            wv = _np.ones(max(ids) + 1, dtype=_np.float32)
            for t, i in vocab.stoi.items():
                if any(c.isdigit() for c in t):
                    wv[i] = attach_weight
            _w_vec = torch.from_numpy(wv).to(device)
        else:
            _w_vec[_w_vec > 1.0] = attach_weight
        return _w_vec

    def run(batch, attach_weight=1.0):
        L = max(len(x[0]) for x in batch)
        x = torch.full((len(batch), L), pad, dtype=torch.long)
        mask = torch.zeros(len(batch), L - 1, dtype=torch.bool)
        for i, (ids, ls) in enumerate(batch):
            x[i, : len(ids)] = torch.tensor(ids)
            mask[i, max(ls - 1, 0) : len(ids) - 1] = True
        x, mask = x.to(device), mask.to(device)
        from torch.amp import autocast as _ac

        with _ac("cuda", dtype=torch.bfloat16, enabled=bf16 and str(device).startswith("cuda")):
            logits = model.gpt(x[:, :-1]).logits
        lp = F.log_softmax(logits.float(), -1)
        tok = lp.gather(-1, x[:, 1:].unsqueeze(-1)).squeeze(-1)
        m = mask[:, : tok.shape[1]]
        if m.sum() == 0:
            return None
        if attach_weight != 1.0:
            w = _attach_w(attach_weight)[x[:, 1:]].clamp(min=1.0)[:, : tok.shape[1]]
            denom = (w * m.float()).sum().clamp_min(1)
            return -((tok * w * m.float()).sum() / denom)
        return -(tok[m]).mean()

    from halo.generate.grpo import GRPOUpdater  # noqa: F401 (import check)
    step = 0
    rng = random.Random(1)
    for ep in range(epochs):
        model.train()
        rng.shuffle(train)
        tot, nb = 0.0, 0
        opt.zero_grad()
        for i in range(0, len(train), batch_size):
            b = train[i : i + batch_size]
            if len(b) < 4:
                continue
            loss = run(b, attach_weight)
            if loss is None:
                continue
            (loss / grad_accum).backward()
            if (i // batch_size + 1) % grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                for g in opt.param_groups:
                    g["lr"] = sched.get_last_lr()[0]
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
            tot += float(loss.detach())
            nb += 1
            if nb % 2000 == 0:
                log(f"[safe] step {step} running-train {tot/nb:.4f}")
                import sys as _sys
                _sys.stdout.flush()
        model.eval()
        with torch.no_grad():
            vt, vn = 0.0, 0
            for j in range(0, len(val), batch_size):
                vb = val[j : j + batch_size]
                if len(vb) < 2:
                    continue
                vl = run(vb)
                if vl is not None:
                    vt += float(vl)
                    vn += 1
        log(f"[safe] epoch {ep} train {tot/max(nb,1):.4f} val {vt/max(vn,1):.4f} steps {step}")
        import sys as _sys
        _sys.stdout.flush()
        if save_path:
            torch.save(model.state_dict(), save_path)  # checkpoint every epoch
            torch.save(model.state_dict(), f"{save_path}.ep{ep}")  # per-epoch snapshot
    return {"val": vt / max(vn, 1)}


_DIGIT_SPLIT = re.compile(r"(%\d{2,3}|\d)")


def digit_split_words(s: str) -> list[str]:
    """Split a SAFE string into words so that ring digits stand alone and
    atom runs stay mergeable: 'N14CCOCC1' -> N|1|4|CCOCC|1."""
    return [p for p in _DIGIT_SPLIT.split(s) if p]


class DigitBPEVocab:
    """Digit-isolated vocabulary with in-word longest-match encoding.

    Words come from digit_split_words: ring digits stand alone, chemistry
    runs stay whole. Chemistry tokens are BPE-mined but constrained to be
    digit-free; encoding is greedy longest-match per word with single-char
    fallback - fully deterministic, no tokenizer-library pre-tokenization
    involved (that path proved unreliable in tokenizers 0.22)."""

    def __init__(self, tokens: list[str]):
        self._tokens = tokens
        self.stoi = {t: i for i, t in enumerate(tokens)}
        self.itos = {i: t for t, i in self.stoi.items()}
        self._by_len: dict[int, set[str]] = {}
        for t in tokens:
            self._by_len.setdefault(len(t), set()).add(t)
        self._max_len = max(self._by_len) if self._by_len else 1

    def __len__(self):
        return len(self._tokens)

    def _encode_word(self, w: str) -> list[int]:
        out, i = [], 0
        while i < len(w):
            for L in range(min(self._max_len, len(w) - i), 0, -1):
                t = w[i:i + L]
                if t in self.stoi:
                    out.append(self.stoi[t])
                    i += L
                    break
            else:
                raise KeyError(f"unit out of vocabulary: {w[i]!r} in {w!r}")
        return out

    def encode_text(self, text: str) -> list[int]:
        ids: list[int] = [self.stoi["<bos>"]]
        for w in digit_split_words(text):
            ids.extend(self._encode_word(w))
        ids.append(self.stoi["<eos>"])
        return ids

    def encode(self, text: str) -> list[int]:
        return self.encode_text(text)

    def decode_ids(self, ids: list[int]) -> str:
        return "".join(self.itos[i] for i in ids
                       if i in self.itos and not (self.itos[i].startswith("<") and self.itos[i].endswith(">")))

    def decode(self, ids: list[int]) -> str:
        return self.decode_ids(ids)

    class _TokAdapter:
        def __init__(self, owner):
            self._o = owner

        def encode(self, text):
            ids: list[int] = []
            for w in digit_split_words(text):
                ids.extend(self._o._encode_word(w))
            return type("Enc", (), {"ids": ids})()

    @property
    def tok(self):
        if not hasattr(self, "_tok_adapter"):
            self._tok_adapter = DigitBPEVocab._TokAdapter(self)
        return self._tok_adapter

    def save(self, path):
        from pathlib import Path as _P

        _P(path).write_text(json.dumps(self._tokens))

    @classmethod
    def load(cls, path):
        from pathlib import Path as _P

        return cls(json.loads(_P(path).read_text()))

    @classmethod
    def load_extended(cls, path, extra_tokens: list[str]):
        """Vocab plus appended special tokens (e.g. '<hop>' as the f-RAG
        retrieval separator). New ids continue after the original table so
        checkpoints load by row-prefix copy."""
        from pathlib import Path as _P

        tokens = json.loads(_P(path).read_text())
        for t in extra_tokens:
            if t not in tokens:
                tokens.append(t)
        return cls(tokens)


def load_prior_extended(prior_dir, extra_tokens: list[str], device="cpu"):
    """GPT2Prior from a checkpoint dir with an EXTENDED vocabulary: the
    embedding/lm_head matrices grow by len(extra_tokens) rows; old rows
    copy the trained weights, new rows keep their fresh initialization for
    fine-tuning to learn. Returns (model, vocab)."""
    import json as _json
    import torch as _torch
    from pathlib import Path as _P

    prior_dir = _P(prior_dir)
    vocab = DigitBPEVocab.load_extended(prior_dir / 'digit_bpe_tokens.json', extra_tokens)
    meta = _json.loads((prior_dir / 'model_meta.json').read_text())
    from halo.generate.gpt2_prior import GPT2Prior

    model = GPT2Prior(vocab, d_model=meta['d_model'], n_layers=meta['n_layers'],
                      n_heads=meta['n_heads'], max_len=meta['max_len'])
    sd = _torch.load(prior_dir / 'prior.pt', map_location='cpu', weights_only=True)
    old_n = sd['gpt.transformer.wte.weight'].size(0)
    if old_n != len(vocab):
        wte = model.gpt.transformer.wte.weight.data
        wte[:old_n] = sd['gpt.transformer.wte.weight']
        sd['gpt.transformer.wte.weight'] = wte
        if 'gpt.lm_head.weight' in sd:
            lm = model.gpt.lm_head.weight.data
            lm[:old_n] = sd['gpt.lm_head.weight']
            sd['gpt.lm_head.weight'] = lm
    model.load_state_dict(sd)
    return model.to(device), vocab
