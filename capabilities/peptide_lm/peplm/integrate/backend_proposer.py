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
                 allow_extra_cys: bool = False,
                 ncaa_decode_bias: float = 0.5,
                 log=print):
        """ncaa_pool: CCD codes the user allowed (production peptideResiduePool
        non-natural entries); empty = pure natural design (ncaa_max forced 0).
        design_mode/cys_positions: bicyclic layout — cys_positions are the
        0-based anchor indices (exactly 3 in manual mode; empty = auto).
        allow_extra_cys: keep non-anchor Cys unlinked instead of scrubbing.
        fixed_residues: [{'position': 1-based, 'residue': 'F' | '[AIB]'}] —
        the production peptideSequenceMask letters.
        ncaa_decode_bias: decode-time soft bias toward the user pool."""
        if not Path(DEFAULT_PRIOR).exists():
            raise FileNotFoundError(f"PeptideLM prior not found: {DEFAULT_PRIOR}")
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

            self.agent, self.vocab = load_prior(DEFAULT_PRIOR, device=device)
            self.prior, _ = load_prior(DEFAULT_PRIOR, device=device)
            self.prior.eval()
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
                                       device=device)
            self.device = device
            self.len_range = _normalize_len(peptide_length, len_range)
            self.L = self.len_range[1]
            self.design_mode = design_mode
            self.cys_positions = sorted({int(p) for p in (cys_positions or [])})
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
    def _cand_tokens(self, sequence: str, modifications: list[dict]):
        from peplm.vocab import from_modifications

        toks = [self._struct_token()] + from_modifications(
            sequence.upper(), modifications or [])
        return toks

    def propose(self, natural_pool, unnatural_pool, elite_rows, n: int,
                plddt_hint=None):
        """elite_rows: production result rows (sequence, modifications,
        pldds, composite metrics, cys_positions). Returns
        [(base_sequence, modifications, cys_anchor_positions)]."""
        from peplm.candidate import Candidate
        from peplm.loop.constraints import choose_bicyclic_anchors

        struct = self._struct_token()
        out: list[tuple[str, list[dict], list[int]]] = []
        if not elite_rows:
            from peplm.data.build_corpus import bucket_tag
            from peplm.loop.constraints import build_plan

            # adaptive length: sample the target bucket per call within the
            # user range (or the single fixed length)
            target_len = (self.len_range[0] if self.len_range[0] == self.len_range[1]
                          else self.rng.randint(*self.len_range))
            plan = build_plan(self._plan_cfg(), self.vocab,
                              length=(target_len if self.len_range[0] == self.len_range[1] else None),
                              fixed=dict(self.fixed_map),
                              ncaa_pool_tokens=list(self.ncaa_pool_tokens))
            prompt = ["<sol_h>", "<syn_h>", "<liab_h>",
                      bucket_tag(target_len), struct]
            toks_list = self.agent.sample_with_prompt(
                prompt, n, self.device, temperature=1.0,
                top_p=0.95, constraints=plan, target_len=target_len,
                return_tokens=True,
                ban_tokens=["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>",
                            "<syn_m>", "<syn_l>", "<liab_h>", "<liab_m>",
                            "<liab_l>", "<dev_hi>", "<dev_md>", "<dev_lo>",
                            "<cont>", "<mask>", "<pre>", "<suf>", "<mid>",
                            "<lin>", "<cyc>", "<bicy>"]
                + [f"<L{5*k}>" for k in range(1, 10)])
            for toks in toks_list:
                res = [t for t in toks if not t.startswith("<")]
                if not res:
                    continue
                res = self._bicy_post_edit(res)
                anchors = list(choose_bicyclic_anchors(
                    len(res), self.fixed_map, tuple(self.cys_positions)))
                seq, mods = self._to_modifications(self._apply_fixed(res))
                out.append((seq, mods, anchors))
            return out
        # later generations: edits of elites + NCAA point moves
        parents = []
        for row in elite_rows[:8]:
            c = Candidate(tokens=self._cand_tokens(row["sequence"],
                                                   row.get("modifications") or []),
                          cyclic=self.cyclic)
            c.metrics = {"binder_plddt": row.get("plddts") or None}
            c._bicy_anchors = self._row_anchors(row)
            parents.append(c)
        per_parent = max(2, n // len(parents))
        plan_kwargs = {
            "len_range": tuple(self.len_range),
            "design_mode": self.design_mode,
            "bicyclic_layout": "first_last",
            "ncaa_range": (self.ncaa_min, self.ncaa_max),
            "ncaa_decode_bias": self.ncaa_decode_bias,
            "cys_positions": tuple(self.cys_positions),
            "allow_extra_cys": self.allow_extra_cys,
        }
        for p in parents[:4]:
            c = self._cand_wrap(p)
            c._protected = set(self.fixed_map) | set(p._bicy_anchors)
            c.ncaa_pool = list(self.ncaa_pool_tokens)
            for cand in self._edit_candidates(
                    self.agent, self.vocab, c, per_parent, self.device,
                    self.rng, ncaa_max=self.ncaa_max,
                    fixed_abs=dict(self.fixed_map),
                    pool_tokens=list(self.ncaa_pool_tokens),
                    plan_kwargs=plan_kwargs)[:per_parent]:
                res = self._bicy_post_edit(cand.residues)
                anchors = list(choose_bicyclic_anchors(
                    len(res), self.fixed_map, tuple(self.cys_positions)))
                seq, mods = self._to_modifications(self._apply_fixed(res))
                out.append((seq, mods, anchors))
        while len(out) < n and parents:
            c = self._cand_wrap(self.rng.choice(parents))
            c._protected = set(self.fixed_map) | set(c._bicy_anchors)
            c.ncaa_pool = list(self.ncaa_pool_tokens)
            cand = self._mutate_candidate(c, self.rng)
            res = self._bicy_post_edit(cand.residues)
            anchors = list(choose_bicyclic_anchors(
                len(res), self.fixed_map, tuple(self.cys_positions)))
            seq, mods = self._to_modifications(self._apply_fixed(res))
            out.append((seq, mods, anchors))
        return out[:n]

    def _row_anchors(self, row) -> tuple:
        """Anchor positions for an elite row: recorded when the caller kept
        them, else recomputed from the sequence length by the shared rule."""
        from peplm.loop.constraints import choose_bicyclic_anchors

        if self.design_mode != "bicyclic":
            return ()
        recorded = row.get("cys_positions") if isinstance(row, dict) else None
        if recorded and len(recorded) == 3:
            return tuple(int(p) for p in recorded)
        seq = str(row.get("sequence") or "")
        return choose_bicyclic_anchors(len(seq), self.fixed_map,
                                       tuple(self.cys_positions))



    def _struct_token(self) -> str:
        if self.design_mode == "bicyclic":
            return "<bicy>"
        if self.design_mode == "cyclic" or self.cyclic:
            return "<cyc>"
        return "<lin>"

    class _PlanCfg:
        def __init__(self, len_range, design_mode, bicyclic_layout,
                     ncaa_range, ncaa_decode_bias, cys_positions=(),
                     allow_extra_cys=False):
            self.len_range = len_range
            self.design_mode = design_mode
            self.bicyclic_layout = bicyclic_layout
            self.ncaa_range = ncaa_range
            self.ncaa_decode_bias = ncaa_decode_bias
            self.cys_positions = tuple(cys_positions)
            self.allow_extra_cys = bool(allow_extra_cys)

    def _plan_cfg(self):
        return self._PlanCfg(tuple(self.len_range), self.design_mode,
                             "first_last", (self.ncaa_min, self.ncaa_max),
                             self.ncaa_decode_bias,
                             cys_positions=tuple(self.cys_positions),
                             allow_extra_cys=self.allow_extra_cys)

    def _bicy_post_edit(self, res: list[str]) -> list[str]:
        """Bounded post-edit for adaptive-length bicyclic anchors (terminal +
        interior Cys; the decode-time plan already handles fixed length)."""
        if self.design_mode != "bicyclic" or not res:
            return res
        from peplm.loop.constraints import apply_post_edit, plan_for_post_edit

        return apply_post_edit(res, plan_for_post_edit(
            dict(self.fixed_map), self.vocab, tuple(self.cys_positions),
            allow_extra_cys=self.allow_extra_cys),
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
        the production metrics)."""
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
                         ("iptm", "pair_iptm", "ipsae_dom", "binder_avg_plddt")}
            if not any(v is not None for v in c.metrics.values()):
                continue
            c.reward, _ = rw.machine_reward(c)
            toks = cond_prefix(c.residues, self.cyclic,
                               modality=self._struct_token()) + c.residues
            samples.append((toks, c.reward, row.get("sequence", "")[:6],
                            "oracle", 0))
        if len(samples) < 4:
            return {}
        return self.updater.update(samples, epochs=1, log=self.log)
