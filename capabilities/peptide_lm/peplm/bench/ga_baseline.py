"""V-Bio production GA baseline (faithful port for head-to-head benchmarks).

Replicates the backend peptide design loop exactly: strategy-weighted mutation
(exploit .48 / diversify .24 / explore .18 / crossover .10), pLDDT-weighted
position choice, conservative substitution table, random NCAA overlay
(_sample_peptide_modifications semantics), the production composite score, and
the production NSGA-II elite selection (including its first-front-only
behavior — ported verbatim so the baseline IS the production algorithm).

Both arms in a benchmark share the same Boltz oracle instance settings and the
same per-round candidate/oracle budget.
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

from peplm.candidate import Candidate
from peplm.residues import NCAA_PRESETS
from peplm.score.production import (
    production_composite,
    production_liability_penalty,
)

AA20 = "ACDEFGHIKLMNPQRSTVWY"
CONSERVATIVE = {
    "A": "GSV", "R": "KHQ", "N": "DQST", "D": "EN", "C": "ST", "Q": "ENKR",
    "E": "DQK", "G": "AS", "H": "NQKR", "I": "LVMA", "L": "IVMF", "K": "RQE",
    "M": "ILV", "F": "YWL", "P": "AGS", "S": "ATGN", "T": "SAV", "W": "FY",
    "Y": "FW", "V": "ILMA",
}
STRATEGY_WEIGHTS = [("exploit", 0.48), ("diversify", 0.24),
                    ("explore", 0.18), ("crossover", 0.10)]


def _weighted_choice(options, weights, rng):
    return rng.choices(options, weights=weights, k=1)[0]


class GABaseline:
    def __init__(self, oracle, run_dir, *, peptide_length=15, population=16,
                 elite_size=4, mutation_rate=0.25, generations=12,
                 ncaa_pool=None, ncaa_min=0, ncaa_max=0, initial_sequence=None,
                 seed=0, log=print):
        self.oracle = oracle
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.L = peptide_length
        self.pop = population
        self.elite_size = elite_size
        self.rate = mutation_rate
        self.generations = generations
        self.ncaa_pool = list(ncaa_pool or [])
        self.ncaa_min = ncaa_min
        self.ncaa_max = ncaa_max
        self.initial_sequence = initial_sequence
        self.rng = random.Random(seed)
        self.log = log
        self.all_results: list[dict] = []
        self.seen: set[str] = set()

    # ------------------------------------------------- production moves
    def _random_sequence(self) -> str:
        return "".join(self.rng.choice(AA20) for _ in range(self.L))

    def _mutate(self, sequence: str, strategy: str, plddt_scores,
                elite_sequences) -> str:
        seq = list(sequence.upper())
        available = list(range(len(seq)))
        if not available:
            return "".join(seq)
        base_mutations = max(1, int(round(len(available) * max(0.01, min(1.0, self.rate)))))
        if strategy == "explore":
            n = max(base_mutations, min(len(available), max(2, len(available) // 3)))
        elif strategy == "diversify":
            n = max(base_mutations, min(len(available), max(2, len(available) // 4)))
        elif strategy == "crossover":
            n = max(1, min(len(available), base_mutations // 2 or 1))
        else:
            n = min(len(available), base_mutations)
        if strategy == "crossover" and elite_sequences:
            mate = self.rng.choice([e for e in elite_sequences if len(e) == len(seq)]
                                   or elite_sequences)
            if len(mate) == len(seq):
                cut = self.rng.randint(1, len(seq) - 1) if len(seq) > 1 else 1
                seq = seq[:cut] + list(mate[cut:])
        if strategy == "diversify" and elite_sequences:
            sim = sorted(available,
                         key=lambda i: -sum(1 for e in elite_sequences
                                            if len(e) > i and e[i] == seq[i]))
            positions = sim[:n]
        elif plddt_scores and len(plddt_scores) == len(seq) and strategy != "explore":
            weights = [max(1.0, 100.0 - float(plddt_scores[i])) for i in available]
            positions = []
            rem, rem_w = list(available), list(weights)
            for _ in range(min(n, len(rem))):
                c = _weighted_choice(rem, rem_w, self.rng)
                positions.append(c)
                rem_w.pop(rem.index(c))
                rem.remove(c)
        else:
            positions = self.rng.sample(available, k=min(n, len(available)))
        for pos in positions:
            cur = seq[pos]
            cands = [a for a in AA20 if a != cur]
            if strategy == "explore":
                seq[pos] = self.rng.choice(cands)
                continue
            cons = set(CONSERVATIVE.get(cur, ""))
            seq[pos] = _weighted_choice(cands, [2.5 if a in cons else 1.0 for a in cands], self.rng)
        return "".join(seq)

    def _overlay_ncaa(self, sequence: str) -> tuple[str, list[dict]]:
        """Production _sample_peptide_modifications: random positions, random
        NCAA from the pool (placement-aware), base residue written back."""
        if not self.ncaa_pool or self.ncaa_max <= 0:
            return sequence, []
        L = len(sequence)

        def allowed_at(idx):
            out = []
            for ccd in self.ncaa_pool:
                pl = NCAA_PRESETS.get(ccd, {}).get("placement", "any")
                if pl == "n_term" and idx != 0:
                    continue
                if pl in ("c_term", "terminal") and idx != L - 1:
                    continue
                out.append(ccd)
            return out

        eligible = [i for i in range(L) if allowed_at(i)]
        rmin = max(0, min(L, self.ncaa_min))
        rmax = max(rmin, min(L, self.ncaa_max))
        if rmin > len(eligible):
            return sequence, []
        eff_max = min(rmax, len(eligible))
        count = self.rng.randint(rmin, eff_max) if eff_max > rmin else rmin
        if count <= 0:
            return sequence, []
        positions = self.rng.sample(eligible, k=count)
        seq = list(sequence)
        mods = []
        for idx in positions:
            ccd = self.rng.choice(allowed_at(idx))
            base = NCAA_PRESETS[ccd]["base"]
            seq[idx] = base
            mods.append({"position": idx + 1, "ccd": ccd, "baseResidue": base})
        mods.sort(key=lambda m: m["position"])
        return "".join(seq), mods

    # ------------------------------------------------- NSGA-II (verbatim)
    def _objectives(self, row):
        def f(key, default=0.0):
            v = row.get(key)
            return float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else default
        return (f("interface_confidence"), f("binder_confidence"),
                f("pair_iptm_confidence"), f("developability_score", 1.0))

    def _dominates(self, a, b):
        oa, ob = self._objectives(a), self._objectives(b)
        return all(x >= y for x, y in zip(oa, ob)) and any(x > y for x, y in zip(oa, ob))

    def _rank(self, row):
        v = row.get("composite_score")
        return float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else float("-inf")

    def _select_elites(self, results):
        cands = [r for r in results if r.get("sequence")]
        # domination counts / first front (production non-dominated fronts)
        dom_count, dominated = {}, {}
        first = []
        for i, row in enumerate(cands):
            dominated_by, cnt = [], 0
            for j, other in enumerate(cands):
                if i == j:
                    continue
                if self._dominates(row, other):
                    dominated_by.append(j)
                elif self._dominates(other, row):
                    cnt += 1
            dom_count[i] = cnt
            dominated[i] = dominated_by
            if cnt == 0:
                first.append(i)
        selected = []
        front = [cands[i] for i in first]
        if front:
            # crowding distance on the first front (production behavior:
            # only the first front is used, then padding by composite rank)
            def crowd(front_rows):
                if len(front_rows) <= 2:
                    return {i: float("inf") for i in range(len(front_rows))}
                dist = {i: 0.0 for i in range(len(front_rows))}
                for oi in range(4):
                    vals = [self._objectives(r)[oi] for r in front_rows]
                    order = sorted(range(len(front_rows)), key=lambda k: vals[k])
                    dist[order[0]] = dist[order[-1]] = float("inf")
                    span = vals[order[-1]] - vals[order[0]]
                    if span <= 1e-12:
                        continue
                    for r_ in range(1, len(order) - 1):
                        dist[order[r_]] += (vals[order[r_ + 1]] - vals[order[r_ - 1]]) / span
                return dist

            dist = crowd(front)
            ranked = sorted(enumerate(front),
                            key=lambda kv: (dist.get(kv[0], 0.0), self._rank(kv[1])),
                            reverse=True)
            for _, row in ranked:
                seq = row.get("base", row["sequence"])
                if all(self._similarity(seq, p.get("base", p["sequence"])) < 0.92
                       for p in selected):
                    selected.append(row)
                if len(selected) >= self.elite_size:
                    break
        if len(selected) < self.elite_size:
            for row in sorted(results, key=self._rank, reverse=True):
                if row not in selected:
                    selected.append(row)
                if len(selected) >= self.elite_size:
                    break
        return selected[: self.elite_size]

    @staticmethod
    def _similarity(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x == y for x, y in zip(a, b)) / max(1, len(a))

    # ------------------------------------------------------------- run
    def run(self):
        t0 = time.time()
        # init population (lead-opt: seeded with the provided initial
        # sequence, mirroring the production peptideInitialSequence option)
        seqs = [self._random_sequence() for _ in range(self.pop)]
        if self.initial_sequence:
            seqs[0] = str(self.initial_sequence).upper()[: self.L] \
                .ljust(self.L, "A")
        elite_pop = []
        for gen in range(1, self.generations + 1):
            gen_rows = []
            # build this generation's candidates (elites + mutants)
            tasks = list(elite_pop)
            while len(tasks) < self.pop:
                parent = self.rng.choice(elite_pop) if elite_pop else self.rng.choice(seqs)
                strat = _weighted_choice([s for s, _ in STRATEGY_WEIGHTS],
                                         [w for _, w in STRATEGY_WEIGHTS], self.rng)
                elite_seqs = [e for e in elite_pop] or None
                plddts = None
                if elite_pop and strat != "explore" and self.all_results:
                    prev = [r for r in self.all_results
                            if r["sequence"] == parent and r.get("plddts")]
                    if prev:
                        plddts = prev[-1]["plddts"]
                mut = self._mutate(parent, strat, plddts, elite_seqs)
                tasks.append(mut)
            # dedup within generation
            uniq, seen = [], set()
            for s in tasks:
                if s not in seen:
                    seen.add(s)
                    uniq.append(s)
            cands = []
            for s in uniq:
                # genotype = natural base sequence (production contract);
                # NCAAs are overlaid fresh per candidate, never mutated through
                base, mods = self._overlay_ncaa(s)
                key = f"{base}|{json.dumps(mods, sort_keys=True)}"
                if key in self.seen:
                    continue
                self.seen.add(key)
                toks = ["<lin>"] + _tokens_from_base_mods(base, mods)
                cands.append((base, mods, Candidate(tokens=toks, origin="ga")))
            if cands:
                self.oracle.score([c for _, _, c in cands], tag=f"ga_g{gen:03d}")
            for base, mods, c in cands:
                m = c.metrics
                iptm = m.get("pair_iptm") or m.get("iptm")
                interface = (m.get("ipsae_dom") if m.get("ipsae_dom") is not None
                             else m.get("ligand_ipsae_max") if m.get("ligand_ipsae_max") is not None
                             else iptm)
                liab = production_liability_penalty(base)
                row = {
                    "sequence": c.seq_str,
                    "base": base,
                    "mods": mods,
                    "interface_confidence": max(0.0, min(1.0, float(interface))) if interface is not None else 0.0,
                    "binder_confidence": max(0.0, min(1.0, m.get("binder_avg_plddt", 0) / 100.0)),
                    "pair_iptm_confidence": max(0.0, min(1.0, float(iptm))) if iptm is not None else 0.0,
                    "developability_score": max(0.0, 1.0 - liab["penalty"]),
                    "plddts": m.get("binder_plddt") or [],
                    "generation": gen,
                }
                row["composite_score"] = production_composite(m, base)
                gen_rows.append(row)
            self.all_results.extend(gen_rows)
            elite_pop = [r["base"] for r in self._select_elites(self.all_results)]
            comps = [r["composite_score"] for r in gen_rows
                     if r.get("composite_score") is not None]
            self.log(f"[ga g{gen:2d}] candidates={len(cands):3d} "
                     f"bestComp={max(comps) if comps else float('nan'):.3f} "
                     f"elite={'/'.join(elite_pop[:2])[:24]}")
        best = sorted((r for r in self.all_results
                       if r.get("composite_score") is not None),
                      key=lambda r: -r["composite_score"])
        out = {"best_composite": best[0]["composite_score"] if best else None,
               "top5_mean": (sum(r["composite_score"] for r in best[:5]) / min(5, len(best))) if best else None,
               "unique": len({r["sequence"] for r in self.all_results}),
               "oracle_calls": self.oracle.n_calls,
               "elapsed_s": time.time() - t0}
        (self.run_dir / "ga_results.json").write_text(
            json.dumps({"summary": out, "top": best[:20]}, indent=1, default=str))
        return out


def _tokens_from_base_mods(base: str, mods: list[dict]) -> list[str]:
    toks = list(base)
    for m in mods:
        pos = int(m["position"]) - 1
        if 0 <= pos < len(toks):
            toks[pos] = f"[{m['ccd']}]"
    return toks
