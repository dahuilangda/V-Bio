"""ESM3 proposal engine — HTTP client to the ESM3 inference service.

Implements a two-mode proposal strategy per generation (BindCraft2-style
semigreedy mutation + de novo exploration):

- Generation 1: full de novo sampling from the receptor-conditioned prior.
- Generation 2+: for each elite parent, mutate the positions with the
  lowest per-residue pLDDT (p_i proportional to 1 - pLDDT_i), keeping
  confident residues fixed. A de novo quota stays open so the search
  never collapses onto the surviving parents' neighborhoods.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from backend.core.config import (
        ESM3_SERVER_URL, ESM3_TIMEOUT_SECONDS,
        ESM3_SS_PROFILE, ESM3_PEPTIDE_FIRST,
    )
except ImportError:
    ESM3_SERVER_URL = "http://localhost:9333"
    ESM3_TIMEOUT_SECONDS = 600
    ESM3_SS_PROFILE = "H"
    ESM3_PEPTIDE_FIRST = False

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_S = 3


def _weighted_sample(items: list, probs: list, k: int) -> list:
    """Weighted sampling without replacement (no numpy dependency)."""
    items = list(items)
    probs = list(probs)
    chosen = []
    for _ in range(min(k, len(items))):
        r = random.random() * sum(probs)
        cum = 0.0
        for idx in range(len(items)):
            cum += probs[idx]
            if r <= cum:
                chosen.append(items.pop(idx))
                probs.pop(idx)
                break
        else:
            chosen.append(items.pop())
            probs.pop()
    return chosen


def _sequence_identity(a: str, b: str) -> float:
    """Fractional identity between two equal-length sequences."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def _diversity_filter(
    proposals: list[tuple], threshold: float = 0.85
) -> list[tuple]:
    """Reject proposals whose identity to an already-accepted proposal
    exceeds the threshold. Greedy first-come-first-kept; the caller
    sorts by quality before filtering so the best variant of each
    cluster survives."""
    kept: list[tuple] = []
    kept_seqs: list[str] = []
    for prop in proposals:
        seq = prop[0]
        if any(_sequence_identity(seq, k) > threshold for k in kept_seqs):
            continue
        kept.append(prop)
        kept_seqs.append(seq)
    return kept


