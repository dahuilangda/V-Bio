"""Proposal operators: structure-guided editing and NCAA point moves.

edit: keep the elite prefix up to its least-confident residue, regenerate the
tail with the agent (the causal-LM span editor — PepEVOLVE/Pepti-Agent style
residue editing, generalized to tails). The prefix stays visible in the prompt
so GRPO treats the trajectory as conditioned (prompt-masked) and improves the
edit operator itself.

mut: epsilon-exploration point moves — conservative substitutions (V-Bio
table) plus NCAA swaps: replace a natural residue with an NCAA token (or vice
versa), the channel that injects NCAA gradients into a prior pretrained on
natural peptides only.
"""

from __future__ import annotations

import random

from peplm.candidate import Candidate
from peplm.residues import NCAA_TOKENS, placement_of
from peplm.vocab import is_residue_token

CONSERVATIVE = {
    "A": "GSV", "R": "KHQ", "N": "DQST", "D": "EN", "C": "ST", "Q": "ENKR",
    "E": "DQK", "G": "AS", "H": "NQKR", "I": "LVMA", "L": "IVMF", "K": "RQE",
    "M": "ILV", "F": "YWL", "P": "AGS", "S": "ATGN", "T": "SAV", "W": "FY",
    "Y": "FW", "V": "ILMA",
}


def worst_positions(cand: Candidate, k: int = 1) -> list[int]:
    """Residue indices (into cand.residues) with the lowest per-residue
    pLDDT — the structure-guided edit map (residue analogue of HALO's
    per-atom pLDDT editing)."""
    plddts = cand.metrics.get("binder_plddt")
    res = cand.residues
    if not plddts or len(plddts) != len(res):
        return []
    order = sorted(range(len(res)), key=lambda i: plddts[i])
    return order[:k]




def parent_modality(parent) -> str:
    return "<bicy>" if "<bicy>" in parent.tokens[:2] else (
        "<cyc>" if (parent.cyclic or "<cyc>" in parent.tokens[:2]) else "<lin>")


class _PlanCfg:
    """Minimal config facade for build_plan inside the edit operator (the
    engine passes exactly the fields the plan needs)."""

    def __init__(self, len_range, design_mode, bicyclic_layout, ncaa_range,
                 ncaa_decode_bias=0.0, cys_positions=(),
                 allow_extra_cys=False):
        self.len_range = len_range
        self.design_mode = design_mode
        self.bicyclic_layout = bicyclic_layout
        self.ncaa_range = ncaa_range
        self.ncaa_decode_bias = ncaa_decode_bias
        self.cys_positions = tuple(cys_positions)
        self.allow_extra_cys = bool(allow_extra_cys)


def cond_prefix(residues: list[str], cyclic: bool,
                aspiration: bool = True,
                modality: str | None = None) -> list[str]:
    """Conditioning prefix: property tags (aspirational hhh during design)
    + length bucket + structure token (linear/cyclic/bicyclic)."""
    from peplm.data.build_corpus import bucket_tag

    if modality is None:
        modality = "<cyc>" if cyclic else "<lin>"
    tags = ["<sol_h>", "<syn_h>", "<liab_h>"] if aspiration else None
    if tags is None:
        from peplm.props.descriptors import compute_props

        p = compute_props(residues, cyclic=cyclic)
        def cls(v, lo=0.35, hi=0.6):
            return "h" if v >= hi else ("m" if v >= lo else "l")
        tags = [f"<sol_{cls(p['solubility'])}>",
                f"<syn_{cls(p['synthesizability'])}>",
                f"<liab_{cls(p['liability'])}>"]
    return tags + [bucket_tag(len(residues)), modality]


