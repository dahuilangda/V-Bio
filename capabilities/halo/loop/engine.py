"""HALO closed-loop engine.

Per round:
  1. propose   - agent SMILES samples + MMP moves on the elite pool + random
                 seed mutants (mixture weights adapt by acceptance rate)
  2. filter    - validity, property windows, PAINS, duplicate/scaffold memory
  3. gate      - surrogate mean+uncertainty selects which candidates earn
                 real Boltz2Score oracle calls (active learning)
  4. score     - Boltz2Score (pose + affinity + ipSAE + pLDDT) on the gated set
  5. learn     - surrogate online update; REINVENT agent RL update on the
                 full filtered pool (oracle-scored where available, surrogate
                 otherwise); preference model refit after human review
  6. review    - every K rounds the human inspects top/uncertain molecules and
                 gives preferences that shape the reward (HITL)

All state is persisted under run_dir for resumability and analysis.
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from rdkit import Chem

from halo.config import HaloConfig
from halo.data.ligands import load_ligand_table
from halo.data.targets import Target
from halo.generate.agent import AgentUpdater, ReplayBuffer
from halo.generate.grpo import GRPOUpdater
from halo.loop.diversity import ScaffoldMemory
from halo.score.properties import (DEFAULT_WINDOW, compute_descriptors, is_pains,
                                   passes_window, canonical_smiles)
from halo.score.preference import PreferenceModel
from halo.score.reward import RewardFunction
from halo.score.surrogate import TARGET_KEYS, Surrogate


class MockOracle:
    """CPU stand-in for Boltz2Score (smoke tests only): RF on descriptors."""

    def __init__(self, target: Target, seed: int = 0):
        from sklearn.ensemble import RandomForestRegressor

        table = load_ligand_table(target.ligands_sdf).dropna(subset=["activity_pic50"])
        from halo.score.properties import descriptor_vector

        if len(table) >= 4:
            X = np.stack([descriptor_vector(Chem.MolFromSmiles(s)) for s in table["smiles"]])
            y = table["activity_pic50"].values
            self.rf = RandomForestRegressor(n_estimators=60, random_state=seed).fit(X, y)
        else:  # no activity data (user-provided reference only): heuristic mock
            mols = [Chem.MolFromSmiles(s) for s in table["smiles"].tolist()] or [Chem.MolFromSmiles("c1ccccc1")]
            mols = [m for m in mols if m is not None]
            X = np.stack([descriptor_vector(m) for m in mols])
            y = np.array([6.0] * len(mols))
            self.rf = RandomForestRegressor(n_estimators=20, random_state=seed).fit(X, y)
        self.n_calls = 0
        self.n_gpu_seconds = 0.0
        self.rng = np.random.RandomState(seed)

    def score_smiles(self, smiles_list, tag="batch"):
        from halo.score.properties import descriptor_vector

        self.n_calls += len(smiles_list)
        rows = []
        for s in smiles_list:
            m = Chem.MolFromSmiles(s)
            if m is None:
                rows.append({"smiles": s, "affinity_pic50": None, "ipsae": None, "ligand_plddt_mean": None})
                continue
            pic = float(self.rf.predict(descriptor_vector(m)[None])[0]) + self.rng.normal(0, 0.15)
            rows.append({
                "smiles": s, "affinity_pic50": pic,
                "ipsae": float(np.clip(0.25 + 0.04 * (pic - 6), 0.0, 0.8)),
                "ligand_plddt_mean": float(np.clip(55 + 3 * (pic - 6), 30, 95)),
                "pose_method": "mock", "confidence_score": 0.5, "iptm": 0.5,
            })
        return pd.DataFrame(rows)


class HaloLoop:
    def __init__(self, cfg: HaloConfig, target: Target, prior, agent, vocab,
                 oracle, human: HumanInterface, run_dir: Path, device="cuda",
                 log=print):
        self.cfg = cfg
        self.target = target
        self.prior = prior
        self.agent = agent
        self.vocab = vocab
        if not hasattr(vocab, "tok"):
            raise ValueError("the loop requires a SAFE prior (runs/prior_unified); "
                             "train one with scripts/train_prior.py")
        self.oracle = oracle
        self.human = human
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.log = log
        self.rng = random.Random(cfg.seed)
        torch.manual_seed(cfg.seed)

        self.surrogate = Surrogate(cfg.surrogate, device=device)
        self.pref = PreferenceModel()
        if getattr(cfg.generator, "rl_algorithm", "grpo") == "grpo":
            from halo.generate.grpo import GRPOUpdater

            self.updater = GRPOUpdater(
                agent, prior, vocab, device=device,
                lr=getattr(cfg.generator, "rl_lr", 3e-5),
                clip_eps=getattr(cfg.generator, "rl_clip_eps", 0.2),
                kl_beta=getattr(cfg.generator, "rl_kl_beta", 0.01),
                ent_coef=getattr(cfg.generator, "rl_ent_coef", 0.003),
                use_replay=getattr(cfg.generator, "rl_use_replay", True),
            )
        else:
            self.updater = AgentUpdater(agent, prior, vocab, device=device)
        self.replay = ReplayBuffer(cfg.generator.replay_size)
        self.memory = ScaffoldMemory(cfg.loop.diversity_scaffold_limit)

        ligand_table = load_ligand_table(target.ligands_sdf).dropna(subset=["activity_pic50"])
        if getattr(cfg.loop, "seed_ligands", None):
            # benchmark protocol: reward anchors see only the seed half
            ligand_table = ligand_table[ligand_table["smiles"].isin(set(cfg.loop.seed_ligands))]
        self.known_ligands = [s for s in ligand_table["smiles"].tolist() if isinstance(s, str)]
        # ChEMBL novelty index (reward term + memorization guard)
        from halo.score.novelty_index import load_default

        self.novelty_idx = load_default()
        if cfg.loop.reference_smiles and cfg.loop.reference_smiles not in self.known_ligands:
            self.known_ligands = [cfg.loop.reference_smiles] + self.known_ligands
        self.known_pic50 = dict(zip(ligand_table["smiles"], ligand_table["activity_pic50"]))
        seeds = list(self.known_pic50) or self.known_ligands[:1]
        self.reward_fn = RewardFunction(
            target_pic50=float(np.percentile(list(self.known_pic50.values()), 80)) if self.known_pic50 else 8.0,
            pref_lambda_max=cfg.loop.pref_lambda_max,
            seed_smiles=seeds,
            use_pose_gate=getattr(cfg.loop, "use_pose_gate", True),
            novelty_index=self.novelty_idx,
        )
        self.reward_fn.pref_model = self.pref
        self.window = dict(DEFAULT_WINDOW)
        self.alpha = cfg.generator.alpha_init
        self.acceptance = {"agent": 0.5, "mmp": 0.5, "mut": 0.5}
        self.proposal_stats = {"agent": [1, 1], "mmp": [1, 1], "mut": [1, 1]}  # [tried, passed]
        self.elite_pool: list[str] = []
        self.best_machine: list[dict] = []
        self.dpo = None  # lazy DPO updater (RLHF alignment on feedback)
        from halo.generate.rl_components import ScaffoldSoftPenalty, RNDIntrinsicReward

        self._ims = ScaffoldSoftPenalty(bucket=int(getattr(cfg.loop, "ims_bucket", 25)))
        self._safe_cache: dict[str, str] = {}
        self._rnd = RNDIntrinsicReward(device=device)
        self._pool_meta: dict[str, tuple[str, str | None]] = {}
        self._proposal_parent: dict[str, str | None] = {}
        self._proposal_cond: dict[str, tuple[str, str]] = {}
        self.round_i = 0
        self.oracle_log: list[dict] = []
        self.feedback_log: list[dict] = []
        self.rounds_log: list[dict] = []

    # ------------------------------------------------------------------ paths
    @property
    def candidates_csv(self) -> Path:
        return self.run_dir / "candidates.csv"

    @property
    def oracle_csv(self) -> Path:
        return self.run_dir / "oracle_scores.csv"

    # ------------------------------------------------------------------ steps
    def _propose(self) -> dict[str, list[str]]:
        """Three proposal channels, all from the same prior:
        agent (de novo via the <cont> prompt), edit (unified_edit on elite
        parents; the scaffold-hop fraction edits with the core dropped),
        mut (local chemistry moves on known ligands)."""
        out = {"agent": [], "edit": [], "mut": []}
        self._proposal_parent = {}
        self._proposal_cond = {}
        cfg = self.cfg.loop
        if cfg.use_agent:
            samples = self.agent.sample_with_prompt(
                "<cont>", cfg.n_agent_samples, self.device, temperature=1.0, top_p=0.95,
                max_len=self.cfg.generator.max_smiles_len + 8, include_prompt=False,
                ban_tokens=["<hop>", "<core>", "<cont>"])
            import safe as _sl

            def _dec(x):
                try:
                    return _sl.decode(x, ignore_errors=True)
                except Exception:
                    return None
            out["agent"] = [s for s in (canonical_smiles(_dec(x) or "") for x in samples) if s]
        if cfg.n_mmp_samples > 0:
            out["edit"] = [s for s in (canonical_smiles(x) for x in
                                       self._edit_channel(take=cfg.n_mmp_samples)) if s]
        # local moves for epsilon-exploration
        from halo.generate.mmp_moves import mutate_random
        muts: list[str] = []
        for s in self.rng.sample(self.known_ligands, k=min(6, len(self.known_ligands))):
            got = mutate_random(s, self.rng, self.window)
            muts.extend(got)
            for g in got:
                self._proposal_parent.setdefault(g, s)
        out["mut"] = [s for s in (canonical_smiles(x) for x in muts) if s][: cfg.n_random_mutants]
        return out

    def _passes_filters(self, smi: str) -> bool:
        if "*" in smi:
            return False
        m = Chem.MolFromSmiles(smi)
        if m is None:
            return False
        # single organic component; mixtures distort property scores
        if "." in Chem.MolToSmiles(m):
            return False
        try:
            if abs(Chem.GetFormalCharge(m)) > 1:
                return False
        except Exception:
            return False
        if any(a.GetSymbol() in ("Fe", "Zn", "Cu", "Mg", "Ca", "Na", "K", "Li", "Al", "Au", "Pt")
               for a in m.GetAtoms()):
            return False
        desc = compute_descriptors(m)
        if not passes_window(desc, self.window):
            return False
        if is_pains(smi):
            return False
        # known ChEMBL compounds never enter the pool (seed ligands excepted)
        if self.novelty_idx is not None and self.novelty_idx.is_known(smi) \
                and smi not in self.known_ligands:
            return False
        if self.memory.is_duplicate(smi) or self.memory.scaffold_full(smi):
            return False
        return True

    def _filter(self, proposals: dict[str, list[str]]) -> list[str]:
        keep: list[str] = []
        for src, smis in proposals.items():
            passed = 0
            for s in smis:
                if s in keep:
                    continue
                if self._passes_filters(s):
                    keep.append(s)
                    # channel + parent context for GRPO grouping
                    self._pool_meta[s] = (src, self._proposal_parent.get(s))
                    passed += 1
            stats = self.proposal_stats.setdefault(src, [0, 0])
            stats[0] += len(smis)
            stats[1] += passed
        return keep

    def _select_for_oracle(self, pool: list[str]) -> list[str]:
        cfg = self.cfg.loop
        budget = min(cfg.oracle_budget_per_round, len(pool))
        if budget <= 0:
            return []
        if not cfg.use_surrogate or self.surrogate.n_obs < 12:
            # cold start: diverse random+novelty-first selection
            self.rng.shuffle(pool)
            return pool[:budget]
        mu, sig = self.surrogate.predict(pool)
        aff_mu = mu[:, 0]
        aff_sig = sig[:, 0]
        # acquisition: exploit predicted affinity + explore uncertainty
        acq = aff_mu + cfg.acquisition_kappa * aff_sig
        order = np.argsort(-acq)
        n_exploit = int(budget * cfg.exploit_fraction)
        chosen = [pool[i] for i in order[:n_exploit]]
        # diversity-aware uncertainty fill
        from halo.loop.diversity import ScaffoldMemory

        smem = ScaffoldMemory(limit_per_scaffold=2)
        smem.register(chosen)
        for i in order[n_exploit:]:
            if len(chosen) >= budget:
                break
            if not smem.is_duplicate(pool[i]) and not smem.scaffold_full(pool[i]):
                chosen.append(pool[i])
                smem.register([pool[i]])
        return chosen

    def _compute_rewards(self, pool: list[str], oracle_df: pd.DataFrame) -> pd.DataFrame:
        """Rewards for the whole pool; oracle rows where scored, surrogate elsewhere."""
        scored = {}
        if len(oracle_df):
            for _, r in oracle_df.iterrows():
                if r.get("affinity_pic50") is not None and not (isinstance(r.get("affinity_pic50"), float) and math.isnan(r.get("affinity_pic50"))):
                    scored[r["smiles"]] = r
        unscored = [s for s in pool if s not in scored]
        if unscored and self.cfg.loop.use_surrogate and self.surrogate.n_obs >= 12:
            mu, sig = self.surrogate.predict(unscored)
        else:
            mu = np.tile(self.surrogate._mean, (len(unscored), 1)) if unscored else np.zeros((0, 3))
            sig = np.ones((len(unscored), 3))
        rows = []
        all_parts: list[dict] = []
        pref_conf = self.pref.confidence()
        kappa = getattr(self.cfg.loop, "conservative_kappa", 0.3)
        for s in pool:
            if s in scored:
                r = scored[s]
                aff, ips, plddt, src = (r["affinity_pic50"], r.get("ipsae"), r.get("ligand_plddt_mean"), "oracle")
                machine, parts = self.reward_fn.machine_reward(
                    s, float(aff), float(ips or 0), float(plddt or 0), iptm=r.get("iptm"))
            else:
                j = unscored.index(s)
                aff, ips, plddt, src = mu[j, 0], mu[j, 1], mu[j, 2], "surrogate"
                # conservative (risk-averse) surrogate score: pessimistic
                # affinity AND pessimistic pose evidence (unverified molecules
                # must not out-earn oracle-verified ones through the gate's
                # iptm=None default of 0.6)
                machine, parts = self.reward_fn.machine_reward(
                    s, float(aff), float(ips or 0), float(plddt or 0),
                    conservative_sigma=float(sig[j, 0]) * kappa,
                    surrogate_uncertainty=(float(sig[j, 1]), float(sig[j, 2])))
            final, bonus = self.reward_fn.final_reward(machine, s, pref_conf)
            m = Chem.MolFromSmiles(s)
            d = compute_descriptors(m) if m else {}
            rows.append({"smiles": s, "source": src, "affinity_pic50": aff, "ipsae": ips,
                         "ligand_plddt_mean": plddt, "machine_reward": machine,
                         "pref_bonus": bonus, "final_reward": final,
                         "desc": json.dumps({k: round(v, 3) for k, v in d.items()}) if d else "",
                         **{f"sur_{k}": None for k in TARGET_KEYS}})
            all_parts.append(parts)
        df = pd.DataFrame(rows)
        # per-dimension batch normalization keeps saturated objectives from
        # drowning the others in the combined reward
        if len(all_parts) == len(rows) and all_parts:
            try:
                normed = self.reward_fn.combine_batch(all_parts)
                for i, r in enumerate(rows):
                    r["machine_reward"] = (1 - 0.3) * r["machine_reward"] + 0.3 * normed[i]
                    lam_mix = self.reward_fn.pref_lambda_max * pref_conf
                    r["final_reward"] = (1 - lam_mix) * r["machine_reward"] + lam_mix * r["pref_bonus"]
                df = pd.DataFrame(rows)
            except Exception:
                pass
        # store surrogate uncertainty for unscored
        if len(unscored):
            for j, s in enumerate(unscored):
                df.loc[df["smiles"] == s, "sur_sigma_affinity"] = sig[j, 0]
        return df

    def _human_review(self, rewards_df: pd.DataFrame) -> FeedbackBatch:
        k = self.cfg.loop.human_topk
        top = rewards_df.sort_values("final_reward", ascending=False).head(k)
        uncertain = rewards_df[rewards_df["source"] == "surrogate"].nlargest(max(2, k // 3), "sur_sigma_affinity") if "sur_sigma_affinity" in rewards_df else pd.DataFrame()
        cands = []
        for _, r in pd.concat([top, uncertain]).drop_duplicates("smiles").iterrows():
            cands.append({"smiles": r["smiles"], "affinity_pic50": r.get("affinity_pic50"),
                          "ipsae": r.get("ipsae"), "reward": r["final_reward"],
                          "desc": json.loads(r["desc"]) if r.get("desc") else {}})
        fb = self.human.review(cands, {"round": self.round_i})
        return fb

    def _apply_feedback(self, fb: FeedbackBatch) -> None:
        if not fb:
            return
        n_added = 0
        rules_credited = 0
        for a, b in fb.pairs:
            if self.pref.add_pair_from_smiles(a, b):
                n_added += 1
                rules_credited += 1
        # accepted/rejected become implicit preferences vs the batch median
        if fb.accepted or fb.rejected:
            for s in fb.accepted:
                for t in fb.rejected:
                    self.pref.add_pair_from_smiles(s, t)
                    if self.cfg.loop.use_dpo and self.dpo is not None:
                        fb.pairs.append((s, t))
        info = self.pref.fit()
        # DPO: align the generative agent directly on preference pairs
        dpo_info = {}
        if self.cfg.loop.use_dpo and fb.pairs:
            if self.dpo is None:
                from halo.generate.dpo import DPOUpdater

                self.dpo = DPOUpdater(self.agent, self.prior, self.vocab, device=self.device)
            dpo_info = self.dpo.update(list(dict.fromkeys(fb.pairs)), log=self.log)
        for k, v in fb.rules.items():
            if k.endswith("_max"):
                key = k[:-4]
                if key in self.window:
                    self.window[key] = (self.window[key][0], v)
            elif k.endswith("_min"):
                key = k[:-4]
                if key in self.window:
                    self.window[key] = (v, self.window[key][1])
        for k, v in fb.weights.items():
            if k in self.reward_fn.weights:
                self.reward_fn.weights[k] = v
        self.feedback_log.append({"round": self.round_i, "pairs": n_added,
                                  "pref_pairs_total": self.pref.n_pairs, "fit": info,
                                  "mmp_rules_credited": rules_credited, "dpo": dpo_info,
                                  "rules": fb.rules, "weights": fb.weights})
        self.log(f"[round {self.round_i}] human feedback: +{n_added} pairs (total {self.pref.n_pairs}), "
                 f"dpo={dpo_info.get('implicit_acc', '-')}, "
                 f"pref train_acc={info.get('train_acc', float('nan')):.2f}")

    # ------------------------------------------------------------------ main
    def run(self, n_rounds: int | None = None) -> dict:
        n_rounds = n_rounds or self.cfg.loop.n_rounds
        t_start = time.time()
        for _ in range(n_rounds):
            t0 = time.time()
            self.round_i += 1

            proposals = self._propose()
            pool = self._filter(proposals)
            if not pool:
                self.log(f"[round {self.round_i}] empty pool after filters")
                continue
            self.memory.register(pool)

            selected = self._select_for_oracle(pool)
            oracle_df = self.oracle.score_smiles(selected, tag=f"r{self.round_i:03d}") if selected else pd.DataFrame()
            if len(oracle_df):
                oracle_df["round"] = self.round_i
                oracle_df.to_csv(self.oracle_csv, mode="a", header=not self.oracle_csv.exists(), index=False)
                self.oracle_log.extend(oracle_df.to_dict("records"))
            if self.cfg.loop.use_surrogate and len(oracle_df):
                self.surrogate.add_observations(oracle_df.to_dict("records"))
                fit_info = self.surrogate.fit()
            else:
                fit_info = {"n": self.surrogate.n_obs}

            rewards_df = self._compute_rewards(pool, oracle_df)
            rewards_df.insert(0, "round", self.round_i)
            rewards_df.to_csv(self.candidates_csv, mode="a", header=not self.candidates_csv.exists(), index=False)

            # elite pool update (machine reward, oracle-scored preferred)
            pref_oracle = rewards_df[rewards_df["source"] == "oracle"]
            pool_rank = pref_oracle if len(pref_oracle) >= 4 else rewards_df
            top = pool_rank.sort_values("final_reward", ascending=False).head(12)
            self.elite_pool.extend([s for s in top["smiles"] if s not in self.elite_pool][-16:])
            self.best_machine = (top.to_dict("records") + self.best_machine)[:64]

            # agent RL update with final rewards on the filtered pool
            if self.cfg.loop.use_agent:
                samples = list(zip(rewards_df["smiles"], rewards_df["final_reward"],
                                   rewards_df["source"] if "source" in rewards_df else ["mix"] * len(rewards_df)))
                # multi-objective shaping: TanhIMS soft diversity + RND novelty
                if getattr(self.cfg.loop, "use_rnd_diversity", True):
                    import numpy as _np
                    from halo.generate.rl_components import compose_multiobjective_reward

                    smiles_arr = [s for s, _, _ in samples]
                    base = _np.array([r for _, r, _ in samples], dtype=float)
                    shaped = compose_multiobjective_reward(base, smiles_arr,
                                                           scaffold_penalty=self._ims,
                                                           rnd=self._rnd)
                    samples = list(zip(smiles_arr, shaped.tolist(),
                                       [src for _, _, src in samples]))
                if isinstance(self.updater, GRPOUpdater):
                    # GRPO trains on the SAFE text, grouped by proposal
                    # context (parent for edits, scaffold otherwise); edit
                    # proposals keep their conditioned trajectory so the RL
                    # signal improves the edit operator itself
                    upd_samples = []
                    for s, r, src in samples:
                        chan, parent = self._pool_meta.get(s, (None, None))
                        cond = self._proposal_cond.get(s) if chan == "edit" else None
                        key = parent or GRPOUpdater._fallback_group(s)
                        if cond is not None:
                            prompt_text, tail_text = cond
                            plen = len(self.vocab.tok.encode(prompt_text).ids)
                            upd_samples.append((prompt_text + tail_text, float(r), key, str(src), plen))
                            continue
                        st = self._safe_encode(s)
                        if not st:
                            continue
                        upd_samples.append((st, float(r), key, str(src)))
                    # randomized traversals reuse each oracle score twice
                    import safe as _sl

                    for s, r, src in samples:
                        if src != "oracle":
                            continue
                        for _ in range(2):
                            try:
                                st = _sl.encode(s, canonical=False,
                                                seed=self.rng.randrange(1 << 30))
                            except Exception:
                                continue
                            if st:
                                upd_samples.append((st, float(r),
                                                    GRPOUpdater._fallback_group(s), "augmented"))
                    upd = self.updater.update(upd_samples,
                                              epochs=self.cfg.generator.rl_epochs_per_round,
                                              log=self.log)
                else:
                    upd = self.updater.update([(s, r) for s, r, _ in samples],
                                              alpha=self.alpha,
                                              sigma=self.cfg.generator.sigma,
                                              kl_beta=self.cfg.generator.kl_beta,
                                              epochs=self.cfg.generator.rl_epochs_per_round,
                                              replay=self.replay)
                self.alpha = max(self.cfg.generator.alpha_min, self.alpha * self.cfg.generator.alpha_decay)
            else:
                upd = {}

            # human review
            if self.cfg.loop.use_human and self.round_i % self.cfg.loop.human_every_rounds == 0:
                fb = self._human_review(rewards_df)
                self._apply_feedback(fb)

            stats = {
                "round": self.round_i,
                "pool": len(pool),
                "proposals": {k: len(v) for k, v in proposals.items()},
                "oracle_calls": len(oracle_df),
                "oracle_s": float(oracle_df.attrs.get("wall_s", 0.0)) if len(oracle_df) else 0.0,
                "surrogate_n": self.surrogate.n_obs,
                "surrogate_fit": fit_info,
                "top_final_reward": float(rewards_df["final_reward"].max()),
                "mean_final_reward": float(rewards_df["final_reward"].mean()),
                "top_machine_oracle": float(pref_oracle["machine_reward"].max()) if len(pref_oracle) else None,
                "best_affinity_oracle": float(pd.to_numeric(pref_oracle["affinity_pic50"], errors="coerce").max()) if len(pref_oracle) else None,
                "alpha": self.alpha,
                "agent_update": upd,
                "pref_pairs": self.pref.n_pairs,
                "elapsed_s": time.time() - t0,
            }
            self.rounds_log.append(stats)
            (self.run_dir / "rounds.jsonl").write_text("\n".join(json.dumps(s, default=str) for s in self.rounds_log))
            self.log(
                f"[round {self.round_i:3d}] pool={stats['pool']:4d} oracle={stats['oracle_calls']:3d} "
                f"topR={stats['top_final_reward']:.3f} meanR={stats['mean_final_reward']:.3f} "
                f"sur={stats['surrogate_n']:3d} pairs={stats['pref_pairs']:3d} "
                f"({stats['elapsed_s']:.0f}s)"
            )
            self._checkpoint()
        return {"rounds": self.rounds_log, "total_s": time.time() - t_start}

    def _safe_encode(self, smi: str) -> str | None:
        if smi not in self._safe_cache:
            from halo.generate.safe_tasks import safe_encode_canonical
            self._safe_cache[smi] = safe_encode_canonical(smi)
        return self._safe_cache[smi]

    def _edit_channel(self, take: int) -> list[str]:
        """unified_edit on elite parents; a fraction of edits drops the core
        (scaffold hop) with the radius sampled per attempt."""
        import random as _r

        from halo.generate.safe_edit import unified_edit

        parents = self.elite_pool[-10:] or self.rng.sample(self.known_ligands, k=min(6, len(self.known_ligands)))
        out = []
        hop_ratio = float(getattr(self.cfg.loop, "scaffold_hop_ratio", 0.0) or 0.0)
        budget = take * max(len(parents), 1)
        n_hop_total = int(round(budget * hop_ratio))
        n_hop_done = 0
        for p in parents:
            ss = self._safe_encode(p)
            if not ss:
                continue
            rng = _r.Random(self.rng.randrange(1 << 30))
            got = unified_edit(self.agent, self.vocab, p, ss, self.device,
                               n=16, rng=rng, window=self.window,
                               radius=None, keep_core=True)
            out.extend(r["smiles"] for r in got)
            self._register_proposals(got, p)
            if n_hop_done < n_hop_total:
                hops = unified_edit(self.agent, self.vocab, p, ss, self.device,
                                    n=12, rng=rng, window=self.window,
                                    radius=None, keep_core=False)
                out.extend(r["smiles"] for r in hops)
                n_hop_done += len(hops)
                self._register_proposals(hops, p)
        from halo.score.properties import canonical_smiles as _cs

        return [s for s in (_cs(x) for x in out) if s][:take * max(len(parents), 1)]

    def _register_proposals(self, results, parent: str) -> None:
        from halo.score.properties import canonical_smiles as _cs

        for r in results:
            cs = _cs(r["smiles"]) or r["smiles"]
            self._proposal_parent.setdefault(cs, parent)
            if r.get("cond_prompt"):
                self._proposal_cond[cs] = (r["cond_prompt"], r["cond_tail"])

    def _checkpoint(self) -> None:
        torch.save(self.agent.state_dict(), self.run_dir / "agent.pt")
        if self.surrogate.n_obs:
            self.surrogate.save(self.run_dir / "surrogate.pt")
        if self.pref.n_pairs:
            self.pref.save(self.run_dir / "pref.npz")
        with open(self.run_dir / "feedback.jsonl", "w") as f:
            for r in self.feedback_log:
                f.write(json.dumps(r, default=str) + "\n")
        with open(self.run_dir / "final_state.json", "w") as f:
            json.dump({"round": self.round_i, "alpha": self.alpha,
                       "window": {k: list(v) for k, v in self.window.items()},
                       "reward_weights": self.reward_fn.weights,
                       "elite": self.elite_pool[-16:],
                       "oracle_calls": self.oracle.n_calls,
                       "oracle_gpu_s": self.oracle.n_gpu_seconds}, f, indent=1)
