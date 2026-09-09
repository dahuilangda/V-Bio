"""Backend integration: PeptideLM proposal engine for the production design
loop.

The production loop (backend/runtime/run_single_prediction.py) keeps its
Celery dispatch, Boltz scoring, progress reporting and NSGA-II elite
selection; this module replaces the proposal step (random init +
strategy mutation + random NCAA overlay) with the PeptideLM agent:

  * generation 0: prior samples (dev-tag + structure-token conditioned,
    placement-masked decoding) instead of random sequences
  * later generations: structure-guided edits of the elites (prefix kept,
    tail regenerated — the agent improves through GRPO on every scored
    generation), plus NCAA point moves
  * NCAA identity/position come from the learned policy, not random overlay

Interface: propose_sequences(natural_pool, unnatural_pool, elite_rows, ...)
-> list[(base_sequence, modifications, cys_anchors)] — cys_anchors are the
0-based Cys anchor positions the caller must bond to the linker ligand.

Initialisation fails loudly when the prior checkpoint is unavailable; the
production task surfaces the error (no fallback, no degradation).
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

PEPTIDELM_ROOT = Path("/data/V-Bio/capabilities/peptide_lm")
DEFAULT_PRIOR = os.environ.get(
    "VBIO_PEPTIDELM_PRIOR",
    str(PEPTIDELM_ROOT / "models" / "prior.pt"))
# SS-conditioned prior (psslm v7, 87.5M): strict vocab superset of prior.pt
# (+ <h>/<e>/<l>/<s> per-residue SS tokens, + length buckets to 120). Trained
# with per-residue SS3 prefix blocks; the SS gain is 1.53 nats over baseline
# (evals/ss_prior_eval.json, 2026-09-09 audit-verified). Used when the caller
# supplies an ss_profile — target-appropriate structural priors for groove/
# epitope binding (e.g. hairpin profiles for TNF-family receptor grooves).
_SS_V8 = PEPTIDELM_ROOT / "models" / "ss_track_v8.pt"
SS_PRIOR = os.environ.get(
    "VBIO_PEPTIDELM_SS_PRIOR",
    str(_SS_V8 if _SS_V8.exists()
        else PEPTIDELM_ROOT / "models" / "ss_prior_v7.pt"))
_SS_TOKENS = ("<h>", "<e>", "<l>", "<s>")


def _normalize_len(peptide_length: int | None,
                   len_range: tuple[int, int] | None = None) -> tuple[int, int]:
    """Length is optional in the UI: None = free optimization over a wide
    adaptive range; a fixed value pins the range; an explicit range (the
    frontend min/max inputs) wins over both."""
    if len_range is not None and len_range[0] <= len_range[1]:
        return (int(len_range[0]), int(len_range[1]))
    if peptide_length is None:
        return (10, 30)
    if peptide_length <= 0:
        return (10, 30)
    return (peptide_length, peptide_length)


class BackendProposer:
    """Holds the agent across generations within one design task.

    Free-optimization defaults: no fixed residues, no length constraint
    (adaptive), auto Cys anchors. Non-natural amino acids come ONLY from the
    user-specified pool (production peptideResiduePool non-natural entries +
    user custom CCDs): no pool given = pure natural design."""

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
        ncaa_decode_bias: decode-time soft bias toward the user pool."""
        if not Path(DEFAULT_PRIOR).exists():
            raise FileNotFoundError(f"PeptideLM prior not found: {DEFAULT_PRIOR}")
        # SS-conditioned prior swap: the SS profile (per-residue h/e/l/s
        # string) requires the v7 SS prior. Its vocab is a strict superset,
        # so every downstream token operation (NCAA extension, edits, GRPO)
        # is unchanged; only the sampled distribution is SS-informed.
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
        sys.path.insert(0, str(PEPTIDELM_ROOT))
        try:
            import torch

            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            from peplm.generate.edit import edit_candidates, mutate_candidate
            from peplm.models.train import load_prior
            from peplm.vocab import to_modifications

            self._edit_candidates = edit_candidates
            self._mutate_candidate = mutate_candidate
            self._to_modifications = to_modifications

            self.agent, self.vocab = load_prior(prior_path, device=device)
            self.prior, _ = load_prior(prior_path, device=device)
            self.prior.eval()
            # v8 additive-track detection: a trained ss_track (any nonzero
            # row) switches SS conditioning from the v7 prefix block to the
            # per-position track channel — sampling, edit parents and GRPO
            # rows all carry the SAME mechanism (train/inference parity).
            with torch.no_grad():
                self.ss_track_ready = bool(
                    hasattr(self.agent, "ss_track")
                    and float(self.agent.ss_track.weight.abs().sum()) > 0)
            if self.ss_profile and self.ss_track_ready:
                log("SS conditioning via additive ss_track (v8 mechanism)")
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
                                       # align with the prior's context:
                                       # SS-conditioned rows carry a
                                       # per-residue SS block that can exceed
                                       # the 96-token default — a truncated
                                       # row trains full-sequence reward on
                                       # partial-sequence gradients
                                       max_len=self.agent.max_len)
            self.device = device
            self.len_range = _normalize_len(peptide_length, len_range)
            self.L = self.len_range[1]
            self.design_mode = design_mode
            self.cys_positions = sorted({int(p) for p in (cys_positions or [])})
            self.cys_layout = dict(cys_layout) if isinstance(cys_layout, dict) else None
            self.allow_extra_cys = bool(allow_extra_cys)
            self.ncaa_decode_bias = float(ncaa_decode_bias)
            # NCAA pool is strictly user-specified (preset catalog is a
            # menu, not a default); user custom residues join the pool too
            pool_ccds = [str(c).strip().upper() for c in (ncaa_pool or [])]
            for e in (user_residues or []):
                ccd = str(e.get("ccd") or "").strip().upper()
                if ccd and ccd not in pool_ccds:
                    pool_ccds.append(ccd)
            self.ncaa_pool_tokens_ = [f"[{c}]" for c in pool_ccds]
            self.ncaa_max = ncaa_max if self.ncaa_pool_tokens else 0
            self.ncaa_min = min(ncaa_min, self.ncaa_max)
            self.cyclic = cyclic
            self.rng = random.Random(seed)
            self.log = log
            self.fixed_map = {}
            for e in (fixed_residues or []):
                pos = int(e.get("position") or 0) - 1
                res = str(e.get("residue") or "")
                if pos >= 0 and res:
                    self.fixed_map[pos] = res
        finally:
            sys.path.remove(str(PEPTIDELM_ROOT))

    # ------------------------------------------------------------------
    def _learn_row(self, c) -> tuple[list[str], list[int] | None]:
        """Training row for one candidate under the ACTIVE SS mechanism.

        v8 additive track: tokens carry the plain conditioning prefix (no
        SS block) and a parallel per-token id list carries the track —
        residue positions get h/e/l->1/2/3 ('s'/wildcard->0), everything
        else 0. v7 legacy: the SS block rides the tokens, no track list.
        Either way the training row matches the sampling distribution.
        """
        from peplm.generate.edit import cond_prefix

        prefix = cond_prefix(c.residues, self.cyclic,
                             modality=self._struct_token())
        if not self.ss_profile:
            return prefix + c.residues, None
        if getattr(self, "ss_track_ready", False):
            ids = {"h": 1, "e": 2, "l": 3}
            track = [ids.get(ch, 0) for ch in self.ss_profile]
            toks = prefix + c.residues
            ss = [0] * len(toks)
            # head-aligned to the residue span: track[j] conditions residue
            # j — the SAME indexing the sampler uses (ss_track[emitted],
            # emitted counts generated residues from 0). A track shorter
            # than the sequence leaves the tail free; longer tracks are
            # truncated.
            base = len(prefix)
            for j in range(len(c.residues)):
                ss[base + j] = track[j] if j < len(track) else 0
            return toks, ss
        return prefix + self._ss_block() + c.residues, None

    def _ss_block(self) -> list[str]:
        """SS conditioning block in token form, shared by sampling prompts,
        edit parents and GRPO training rows — the SAME conditioned
        distribution everywhere (a train/inference prefix mismatch
        mis-attributes what the policy actually learned)."""
        return [f"<{ch}>" for ch in self.ss_profile] if self.ss_profile else []

    def _cand_tokens(self, sequence: str, modifications: list[dict]):
        from peplm.vocab import from_modifications

        toks = [self._struct_token()]
        # keep the SS conditioning block in every generation: FIM edits and
        # GRPO samples rebuild their prompts from these tokens, so without
        # the block the later generations would silently drop back to the
        # unconditional distribution
        if self.ss_profile:
            toks = toks + [f"<{ch}>" for ch in self.ss_profile]
        toks = toks + from_modifications(
            sequence.upper(), modifications or [])
        return toks

    def propose(self, natural_pool, unnatural_pool, elite_rows, n: int,
                plddt_hint=None):
        """elite_rows: production result rows (sequence, modifications,
        pldds, composite metrics, cys_positions). Returns
        [(base_sequence, modifications, cys_anchors, proposal_group)].

        proposal_group is the GRPO grouping key: gen-0 de novo samples share
        one group; edit/mutation children are grouped by their parent so the
        group advantage reflects "the quality of edits on this parent" — the
        signal GRPO is supposed to learn from. A per-generation de novo quota
        (n // 4) stays open after generation 1 so the search never collapses
        onto the survivors' span neighborhoods."""
        from peplm.candidate import Candidate
        from peplm.data.build_corpus import bucket_tag
        from peplm.loop.constraints import build_plan, choose_bicyclic_anchors, resolve_bicyclic_anchors

        struct = self._struct_token()
        out: list[tuple[str, list[dict], list[int], str]] = []

        def _de_novo(count: int) -> None:
            """Gen-0-style prior sampling; also the standing diversity
            channel for later generations (25% quota)."""
            target_len = (self.len_range[0] if self.len_range[0] == self.len_range[1]
                          else self.rng.randint(*self.len_range))
            plan = build_plan(self._plan_cfg(), self.vocab,
                              length=(target_len if self.len_range[0] == self.len_range[1] else None),
                              fixed=dict(self.fixed_map),
                              ncaa_pool_tokens=list(self.ncaa_pool_tokens))
            prompt = ["<sol_h>", "<syn_h>", "<liab_h>",
                      bucket_tag(target_len), struct]
            # SS conditioning, mechanism-selected:
            #  - v8 additive track: per-position ids ride the ss_track
            #    channel ('s'/wildcard chars map to 0 = free position) —
            #    partial specification is native
            #  - v7 legacy prefix: the SS block rides the prompt (proper
            #    <h>/<e>/<l>/<s> tokens, NOT bare chars — bare chars are
            #    silently dropped by the stoi filter)
            track_arg = None
            if self.ss_profile:
                if getattr(self, "ss_track_ready", False):
                    track_arg = [
                        {"h": 1, "e": 2, "l": 3}.get(ch, 0)
                        for ch in self.ss_profile]
                else:
                    prompt = prompt + [f"<{ch}>" for ch in self.ss_profile]
            toks_list = self.agent.sample_with_prompt(
                prompt, count, self.device, temperature=1.0,
                top_p=0.95, constraints=plan, target_len=target_len,
                return_tokens=True, ss_track=track_arg,
                ban_tokens=["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>",
                            "<syn_m>", "<syn_l>", "<liab_h>", "<liab_m>",
                            "<liab_l>", "<dev_hi>", "<dev_md>", "<dev_lo>",
                            "<cont>", "<mask>", "<pre>", "<suf>", "<mid>",
                            "<lin>", "<cyc>", "<bicy>"]
                + list(_SS_TOKENS)
                + [f"<L{5*k}>" for k in range(1, 10)])
            for toks in toks_list:
                res = [t for t in toks if not t.startswith("<")]
                if not res:
                    continue
                res = self._bicy_post_edit(res)
                anchors = list(self._anchors_for_length(len(res)))
                seq, mods = self._to_modifications(self._apply_fixed(res))
                out.append((seq, mods, anchors, "denovo"))

        if not elite_rows:
            _de_novo(n)
            return out
        # later generations: a de novo quota keeps the prior's structural
        # diversity in the mix, then FIM edits of elites + point mutations
        _de_novo(max(1, n // 4))
        parents = []
        for row in elite_rows[:8]:
            c = Candidate(tokens=self._cand_tokens(row["sequence"],
                                                   row.get("modifications") or []),
                          cyclic=self.cyclic)
            c.metrics = {"binder_plddt": row.get("plddts") or None}
            c._bicy_anchors = self._row_anchors(row)
            parents.append(c)
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
            c = self._cand_wrap(p)
            c._protected = set(self.fixed_map) | set(p._bicy_anchors)
            c.ncaa_pool = list(self.ncaa_pool_tokens)
            group = f"edit:{''.join(p.residues[:10])}"
            for cand in self._edit_candidates(
                    self.agent, self.vocab, c, per_parent, self.device,
                    self.rng, ncaa_max=self.ncaa_max,
                    fixed_abs=dict(self.fixed_map),
                    pool_tokens=list(self.ncaa_pool_tokens),
                    plan_kwargs=plan_kwargs)[:per_parent]:
                res = self._bicy_post_edit(cand.residues)
                anchors = list(self._anchors_for_length(len(res)))
                seq, mods = self._to_modifications(self._apply_fixed(res))
                out.append((seq, mods, anchors, group))
        while len(out) < n and parents:
            p = self.rng.choice(parents)
            c = self._cand_wrap(p)
            c._protected = set(self.fixed_map) | set(c._bicy_anchors)
            c.ncaa_pool = list(self.ncaa_pool_tokens)
            # Explicit pool + cap: an empty user pool means pure-natural design (no
            # natural->NCAA moves at all) and ncaa_max bounds how many the move may add.
            cand = self._mutate_candidate(
                c, self.rng,
                ncaa_pool=list(self.ncaa_pool_tokens),
                ncaa_max=self.ncaa_max)
            res = self._bicy_post_edit(cand.residues)
            anchors = list(self._anchors_for_length(len(res)))
            seq, mods = self._to_modifications(self._apply_fixed(res))
            out.append((seq, mods, anchors, f"mut:{''.join(p.residues[:10])}"))
        return out[:n]

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

    def _cand_wrap(self, parent) -> Candidate:
        from peplm.candidate import Candidate

        if isinstance(parent, Candidate):  # already wrapped
            return parent
        return Candidate(tokens=self._cand_tokens(parent["sequence"],
                                                  parent.get("modifications") or []),
                         cyclic=self.cyclic)

    def _apply_fixed(self, res: list[str]) -> list[str]:
        toks = list(res)
        for pos, tok in self.fixed_map.items():
            if 0 <= pos < len(toks):
                toks[pos] = tok
        return toks

    def learn(self, elite_rows, all_rows):
        """GRPO update from the generation's scored rows (pose-gated reward on
        the production metrics).

        The rows must carry ``proposal_group`` (the GRPO grouping key set by
        :meth:`propose` — de novo / per-parent edit / mutation groups).
        Rows marked ``gate_rejected`` are quality-gate failures (integrity /
        pocket / ring / chirality): they enter learning with a floor-scaled
        reward instead of being dropped. Dropping them starves the group of
        variance — with a low survival rate every group degenerates to
        singletons, every advantage is zero, and the policy never updates.
        The floor reward encodes "this sequence failed the gates" as a
        negative signal relative to survivors while still ranking rejects
        among themselves by whatever stage metrics they carry."""
        from peplm.candidate import Candidate
        from peplm.generate.edit import cond_prefix
        from peplm.score.reward import PeptideReward

        if not all_rows:
            return {}
        rw = PeptideReward(
            ncaa_range=(self.ncaa_min, max(self.ncaa_min, self.ncaa_max)),
            len_range=(max(5, self.L - 6), self.L + 6))
        samples = []
        for row in all_rows:
            c = Candidate(tokens=self._cand_tokens(row["sequence"],
                                                   row.get("modifications") or []),
                          cyclic=self.cyclic)
            c.metrics = {k: row.get(k) for k in
                         ("iptm", "pair_iptm", "ipsae_dom", "binder_avg_plddt",
                          "chem_comp")}
            if row.get("gate_rejected"):
                # floor-scaled reward: constant negative signal relative to
                # survivors; the tiny machine-reward factor still ranks
                # rejects that reached scoring by their stage metrics
                base, _ = (rw.machine_reward(c)
                           if any(v is not None for v in c.metrics.values())
                           else (0.0, None))
                c.reward = 0.05 * base
                toks, ss = self._learn_row(c)
                samples.append((toks, c.reward,
                                row.get("proposal_group") or "rejected", "oracle",
                                len(toks) - len(c.residues), ss))
                continue
            if not any(v is not None for v in c.metrics.values()):
                continue
            c.reward, _ = rw.machine_reward(c)
            toks, ss = self._learn_row(c)
            # plen masks the conditioning prefix so the policy gradient
            # lands on the residue stream, matching the sampler's prompt
            # layout; grouping uses the proposer's group key (de novo pool
            # or per-parent edit/mutation cohort), never the sequence
            # itself — sequence keys made every gen-0 sample a singleton
            # group with zero advantage and silently disabled learning
            samples.append((toks, c.reward,
                            row.get("proposal_group") or "ungrouped", "oracle",
                            len(toks) - len(c.residues)))
        if len(samples) < 4:
            return {}
        return self.updater.update(samples, epochs=1, log=self.log)
