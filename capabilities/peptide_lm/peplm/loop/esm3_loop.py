"""Online ESM3 RL design loop: propose -> oracle -> reward -> GRPO.

Interface PAE carries the gradient (a cold policy's candidates all score
constant ipSAE ~0, which would zero the group advantage); per-residue
credit weights the GRPO tokens; refill groups keep >= 2 members, since
singletons carry no advantage after mean-centering."""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from peplm.generate.masked_grpo import MaskedGRPOUpdater, Rollout
from peplm.models.esm3_policy import ESM3Policy
from peplm.oracle.esm3_oracle import OracleConfig, dock_batch
from peplm.score.peptide_reward import (
    RelativeReward,
    sequence_is_eligible,
)


@dataclass
class LoopBudget:
    """Per-round dock budget split between exploration and exploitation."""
    oracle_batch: int = 12        # total oracle docks per round
    oversample: int = 48          # de novo samples drawn before filtering
    parents_kept: int = 2         # top parents carried into the next round
    refills_per_parent: int = 3   # sibling edits per parent (own GRPO group)

    def __post_init__(self):
        # refill groups claim slots whole; over-budget refills would
        # starve de novo entirely
        if self.parents_kept * self.refills_per_parent > self.oracle_batch - 2:
            raise ValueError(
                f"refill demand ({self.parents_kept}x{self.refills_per_parent}) "
                f"leaves no de novo slots in an oracle batch of "
                f"{self.oracle_batch}")
        if self.refills_per_parent < 2:
            raise ValueError("refills_per_parent >= 2: singleton GRPO "
                             "groups carry no advantage")