class ESM3Proposer:
    """Receptor-conditioned proposals from ESM3 with pLDDT-guided mutation."""

    def __init__(
        self,
        receptor_sequence: str,
        peptide_length: int | None = None,
        len_range: tuple[int, int] = (10, 18),
        cyclic: bool = False,
        seed: int = 42,
        log=print,
        **_unused: Any,
    ):
        self.receptor_sequence = receptor_sequence
        self.peptide_length = peptide_length
        self.len_range = len_range
        self._log = log
        self._generation = 0
        self._endpoint = ESM3_SERVER_URL.rstrip("/")
        # ss8 conditioning + context layout, identical across
        # propose/refill/perplexity/learn so the policy family is stable
        self._cond = {
            "ss_profile": ESM3_SS_PROFILE or None,
            "peptide_first": bool(ESM3_PEPTIDE_FIRST),
        }

    def _post(self, payload: dict, retries: int = _RETRY_ATTEMPTS) -> dict:
        data = json.dumps(payload).encode()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    self._endpoint, data=data,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(
                        req, timeout=ESM3_TIMEOUT_SECONDS) as resp:
                    return json.loads(resp.read())
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(_RETRY_DELAY_S * (attempt + 1))
        raise ConnectionError(
            f"ESM3 service unreachable at {self._endpoint} "
            f"after {retries + 1} attempts: {last_exc}")

    def propose(
        self,
        natural_pool: list[str],
        unnatural_pool: list[dict],
        elite_rows: list[dict],
        n: int,
        plddt_hint: float | None = None,
    ) -> list[tuple[str, list[dict], list[int], str]]:
        proposals: list[tuple[str, list[dict], list[int], str]] = []

        # Semigreedy mutation from elites (pLDDT-guided, 2/3 of budget)
        if elite_rows and self._generation > 0:
            n_mutate = max(1, n * 2 // 3)
            proposals.extend(self._mutate_from_elites(elite_rows, n_mutate))

        # De novo exploration (remaining 1/3, always fresh)
        n_denovo = n - len(proposals)
        if n_denovo > 0:
            proposals.extend(self._denovo(n_denovo, elite_rows))

        # Batch diversity filter: reject proposals too similar to ones
        # already accepted in this batch (Hamming identity > 0.85). This
        # prevents the mutation mode from clustering every proposal around
        # the same parent's neighborhood.
        proposals = _diversity_filter(proposals, threshold=0.85)

        self._generation += 1
        self._log(f"[esm3] gen {self._generation}: {len(proposals)} proposals "
                  f"(diversity-filtered)")
        return proposals[:n]

    def _denovo(self, n: int, elite_rows: list[dict]) -> list:
        if self.peptide_length:
            pep_len = self.peptide_length
        elif elite_rows:
            lens = [len(r["sequence"]) for r in elite_rows if r.get("sequence")]
            pep_len = random.choice(lens) if lens else sum(self.len_range) // 2
        else:
            pep_len = random.randint(*self.len_range)

        # Progressive temperature: high early (broad exploration),
        # anneal toward exploitation in later generations, with a floor
        # to maintain minimum diversity
        gen = self._generation
        temperature = max(1.2 - 0.1 * gen, 0.8)
        for _ in range(3):
            result = self._post({
                "mode": "propose",
                "receptor_sequence": self.receptor_sequence,
                "peptide_length": pep_len,
                "n_samples": max(n * 3, 48),
                "n_keep": n,
                "temperature": temperature,
                **self._cond,
            })
            sequences = [
                item for item in result.get("sequences", [])
                if item.get("sequence") and len(item["sequence"]) >= 4
            ]
            if sequences:
                break
            temperature += 0.2
        else:
            return []

        return [(s["sequence"], [], [], f"denovo_gen{self._generation}")
                for s in sequences]

    def _mutate_from_elites(self, elite_rows: list[dict], n: int) -> list:
        """BindCraft2 Semigreedy: p(position_to_mutate) ∝ (1 - pLDDT_i).

        Diversity controls: round-robin across distinct parents (every
        elite gets mutation budget, not just the top one), mutation
        count anneals from 3-5 (explore) in early generations to 1-2
        (exploit) later, and the batch-level diversity filter in
        propose() rejects near-duplicates."""
        # Sort elites by score so round-robin starts with the best
        elites = sorted(
            [e for e in elite_rows if e.get("sequence")],
            key=lambda e: -(e.get("composite_score") or 0))
        if not elites:
            return []

        # Adaptive mutation count: more positions early (broad search),
        # fewer later (fine-tuning)
        gen = self._generation
        if gen <= 2:
            k_choices, k_weights = [3, 4, 5], [0.4, 0.3, 0.3]
        elif gen <= 4:
            k_choices, k_weights = [2, 3, 4], [0.4, 0.4, 0.2]
        else:
            k_choices, k_weights = [1, 2, 3], [0.4, 0.4, 0.2]

        proposals = []
        for i in range(n):
            # Round-robin: each elite gets equal mutation budget
            parent = elites[i % len(elites)]
            seq = parent.get("sequence", "")
            plddts = parent.get("plddts") or []
            if not seq or len(seq) < 4:
                continue
            pep_len = len(seq)

            # Per-residue mutation weights: low pLDDT -> high mutation prob
            if plddts and len(plddts) == pep_len:
                weights = [max(1.0 - float(p), 0.01) for p in plddts]
            else:
                weights = [1.0] * pep_len

            # Sample positions (weighted categorical)
            total = sum(weights)
            probs = [w / total for w in weights]
            k = random.choices(k_choices, weights=k_weights)[0]
            k = min(k, pep_len)
            positions = sorted(_weighted_sample(range(pep_len), probs, k))

            # Ask ESM3 to refill only those positions
            mutated = None
            try:
                result = self._post({
                    "mode": "refill",
                    "receptor_sequence": self.receptor_sequence,
                    "parent_sequence": seq,
                    "remask_positions": positions,
                    "temperature": 0.7 + 0.1 * random.random(),
                    **self._cond,
                })
                if result.get("ok"):
                    mutated = result.get("sequence")
            except Exception as exc:
                self._log(f"[esm3] refill error: {exc}")

            # ESM3 may sample back the same residues (it considers those
            # positions optimal) — fall back to a random substitution
            # to guarantee exploration
            if not mutated:
                chars = list(seq)
                AA = "ACDEFGHIKLMNPQRSTVWY"
                for pos in positions:
                    chars[pos] = random.choice(
                        [a for a in AA if a != chars[pos]])
                mutated = "".join(chars)

            group = f"mut_{parent.get('sequence', '')[:8]}_g{self._generation}"
            proposals.append((mutated, [], [], group))

        return proposals

    def pseudo_perplexity(self, sequence: str) -> float | None:
        try:
            result = self._post({
                "mode": "perplexity",
                "receptor_sequence": self.receptor_sequence,
                "peptide_sequence": sequence,
                **self._cond,
            })
            return float(result.get("perplexity", 10.0))
        except Exception as exc:
            self._log(f"[esm3] perplexity unavailable: {exc}")
            return None

    def learn(self, elite_rows: list[dict], all_rows: list[dict]):
        """GRPO with per-residue pLDDT credit.

        Positions with low pLDDT get higher gradient weight — the model
        learns to change what's broken, not what works."""
        rollouts = []
        for r in all_rows:
            score = r.get("composite_score")
            if score is None:
                score = r.get("ipsae_dom")
            if r.get("sequence") and isinstance(score, (int, float)):
                rollouts.append({
                    "sequence": r["sequence"],
                    "reward": float(score),
                    "group": r.get("proposal_group", "default"),
                    "plddts": r.get("plddts") or [],
                })
        if len(rollouts) < 2:
            return
        try:
            result = self._post({
                "mode": "learn",
                "receptor_sequence": self.receptor_sequence,
                "peptide_length": self.peptide_length or 14,
                "rollouts": rollouts,
                **self._cond,
            })
            if result.get("ok"):
                self._log(f"[esm3] GRPO update → gen {self._generation}")
            else:
                self._log(f"[esm3] GRPO skipped: {result.get('skipped', '?')}")
        except Exception as exc:
            self._log(f"[esm3] GRPO failed (continuing): {exc}")

    def close(self):
        pass

    def __del__(self):
        self.close()
