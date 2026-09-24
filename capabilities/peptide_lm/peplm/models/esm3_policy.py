"""ESM3-open 1.4B trainable masked-LM policy for peptide binder design.

Pitfalls: never pass ``sequence_id`` (it block-diagonalises attention and
kills receptor conditioning); the open checkpoint is per-protein
pretrained, so receptor-conditioned infilling needs a binder-pair SFT
warm-up before RL."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F

PEPTIDELM_ROOT = Path(__file__).resolve().parents[2]
ESM3_WEIGHTS = PEPTIDELM_ROOT / "models/external/esm3_sm_open_v1.pth"

AA20 = set("ACDEFGHIKLMNPQRSTVWY")


def _greedy_dpp_select(
    quality: np.ndarray,
    sim: np.ndarray,
    k: int,
) -> list[int]:
    """Greedy MAP inference over L = Diag(q) . S . Diag(q).

    quality: [n] nonnegative; sim: [n, n] in [0, 1]. Greedy conditional
    gain = q_i^2 * (1 - max similarity to selected). O(k n^2).
    """
    n = len(quality)
    if k >= n:
        return list(range(n))
    selected: list[int] = []
    taken_mask = np.zeros(n, dtype=bool)
    max_sim = np.zeros(n)
    for _ in range(k):
        gain = quality * quality * (1.0 - max_sim)
        gain[taken_mask] = -1.0
        j = int(np.argmax(gain))
        selected.append(j)
        taken_mask[j] = True
        max_sim = np.maximum(max_sim, sim[j])
    return selected


@dataclass
class Trajectory:
    """One sampled peptide with its unmasking trajectory (for d2-GRPO).

    states[t]: the token tensor fed to the model at unmasking step t
    (includes prompt; first state is the fully-masked prompt).
    revealed[t]: (position, token) unmasked by step t's sampling.
    old_logprob[t]: log pi_old(token | states[t]) at the revealed position,
    recorded under no_grad at sampling time.
    """
    peptide_tokens: list[int]
    states: list[torch.Tensor] = field(default_factory=list)
    revealed: list[tuple[int, int]] = field(default_factory=list)
    old_logprob: list[float] = field(default_factory=list)
    # per-step sampling temperature: PPO needs new/old logprobs from the
    # same tempered family, so training re-tempers with these
    temps: list[float] = field(default_factory=list)
    mean_logprob: float = 0.0


class ESM3Policy(torch.nn.Module):
    """LoRA-adapted ESM3 masked-LM proposal policy."""

    def __init__(
        self,
        checkpoint: Optional[Path] = None,
        device: str = "cuda",
        lora_r: int = 32,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        dtype: torch.dtype = torch.bfloat16,
    ):
        super().__init__()
        from esm.models.esm3 import ESM3
        from esm.tokenization import get_esm3_model_tokenizers
        from esm.utils.constants.models import ESM3_OPEN_SMALL

        ckpt = Path(checkpoint) if checkpoint else ESM3_WEIGHTS
        if not ckpt.exists():
            raise FileNotFoundError(
                f"ESM3 weights not found: {ckpt}. Provision "
                "models/external/esm3_sm_open_v1.pth first (see "
                "models/MANIFEST.json).")
        self.toks = get_esm3_model_tokenizers(ESM3_OPEN_SMALL)
        base = ESM3(
            d_model=1536, n_heads=24, v_heads=256, n_layers=48,
            structure_encoder_fn=lambda d: (_ for _ in ()).throw(
                RuntimeError("structure track unused on this path")),
            structure_decoder_fn=lambda d: (_ for _ in ()).throw(
                RuntimeError("structure track unused on this path")),
            function_decoder_fn=lambda d: (_ for _ in ()).throw(
                RuntimeError("function track unused on this path")),
            tokenizers=self.toks,
        )
        state = torch.load(str(ckpt), map_location="cpu")
        if any(str(k).startswith("esm.") for k in state):
            state = {str(k).split("esm.", 1)[1]: v for k, v in state.items()}
        missing, unexpected = base.load_state_dict(state, strict=False,
                                                   assign=True)
        # the lazy structure/function decoders are legitimately absent
        assert not unexpected, f"unexpected checkpoint keys: {unexpected[:4]}"
        assert all("decoder" in k or "encoder" in k for k in missing), missing[:4]
        self.base = base.to(device=device, dtype=dtype).eval()
        self.device = device
        self.dtype = dtype

        from peft import LoraConfig, get_peft_model
        # single-string regex: peft matches list targets by suffix only
        lora = LoraConfig(
            r=lora_r, lora_alpha=lora_alpha, lora_dropout=lora_dropout,
            bias="none", target_modules=(
                r".*\.(attn\.layernorm_qkv\.1|attn\.out_proj|ffn\.1|ffn\.3)$"),
            # no task_type: ESM3 is not a HF model; CAUSAL_LM plumbing
            # demands prepare_inputs_for_generation
        )
        self.model = get_peft_model(self.base, lora)
        # sequence vocab: ids 4..23 are the canonical amino acids
        seq_vocab = self.toks.sequence.vocab
        self.aa_ids = sorted(v for t, v in seq_vocab.items() if t in AA20)
        self.mask_id = self.toks.sequence.mask_token_id       # <mask> = 32
        self.chainbreak_id = seq_vocab["|"]                    # 31
        self.pad_id = seq_vocab.get("<pad>", 1)

    # encoding
    def encode_context(self, receptor: str, pep_len: int) -> torch.Tensor:
        """[1, L] token ids: cls + receptor + mask*pep_len + eos.

        No chainbreak: ESM3-open is single-chain pretrained (the '|'
        token is out-of-distribution), so receptor and peptide ride one
        continuous sequence — the PepMLM conditioning format."""
        rec_ids = [self.toks.sequence.vocab[c] for c in receptor.upper()]
        ids = ([self.toks.sequence.vocab["<cls>"]] + rec_ids
               + [self.mask_id] * pep_len
               + [self.toks.sequence.vocab["<eos>"]])  # cls=0, eos=2
        return torch.tensor([ids], device=self.device)

    def peptide_start(self, receptor: str) -> int:
        """Absolute index of the first peptide token: cls + receptor."""
        return 1 + len(receptor)

    def _forward_sequence_logits(self, tokens: torch.Tensor) -> torch.Tensor:
        """Sequence-head-only forward under bf16 autocast (bf16 weights
        reject fp32 activations); skipping the structure/function heads
        roughly halves the per-step cost."""
        with torch.autocast("cuda", dtype=self.dtype):
            out = self.model(sequence_tokens=tokens)
        return out.sequence_logits

    # sampling
    @torch.no_grad()
    def sample_batch(
        self,
        receptor: str,
        pep_len: int,
        n: int,
        keep: int,
        temperature: float = 0.9,
        num_steps: int = 0,
        strategy: str = "random",
        top_k: int = 20,
    ) -> list[Trajectory]:
        """Sample ``n`` peptides, return the DPP-best ``keep``.

        num_steps=0 -> one-shot parallel decoding; num_steps>0 -> MaskGIT
        iterative unmasking with per-step trajectory recording
        (``strategy='entropy'`` unmasks most-confident-first). Diversity:
        greedy DPP over quality (mean token logprob) x similarity
        (1 - sequence identity).
        """
        ctx = self.encode_context(receptor, pep_len)  # [1, L]
        L = ctx.shape[1]
        pep_start = L - pep_len - 1  # positions of the mask block (0-based)

        toks = ctx.repeat(n, 1).clone()
        if num_steps <= 0:
            logits = self._forward_sequence_logits(toks).float()
            lp = F.log_softmax(logits[:, pep_start:pep_start + pep_len, :],
                               dim=-1)
            aa_lp = lp[..., self.aa_ids]  # [n, pep, 20]
            # top-k, not top-3: the SFT-grounded conditional is near-uniform
            # per position (entropy near ln 20), where top-3 collapses
            # to the tilt-top tokens (G/S/C) at every position
            k = max(1, min(int(top_k), len(self.aa_ids)))
            top_lp, top_idx = aa_lp.topk(k, dim=-1)
            probs = F.softmax(top_lp / max(temperature, 1e-3), dim=-1)
            sel3 = torch.multinomial(
                probs.reshape(-1, k), 1).reshape(n, pep_len)
            sel = top_idx.gather(-1, sel3.unsqueeze(-1)).squeeze(-1)
            tok_ids = torch.tensor(self.aa_ids, device=self.device)[sel]
            toks[:, pep_start:pep_start + pep_len] = tok_ids
            chosen_lp = top_lp.gather(-1, sel3.unsqueeze(-1)).squeeze(-1)
            traj_mean = chosen_lp.mean(dim=1)
            trajs = [Trajectory(peptide_tokens=toks[i, pep_start:
                                                    pep_start + pep_len]
                                .tolist(),
                                mean_logprob=float(traj_mean[i]))
                     for i in range(n)]
        else:
            trajs = self._iterative_unmask(toks, pep_start, pep_len, n,
                                           num_steps, temperature, strategy,
                                           top_k=top_k)

        return self._dpp_filter(trajs, keep)

    def _iterative_unmask(
        self, toks: torch.Tensor, pep_start: int, pep_len: int, n: int,
        num_steps: int, temperature: float, strategy: str, top_k: int = 20,
    ) -> list[Trajectory]:
        """MaskGIT loop with trajectory recording (per-step likelihoods
        for GRPO): random or entropy (most-confident-first) unmask
        ordering, temperature annealing."""
        trajs = [Trajectory(peptide_tokens=[], states=[], revealed=[],
                            old_logprob=[]) for _ in range(n)]
        remaining = torch.ones(n, pep_len, dtype=torch.bool,
                               device=self.device)
        per_step = max(1, math.ceil(pep_len / num_steps))

        for step in range(num_steps):
            raw = self._forward_sequence_logits(toks).float()
            # ordering confidence from the plain AA log-softmax
            lp = F.log_softmax(raw[:, pep_start:pep_start + pep_len, :],
                               dim=-1)
            aa_lp = lp[..., self.aa_ids]
            conf, aa_sel = aa_lp.max(dim=-1)          # [n, pep]
            # linear anneal only: the squared schedule drives late steps to
            # effective argmax, freezing late positions onto the tilt-top
            t_anneal = max(temperature * (1 - step / num_steps), 1e-2)

            if not bool(remaining.any()):
                break
            # rank remaining positions only: unmasked slots must not
            # waste unmask-budget slots
            masked_conf = conf.masked_fill(~remaining, float("-inf"))
            order = (masked_conf.argsort(dim=-1, descending=True)
                     if strategy == "entropy"
                     else torch.argsort(
                         torch.rand(n, pep_len, device=self.device)
                         .masked_fill(~remaining, 2.0)))
            k = min(per_step,
                    int(remaining.sum(dim=1).min().item()))
            keep_mask = torch.zeros_like(remaining)
            rank = torch.arange(pep_len, device=self.device).expand(n, pep_len)
            keep_mask.scatter_(-1, order, rank < k)
            commit = keep_mask & remaining

            # tempered AA-restricted sampling on raw logits — the exact
            # family the GRPO updater re-computes
            aa_mask = torch.full(
                (1, 1, raw.shape[-1]), float("-inf"),
                device=raw.device, dtype=raw.dtype)
            aa_mask[..., self.aa_ids] = 0.0
            sampled_lp = F.log_softmax(
                (raw[:, pep_start:pep_start + pep_len, :] + aa_mask)
                / max(t_anneal, 1e-3), dim=-1)
            k_i = max(1, min(int(top_k), len(self.aa_ids)))
            top_lp, top_idx = sampled_lp.topk(k_i, dim=-1)
            p3 = F.softmax(top_lp, dim=-1)
            sel3 = torch.multinomial(p3.reshape(-1, k_i), 1).reshape(n, pep_len)
            chosen = top_idx.gather(-1, sel3.unsqueeze(-1)).squeeze(-1)
            chosen_lp = top_lp.gather(-1, sel3.unsqueeze(-1)).squeeze(-1)

            rows, cols = commit.nonzero(as_tuple=True)
            if len(rows) == 0:
                break
            # snapshot the PRE-REVEAL state first: the d2 ratio is
            # pi(token | x_{t-1}) — a post-reveal state leaks the answer
            pre_states = {int(r): toks[r].clone()
                          for r in rows.tolist()}
            toks[rows, pep_start + cols] = chosen[rows, cols]
            for r, c in zip(rows.tolist(), cols.tolist()):
                trajs[r].states.append(pre_states[r])
                # absolute position: the updater gathers logits on
                # full-sequence state tensors
                trajs[r].revealed.append((int(pep_start + c),
                                          int(chosen[r, c])))
                trajs[r].old_logprob.append(float(chosen_lp[r, c]))
                trajs[r].temps.append(float(t_anneal))
            remaining &= ~commit

        for i, t in enumerate(trajs):
            # rebuild in position order from the final state
            t.peptide_tokens = (
                toks[i, pep_start:pep_start + pep_len].tolist())
            t.mean_logprob = (sum(t.old_logprob) / len(t.old_logprob)
                              if t.old_logprob else 0.0)
        return trajs

    def _dpp_filter(self, trajs: list[Trajectory], keep: int
                    ) -> list[Trajectory]:
        if keep >= len(trajs):
            return trajs
        seqs = ["".join(self.toks.sequence.decode_ids(
            t.peptide_tokens)) if hasattr(self.toks.sequence, "decode_ids")
            else None for t in trajs]
        if seqs[0] is None:  # decode via vocab inverse
            inv = {v: k for k, v in self.toks.sequence.vocab.items()}
            seqs = ["".join(inv[t] for t in tr.peptide_tokens)
                    for tr in trajs]
        n = len(trajs)
        # identity similarity matrix over peptide spans
        min_len = min(len(s) for s in seqs)
        arr = np.array([[ord(c) for c in s[:min_len]] for s in seqs])
        sim = (arr[:, None, :] == arr[None, :, :]).mean(axis=-1)
        quality = np.array([t.mean_logprob for t in trajs])
        quality = np.exp(quality - quality.max())  # nonnegative, bounded
        pick = _greedy_dpp_select(quality, sim, keep)
        return [trajs[i] for i in sorted(pick)]

    # refill
    @staticmethod
    def weak_positions(credit, max_frac: float = 0.4,
                       min_positions: int = 1) -> list[int]:
        """Positions to re-mask: the worst ``max_frac`` of the credit
        vector. An absolute pLDDT threshold would mark every position weak
        early in training (de novo peptides score ~30 everywhere) and
        degenerate the refill into de novo sampling."""
        n = len(credit)
        if n == 0:
            return []
        k = max(min_positions, int(round(max_frac * n)))
        k = min(k, n - 1) if n > 1 else n
        order = sorted(range(n), key=lambda i: -float(credit[i]))
        return sorted(order[:k])

    @torch.no_grad()
    def refill(
        self,
        receptor: str,
        parent_tokens: list[int],
        remask_positions: list[int],
        num_steps: int = 4,
        temperature: float = 0.7,
        top_k: int = 20,
    ) -> Optional[Trajectory]:
        """Resample only the weak positions; confident positions stay as
        observed context. Returns None when nothing changed."""
        pep_len = len(parent_tokens)
        ctx = self.encode_context(receptor, pep_len)
        L = ctx.shape[1]
        pep_start = L - pep_len - 1
        toks = ctx.clone()
        for j, tok in enumerate(parent_tokens):
            toks[0, pep_start + j] = tok
        for j in remask_positions:
            toks[0, pep_start + j] = self.mask_id
        traj = Trajectory(peptide_tokens=[], states=[], revealed=[],
                          old_logprob=[], temps=[])
        remaining_n = len(remask_positions)
        if remaining_n == 0:
            return None
        per_step = max(1, math.ceil(remaining_n / num_steps))

        for step in range(num_steps):
            logits = self._forward_sequence_logits(toks).float()
            lp = F.log_softmax(logits[0], dim=-1)  # [L, V]
            t_anneal = max(temperature * (1 - step / num_steps), 1e-3) ** 2
            open_pos = [j for j in remask_positions
                        if toks[0, pep_start + j].item() == self.mask_id]
            if not open_pos:
                break
            take = open_pos[:per_step]
            for j in take:
                pre = toks[0].clone()
                pos_lp = lp[pep_start + j]
                aa_mask = torch.full_like(pos_lp, float("-inf"))
                aa_mask[self.aa_ids] = 0.0
                s = F.log_softmax((pos_lp + aa_mask) / t_anneal, dim=-1)
                tl, ti = s.topk(max(1, min(int(top_k), len(self.aa_ids))))
                p3 = F.softmax(tl, dim=-1)
                c = int(torch.multinomial(p3, 1))
                toks[0, pep_start + j] = ti[c]
                traj.states.append(pre)
                traj.revealed.append((pep_start + j, int(ti[c])))
                traj.old_logprob.append(float(tl[c]))
                traj.temps.append(float(t_anneal))
        new_pep = toks[0, pep_start:pep_start + pep_len].tolist()
        if new_pep == list(parent_tokens):
            return None
        traj.peptide_tokens = new_pep
        traj.mean_logprob = (sum(traj.old_logprob) / len(traj.old_logprob)
                             if traj.old_logprob else 0.0)
        return traj

    # training
    # -- Production integration helpers (ESM3Proposer interface) --

    def decode_tokens(self, token_ids: list[int]) -> str | None:
        """Decode sequence token IDs to a one-letter amino acid string."""
        vocab = self.toks.sequence.vocab
        inv = {v: k for k, v in vocab.items()}
        letters = []
        for tid in token_ids:
            ch = inv.get(tid, "")
            if len(ch) == 1 and ch in AA20:
                letters.append(ch)
            elif ch in ("<cls>", "<eos>", "<pad>", "|", "<mask>"):
                continue
            else:
                return None  # non-standard token → reject
        return "".join(letters) or None

    def encode_peptide(self, sequence: str, pep_len: int) -> list[int] | None:
        """Encode a peptide sequence to token IDs (padded to pep_len)."""
        sequence = sequence.upper()[:pep_len]
        if len(sequence) < pep_len:
            sequence = sequence + "G" * (pep_len - len(sequence))
        vocab = self.toks.sequence.vocab
        try:
            return [vocab[ch] for ch in sequence]
        except KeyError:
            return None

    def score_sequence(self, receptor: str, peptide: str) -> float | None:
        """Mean log-likelihood of the peptide tokens given the receptor."""
        import torch.nn.functional as Fn
        ctx = self.encode_context(receptor, len(peptide))
        pep_ids = self.encode_peptide(peptide, len(peptide))
        if pep_ids is None:
            return None
        pep_start = ctx.shape[1] - len(peptide) - 1
        toks = ctx.clone()
        for i, tid in enumerate(pep_ids):
            toks[0, pep_start + i] = tid
        with torch.no_grad():
            logits = self._forward_sequence_logits(toks)
            lp = Fn.log_softmax(logits[0, pep_start:pep_start + len(peptide), :], dim=-1)
            aa_lp = lp[:, self.aa_ids]
            # gather the actual token's logprob
            tok_tensor = torch.tensor(pep_ids, device=self.device)
            scores = aa_lp.gather(1, tok_tensor.unsqueeze(1)).squeeze(1)
            return float(scores.mean())

    def adapter_parameters(self):
        return [p for p in self.model.parameters() if p.requires_grad]

    @property
    def ref_enabled(self):
        return not self.model.disable_adapter.__self__.is_disabled \
            if hasattr(self.model, "disable_adapter") else True

    def save_adapter(self, path: Path):
        self.model.save_pretrained(str(path))

    def load_adapter(self, path: Path):
        """Load an adapter into the live PeftModel: a from_pretrained
        reload would build a wrapper the existing optimizer never sees."""
        from peft import load_peft_weights
        weights = load_peft_weights(str(path), device=str(self.device))
        # load into the default adapter slot and mark trainable
        from peft.utils import set_peft_model_state_dict
        set_peft_model_state_dict(self.model, weights)
        for p_ in self.model.parameters():
            if p_.requires_grad:
                p_.data = p_.data.to(self.device)
