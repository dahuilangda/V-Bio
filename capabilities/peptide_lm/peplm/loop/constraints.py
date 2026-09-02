"""Constraint plan: decode-time enforcement for Tier-2 design constraints.

Upgrade-3 core: move constraint satisfaction from post-hoc sequence repair
into the autoregressive sampler, so the sequence the model scores is the
sequence it generates. Guarantees provided AT DECODE TIME:

  * fixed residues (user-pinned positions -> hard-forced tokens; positions
    are counted in emitted residues, so FIM fills and de novo sampling are
    handled uniformly)
  * NCAA pool legality (strict: off-pool bracket tokens are banned) and an
    optional soft bias toward pool tokens (fixes the measured NCAA avoidance)
  * bicyclic Cys anchors — three explicit user positions when given, else
    the first/interior/last layout. Known length -> all anchors are
    decode-time hard-forced and non-anchor Cys banned (when extra Cys are
    not allowed); adaptive length -> terminal/interior anchors come from a
    single bounded post-edit at EOS

Design notes (engineering-grade):
  * a plan is immutable; build_plan() derives it from the loop config once
    per round (length may change between rounds in adaptive mode)
  * apply() mutates logits in place; hard-forcing (fixed/anchor positions)
    and soft biasing (ncaa lambda) are the only two mechanisms — no
    rejection sampling, no post-hoc overwrite cascades
  * GRPO text stays the *final* candidate text: the policy trains on what
    was actually scored (no train/serve skew from repair)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import torch

from peplm.models.gpt2 import PlacementMask
from peplm.vocab import Vocab


@dataclass(frozen=True)
class ConstraintPlan:
    vocab: Vocab
    fixed: dict = field(default_factory=dict)        # emitted-residue idx -> token
    anchors: tuple = ()                              # 0-based Cys anchor positions
    ncaa_pool: tuple = ()                            # allowed bracket tokens
    ncaa_max: int | None = None
    ncaa_min: int = 0
    ncaa_bias: float = 0.0                           # soft logit bias toward pool
    min_len: int = 0
    max_len: int = 96
    post_edit: tuple = ()  # (positions to set AFTER decode, token) — terminal
                           # Cys for adaptive length; interior anchor likewise
    cys_positions: tuple = ()                        # user-specified interior anchors
    allow_extra_cys: bool = False                    # keep non-anchor Cys
    ban_cys: bool = False                            # ban "C" outside anchors at decode

    # ------------------------------------------------------------------
    def ncaa_ids(self) -> set[int]:
        return {self.vocab.stoi[t] for t in self.ncaa_pool if t in self.vocab.stoi}

    def strict_ban_ids(self) -> set[int]:
        """Every bracket token outside the user pool is banned at decode."""
        allowed = set(self.ncaa_pool)
        return {self.vocab.stoi[t] for t in self.vocab.itos.values()
                if t.startswith("[") and t not in allowed} - self.ncaa_ids()

    def apply(self, logits: torch.Tensor, emitted: int, ncaa_used: int,
              total_hint: int | None = None) -> None:
        """In-place logits surgery for one decoding step of one row."""
        stoi = self.vocab.stoi
        ban = set(self.strict_ban_ids())
        if self.ncaa_max is not None and ncaa_used >= self.ncaa_max:
            ban |= self.ncaa_ids()
        if emitted in self.fixed:
            tok = self.fixed[emitted]
            if tok in stoi:
                logits.fill_(float("-inf"))
                logits[stoi[tok]] = 0.0
                return
        if emitted in self.anchors:
            logits.fill_(float("-inf"))
            logits[stoi["C"]] = 0.0
            return
        if self.ban_cys:
            logits[stoi["C"]] = float("-inf")
        if ban:
            logits.index_fill_(0, torch.tensor(sorted(ban), device=logits.device),
                               float("-inf"))
        # NCAA minimum as a decode guarantee: with < 2 steps left to max_len
        # and the quota unmet, deterministically force the first placement-
        # legal pool token (no RNG, no post-hoc random injection)
        if self.ncaa_min > 0 and ncaa_used < self.ncaa_min \
                and emitted >= self.max_len - 2:
            from peplm.residues import placement_lookup

            for tok in self.ncaa_pool:
                pl = placement_lookup(tok)
                at_n = emitted == 0
                near_c = total_hint and emitted >= total_hint - 1
                if pl == "n_term" and not at_n:
                    continue
                if pl in ("c_term", "terminal") and not near_c:
                    continue
                if tok in stoi:
                    logits.fill_(float("-inf"))
                    logits[stoi[tok]] = 0.0
                    return
        if self.ncaa_bias > 0 and self.ncaa_ids():
            pool = list(self.ncaa_ids())
            logits[torch.tensor(pool, device=logits.device)] += self.ncaa_bias
        # length bounds as decoder guarantees: eos blocked before min_len,
        # only eos legal at/after max_len
        if emitted < self.min_len:
            logits[stoi["<eos>"]] = float("-inf")
        if emitted >= self.max_len:
            keep = torch.full_like(logits, float("-inf"))
            keep[stoi["<eos>"]] = 0.0
            logits.copy_(keep)


def choose_bicyclic_anchors(length: int, fixed: dict | None = None,
                            cys_positions: tuple = ()) -> tuple:
    """The 3 anchor positions for a bicyclic candidate of this length.

    Explicit user positions win when all three fit the length; otherwise the
    first/interior/last layout applies, with the interior anchor chosen as:
    user-pinned C > first valid explicit position > nearest free midpoint.
    """
    fixed = fixed or {}
    explicit = sorted({int(p) for p in cys_positions
                       if isinstance(p, int) and 0 <= p < length})
    if len(explicit) >= 3:
        return tuple(explicit[:3])
    interior = None
    for pos, tok in fixed.items():
        if tok == "C" and 0 < pos < length - 1:
            interior = pos
            break
    if interior is None:
        for pos in explicit:
            if 0 < pos < length - 1 and pos not in fixed:
                interior = pos
                break
    if interior is None:
        mid = (length - 1) // 2
        for d in range(length - 1):
            cand = mid + d if d % 2 == 0 else mid - d
            if 0 < cand < length - 1 and cand not in fixed:
                interior = cand
                break
    return (0, interior, length - 1)


def build_plan(cfg, vocab: Vocab, length: int | None = None,
               fixed: dict | None = None,
               ncaa_pool_tokens: list[str] | None = None) -> ConstraintPlan:
    """Derive the decode-time plan from loop/config inputs.

    fixed: {0-based position: token} — user-pinned residues.
    length: known (fixed design length) -> anchors fully decodable; None
            (adaptive) -> post_edit carries the terminal + interior anchors.
    """
    stoi = vocab.stoi
    fixed = dict(fixed or {})
    pool = tuple(ncaa_pool_tokens or [])
    min_len = int(cfg.len_range[0])
    max_len = min(int(cfg.len_range[1]), 90)

    anchors: tuple = ()
    post_edit: tuple = ()
    allow_extra = bool(cfg.allow_extra_cys)
    if cfg.design_mode == "bicyclic":
        if length is not None:
            if cfg.bicyclic_layout == "first_last":
                anchors = choose_bicyclic_anchors(length, fixed,
                                                  tuple(cfg.cys_positions))
            else:
                anchors = (length - 1,)
        else:
            # adaptive: position 0 decodable; interior+terminal via post-edit
            anchors = (0,)
            post_edit = ("interior_terminal", "C")
    return ConstraintPlan(
        vocab=vocab, fixed=fixed, anchors=anchors, ncaa_pool=pool,
        ncaa_max=int(cfg.ncaa_range[1]) if pool else 0,
        ncaa_min=min(int(cfg.ncaa_range[0]), int(cfg.ncaa_range[1])) if pool else 0,
        ncaa_bias=float(getattr(cfg, "ncaa_decode_bias", 0.0)),
        min_len=min_len, max_len=max_len, post_edit=post_edit,
        cys_positions=tuple(cfg.cys_positions),
        allow_extra_cys=allow_extra,
        ban_cys=(cfg.design_mode == "bicyclic" and not allow_extra))


def plan_for_post_edit(fixed: dict, vocab: Vocab,
                       cys_positions: tuple = (),
                       allow_extra_cys: bool = False) -> ConstraintPlan:
    """Minimal plan for the bounded bicycle post-edit (fixed map + the
    user-specified interior anchors)."""
    return ConstraintPlan(vocab=vocab, fixed=dict(fixed),
                          cys_positions=tuple(cys_positions),
                          allow_extra_cys=allow_extra_cys,
                          post_edit=("bicyclic",))


def apply_post_edit(tokens: list[str], plan: ConstraintPlan,
                    layout: str = "first_last") -> list[str]:
    """Bounded post-edit for what decoding cannot know (adaptive-length
    terminal/interior anchors). Exactly the plan.post_edit slots — never a
    general repair cascade. `layout` selects the anchor policy (only
    first_last today); the function runs whenever the plan carries a
    post_edit marker, regardless of the layout string's spelling."""
    if not plan.post_edit:
        return tokens
    out = list(tokens)
    L = len(out)
    if L < 1:
        return out
    anchor_set = {a for a in choose_bicyclic_anchors(
        L, plan.fixed, plan.cys_positions) if a is not None}
    for a in anchor_set:
        out[a] = "C"
    if not plan.allow_extra_cys:
        for i in range(L):
            if i not in anchor_set and out[i] == "C" \
                    and i not in plan.fixed:
                out[i] = "A"  # drop stray Cys outside anchors (deterministic)
    return out