def edit_candidates(agent, vocab, parent: Candidate, n: int, device,
                    rng: random.Random, ncaa_max: int | None = None,
                    temperature: float = 1.0, *,
                    fixed_abs: dict | None = None,
                    pool_tokens: list[str] | None = None,
                    plan_kwargs: dict | None = None) -> list[Candidate]:
    """FIM span editing (ProteinMPNN's fixed-context redesign as an LM
    operator): mask the span around the parent's least-confident residue,
    regenerate it conditioned on BOTH flanks. Trained-in from pretraining
    (50% PSM lines), so the infill policy is the pretrained prior itself.

    Decode-time constraints (upgrade 3): fixed residues inside the fill are
    enforced with positions RELATIVE to the span origin (the prefix is
    already satisfied as prompt context); the NCAA pool and length bounds
    come from the same plan machinery as de novo sampling."""
    from peplm.loop.constraints import build_plan

    res = parent.residues
    if len(res) < 10:
        return []
    weak = worst_positions(parent, k=1)
    protected = getattr(parent, "_protected", set())
    w = rng.randint(3, min(8, len(res) - 4))
    center = weak[0] if weak else rng.randrange(2, len(res) - 2)
    for _ in range(20):
        a = max(1, min(center - w // 2, len(res) - w - 1))
        b = a + w
        if not (set(range(a, b)) & protected):
            break
        center = rng.randrange(2, len(res) - 2)
    else:
        return []
    prefix_res, suffix_res = res[:a], res[b:]
    prompt = (cond_prefix(res, parent.cyclic,
                          modality=parent_modality(parent)) + ["<pre>"] + prefix_res
              + ["<suf>"] + suffix_res + ["<mid>"])
    plan = None
    if plan_kwargs:
        fixed_rel = {pos - a: tok for pos, tok in (fixed_abs or {}).items()
                     if pos >= a}
        plan = build_plan(_PlanCfg(**plan_kwargs), vocab,
                          length=len(res), fixed=fixed_rel,
                          ncaa_pool_tokens=pool_tokens)
    fills = agent.sample_with_prompt(
        prompt, n, device, temperature=temperature, top_p=0.95,
        constraints=plan, target_len=len(res),
        return_tokens=True,
        ban_tokens=["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>", "<syn_m>",
                    "<syn_l>", "<liab_h>", "<liab_m>", "<liab_l>",
                    "<dev_hi>", "<dev_md>", "<dev_lo>", "<cont>", "<mask>",
                    "<pre>", "<suf>", "<mid>"] + [f"<L{5*k}>" for k in range(1, 10)])
    out = []
    for fill in fills:
        mid = [t for t in fill if is_residue_token(t)][: 10]
        if not mid:
            continue
        full = prefix_res + mid + suffix_res
        if (fixed_abs or {}):
            for pos, tok in fixed_abs.items():
                if 0 <= pos < len(full):
                    full[pos] = tok
        if not (6 <= len(full) <= 48):
            continue
        cand = Candidate(tokens=([parent_modality(parent)] + full),
                         cyclic=parent.cyclic, origin="edit",
                         parent=parent.key, cond_prompt=prompt)
        out.append(cand)
    return out


def mutate_candidate(parent: Candidate, rng: random.Random,
                     ncaa_pool: list[str] | None = None,
                     ncaa_max: int | None = None) -> Candidate:
    """One point move: conservative aa swap, or NCAA swap (either direction).

    NCAA legality follows the CALLER's pool exactly: an explicitly passed (or
    parent-attribute) empty pool means the user restricted the design to
    natural residues — no NCAA move may happen. Only when no pool information
    exists at all (standalone use) does the full preset catalog apply.
    ``ncaa_max`` additionally caps the natural->NCAA channel at the user's
    non-natural count budget, mirroring the decode-time constraint plan.
    """
    res = list(parent.residues)
    if not res:
        return parent
    protected = getattr(parent, "_protected", set())
    idxs = [i for i in range(len(res)) if i not in protected]
    if not idxs:
        return parent
    idx = rng.choice(idxs)
    if ncaa_pool is None:
        parent_pool = getattr(parent, "ncaa_pool", None)
        if parent_pool is not None:
            ncaa_pool = list(parent_pool)
        else:
            from peplm.residues import USER_RESIDUES
            ncaa_pool = list(NCAA_TOKENS) + [f"[{c}]" for c in USER_RESIDUES]
    else:
        ncaa_pool = list(ncaa_pool)
    ncaa_count = sum(1 for tok in res if len(tok) > 1)
    at_ncaa_cap = ncaa_max is not None and ncaa_count >= max(0, int(ncaa_max))
    move = rng.random()
    if move < 0.45 and len(res[idx]) == 1:
        candidates = CONSERVATIVE.get(res[idx], "ACDEFGHIKLMNPQRSTVWY")
        res[idx] = rng.choice(candidates)
    elif move < 0.75 and ncaa_pool and not at_ncaa_cap:
        # natural -> NCAA (placement-legality checked against position)
        cands = [t for t in ncaa_pool
                 if placement_of(t) in ("any", "n_term" if idx == 0 else
                                        "c_term" if idx == len(res) - 1 else "any")]
        # c_term/terminal tokens only legal at the last position
        cands = [t for t in cands
                 if not (placement_of(t) in ("c_term", "terminal") and idx != len(res) - 1)
                 and not (placement_of(t) == "n_term" and idx != 0)]
        if cands:
            res[idx] = rng.choice(cands)
        elif len(res[idx]) == 1:
            res[idx] = rng.choice(CONSERVATIVE.get(res[idx], "ACDEFGHIKLMNPQRSTVWY"))
    elif len(res[idx]) > 1:
        # NCAA -> its base or another NCAA sharing the base
        base = res[idx][1:-1]
        from peplm.residues import NCAA_PRESETS
        base_res = NCAA_PRESETS[base]["base"]
        res[idx] = base_res
    else:
        res[idx] = rng.choice("ACDEFGHIKLMNPQRSTVWY")
    return Candidate(tokens=(["<cyc>" if parent.cyclic else "<lin>"] + res),
                     cyclic=parent.cyclic, origin="mut", parent=parent.key)
