"""Receptor-conditioned peptide prior: PepMLM-650M (the fine-tuned
ESM-2 650M, Nat Biotech 2025). Target-conditioned binder generation via
masked infilling (receptor + <mask>*N, one-shot parallel top-k=3
decoding), pseudo-perplexity ranking, and re-infill refinement of
elites."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import torch
import torch.nn.functional as F

PEPTIDELM_ROOT = Path(__file__).resolve().parents[2]
PEPMLM650_DIR = PEPTIDELM_ROOT / "models/external/pepmlm650"

AA_TOKENS = set("ACDEFGHIKLMNPQRSTVWY")


class _HFAlphabet:
    """fair-esm-style alphabet face over an HF EsmTokenizer."""

    def __init__(self, tok):
        self.tok = tok
        self.cls_idx = tok.cls_token_id
        self.eos_idx = tok.eos_token_id
        self.mask_idx = tok.mask_token_id
        self.padding_idx = tok.pad_token_id
        self.tok_to_idx = tok.get_vocab()

    def get_idx(self, c):
        return self.tok.convert_tokens_to_ids(c)

    def get_tok(self, i):
        return self.tok.convert_ids_to_tokens(int(i))


class _HFModel:
    """Call face for EsmForMaskedLM: model(ids) -> {"logits": ...}."""

    def __init__(self, hf_model):
        self.model = hf_model

    def __call__(self, ids):
        return {"logits": self.model(input_ids=ids).logits}


def load_pepmlm_prior(
    checkpoint: Optional[Path] = None,
    device: str = "cpu",
):
    """Load PepMLM-650M from the local HF checkpoint directory."""
    import os
    from transformers import AutoModelForMaskedLM, EsmTokenizer

    path = Path(checkpoint) if checkpoint else PEPMLM650_DIR
    if not (path / "pytorch_model.bin").exists():
        raise FileNotFoundError(
            f"PepMLM-650M weights not found in {path}. Place the HF "
            "checkpoint files (config.json, pytorch_model.bin, tokenizer) "
            "there — the design pipeline has no substitute for this model.")
    # local-only: never round-trip to the hub from the design pipeline
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model = AutoModelForMaskedLM.from_pretrained(str(path), local_files_only=True)
    model.eval().to(device)
    tok = EsmTokenizer.from_pretrained(str(path), local_files_only=True)
    print(f"[pepmlm-650M] loaded from {path}")
    return PepMLMPrior(_HFModel(model), _HFAlphabet(tok), device)


class PepMLMPrior(torch.nn.Module):
    """Masked-infilling prior with the shared V-Bio sampling surface."""

    def __init__(self, model, alphabet, device="cpu"):
        super().__init__()
        self.esm = model
        self.alphabet = alphabet
        self.device = device
        self.receptor: Optional[str] = None
        import random as _random
        self._rng = _random.Random()
        self._aa_ids = torch.tensor(
            [alphabet.get_idx(c) for c in sorted(AA_TOKENS)], device=device)

    def _aa_mask(self, exclude=None):
        """Logit mask: -inf everywhere except the natural residues, minus
        the excluded set (user-blocked residues, and free Cys on designs
        whose topology has no structural role for free thiols)."""
        m = torch.full((len(self.alphabet.tok_to_idx),), float("-inf"),
                       device=self.device)
        m[self._aa_ids] = 0.0
        if exclude:
            for c in exclude:
                m[self.alphabet.get_idx(c)] = float("-inf")
        return m

    def set_receptor(self, sequence: str):
        self.receptor = sequence.upper()[:450]

    def sample_with_prompt(
        self, prompt, n, device, temperature=1.0,
        constraints=None, target_len=None, return_tokens=False,
        ss_track=None, target_prefix=None, receptor=None,
        exclude=None, **_kw,
    ):
        """Sample n binders: receptor + all-mask tail, one-shot parallel
        top-k=3 decoding at the masked positions."""
        rec = (receptor or self.receptor or "")[:450]
        if not rec:
            raise ValueError("PepMLMPrior requires a receptor sequence")
        length = max(5, min(target_len or 12, 50))

        mask_idx = self.alphabet.mask_idx
        rec_ids = [self.alphabet.get_idx(c) for c in rec]
        pep_start = 1 + len(rec)
        base = [self.alphabet.cls_idx] + rec_ids + [mask_idx] * length + [self.alphabet.eos_idx]
        ids = torch.tensor([list(base) for _ in range(n)], device=self.device)

        aa_mask = self._aa_mask(exclude)
        temp = max(float(temperature), 1e-4)
        with torch.no_grad():
            logits = self.esm(ids)["logits"][:, pep_start:pep_start + length, :].float()
            lp = F.log_softmax((logits + aa_mask) / temp, dim=-1)
            topk_lp, topk_idx = lp.topk(3, dim=-1)
            p3 = F.softmax(topk_lp, dim=-1)
            sel = torch.multinomial(p3.reshape(-1, 3), 1).reshape(n, length)
            chosen = topk_idx.gather(-1, sel.unsqueeze(-1)).squeeze(-1)
            rows = torch.arange(n, device=self.device).unsqueeze(1).expand(n, length)
            cols = torch.arange(length, device=self.device).unsqueeze(0).expand(n, length)
            ids[rows, pep_start + cols] = chosen

        results = []
        for i in range(n):
            results.append([self.alphabet.get_tok(ids[i, pep_start + j].item())
                            for j in range(length)])
        return results if return_tokens else [''.join(r) for r in results]

    def refine_with_prompt(
        self, sequence, n, device, n_mutations=None, temperature=1.0,
        exclude=None, receptor=None, return_tokens=False,
        focus_positions=None, **_kw,
    ):
        """Model-guided mutation of one candidate: mask a few residues of
        an otherwise-complete binder and re-infill, so variants stay in
        the parent's neighborhood.

        focus_positions: residue indices the oracle scored poorly (e.g.
        low per-residue pLDDT); masking is biased toward them so the
        weakest parts of the binder are repaired first."""
        rec = (receptor or self.receptor or "")[:450]
        seq = sequence.upper()[:50]
        if not rec or not seq or not set(seq) <= AA_TOKENS:
            raise ValueError("refine requires a receptor and a clean sequence")
        length = len(seq)
        if n_mutations is None:
            n_mutations = max(2, min(5, round(length * 0.25)))
        n_mutations = max(1, min(n_mutations, length - 1))

        mask_idx = self.alphabet.mask_idx
        rec_ids = [self.alphabet.get_idx(c) for c in rec]
        pep_start = 1 + len(rec)
        base = ([self.alphabet.cls_idx] + rec_ids
                + [self.alphabet.get_idx(c) for c in seq]
                + [self.alphabet.eos_idx])
        # each row masks a subset of the peptide, biased toward the focus
        # positions; the rest random
        focus = [i for i in {int(p) for p in (focus_positions or [])}
                 if 0 <= i < length]
        rows = [list(base) for _ in range(n)]
        for r in range(n):
            pos: list[int] = []
            if focus and self._rng.random() < 0.7:
                k_focus = min(len(focus), max(1, n_mutations // 2))
                pos = list(self._rng.sample(focus, k_focus))
            while len(pos) < n_mutations:
                cand = int(torch.randint(length, (1,)).item())
                if cand not in pos:
                    pos.append(cand)
            for p in pos:
                rows[r][pep_start + p] = mask_idx
        ids = torch.tensor(rows, device=self.device)

        aa_mask = self._aa_mask(exclude)
        temp = max(float(temperature), 1e-4)
        with torch.no_grad():
            logits = self.esm(ids)["logits"][:, pep_start:pep_start + length, :].float()
        lp = F.log_softmax((logits + aa_mask) / temp, dim=-1)
        topk_lp, topk_idx = lp.topk(3, dim=-1)
        p3 = F.softmax(topk_lp, dim=-1)
        sel = torch.multinomial(p3.reshape(-1, 3), 1).reshape(n, length)

        out = []
        for r in range(n):
            res = list(seq)
            for j in range(length):
                if ids[r, pep_start + j].item() == mask_idx:
                    tok = self.alphabet.get_tok(
                        topk_idx[r, j, sel[r, j]].item())
                    if tok in AA_TOKENS and tok not in (exclude or ()):
                        res[j] = tok
            # count REAL substitutions: a re-predicted original residue
            # is a no-op that would collide with the parent in dedup
            if sum(a != b for a, b in zip(res, seq)):
                out.append(res)
        return out if return_tokens else [''.join(r) for r in out]

    def pseudo_perplexity(self, sequence, receptor=None):
        """PepMLM pseudo-perplexity, vectorized: one row per masked
        peptide position, a single batched forward pass per sequence."""
        rec = (receptor or self.receptor or "")[:450]
        seq = sequence.upper()[:50]
        if not rec or not seq or not set(seq) <= AA_TOKENS:
            return float("inf")

        rec_ids = [self.alphabet.get_idx(c) for c in rec]
        pep_ids = [self.alphabet.get_idx(c) for c in seq]
        mask_idx = self.alphabet.mask_idx
        base = [self.alphabet.cls_idx] + rec_ids + pep_ids + [self.alphabet.eos_idx]
        pep_start = 1 + len(rec)
        n = len(seq)

        batch = []
        for i in range(n):
            tok = list(base)
            tok[pep_start + i] = mask_idx
            batch.append(tok)
        ids = torch.tensor(batch, device=self.device)
        with torch.no_grad():
            logits = self.esm(ids)["logits"]  # [n, L, vocab]

        total_nll = 0.0
        for i in range(n):
            lg = logits[i, pep_start + i].float()
            total_nll -= float(F.log_softmax(lg, dim=-1)[pep_ids[i]])
        return math.exp(total_nll / max(n, 1))
