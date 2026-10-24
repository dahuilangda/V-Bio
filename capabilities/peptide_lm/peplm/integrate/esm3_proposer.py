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
import os
from pathlib import Path
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

from peplm import sequence_quality
from peplm.integrate.ss_modes import normalize_mode

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
    proposals: list[tuple], threshold: float = 0.85,
    excluded_aas: frozenset[str] = frozenset(),
    fixed_prefix: str = "", fixed_suffix: str = "",
) -> list[tuple]:
    """Reject degenerate sequences, proposals carrying a user-excluded
    residue in the variable region, and proposals whose identity to an
    already-kept proposal exceeds the threshold. Fixed layout (mask edges)
    is stripped first — anchors are design, not violations. Greedy
    first-come-first-kept; the caller sorts by quality before filtering so
    the best variant of each cluster survives. Degenerates are dropped
    before evaluation: the composite penalty cannot recover a spent
    structure-prediction slot."""
    kept: list[tuple] = []
    kept_vars: list[str] = []
    for prop in proposals:
        seq = prop[0]
        variable = seq
        if fixed_prefix and variable.startswith(fixed_prefix):
            variable = variable[len(fixed_prefix):]
        if fixed_suffix and variable.endswith(fixed_suffix):
            variable = variable[:-len(fixed_suffix)]
        if sequence_quality.degeneracy_flags(variable):
            continue
        if any(aa in variable for aa in excluded_aas):
            continue
        # 恒等度算可变区: 全长恒等度会把 1-2 步突变(14/15 > 0.85)当
        # 近重复剔除, 晚代爬山只剩每父本一个子代
        if any(_sequence_identity(variable, k) > threshold for k in kept_vars):
            continue
        kept.append(prop)
        kept_vars.append(variable)
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
        structure_mode: str = "env",
        chirality: str = "l",
        project_id: str = "",
        excluded_aas: list[str] | None = None,
        fixed_prefix: str = "",
        fixed_suffix: str = "",
        log=print,
        **_unused: Any,
    ):
        self.receptor_sequence = receptor_sequence
        self.peptide_length = peptide_length
        self.len_range = len_range
        from peplm.shared import AA20
        # 用户排除的残基类型: worker 采样后过滤, 本地批次过滤与兜底替换再兜一层。
        self._excluded_aas = frozenset(
            a for a in (str(x).strip().upper() for x in (excluded_aas or []))
            if a in AA20)
        self._fixed_prefix = str(fixed_prefix or "")
        self._fixed_suffix = str(fixed_suffix or "")
        self._log = log
        self._generation = 0
        self._endpoint = ESM3_SERVER_URL.rstrip("/")
        # GRPO 更新按 手性+靶点 维度的 LoRA adapter: L/D 不交叉写入, 不同靶点
        # 也不共享权重——共享会让多任务梯度互相拉扯, 单靶点学习信号被稀释。
        # adapter 持久化在磁盘, 学习结果跨服务重启存活。
        import hashlib
        chir = str(chirality or "l").strip().lower()
        target_tag = hashlib.md5(
            (receptor_sequence or "").encode()).hexdigest()[:8] or "notarget"
        # adapter 按 项目/靶点 隔离存储: 不同项目、不同靶点绝不共享权重,
        # 且持久化在结果盘(_runtime_adapters, 不进 git), 服务重启存活。
        self.project_id = str(project_id or "").strip() or "default"
        target_tag = f"{self.project_id}-{target_tag}"
        self.adapter_key = f"peptide-{chir}-{target_tag}"
        adapter_root = Path(os.environ.get(
            "ESM3_ADAPTER_ROOT",
            "/data/boltz_central_results/_runtime_adapters",
        ))
        self.adapter_dir = adapter_root / self.project_id / self.adapter_key
        # structure_mode travels with every request; the service builds
        # the ss8 template at each call's actual peptide length (hairpin
        # turns must sit mid-chain, so a length-mismatched static string
        # would misalign). "env" keeps the legacy ESM3_SS_PROFILE static
        # profile for callers that never picked a mode.
        self.structure_mode = normalize_mode(structure_mode)
        self._cond = {
            "structure_mode": self.structure_mode,
            "peptide_first": bool(ESM3_PEPTIDE_FIRST),
        }
        if self.structure_mode == "env":
            self._cond["ss_profile"] = ESM3_SS_PROFILE or None

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

        # Semigreedy mutation from elites (pLDDT-guided, 2/3 of budget).
        # Generation 1 mutates the seed/initial sequence the same way —
        # gating on _generation silently discarded user seeds.
        if elite_rows:
            n_mutate = max(1, n * 2 // 3)
            proposals.extend(self._mutate_from_elites(elite_rows, n_mutate))

        # De novo exploration (remaining 1/3, always fresh)
        n_denovo = n - len(proposals)
        if n_denovo > 0:
            proposals.extend(self._denovo(n_denovo))

        # Threshold tuned for the mutation mode: without it every proposal
        # clusters around one parent's neighborhood.
        proposals = _diversity_filter(
            proposals, threshold=0.85, excluded_aas=self._excluded_aas,
            fixed_prefix=self._fixed_prefix, fixed_suffix=self._fixed_suffix)

        self._generation += 1
        self._log(f"[esm3] gen {self._generation}: {len(proposals)} proposals "
                  f"(diversity-filtered)")
        return proposals[:n]

    def _denovo(self, n: int) -> list:
        # de novo is the length-diversity channel: split the batch across
        # a few window-random lengths instead of one (the mutation path
        # climbs around its parents at fixed length).
        gen = self._generation
        temperature = max(1.2 - 0.1 * gen, 0.8)
        if self.peptide_length:
            length_groups = [self.peptide_length]
        else:
            n_groups = max(1, min(4, n))
            length_groups = sorted({
                random.randint(*self.len_range) for _ in range(n_groups)})
        per_group = max(1, n // len(length_groups))

        proposals: list[tuple[str, list, list, str]] = []
        for pep_len in length_groups:
            # Progressive temperature: high early (broad exploration),
            # anneal toward exploitation in later generations, with a floor
            # to maintain minimum diversity
            for _ in range(3):
                result = self._post({
                    "mode": "propose",
                    "receptor_sequence": self.receptor_sequence,
                    "peptide_length": pep_len,
                    "n_samples": max(per_group * 3, 48),
                    "n_keep": per_group,
                    "temperature": temperature,
                    "adapter_key": self.adapter_key,
                    "adapter_dir": str(self.adapter_dir),
                    "excluded_aas": sorted(self._excluded_aas),
                    **self._cond,
                })
                if result.get("ok") is False:
                    self._log(f"[esm3] propose rejected: {result.get('error')}")
                    break
                sequences = [
                    item for item in result.get("sequences", [])
                    if item.get("sequence") and len(item["sequence"]) >= 4
                ]
                if sequences:
                    proposals.extend(
                        (s["sequence"], [], [], f"denovo_gen{self._generation}")
                        for s in sequences)
                    break
                temperature += 0.2
        return proposals[:n]

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
                    "adapter_key": self.adapter_key,
                    "adapter_dir": str(self.adapter_dir),
                    "excluded_aas": sorted(self._excluded_aas),
                    "fixed_prefix": self._fixed_prefix,
                    "fixed_suffix": self._fixed_suffix,
                    **self._cond,
                })
                if result.get("ok"):
                    mutated = result.get("sequence")
            except Exception as exc:
                self._log(f"[esm3] refill error: {exc}")

            # ESM3 may sample back the same residues (it considers those
            # positions optimal) or a user-excluded residue in the variable
            # region — fall back to a random substitution to guarantee
            # exploration
            variable = mutated or ""
            if self._fixed_prefix and variable.startswith(self._fixed_prefix):
                variable = variable[len(self._fixed_prefix):]
            if self._fixed_suffix and variable.endswith(self._fixed_suffix):
                variable = variable[:-len(self._fixed_suffix)]
            if not mutated or any(aa in variable for aa in self._excluded_aas):
                chars = list(seq)
                AA = "ACDEFGHIKLMNPQRSTVWY"
                for pos in positions:
                    allowed = [a for a in AA
                               if a != chars[pos] and a not in self._excluded_aas]
                    if allowed:
                        chars[pos] = random.choice(allowed)
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
        """GRPO with per-residue pLDDT credit + foldability prerequisite.

        Positions with low pLDDT get higher gradient weight — the model
        learns to change what's broken, not what works. Candidates below
        the pLDDT gate have their reward dampened proportionally: GRPO
        never reinforces contact-maximizing junk as a positive advantage
        (measured without this: H 19% / G 16% / hydrophobic 8% after 7
        auto-mode generations)."""
        PLDDT_GATE = float(os.environ.get("DPEPTIDE_PLDDT_GATE", "55"))
        rollouts = []
        for r in all_rows:
            score = r.get("composite_score")
            if score is None:
                score = r.get("ipsae_dom")
            if r.get("sequence") and isinstance(score, (int, float)):
                plddts = r.get("plddts") or []
                # pLDDT-gated damping: only apply when we have per-residue
                # data. Empty plddts means no foldability evidence either
                # way — damping to 0.1× would crush the learning signal
                # for every candidate lacking confidence output.
                if plddts:
                    mean_plddt = sum(plddts) / len(plddts) * 100
                    if mean_plddt < PLDDT_GATE:
                        score = float(score) * (
                            0.1 + 0.9 * mean_plddt / PLDDT_GATE)
                rollouts.append({
                    "sequence": r["sequence"],
                    "reward": float(score),
                    "group": r.get("proposal_group", "default"),
                    "plddts": plddts,
                })
        if len(rollouts) < 2:
            return
        try:
            result = self._post({
                "mode": "learn",
                "receptor_sequence": self.receptor_sequence,
                "rollouts": rollouts,
                "adapter_key": self.adapter_key,
                # the service saves the LoRA here after applying the update;
                # it also lazy-loads this path before the next propose/learn
                "new_adapter_dir": str(self.adapter_dir),
                **self._cond,
            })
            if result.get("ok"):
                self._log(f"[esm3] GRPO update → gen {self._generation}")
            else:
                self._log(f"[esm3] GRPO skipped: "
                          f"{result.get('skipped') or result.get('error') or '?'}")
        except Exception as exc:
            self._log(f"[esm3] GRPO failed (continuing): {exc}")

    def close(self):
        pass

    def __del__(self):
        self.close()