class ESM3Loop:
    def __init__(
        self,
        policy: ESM3Policy,
        oracle_cfg: OracleConfig,
        receptor_sequence: str,
        run_root: Path,
        *,
        pep_len: int = 14,
        budget: LoopBudget | None = None,
        gpus: tuple[int, ...] = (3,),
        seed: int = 101,
        weak_frac: float = 0.4,
        log=print,
    ):
        self.pol = policy
        self.cfg = oracle_cfg
        self.rec_seq = receptor_sequence
        self.run_root = Path(run_root)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.bdg = budget or LoopBudget()
        self.gpus = list(gpus)
        self.seed = seed
        self.weak_frac = weak_frac
        self._pep_len = pep_len
        self.reward = RelativeReward()
        self.updater = MaskedGRPOUpdater(policy)
        self.history: list[dict] = []
        self.log = log
        self._inv = {v: k for k, v in policy.toks.sequence.vocab.items()}

    # util
    def _decode(self, toks) -> str:
        return "".join(self._inv[t] for t in toks)

    def _rollout(self, traj, group: str, credit, rewards, pep_start: int,
                 ) -> Rollout:
        return Rollout(states=[s.cpu() for s in traj.states],
                       revealed=traj.revealed,
                       old_logprob=traj.old_logprob,
                       temps=traj.temps,
                       rewards=rewards,
                       group=group,
                       credit=[float(c) for c in credit],
                       pep_start=pep_start)

    def _novel(self, seq: str) -> bool:
        return sequence_is_eligible(seq, min_entropy=getattr(self, '_min_entropy', 2.2)) and self.reward.memory.is_novel(seq)

    # round
    def round(self, r: int) -> dict:
        t0 = time.time()
        parents = self.history[-1]["parents"] if self.history else []
        bd = self.bdg
        pep_start = self.pol.peptide_start(self.rec_seq)
        pending: list[dict] = []       # oracle queue (unregistered until sent)

        # 1a) sibling refills per parent: a group that cannot reach >= 2
        # members is dropped whole (singletons carry zero advantage)
        for pi, parent in enumerate(parents):
            weak = ESM3Policy.weak_positions(parent["credit"],
                                             max_frac=self.weak_frac)
            group: list[dict] = []
            attempts = 0
            while len(group) < bd.refills_per_parent and attempts < bd.refills_per_parent * 3:
                attempts += 1
                tr = self.pol.refill(self.rec_seq, parent["tokens"], weak,
                                     num_steps=3, temperature=0.7)
                if tr is None:
                    continue
                seq = self._decode(tr.peptide_tokens)
                if not self._novel(seq) or seq in (p["seq"] for p in pending):
                    continue
                group.append({"seq": seq, "traj": tr,
                              "group": f"parent{pi}", "parent_idx": pi})
            if len(group) >= 2:
                pending.extend(group)
            elif group:
                self.log(f"[loop r{r}] parent{pi} refill group degenerated "
                         f"to {len(group)}; dropped")

        # 1b) de novo, sampled with slack so filtering cannot shrink the
        # exploration group below 2
        n_denovo = max(bd.oracle_batch - len(pending), 2)
        trajs = self.pol.sample_batch(
            self.rec_seq, pep_len=self.pep_len, n=bd.oversample,
            keep=min(n_denovo + 4, bd.oversample), temperature=0.9,
            num_steps=6)
        seen = {p["seq"] for p in pending}
        n_denovo_live = 0
        for tr in trajs:
            if n_denovo_live >= n_denovo:
                break
            seq = self._decode(tr.peptide_tokens)
            if not self._novel(seq) or seq in seen:
                continue
            seen.add(seq)
            n_denovo_live += 1
            pending.append({"seq": seq, "traj": tr, "group": "denovo",
                            "parent_idx": None})

        if len(pending) < 4 or n_denovo_live < 2:
            self.log(f"[loop r{r}] {len(pending)} proposals "
                     f"({n_denovo_live} de novo); insufficient groups, "
                     "skipping round")
            return {"round": r, "skipped": True}
        # budget commits only now: sent sequences enter the diversity
        # memory, a skipped round burns nothing
        for p in pending:
            self.reward.memory.register(p["seq"])

        # 2) oracle
        parent_cifs = []
        for p in pending:
            cif = None
            if p.get("parent_idx") is not None:
                # the parent's docked CIF from the previous round
                cands = sorted((self.run_root / f"r{r-1:03d}").glob(
                    f"c{p['parent_idx']:02d}*/out/*/seed_*/predictions/*_sample_*.cif"))
                if cands:
                    cif = Path(cands[-1])
            parent_cifs.append(cif)
        results = dock_batch([p["seq"] for p in pending], self.cfg,
                             self.run_root / f"r{r:03d}", self.gpus,
                             seed=self.seed + 1000 * r,
                             parents=parent_cifs, log=self.log)
        records = [{**p, "metrics": res.metrics,
                    "min_clash_dist": res.min_clash_dist,
                    "pairs_deep": res.pairs_deep,
                    "nonlocal_contacts": res.nonlocal_contacts,
                    "caca_min": res.caca_min,
                    "chir_mixed": res.chir_mixed,
                    "omega_bad": res.omega_bad,
                    "cif": res.cif, "error": res.error}
                   for p, res in zip(pending, results)]

        # 3) reward: round-normalised tuples + per-residue credit
        self.reward.score_round(records)

        # 4) GRPO update over real groups
        rollouts = [self._rollout(rec["traj"], rec["group"], rec["credit"],
                                  rec["rewards"], pep_start)
                    for rec in records]
        stats = self.updater.update(rollouts, log=self.log)
        self.pol.save_adapter(self.run_root / "adapter")

        # 5) bookkeeping: parents ranked by the binding axis
        ok = [rec for rec in records if rec["metrics"] is not None]
        ok.sort(key=lambda rec: rec["rewards"][0], reverse=True)
        new_parents = [{
            "seq": rec["seq"], "tokens": rec["traj"].peptide_tokens,
            "credit": [float(c) for c in rec["credit"]],
            "ipsae": rec["metrics"].ipsae,
            "ipae": rec["metrics"].mean_interface_pae,
            "plddt": rec["metrics"].binder_plddt,
        } for rec in ok[:self.bdg.parents_kept]]
        if len(new_parents) < self.bdg.parents_kept and parents:
            new_parents += parents[:self.bdg.parents_kept - len(new_parents)]

        summary = {
            "round": r, "proposals": len(records),
            "docked": len(ok),
            "failed": len(records) - len(ok),
            "best_ipsae": max((rec["metrics"].ipsae for rec in ok), default=0.0),
            "best_ipae": min((rec["metrics"].mean_interface_pae for rec in ok),
                             default=None),
            "best_plddt": max((rec["metrics"].binder_plddt for rec in ok),
                              default=0.0),
            "passed_bar": sum(bool(rec.get("passes_bar")) for rec in ok),
            "parents": new_parents, "stats": stats,
            "elapsed_s": round(time.time() - t0, 1),
        }
        self.history.append(summary)
        hist = dict(summary)
        hist["quality"] = {k: v for k, v in (hist.get("quality") or {}).items()
                           if isinstance(v, (int, float, bool, str))}
        with open(self.run_root / "history.jsonl", "a") as fh:
            fh.write(json.dumps({**hist, "parents": [
                {k: v for k, v in p.items() if k != "tokens"}
                for p in new_parents]}) + "\n")
        for rec in ok[:2]:
            if rec["cif"]:
                shutil.copy(rec["cif"],
                            self.run_root / f"best_r{r:03d}.cif")
        # quality battery on the round's best candidate, trended per round
        from peplm.score.structure_quality import audit_complex, summary_line
        if ok and ok[0].get("cif"):
            q = audit_complex(ok[0]["cif"])
            summary["quality"] = q
            self.log(f"[loop r{r}] best quality: {summary_line(q)}")
        self.log(f"[loop r{r}] {len(ok)}/{len(records)} docked, "
                 f"best ipSAE={summary['best_ipsae']:.4f} "
                 f"iPAE={summary['best_ipae'] or 0:.2f} "
                 f"pLDDT={summary['best_plddt']:.1f} "
                 f"({summary['elapsed_s']:.0f}s)")
        return summary

    @property
    def pep_len(self) -> int:
        return self._pep_len

    @pep_len.setter
    def pep_len(self, v: int):
        self._pep_len = int(v)
