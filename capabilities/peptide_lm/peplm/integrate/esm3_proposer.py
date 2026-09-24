"""ESM3 proposal engine — HTTP client to the ESM3 inference service.

The 3B model runs as a daemon (see deploy/scripts/start_esm3_service.sh).
Configuration follows the platform convention: ESM3_SERVER_URL and
ESM3_TIMEOUT_SECONDS are defined in backend.core.config, delivered via
the worker env files.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from backend.core.config import ESM3_SERVER_URL, ESM3_TIMEOUT_SECONDS
except ImportError:
    ESM3_SERVER_URL = "http://localhost:9333"
    ESM3_TIMEOUT_SECONDS = 600

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_S = 3


class ESM3Proposer:
    """Receptor-conditioned sequence proposals from ESM3 + GRPO.

    Transport failures in learn() and pseudo_perplexity() are logged and
    skipped (the design loop continues with the previous adapter / prior
    ranking); only a propose() that yields zero sequences after retries
    raises, since the caller has no candidates to fall back to.
    """

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
        self._adapter_dir: str | None = None
        self._endpoint = ESM3_SERVER_URL.rstrip("/")

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
        if self.peptide_length:
            pep_len = self.peptide_length
        elif elite_rows:
            lens = [len(r["sequence"]) for r in elite_rows if r.get("sequence")]
            pep_len = random.choice(lens) if lens else sum(self.len_range) // 2
        else:
            pep_len = random.randint(*self.len_range)

        # Anneal temperature upward when the diversity filter rejects
        # everything — the entropy gate can starve a cold-start policy
        temperature = 0.9 + 0.1 * min(self._generation, 3)
        for attempt in range(3):
            result = self._post({
                "mode": "propose",
                "receptor_sequence": self.receptor_sequence,
                "peptide_length": pep_len,
                "n_samples": max(n * 3, 48),
                "n_keep": n,
                "temperature": temperature,
            })
            sequences = [
                item for item in result.get("sequences", [])
                if item.get("sequence") and len(item["sequence"]) >= 4
            ]
            if sequences:
                break
            temperature += 0.2  # widen the sampling distribution
            self._log(f"[esm3] gen {self._generation + 1}: 0 eligible at "
                      f"temp={temperature - 0.2:.1f}, retrying at {temperature:.1f}")
        else:
            raise RuntimeError(
                f"ESM3 propose yielded 0 eligible sequences after 3 attempts "
                f"(peptide_length={pep_len}, receptor="
                f"{self.receptor_sequence[:30]}...)")

        proposals = [(s["sequence"], [], [], "denovo") for s in sequences]
        self._generation += 1
        self._log(f"[esm3] gen {self._generation}: {len(proposals)} proposals "
                  f"(len={pep_len})")
        return proposals

    def pseudo_perplexity(self, sequence: str) -> float | None:
        """Mean negative log-likelihood; None when the service is down
        (the caller's pre-rank skips None values rather than scoring
        every candidate at the same fallback constant)."""
        try:
            result = self._post({
                "mode": "perplexity",
                "receptor_sequence": self.receptor_sequence,
                "peptide_sequence": sequence,
            })
            return float(result.get("perplexity", 10.0))
        except (ConnectionError, Exception) as exc:
            self._log(f"[esm3] perplexity unavailable: {exc}")
            return None

    def learn(self, elite_rows: list[dict], all_rows: list[dict]):
        """GRPO update; transport or application failure is logged and
        skipped — the design loop continues with the current adapter."""
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
                })
        if len(rollouts) < 2:
            return
        try:
            adapter = f"/tmp/esm3_adapter_{id(self)}_gen{self._generation}"
            result = self._post({
                "mode": "learn",
                "receptor_sequence": self.receptor_sequence,
                "peptide_length": self.peptide_length or 14,
                "rollouts": rollouts,
                "new_adapter_dir": adapter,
            })
            if result.get("ok") and result.get("adapter_dir"):
                self._adapter_dir = result["adapter_dir"]
                self._log(f"[esm3] GRPO update → gen {self._generation}")
            else:
                self._log(f"[esm3] GRPO skipped: {result.get('skipped', result.get('error', '?'))}")
        except Exception as exc:
            self._log(f"[esm3] GRPO update failed (continuing): {exc}")

    def close(self):
        pass

    def __del__(self):
        self.close()
