"""Backend integration: PeptideLM proposal engine for the production design
loop.

The production loop (backend/runtime/run_single_prediction.py) keeps its
Celery dispatch, Boltz scoring, progress reporting and NSGA-II elite
selection; this module replaces the proposal step (random init +
strategy mutation + random NCAA overlay) with the PeptideLM agent:

  * generation 0: prior samples (property-tag + structure-token
    conditioned, constraint-plan decoding) instead of random sequences
  * later generations: structure-guided edits of the elites plus NCAA
    point moves, with a standing de novo quota
    (peplm/generate/proposals.py has the operator implementations)
  * NCAA identity/position come from the learned policy, not random overlay
  * an optional per-residue SS3 profile switches to the SS-conditioned
    prior (additive ss_track mechanism)

Interface: propose(natural_pool, unnatural_pool, elite_rows, n) ->
list[(base_sequence, modifications, cys_anchors, proposal_group)] —
cys_anchors are the 0-based Cys anchor positions the caller must bond to
the linker ligand; proposal_group is the GRPO grouping key.

Initialisation fails loudly when a prior checkpoint is unavailable (no
fallback, no degradation).
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

from peplm.generate.proposals import (
    candidate_tokens,
    edit_children,
    elite_parent,
    explore_count,
    learn_row,
    mutated_child,
    normalize_len,
    sample_de_novo,
)
from peplm.vocab import from_modifications, to_modifications
from peplm.shared import placement_allows
from peplm.proposer_config import ProposerConfig

PEPTIDELM_ROOT = Path("/data/V-Bio/capabilities/peptide_lm")
DEFAULT_PRIOR = os.environ.get(
    "VBIO_PEPTIDELM_PRIOR",
    str(PEPTIDELM_ROOT / "models" / "prior.pt"))
# Receptor-conditioned prior: the official PepMLM-650M (ChatterjeeLab,
# Nat Biotech 2025) — masked-infilling sampling in the receptor's binding
# context with pseudo-PPL ranking.
PEPMLM650_DIR = PEPTIDELM_ROOT / "models/external/pepmlm650"

# SS-conditioned prior: strict vocab superset of prior.pt (+ <h>/<e>/<l>/<s>
# SS tokens + length buckets to 120). Conditioning rides the additive
# ss_track channel; used when the caller supplies an ss_profile.
SS_PRIOR = os.environ.get(
    "VBIO_PEPTIDELM_SS_PRIOR",
    str(PEPTIDELM_ROOT / "models" / "ss_track.pt"))


class BackendProposer:
    """Holds the agent across generations within one design task.

    Free-optimization defaults: no fixed residues, no length constraint
    (adaptive), auto Cys anchors. Non-natural amino acids come ONLY from the
    user-specified pool (production peptideResiduePool non-natural entries +
    user custom CCDs): no pool given = pure natural design."""

    @classmethod
    def from_config(cls, config: ProposerConfig, log=print):
        """Construct from a typed ProposerConfig dataclass."""
        return cls(
            peptide_length=config.peptide_length,
            len_range=config.len_range,
            ncaa_min=config.ncaa_min,
            ncaa_max=config.ncaa_max,
            ncaa_pool=config.ncaa_pool,
            user_residues=config.user_residues,
            fixed_residues=config.fixed_residues,
            design_mode=config.design_mode,
            cyclic=config.cyclic,
            cys_positions=config.cys_positions,
            cys_layout=config.cys_layout,
            allow_extra_cys=config.allow_extra_cys,
            ncaa_decode_bias=config.ncaa_decode_bias,
            ss_profile=config.ss_profile,
            target_sequence=config.target_sequence,
            target_pocket_positions=config.target_pocket_positions,
            device=config.device,
            seed=config.seed,
            log=log,
        )

    def __init__(self, *, peptide_length: int | None = None,
                 len_range: tuple[int, int] | None = None,
                 ncaa_min: int = 0, ncaa_max: int = 0, cyclic: bool = False,
                 device: str = "cpu", seed: int = 0,
                 ncaa_pool: list[str] | None = None,
                 user_residues: list[dict] | None = None,
                 fixed_residues: list[dict] | None = None,
                 design_mode: str = "linear",
                 cys_positions: list[int] | None = None,
                 cys_layout: dict | None = None,
                 allow_extra_cys: bool = False,
                 ncaa_decode_bias: float = 0.5,
                 ss_profile: str | None = None,
                 target_sequence: str | None = None,
                 target_pocket_positions: list[int] | None = None,
                 log=print):
        """ncaa_pool: CCD codes the user allowed (production peptideResiduePool
        non-natural entries); empty = pure natural design (ncaa_max forced 0).
        design_mode/cys_positions: bicyclic layout — cys_positions are the
        0-based anchor indices (exactly 3 in manual mode; empty = auto).
        cys_layout: optional length-function layout dict
        ({"mode": "ring"|"ratio", ...}) that supersedes cys_positions and
        keeps manual topologies valid across adaptive design lengths.
        allow_extra_cys: keep non-anchor Cys unlinked instead of scrubbing.
        fixed_residues: [{'position': 1-based, 'residue': 'F' | '[AIB]'}] —
        the production peptideSequenceMask letters.
        ncaa_decode_bias: decode-time soft bias toward the user pool.
        ss_profile: per-residue SS3 string over h/e/l ('s' = wildcard);
        switches sampling to the SS-conditioned prior.
        target_sequence: receptor for the receptor-conditioned path
        (masked-infilling proposes in the binding context; ranking-side
        integration lives in pseudo_perplexity). target_pocket_positions:
        reserved for pocket-focused conditioning."""
        prior_path = DEFAULT_PRIOR
        if ss_profile:
            ss = "".join(str(ss_profile).split()).lower()
            if not ss or any(ch not in "hels" for ch in ss):
                raise ValueError(
                    f"peptideSSProfile 只能含 h/e/l/s 字符（逐残基 SS3/通配），"
                    f"得到: {ss_profile!r}")
            if not Path(SS_PRIOR).exists():
                raise FileNotFoundError(f"SS-conditioned prior not found: {SS_PRIOR}")
            self.ss_profile = ss
            prior_path = SS_PRIOR
            log(f"SS-conditioned prior active: profile {ss!r} via {Path(SS_PRIOR).name}")
        else:
            self.ss_profile = None
        # shared proposal config — consumed by BOTH prior paths below
        self.len_range = normalize_len(peptide_length, len_range)
        self.L = self.len_range[1]
        self.design_mode = design_mode
        self.cyclic = cyclic
        self.cys_positions = sorted({int(p) for p in (cys_positions or [])})
        self.cys_layout = dict(cys_layout) if isinstance(cys_layout, dict) else None
        self.allow_extra_cys = bool(allow_extra_cys)
        self.ncaa_decode_bias = float(ncaa_decode_bias)
        # NCAA pool is strictly user-specified (the preset catalog is a
        # menu, not a default); user custom residues join the pool too
        pool_ccds = [str(c).strip().upper() for c in (ncaa_pool or [])]
        for e in (user_residues or []):
            ccd = str(e.get("ccd") or "").strip().upper()
            if ccd and ccd not in pool_ccds:
                pool_ccds.append(ccd)
        self.ncaa_pool_tokens_ = [f"[{c}]" for c in pool_ccds]
        if user_residues:
            from peplm.residues import register_user_residues
            register_user_residues(list(user_residues))
        self.ncaa_max = ncaa_max if self.ncaa_pool_tokens else 0
        self.ncaa_min = min(ncaa_min, self.ncaa_max)
        self.rng = random.Random(seed)
        self.log = log
        self.fixed_map = {}
        for e in (fixed_residues or []):
            pos = int(e.get("position") or 0) - 1
            res = str(e.get("residue") or "")
            if pos >= 0 and res:
                self.fixed_map[pos] = res
        # receptor-conditioned path: the official PepMLM-650M — the only
        # backend, loaded or the task fails loudly (no silent fallback)
        self.uses_pepmlm = bool(target_sequence)
        if self.uses_pepmlm:
            sys.path.insert(0, str(PEPTIDELM_ROOT))
            try:
                import torch

                if device.startswith("cuda") and not torch.cuda.is_available():
                    device = "cpu"
                from peplm.models.pepmlm_prior import load_pepmlm_prior
                self._pepmlm = load_pepmlm_prior(PEPMLM650_DIR, device=device)
                self._pepmlm.set_receptor(str(target_sequence))
                log(f"PepMLM-650M prior active "
                    f"({len(str(target_sequence))} aa receptor)")
                if self.ss_profile:
                    log("SS profile superseded: the receptor-conditioned "
                        "prior replaces SS conditioning on this path")
                self.ss_profile = None
                self.ss_track_ready = False
                self.device = device
                self.updater = None  # GRPO not applicable to the 650M backbone
                return
            finally:
                sys.path.remove(str(PEPTIDELM_ROOT))

        if not Path(prior_path).exists():
            raise FileNotFoundError(f"PeptideLM prior not found: {prior_path}")
        sys.path.insert(0, str(PEPTIDELM_ROOT))
        try:
            import torch

            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            from peplm.models.train import load_prior

            self.agent, self.vocab = load_prior(prior_path, device=device)
            self.prior, _ = load_prior(prior_path, device=device)
            self.prior.eval()
            # SS conditioning requires a trained additive ss_track; an
            # untrained track sums zero and would silently drop the
            # conditioning.
            with torch.no_grad():
                self.ss_track_ready = bool(
                    hasattr(self.agent, "ss_track")
                    and float(self.agent.ss_track.weight.abs().sum()) > 0)
            if self.ss_profile and not self.ss_track_ready:
                raise RuntimeError(
                    f"SS-conditioned prior {prior_path} has no trained "
                    "ss_track; use a checkpoint trained with the SS track on")

            # user-supplied amino acids: register + extend both vocabularies
            # (prior rows copied from the agent so the KL anchor starts exact)
            from peplm.residues import register_user_residues

            if user_residues:
                added = register_user_residues(list(user_residues))
                toks = [f"[{c}]" for c in added]
                if toks and hasattr(self.agent, "extend_vocab"):
                    self.agent.extend_vocab(toks)
                    self.prior.extend_vocab(toks)
                    with torch.no_grad():
                        ea = self.agent.gpt.get_input_embeddings().weight
                        ep = self.prior.gpt.get_input_embeddings().weight
                        for t in toks:
                            ep[self.prior.vocab.stoi[t]] = \
                                ea[self.agent.vocab.stoi[t]]
                    self.vocab = self.agent.vocab
            from peplm.generate.grpo import GRPOUpdater

            self.updater = GRPOUpdater(self.agent, self.prior, self.vocab,
                                       device=device,
                                       # the prior's own context window, not
                                       # the updater default
                                       max_len=self.agent.max_len)
            self.device = device
        finally:
            sys.path.remove(str(PEPTIDELM_ROOT))

    # propose
    def propose(self, natural_pool, unnatural_pool, elite_rows, n: int,
                plddt_hint=None):
        """elite_rows: production result rows (sequence, modifications,
        pldds, composite metrics, cys_positions). Returns
        [(base_sequence, modifications, cys_anchors, proposal_group)].

        With a target receptor the PepMLM path (masked-infilling) replaces
        the small peplm prior entirely. proposal_group is the GRPO grouping
        key: gen-0 de novo samples share one group; edit/mutation children
        are grouped by parent so the advantage reflects edit quality on
        that parent. A per-generation de novo quota stays open after
        generation 1 so the search never collapses onto the survivors'
        span neighborhoods."""
        if getattr(self, "uses_pepmlm", False):
            return self._propose_pepmlm(natural_pool, elite_rows, n)

        from peplm.loop.constraints import build_plan

        struct = self._struct_token()
        out: list[tuple[str, list[dict], list[int], str]] = []

        def _emit(res: list[str], group: str) -> None:
            res = self._bicy_post_edit(res)
            anchors = list(self._anchors_for_length(len(res)))
            seq, mods = to_modifications(self._apply_fixed(res))
            out.append((seq, mods, anchors, group))

        def _de_novo(count: int) -> None:
            target_len = (self.len_range[0] if self.len_range[0] == self.len_range[1]
                          else self.rng.randint(*self.len_range))
            plan = build_plan(self._plan_cfg(), self.vocab,
                              length=(target_len if self.len_range[0] == self.len_range[1] else None),
                              fixed=dict(self.fixed_map),
                              ncaa_pool_tokens=list(self.ncaa_pool_tokens))
            for toks in sample_de_novo(self.agent, self.device, count,
                                       target_len=target_len,
                                       struct_token=struct, constraints=plan,
                                       ss_profile=self.ss_profile):
                res = [t for t in toks if not t.startswith("<")]
                if res:
                    _emit(res, "denovo")

        if not elite_rows:
            _de_novo(n)
            return out
        recent_rewards = [
            float(row.get("score") or 0)
            for row in elite_rows[:6]
            if isinstance(row.get("score"), (int, float))
        ]
        _de_novo(explore_count(recent_rewards, n))
        parents = [elite_parent(row, cyclic=self.cyclic, struct_token=struct,
                                anchors=self._row_anchors(row))
                   for row in elite_rows[:8]]
        per_parent = max(2, (n - len(out)) // min(4, len(parents)))
        plan_kwargs = {
            "len_range": tuple(self.len_range),
            "design_mode": self.design_mode,
            "bicyclic_layout": "first_last",
            "ncaa_range": (self.ncaa_min, self.ncaa_max),
            "ncaa_decode_bias": self.ncaa_decode_bias,
            "cys_positions": tuple(self.cys_positions),
            "allow_extra_cys": self.allow_extra_cys,
            "cys_layout": self.cys_layout,
        }
        for p in parents[:4]:
            group = f"edit:{''.join(p.residues[:10])}"
            for cand in edit_children(self.agent, self.vocab, p, per_parent,
                                      self.device, self.rng,
                                      ncaa_max=self.ncaa_max,
                                      pool_tokens=self.ncaa_pool_tokens,
                                      fixed_map=self.fixed_map,
                                      plan_kwargs=plan_kwargs):
                _emit(cand.residues, group)
        while len(out) < n and parents:
            p = self.rng.choice(parents)
            cand = mutated_child(p, self.rng,
                                 pool_tokens=self.ncaa_pool_tokens,
                                 ncaa_max=self.ncaa_max,
                                 fixed_map=self.fixed_map)
            _emit(cand.residues, f"mut:{''.join(p.residues[:10])}")
        return out[:n]

    def _excluded_residues(self, natural_pool) -> set:
        """Residues the sampler may not emit, mirroring the frontend's
        residue-pool selections: whatever the user deselected is blocked,
        plus Cys on designs whose topology gives free thiols no structural
        role (free thiols are a developability liability). Bicyclic
        layouts and manual Cys anchors keep Cys — the topology needs it."""
        aa20 = set("ACDEFGHIKLMNPQRSTVWY")
        exclude = set()
        if natural_pool:
            allowed = {str(a).upper()[:1] for a in natural_pool} & aa20
            if allowed:
                exclude |= aa20 - allowed
        if not (self.design_mode == "bicyclic" or self.cys_positions
                or self.cys_layout or self.allow_extra_cys):
            exclude.add("C")
        return exclude

    def _evolve_ncaa(self, base: str, mods: list[dict],
                     plddts: list[float]) -> tuple[str, list[dict]] | None:
        """One structured NCAA move on an elite, guided by per-residue
        oracle pLDDT: relocate an NCAA to a placement-legal, unfixed
        position (preferring the oracle's low-confidence spots) or swap
        its identity within the user pool. Returns the new
        (base, modifications) or None when no legal move exists."""
        from peplm.residues import placement_lookup, residue_meta

        n = len(base)
        by_pos = {int(m.get("position", 0)): str(m.get("ccd", "")).upper()
                  for m in mods}
        if not by_pos or self.ncaa_max <= 0 or not self.ncaa_pool_tokens:
            return None
        src_pos = self.rng.choice(sorted(by_pos))
        ccd = by_pos[src_pos]

        def _legal(ccd_code: str) -> list[int]:
            rule = placement_lookup(ccd_code)
            return [
                i0 for i0 in range(1, n + 1)
                if (i0 - 1) not in self.fixed_map and i0 not in by_pos
                and len(base[i0 - 1]) == 1
                and placement_allows(rule, i0 - 1, n)]

        if self.rng.random() < 0.5:
            # move: prefer the lowest-plddt legal positions (oracle feedback)
            cands = _legal(ccd)
            if not cands:
                return None
            if plddts and len(plddts) == n:
                cands.sort(key=lambda i0: plddts[i0 - 1])
                k = max(1, len(cands) // 2)
                cands = cands[:k]
            dst = self.rng.choice(cands)
            new_by_pos = {p: c for p, c in by_pos.items() if p != src_pos}
            new_by_pos[dst] = ccd
        else:
            # identity swap within the user pool at the same position; the
            # position is vacated first so the legality check can pass
            others = []
            for t in self.ncaa_pool_tokens:
                cand = t[1:-1]
                if cand == ccd:
                    continue
                vacated = {p: c for p, c in by_pos.items() if p != src_pos}
                rule = placement_lookup(cand)
                ok = (src_pos - 1) not in self.fixed_map and \
                    placement_allows(rule, src_pos - 1, n)
                if ok:
                    others.append(cand)
            if not others:
                return None
            new_by_pos = dict(by_pos)
            new_by_pos[src_pos] = self.rng.choice(others)

        new_base = list(base)
        for p, c in new_by_pos.items():
            base_aa = str((residue_meta(c) or {}).get("base") or base[p - 1])
            new_base[p - 1] = base_aa
        new_mods = [{"position": p, "ccd": c,
                     "baseResidue": new_base[p - 1]}
                    for p, c in sorted(new_by_pos.items())]
        return ''.join(new_base), new_mods

    def _propose_pepmlm(self, natural_pool, elite_rows, n: int):
        """PepMLM proposal with three evolutionary streams, ranked by
        pseudo-PPL on the natural base (the model's vocabulary):

        - de novo: masked-infilling from the static distribution, NCAAs
          inserted at random legal positions;
        - natural refine: mask & re-infill an elite's weakest residues
          (per-residue pLDDT biases the masking), keeping its NCAA
          modifications at the same positions;
        - NCAA evolve: relocate/swap one elite NCAA guided by the
          oracle's low-confidence positions.
        """
        exclude = self._excluded_residues(natural_pool) or None
        length = self.rng.randint(*self.len_range)

        elites = []
        for r in (elite_rows or [])[:4]:
            if not isinstance(r, dict) or not r.get("sequence"):
                continue
            seq = str(r["sequence"]).upper()
            mods = [m for m in (r.get("modifications") or [])
                    if isinstance(m, dict) and m.get("ccd")
                    and isinstance(m.get("position"), int)]
            plddts = [float(x) for x in (r.get("plddts") or [])
                      if isinstance(x, (int, float))]
            elites.append((seq, mods, plddts))

        scored = []  # (ppl, base, mods, group)

        def _collect(seqs, mods, group):
            for i, seq in enumerate(seqs):
                seq = ''.join(t for t in seq if t in 'ACDEFGHIKLMNPQRSTVWY')
                if len(seq) < self.len_range[0]:
                    continue
                if exclude and any(c in exclude for c in seq):
                    continue
                m = mods[i] if mods and i < len(mods) else []
                scored.append((self._pepmlm.pseudo_perplexity(seq),
                               seq, m, group))

        n_deno = n * 3 if not elites else max(n, 4) * 2
        denovo_seqs = self._pepmlm.sample_with_prompt(
            prompt=[], n=n_deno, device=self.device,
            temperature=0.85, exclude=exclude,
            target_len=length, return_tokens=True)
        # exploration stream: NCAAs at random legal positions
        denovo_mods = []
        for seq in denovo_seqs:
            _, m = to_modifications(
                self._apply_ncaa(self._apply_fixed(list(seq))))
            denovo_mods.append(m)
        _collect(denovo_seqs, denovo_mods, "pepmlm_denovo")

        for parent, mods, plddts in elites:
            # the oracle's per-residue pLDDT picks the masking focus
            focus = None
            if plddts and len(plddts) == len(parent):
                order = sorted(range(len(parent)), key=lambda i: plddts[i])
                focus = order[:max(2, len(order) // 3)]
            per_parent = max(3, (n * 2) // max(1, len(elites)))
            variants = self._pepmlm.refine_with_prompt(
                parent, per_parent, self.device, exclude=exclude,
                focus_positions=focus, return_tokens=True)
            # the elite's NCAA placements carry over unchanged — the model
            # re-optimizes the natural context around them
            _collect(variants, [mods] * len(variants),
                     f"pepmlm_refine:{parent[:10]}")

            # NCAA evolution on the elite's non-natural design
            if mods and self.ncaa_pool_tokens:
                k = max(2, per_parent // 2)
                evolved = []
                for _ in range(k * 2):
                    e = self._evolve_ncaa(parent, mods, plddts)
                    if e is not None:
                        evolved.append(e)
                    if len(evolved) >= k:
                        break
                if evolved:
                    _collect([b for b, _ in evolved], [m for _, m in evolved],
                             f"pepmlm_ncaa:{parent[:10]}")

        scored.sort(key=lambda x: x[0])

        # exploration/exploitation quotas. PPL on the natural base cannot
        # rank NCAA moves (the base barely changes), so each stream gets
        # reserved slots: de novo half, refine and NCAA-evolve split the
        # rest by PPL within their own pool.
        if elites:
            denovo = [s for s in scored if s[3] == "pepmlm_denovo"]
            refine = [s for s in scored if s[3].startswith("pepmlm_refine")]
            ncaa = [s for s in scored if s[3].startswith("pepmlm_ncaa")]
            # exploration anneals with elite quality: early generations keep
            # a half de novo, a converged elite pool drops toward 15%
            rewards = [float(r.get("score") or 0)
                       for r in (elite_rows or [])
                       if isinstance(r, dict)
                       and isinstance(r.get("score"), (int, float))]
            n_deno_keep = max(1, min(n // 2, explore_count(rewards, n)))
            n_exploit = n - n_deno_keep
            n_ncaa_keep = (min(len(ncaa), max(1, n_exploit // 2))
                           if ncaa else 0)
            pools = [denovo[:n_deno_keep], ncaa[:n_ncaa_keep],
                     refine[:n - n_deno_keep - n_ncaa_keep]]
            # round-robin across streams: interleaving keeps every stream's
            # share wherever the caller truncates; PPL ranks only WITHIN a
            # stream (NCAA moves barely move base PPL)
            picked = []
            while any(pools):
                for pool in pools:
                    if pool:
                        picked.append(pool.pop(0))
        else:
            picked = scored[:n]

        out = []
        for ppl, seq, mods, group in picked[:n]:
            res = list(seq)
            if self.design_mode == "bicyclic":
                # place the Cys anchors the topology requires BEFORE mods:
                # ESM rarely emits exactly-3-Cys sequences, so the anchors
                # are imposed at the layout-resolved positions
                anchors = list(self._anchors_for_length(len(res)))
                for p in anchors:
                    if p < len(res):
                        res[p] = "C"
            res = self._apply_fixed(res)
            res = from_modifications(res, mods)
            seq, mods = to_modifications(res)
            anchors_out = (tuple(self._anchors_for_length(len(seq)))
                           if self.design_mode == "bicyclic" else ())
            out.append((seq, mods, anchors_out, group))
        counts = {}
        for _, _, _, g in picked[:n]:
            key = g.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        self.log(f"[pepmlm] proposed {len(out)} {counts} len={length} "
                 f"excluded={''.join(sorted(exclude)) if exclude else '-'} "
                 f"best_ppl={scored[0][0]:.1f}" if scored else
                 "[pepmlm] no valid candidates")
        return out

    # anchors and layout
    def _anchors_for_length(self, length: int) -> tuple:
        """Resolve anchors for one candidate length; a length-function
        layout that cannot fit degrades to the auto layout so the candidate
        still ships a consistent (sequence, anchors) pair."""
        from peplm.loop.constraints import choose_bicyclic_anchors, resolve_bicyclic_anchors

        resolved = resolve_bicyclic_anchors(
            length, self.fixed_map, tuple(self.cys_positions), self.cys_layout)
        if resolved is not None:
            return resolved
        return choose_bicyclic_anchors(
            length, self.fixed_map, tuple(self.cys_positions))

    def _row_anchors(self, row) -> tuple:
        """Anchor positions for an elite row: recorded when the caller kept
        them, else recomputed from the sequence length by the shared rule."""
        if self.design_mode != "bicyclic":
            return ()
        recorded = row.get("cys_positions") if isinstance(row, dict) else None
        if recorded and len(recorded) == 3:
            return tuple(int(p) for p in recorded)
        seq = str(row.get("sequence") or "")
        return self._anchors_for_length(len(seq))

    def _struct_token(self) -> str:
        if self.design_mode == "bicyclic":
            return "<bicy>"
        if self.design_mode == "cyclic" or self.cyclic:
            return "<cyc>"
        return "<lin>"

    class _PlanCfg:
        def __init__(self, len_range, design_mode, bicyclic_layout,
                     ncaa_range, ncaa_decode_bias, cys_positions=(),
                     allow_extra_cys=False, cys_layout=None):
            self.len_range = len_range
            self.design_mode = design_mode
            self.bicyclic_layout = bicyclic_layout
            self.ncaa_range = ncaa_range
            self.ncaa_decode_bias = ncaa_decode_bias
            self.cys_positions = tuple(cys_positions)
            self.allow_extra_cys = bool(allow_extra_cys)
            self.cys_layout = dict(cys_layout) if isinstance(cys_layout, dict) else None

    def _plan_cfg(self):
        return self._PlanCfg(tuple(self.len_range), self.design_mode,
                             "first_last", (self.ncaa_min, self.ncaa_max),
                             self.ncaa_decode_bias,
                             cys_positions=tuple(self.cys_positions),
                             allow_extra_cys=self.allow_extra_cys,
                             cys_layout=self.cys_layout)

    def _bicy_post_edit(self, res: list[str]) -> list[str]:
        """Bounded post-edit for adaptive-length bicyclic anchors (terminal +
        interior Cys; the decode-time plan already handles fixed length)."""
        if self.design_mode != "bicyclic" or not res:
            return res
        from peplm.loop.constraints import apply_post_edit, plan_for_post_edit

        return apply_post_edit(res, plan_for_post_edit(
            dict(self.fixed_map), self.vocab, tuple(self.cys_positions),
            allow_extra_cys=self.allow_extra_cys,
            cys_layout=self.cys_layout),
            "first_last")

    @property
    def ncaa_pool_tokens(self) -> list[str]:
        return list(self.ncaa_pool_tokens_)

    def _apply_ncaa(self, res: list[str]) -> list[str]:
        """Insert user-pool non-natural residues into a natural proposal.

        Position policy: replace a residue matching the monomer's base
        amino acid when a placement-legal, unfixed position has one
        (chemistry-preserving — the prior chose that residue in context);
        otherwise take any legal position. Placement rules (n_term /
        c_term / terminal / any) mirror the bracket-token decode
        constraints of the property-prior path; fixed residues and
        already-substituted positions are never overwritten."""
        if self.ncaa_max <= 0 or not self.ncaa_pool_tokens:
            return res
        from peplm.residues import placement_lookup, residue_meta

        n = len(res)
        out = list(res)
        used: set[int] = set()
        for _ in range(self.rng.randint(self.ncaa_min, self.ncaa_max)):
            token = self.rng.choice(self.ncaa_pool_tokens)
            ccd = token[1:-1]
            rule = placement_lookup(token)
            base = str((residue_meta(ccd) or {}).get("base") or "")
            legal = [
                i for i in range(n)
                if i not in used and i not in self.fixed_map
                and len(out[i]) == 1
                and placement_allows(rule, i, n)
            ]
            if not legal:
                continue
            prefer = [i for i in legal if out[i] == base]
            pos = self.rng.choice(prefer if prefer else legal)
            out[pos] = token
            used.add(pos)
        return out

    def _apply_fixed(self, res: list[str]) -> list[str]:
        toks = list(res)
        for pos, tok in self.fixed_map.items():
            if 0 <= pos < len(toks):
                toks[pos] = tok
        return toks

    # learning
    def pseudo_perplexity(self, sequence: str) -> float:
        if getattr(self, "uses_pepmlm", False):
            return self._pepmlm.pseudo_perplexity(sequence)
        """Conditioned pseudo-perplexity (PepMLM ranking signal).

        exp(mean NLL) of the sequence under the same conditioned prior
        that proposes it (property prefix + SS track when active). Lower
        pseudo-PPL anti-correlates with AF-Multimer ipTM; used to rank
        candidate oversamples BEFORE any GPU oracle call."""
        import math

        import torch

        toks, ss = learn_row(
            from_modifications(sequence.upper(), []),
            cyclic=self.cyclic, struct_token=self._struct_token(),
            ss_profile=self.ss_profile)
        ids = [self.vocab.bos] + self.vocab.encode_tokens(toks)[
            : self.agent.max_len - 2] + [self.vocab.eos]
        x = torch.tensor([ids], device=self.device)
        ss_ids = None
        if ss is not None:
            # track ids align to the token stream; column 0 is the bos slot
            ss_ids = torch.zeros_like(x)
            n = min(len(ss), len(ids) - 1)
            ss_ids[0, 1:n + 1] = torch.tensor(ss[:n], dtype=torch.long,
                                              device=self.device)
        with torch.no_grad():
            lp = self.prior._token_logprobs(x, ss_ids=ss_ids)
        # mean NLL over the RESIDUE tokens (skip the conditioning prefix)
        n_res = len([t for t in toks if not t.startswith("<")])
        if n_res == 0 or lp.numel() == 0:
            return float("inf")
        nll = -float(lp.sum()) / max(n_res, 1)
        return math.exp(nll)

    def learn(self, elite_rows, all_rows):
        if getattr(self, "uses_pepmlm", False):
            # Frozen 650M prior: optimization is selection + model-guided
            # refinement (mask & re-infill on elites), not a policy update
            return {}
        """GRPO update from the generation's scored rows.

        The rows must carry ``proposal_group`` (the GRPO grouping key set
        by :meth:`propose` — de novo / per-parent edit / mutation groups).
        Rows marked ``gate_rejected`` are quality-gate failures (integrity /
        pocket / ring / chirality): they enter learning with a floor-scaled
        reward instead of being dropped — dropping them starves the group
        of variance (with a low survival rate every group degenerates to
        singletons and the policy never updates)."""
        from peplm.candidate import Candidate
        from peplm.score.reward import PeptideReward

        if not all_rows:
            return {}
        rw = PeptideReward(
            ncaa_range=(self.ncaa_min, max(self.ncaa_min, self.ncaa_max)),
            len_range=(max(5, self.L - 6), self.L + 6))
        struct = self._struct_token()
        samples = []
        for row in all_rows:
            c = Candidate(tokens=candidate_tokens(row["sequence"],
                                                  row.get("modifications") or [],
                                                  struct),
                          cyclic=self.cyclic)
            c.metrics = {k: row.get(k) for k in
                         ("iptm", "pair_iptm", "ipsae_dom", "binder_avg_plddt",
                          "chem_comp", "binder_chain_plddt")}
            if row.get("gate_rejected"):
                # floor-scaled reward: constant negative signal relative to
                # survivors; still ranks rejects that reached scoring
                base, _ = (rw.machine_reward(c)
                           if any(v is not None for v in c.metrics.values())
                           else (0.0, None))
                c.reward = 0.05 * base
            elif not any(v is not None for v in c.metrics.values()):
                continue
            else:
                c.reward, _ = rw.machine_reward(c)
            toks, ss = learn_row(c.residues, cyclic=self.cyclic,
                                 struct_token=struct,
                                 ss_profile=self.ss_profile)
            # plen masks the conditioning prefix so the policy gradient
            # lands on the residue stream, matching the sampler's prompt
            # layout; grouping uses the proposer's group key, never the
            # sequence itself
            samples.append((toks, c.reward,
                            row.get("proposal_group") or
                            ("rejected" if row.get("gate_rejected")
                             else "ungrouped"),
                            "oracle",
                            len(toks) - len(c.residues), ss))
        if len(samples) < 4:
            return {}
        return self.updater.update(samples, epochs=1, log=self.log)
