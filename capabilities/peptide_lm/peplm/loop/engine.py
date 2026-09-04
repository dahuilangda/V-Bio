"""PeptideLM closed-loop engine (HALO loop structure, peptide semantics).

Per round:
  1. propose   - agent de novo (dev-tag + structure-token conditioned, NCAA
                 placement-masked decoding), structure-guided edits of elites
                 (prefix kept, tail regenerated), NCAA point moves
  2. filter    - hard windows (length, NCAA count/placement, developability
                 floor), duplicate memory
  3. gate      - surrogate UCB selects which candidates earn real Boltz calls
  4. score     - Boltz-2 co-folding: ipTM / pair ipTM / ipSAE / pLDDT
  5. reward    - pose-gated geometric reward + batch z-norm mixing; surrogate
                 rows get risk-averse scores (cannot out-earn verified rows)
  6. learn     - GRPO on the full pool (grouped by proposal context), KL-anchored
  7. report    - production composite (V-Bio formula) for comparability

All state persists under run_dir (resumable, analyzable).
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import numpy as np
import torch

from peplm.candidate import Candidate
from peplm.generate.edit import edit_candidates, mutate_candidate
from peplm.generate.grpo import GRPOUpdater
from peplm.loop.config import LoopConfig
from peplm.loop.constraints import build_plan, choose_bicyclic_anchors
from peplm.props.descriptors import compute_props
from peplm.score.production import production_composite
from peplm.score.reward import PeptideReward
from peplm.score.surrogate import Surrogate
from peplm.vocab import to_modifications


class PeptideLoop:
    def __init__(self, cfg: LoopConfig, target_sequence: str, prior, vocab,
                 oracle, run_dir, log=print):
        self.cfg = cfg
        self.target_sequence = str(target_sequence).upper()
        self.prior = prior
        self.vocab = vocab
        self.oracle = oracle
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.rng = random.Random(cfg.seed)
        torch.manual_seed(cfg.seed)

        # agent = trainable copy of the Tier-1 prior (prior stays frozen as
        # the KL anchor)
        import copy

        self.agent = copy.deepcopy(prior)
        self.agent.to(cfg.device)
        self.prior.to(cfg.device)
        # arbitrary user residues: register + extend both vocabularies
        # identically (prior embedding rows are copied from the agent so the
        # KL anchor starts exact on new tokens)
        from peplm.residues import register_user_residues

        if cfg.user_residues:
            added = register_user_residues(list(cfg.user_residues))
            new_tokens = [f"[{c}]" for c in added]
            if hasattr(self.agent, "extend_vocab"):
                self.agent.extend_vocab(new_tokens)
                self.prior.extend_vocab(new_tokens)
                with torch.no_grad():
                    emb_a = self.agent.gpt.get_input_embeddings().weight
                    emb_p = self.prior.gpt.get_input_embeddings().weight
                    for t in new_tokens:
                        emb_p[self.prior.vocab.stoi[t]] = \
                            emb_a[self.agent.vocab.stoi[t]]
                self.vocab = self.agent.vocab
            else:
                self.log("[loop] prior lacks extend_vocab; user residues "
                         "limited to preset pool")
        self.updater = GRPOUpdater(self.agent, self.prior, self.vocab,
                                   device=cfg.device, lr=cfg.rl_lr,
                                   kl_beta=cfg.kl_beta)
        self.reward_fn = PeptideReward(ncaa_range=tuple(cfg.ncaa_range),
                                       len_range=tuple(cfg.len_range))
        # user-fixed residues sanity: fixed positions must not collide with
        # the bicyclic Cys anchors (auto layout pins position 1 and the
        # terminal) — that is a config error, not a silent layout corruption
        if cfg.design_mode == "bicyclic" and cfg.fixed_residues:
            fixed = self._fixed_map()
            Lmax = cfg.len_range[1]
            anchors = {a for a in choose_bicyclic_anchors(
                Lmax, fixed, tuple(cfg.cys_positions)) if a is not None}
            for pos, tok in fixed.items():
                if not (0 <= pos < Lmax):
                    raise ValueError(
                        f"fixed position {pos + 1} outside design length "
                        f"{cfg.len_range}")
                if tok != "C" and pos in anchors:
                    raise ValueError(
                        f"bicyclic anchors {sorted(p + 1 for p in anchors)} "
                        f"require Cys at position {pos + 1}; it is fixed to {tok}")
        self.surrogate = Surrogate(seed=cfg.seed)
        self.seen: set[str] = set()
        self.elites: list[Candidate] = []
        self.anchor_elites: list[Candidate] = []  # permanent seed anchors
        self.best: list[Candidate] = []  # cross-round top by composite
        self.round_i = 0
        self.rounds_log: list[dict] = []
        self.seed_sequences: list[str] = []

    # ------------------------------------------------------------- helpers
    def _struct_token(self) -> str:
        if self.cfg.design_mode == "bicyclic":
            return "<bicy>"
        if self.cfg.design_mode == "cyclic" or self.cfg.cyclic:
            return "<cyc>"
        return "<lin>"

    def _fixed_map(self) -> dict[int, str]:
        """0-based position -> fixed residue token (user-pinned amino acids)."""
        return {int(e.get("position")) - 1: str(e.get("residue"))
                for e in self.cfg.fixed_residues
                if e.get("position") and e.get("residue")}

    def _ncaa_pool_tokens(self) -> list[str]:
        """Effective NCAA pool = the user-specified pool (CCD codes) plus
        user custom residues. Empty = pure natural design."""
        ccds = [str(c).strip().upper() for c in self.cfg.ncaa_pool]
        for e in self.cfg.user_residues:
            c = str(e.get("ccd") or "").strip().upper()
            if c and c not in ccds:
                ccds.append(c)
        return [f"[{c}]" for c in ccds]

    def _passes_filters(self, cand: Candidate) -> bool:
        """Validation-only (upgrade 3): constraints are enforced at DECODE
        time by the ConstraintPlan; here we only check invariants and reject
        violations. The only post-hoc op is the bounded bicycle post-edit
        (adaptive-length interior/terminal anchors — nothing else)."""
        if self.cfg.design_mode == "bicyclic":
            from peplm.loop.constraints import (
                apply_post_edit,
                choose_bicyclic_anchors,
                plan_for_post_edit,
            )

            res = apply_post_edit(
                cand.residues,
                plan_for_post_edit(self._fixed_map(), self.vocab,
                                   tuple(self.cfg.cys_positions),
                                   allow_extra_cys=self.cfg.allow_extra_cys),
                self.cfg.bicyclic_layout)
            cand.tokens = ([self._struct_token()] + res)
        res = cand.residues
        L = len(res)
        if not (self.cfg.len_range[0] <= L <= self.cfg.len_range[1]):
            return False
        max_ncaa = self.cfg.ncaa_range[1] if self._ncaa_pool_tokens() else 0
        n_ncaa = sum(1 for t in res if t.startswith("["))
        if n_ncaa > max_ncaa:
            return False
        if n_ncaa < self.cfg.ncaa_range[0]:
            return False  # decode-time guarantee failed -> reject (never repair)
        # fixed residues must survive (decoder forced them; verify)
        fixed = self._fixed_map()
        for pos, tok in fixed.items():
            if pos >= L or res[pos] != tok:
                return False
        # bicyclic layout invariants
        if self.cfg.design_mode == "bicyclic":
            anchors = {a for a in choose_bicyclic_anchors(
                L, fixed, tuple(self.cfg.cys_positions)) if a is not None}
            if any(res[p] != "C" for p in anchors):
                return False
            if not self.cfg.allow_extra_cys and res.count("C") != 3:
                return False
        # placement legality (PCA n-term only etc.)
        for i, t in enumerate(res):
            if t.startswith("["):
                from peplm.residues import placement_of

                pl = placement_of(t)
                if pl == "n_term" and i != 0:
                    return False
                if pl in ("c_term", "terminal") and i != L - 1:
                    return False
        cand.props = compute_props(res, cyclic=cand.cyclic)
        if cand.props["developability"] < self.cfg.dev_floor:
            return False
        key = cand.key
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    # ------------------------------------------------------------- propose
    def _propose(self) -> list[Candidate]:
        cfg = self.cfg
        out: list[Candidate] = []
        struct = self._struct_token()
        target_len = self.rng.randint(*cfg.len_range)
        parents = self.elites[-8:]
        if self.cfg.anchor_seed and self.anchor_elites:
            # lead-opt: the seed lead is always an edit parent (after the
            # round-1 scoring it carries the pLDDT edit map)
            parents = self.anchor_elites + parents
        n_agent = cfg.n_agent
        if not parents:
            # round 1: no edit/mut parents yet -> fold their budget into
            # de novo sampling so the pool stays full
            n_agent += cfg.n_edit + cfg.n_mut
        fixed_abs = self._fixed_map()
        pool_tokens = self._ncaa_pool_tokens()
        if n_agent > 0:
            prompt = ["<sol_h>", "<syn_h>", "<liab_h>",
                      f"<L{min(max((target_len // 5) * 5, 5), 45)}>", struct]
            fixed_len = cfg.len_range[0] == cfg.len_range[1]
            plan_len = target_len if fixed_len else None
            plan = build_plan(cfg, self.vocab, length=plan_len,
                              fixed=fixed_abs, ncaa_pool_tokens=pool_tokens)
            # oversample 4x: filters + duplicate memory still eat samples
            toks_list = self.agent.sample_with_prompt(
                prompt, min(4 * n_agent, 160), cfg.device,
                temperature=cfg.temperature,
                top_p=0.95, constraints=plan, target_len=target_len,
                return_tokens=True,
                ban_tokens=["<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>",
                            "<syn_m>", "<syn_l>", "<liab_h>", "<liab_m>",
                            "<liab_l>", "<dev_hi>", "<dev_md>", "<dev_lo>",
                            "<cont>", "<mask>", "<pre>", "<suf>", "<mid>",
                            "<lin>", "<cyc>"]
                + [f"<L{5*k}>" for k in range(1, 10)])[: 4 * n_agent]
            for toks in toks_list:
                out.append(Candidate(tokens=[struct] + [t for t in toks
                                       if not t.startswith("<")],
                                     cyclic=cfg.cyclic, origin="agent"))
        for p in parents[:4]:
            # FIM edits: fixed residues mapping to EMITTED positions is built
            # inside edit_candidates (the span origin is only known there);
            # the prefix is already fixed by being prompt context.
            plan_kwargs = {
                "len_range": tuple(cfg.len_range),
                "design_mode": cfg.design_mode,
                "bicyclic_layout": cfg.bicyclic_layout,
                "ncaa_range": tuple(cfg.ncaa_range),
                "ncaa_decode_bias": cfg.ncaa_decode_bias,
                "cys_positions": tuple(cfg.cys_positions),
                "allow_extra_cys": cfg.allow_extra_cys,
            }
            out.extend(edit_candidates(
                self.agent, self.vocab, p,
                max(2, cfg.n_edit // max(len(parents), 1)),
                cfg.device, self.rng,
                ncaa_max=cfg.ncaa_range[1],
                fixed_abs=fixed_abs, pool_tokens=pool_tokens,
                plan_kwargs=plan_kwargs,
                temperature=cfg.temperature))
        for _ in range(cfg.n_mut):
            if parents:
                p = self.rng.choice(parents)
                p.ncaa_pool = pool_tokens
                p._protected = set(fixed_abs)
                if cfg.design_mode == "bicyclic":
                    p._protected |= {a for a in choose_bicyclic_anchors(
                        len(p.residues), fixed_abs,
                        tuple(cfg.cys_positions)) if a is not None}
                out.append(mutate_candidate(
                    p, self.rng,
                    ncaa_pool=pool_tokens,
                    ncaa_max=cfg.ncaa_range[1]))
        return out

    # ---------------------------------------------------------------- gate
    def _select_for_oracle(self, pool: list[Candidate]) -> list[Candidate]:
        cfg = self.cfg
        budget = min(cfg.oracle_budget, len(pool))
        if budget <= 0:
            return []
        if not cfg.use_surrogate or self.surrogate.n_obs < 12:
            self.rng.shuffle(pool)
            # diversity: at most 2 candidates per 6-mer prefix signature
            sig_mem: dict[str, int] = {}
            chosen = []
            for c in pool:
                sig = "".join(c.residues)[:6]
                if sig_mem.get(sig, 0) < 2:
                    chosen.append(c)
                    sig_mem[sig] = sig_mem.get(sig, 0) + 1
                if len(chosen) >= budget:
                    break
            return chosen[:budget]
        mu, sig = self.surrogate.predict(pool)
        acq = mu[:, 0] + cfg.acquisition_kappa * sig[:, 0]
        order = np.argsort(-acq)
        chosen = [pool[i] for i in order[: int(budget * cfg.exploit_fraction)]]
        for i in order[int(budget * cfg.exploit_fraction):]:
            if len(chosen) >= budget:
                break
            chosen.append(pool[i])
        return chosen

    # --------------------------------------------------------------- reward
    def _compute_rewards(self, pool: list[Candidate],
                         scored_keys: set[str]) -> list[Candidate]:
        unscored = [c for c in pool if c.key not in scored_keys]
        mu = sig = None
        if unscored and self.cfg.use_surrogate and self.surrogate.fitted:
            mu, sig = self.surrogate.predict(unscored)
        parts_list = []
        for c in pool:
            if c.key in scored_keys:
                c.reward, parts = self.reward_fn.machine_reward(c)
            else:
                pred = {}
                s = None
                if mu is not None:
                    j = unscored.index(c)
                    pred = {"ipsae_dom": mu[j, 0],
                            "binder_avg_plddt": mu[j, 1] * 100.0}
                    s = float(sig[j, 0]) * 0.3  # risk-averse discount
                c.reward, parts = self.reward_fn.machine_reward(
                    c, surrogate_pred=pred, surrogate_sigma=s)
            parts_list.append(parts)
        # Batch z-norm mixing (30%) — HALO's anti-saturation blend. A failure
        # here is a reward-configuration bug; it propagates rather than
        # silently dropping the batch normalization term.
        normed = self.reward_fn.combine_batch(parts_list)
        for c, n in zip(pool, normed):
            c.final_reward = 0.7 * c.reward + 0.3 * n
        return pool

    # ------------------------------------------------------------------ run
    def run(self, n_rounds: int | None = None) -> dict:
        n_rounds = n_rounds or self.cfg.n_rounds
        t_start = time.time()
        for _ in range(n_rounds):
            t0 = time.time()
            self.round_i += 1
            proposals = self._propose()
            pool = [c for c in proposals if self._passes_filters(c)]
            if not pool:
                self.log(f"[r{self.round_i}] empty pool after filters")
                continue
            selected = self._select_for_oracle(pool)
            if (self.round_i == 1 and self.cfg.anchor_seed and self.anchor_elites
                    and not self.anchor_elites[0].metrics):
                anchor = self.anchor_elites[0]
                if all(c.key != anchor.key for c in selected):
                    selected = [anchor] + list(selected)
            if selected:
                self.oracle.score(selected, tag=f"r{self.round_i:03d}")
                # production composite (reporting) + ipSAE-led best (ranking
                # when cfg.best_metric="ipSAE"); both land in scored.jsonl
                for c in selected:
                    base, _ = to_modifications(c.residues)
                    comp = production_composite(c.metrics, base)
                    if comp is not None:
                        c.metrics["composite"] = comp
                        ips = c.metrics.get("ipsae_dom")
                        if ips is None:
                            ips = c.metrics.get("ligand_ipsae_max")
                        if ips is None:
                            ips = c.metrics.get("pair_iptm")
                        c.metrics["best"] = float(ips) if ips is not None else comp
                        if self.cfg.best_metric == "ipSAE" and ips is not None:
                            c.metrics["best"] = float(ips)
            scored_keys = {c.key for c in selected if c.metrics}
            if selected and self.cfg.use_surrogate:
                self.surrogate.add_observations(
                    [c for c in selected if c.metrics])
            self._compute_rewards(pool, scored_keys)

            # lead-opt anchor: once scored, it joins the elites and the GRPO
            # batch as a strong positive trajectory
            anchor = self.anchor_elites[0] if self.cfg.anchor_seed and self.anchor_elites else None
            if anchor is not None and anchor.metrics and anchor.reward is None:
                anchor.reward, _ = self.reward_fn.machine_reward(anchor)
                anchor.final_reward = anchor.reward
                if not any(c.key == anchor.key for c in self.elites):
                    self.elites.insert(0, anchor)

            # elite update: oracle-scored preferred, ranked by final reward
            scored = [c for c in pool if c.metrics]
            ranked = (sorted(scored, key=lambda c: -(c.final_reward or 0))
                      if len(scored) >= 4 else
                      sorted(pool, key=lambda c: -(c.final_reward or 0)))
            elite_keys = {c.key for c in self.elites}
            for c in ranked[: self.cfg.elite_size]:
                if c.key not in elite_keys:
                    self.elites.append(c)
                    elite_keys.add(c.key)
            self.elites = self.elites[-16:]
            # cross-round best-by-composite pool (the reported metric): the
            # elites list ranks by final reward, so a top-composite candidate
            # must be tracked separately or it is lost between rounds
            scored_now = [c for c in pool if c.metrics.get("best") is not None]
            if scored_now:
                merged = self.best + scored_now
                seen_keys: set[str] = set()
                dedup = []
                for c in sorted(merged, key=lambda c: -c.metrics["best"]):
                    if c.key not in seen_keys:
                        seen_keys.add(c.key)
                        dedup.append(c)
                self.best = dedup[:64]

            # GRPO: train on the whole filtered pool (+ the anchor when present)
            samples = []
            grpo_pool = list(pool)
            if anchor is not None and anchor.metrics and anchor.reward is not None:
                grpo_pool = [anchor] + grpo_pool
            for c in grpo_pool:
                from peplm.generate.edit import cond_prefix

                toks = cond_prefix(c.residues, c.cyclic) + c.residues
                plen = 0
                if c.cond_prompt:
                    plen = len(self.vocab.encode_tokens(c.cond_prompt))
                samples.append((toks, c.final_reward or 0.0,
                                c.parent or f"len{len(c.residues)}",
                                "oracle" if c.metrics else "surrogate", plen))
            upd = self.updater.update(samples, epochs=self.cfg.rl_epochs,
                                      log=self.log) if len(samples) >= 4 else {}

            # persist every oracle-scored candidate (full audit trail; the
            # winning sequence must be recoverable from the run dir alone)
            with open(self.run_dir / "scored.jsonl", "a") as f:
                for c in selected:
                    if not c.metrics:
                        continue
                    base, mods = to_modifications(c.residues)
                    f.write(json.dumps({
                        "round": self.round_i, "seq": c.seq_str, "cyclic": c.cyclic,
                        "origin": c.origin, "base": base, "modifications": mods,
                        **{k: c.metrics.get(k) for k in
                           ("iptm", "pair_iptm", "ipsae_dom", "ligand_ipsae_max",
                            "binder_avg_plddt", "composite", "best",
                            "min_ipae", "mean_ipae",
                            "self_consistency", "corr", "d_min_ipae")},
                    }, default=str) + "\n")

            # round report
            comps = [c.metrics.get("composite") for c in pool
                     if c.metrics.get("composite") is not None]
            stats = {
                "round": self.round_i,
                "pool": len(pool),
                "proposals": len(proposals),
                "oracle_calls": len(selected),
                "surrogate_n": self.surrogate.n_obs,
                "best_composite": max(comps) if comps else None,
                "best_ipSAE": max((c.metrics.get("best") or 0)
                                  for c in pool if c.metrics.get("best") is not None)
                              if any(c.metrics.get("best") is not None for c in pool)
                              else None,
                "mean_composite": float(np.mean(comps)) if comps else None,
                "top_final_reward": float(max((c.final_reward or 0) for c in pool)),
                "ncaa_fraction": float(np.mean([
                    1 if any(t.startswith("[") for t in c.residues) else 0
                    for c in pool])),
                "agent_update": upd,
                "elapsed_s": time.time() - t0,
            }
            self.rounds_log.append(stats)
            (self.run_dir / "rounds.jsonl").write_text(
                "\n".join(json.dumps(s, default=str) for s in self.rounds_log))
            self.log(f"[r{self.round_i:2d}] pool={len(pool):3d} "
                     f"oracle={len(selected):3d} "
                     f"bestComp={stats['best_composite'] or float('nan'):.3f} "
                     f"topR={stats['top_final_reward']:.3f} "
                     f"ncaa={stats['ncaa_fraction']:.2f} "
                     f"({stats['elapsed_s']:.0f}s)")
            self._checkpoint()
        return {"rounds": self.rounds_log, "total_s": time.time() - t_start}

    def _checkpoint(self):
        torch.save({"state_dict": self.agent.state_dict()},
                   self.run_dir / "agent.pt")
        rows = []
        for c in (self.elites or []):
            rows.append({
                "seq": c.seq_str, "cyclic": c.cyclic, "origin": c.origin,
                "reward": c.final_reward,
                "iptm": c.metrics.get("iptm"),
                "pair_iptm": c.metrics.get("pair_iptm"),
                "ipsae_dom": c.metrics.get("ipsae_dom"),
                "binder_avg_plddt": c.metrics.get("binder_avg_plddt"),
                "composite": c.metrics.get("composite"),
                "props": c.props,
            })
        with open(self.run_dir / "elites.json", "w") as f:
            json.dump(rows, f, indent=1, default=str)
