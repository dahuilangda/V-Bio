"""Decode-time constraint plan: fixed residues, NCAA pool legality, and
bicyclic Cys anchors enforced inside the autoregressive sampler, so the
sequence the model scores is the sequence it generates. apply() only
hard-forces (fixed/anchor positions) or softly biases (ncaa lambda)
logits — no rejection sampling, no post-hoc repair, so GRPO trains on
the final emitted text."""

from __future__ import annotations

from dataclasses import dataclass, field

import torch


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
    cys_layout: dict = field(default=None, compare=False, hash=False)
    allow_extra_cys: bool = False                    # keep non-anchor Cys
    ban_cys: bool = False                            # ban "C" outside anchors at decode

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
        # NCAA minimum guarantee: near max_len with quota unmet, force the
        # first placement-legal pool token (deterministic)
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

    Explicit user positions win when all three fit; otherwise the
    first/interior/last layout, the interior anchor chosen as: user-pinned
    C > first valid explicit position > nearest free midpoint.
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


def _ratio_anchor(length: int, pct: float) -> int:
    """Scale one percentage to a 0-based position, mirroring the 1-based
    frontend math (peptideCysLayout.ts) exactly: floor(x + 0.5) on both
    sides — Python round() is banker's rounding, JS Math.round is not."""
    one_based = max(1, min(length, int((pct / 100.0) * length + 0.5)))
    return one_based - 1


def resolve_bicyclic_anchors(length: int, fixed: dict | None = None,
                             cys_positions: tuple = (),
                             cys_layout: dict | None = None) -> tuple:
    """Resolve the 3 anchors (0-based) for one candidate of this length.

    cys_layout, when set, supersedes cys_positions and makes the anchors
    a function of length, so manual topologies survive adaptive design:

      * {"mode": "ring", "ring1": r1, "ring2": r2} — C-terminus-anchored
        rigid block: ring sizes stay r1/r2 at every length. None when the
        candidate is shorter than r1 + r2 + 3.
      * {"mode": "ratio", "pct1", "pct2", "pct3"} — percentage-scaled
        anchors, forward-fixed so adjacent anchors keep >= 2 residues
        between them. None when three anchors cannot fit.

    Without a layout dict, falls back to choose_bicyclic_anchors.
    """
    layout = cys_layout if isinstance(cys_layout, dict) else None
    mode = str(layout.get("mode") or "") if layout else ""
    if mode == "ring":
        try:
            ring1 = max(1, int(layout["ring1"]))
            ring2 = max(1, int(layout["ring2"]))
        except (KeyError, TypeError, ValueError):
            return choose_bicyclic_anchors(length, fixed, cys_positions)
        cys3 = length - 1
        cys2 = cys3 - ring2 - 1
        cys1 = cys2 - ring1 - 1
        if cys1 < 0:
            return None
        return (cys1, cys2, cys3)
    if mode == "ratio":
        try:
            pct1 = float(layout["pct1"])
            pct2 = float(layout["pct2"])
            pct3 = float(layout["pct3"])
        except (KeyError, TypeError, ValueError):
            return choose_bicyclic_anchors(length, fixed, cys_positions)
        cys1 = _ratio_anchor(length, pct1)
        cys2 = max(_ratio_anchor(length, pct2), cys1 + 2)
        cys3 = max(_ratio_anchor(length, pct3), cys2 + 2)
        if cys3 > length - 1:
            return None
        return (cys1, cys2, cys3)
    return choose_bicyclic_anchors(length, fixed, cys_positions)


def min_feasible_length_ratio(pct1: float, pct2: float, pct3: float,
                              lo: int = 5, hi: int = 120) -> int:
    """Shortest length at which a ratio layout fits three anchors."""
    for length in range(lo, hi + 1):
        if resolve_bicyclic_anchors(
                length, None, (), {"mode": "ratio", "pct1": pct1,
                                   "pct2": pct2, "pct3": pct3}) is not None:
            return length
    return hi + 1


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
    cys_layout = dict(getattr(cfg, "cys_layout", None) or None) \
        if getattr(cfg, "cys_layout", None) else None
    layout_is_length_fn = bool(cys_layout) and str(cys_layout.get("mode")) in ("ring", "ratio")
    if cfg.design_mode == "bicyclic":
        if length is not None:
            resolved = resolve_bicyclic_anchors(length, fixed,
                                                tuple(cfg.cys_positions),
                                                cys_layout)
            anchors = resolved if resolved is not None else ()
        else:
            # adaptive: length-fn layouts resolve fully in the post-edit;
            # auto keeps the position-0 decode-time anchor
            anchors = () if layout_is_length_fn else (0,)
            post_edit = ("interior_terminal", "C")
    return ConstraintPlan(
        vocab=vocab, fixed=fixed, anchors=anchors, ncaa_pool=pool,
        ncaa_max=int(cfg.ncaa_range[1]) if pool else 0,
        ncaa_min=min(int(cfg.ncaa_range[0]), int(cfg.ncaa_range[1])) if pool else 0,
        ncaa_bias=float(getattr(cfg, "ncaa_decode_bias", 0.0)),
        min_len=min_len, max_len=max_len, post_edit=post_edit,
        cys_positions=tuple(cfg.cys_positions),
        cys_layout=cys_layout,
        allow_extra_cys=allow_extra,
        ban_cys=(cfg.design_mode == "bicyclic" and not allow_extra))


def plan_for_post_edit(fixed: dict, vocab: Vocab,
                       cys_positions: tuple = (),
                       allow_extra_cys: bool = False,
                       cys_layout: dict | None = None) -> ConstraintPlan:
    """Minimal plan for the bounded bicycle post-edit (fixed map + the
    user-specified interior anchors)."""
    return ConstraintPlan(vocab=vocab, fixed=dict(fixed),
                          cys_positions=tuple(cys_positions),
                          cys_layout=cys_layout,
                          allow_extra_cys=allow_extra_cys,
                          post_edit=("bicyclic",))


def apply_post_edit(tokens: list[str], plan: ConstraintPlan,
                    layout: str = "first_last") -> list[str]:
    """Bounded post-edit for what decoding cannot know (adaptive-length
    anchors): exactly the plan.post_edit slots, never a general repair
    cascade. Runs whenever the plan carries a post_edit marker, regardless
    of the layout string's spelling."""
    if not plan.post_edit:
        return tokens
    out = list(tokens)
    L = len(out)
    if L < 1:
        return out
    resolved = resolve_bicyclic_anchors(
        L, plan.fixed, plan.cys_positions, plan.cys_layout)
    anchor_set = {a for a in (resolved or ()) if a is not None}
    for a in anchor_set:
        out[a] = "C"
    if not plan.allow_extra_cys:
        for i in range(L):
            if i not in anchor_set and out[i] == "C" \
                    and i not in plan.fixed:
                out[i] = "A"  # drop stray Cys outside anchors (deterministic)
    return out