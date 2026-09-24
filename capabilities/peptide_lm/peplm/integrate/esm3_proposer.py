"""ESM3 proposal engine — HTTP client to the host ESM3 service.

The 3B model runs as a daemon on the host (Boltz2Score venv + CUDA,
started by deploy/scripts/start_esm3_service.sh). This client is
container-agnostic: any worker with network access to the host can
generate proposals.
"""
from __future__ import annotations

import json
import os
import random
import urllib.request
from typing import Any


class ESM3Proposer:
    """Receptor-conditioned sequence proposals from ESM3 + GRPO."""

    def __init__(
        self,
        receptor_sequence: str,
        peptide_length: int | None = None,
        len_range: tuple[int, int] = (10, 18),
        cyclic: bool = False,
        device: str = "cuda:0",
        seed: int = 42,
        work_dir: str = "/tmp/esm3_proposer",
        log=print,
        **_unused: Any,
    ):
        self.receptor_sequence = receptor_sequence
        self.peptide_length = peptide_length
        self.len_range = len_range
        self._log = log
        self._generation = 0
        self._adapter_dir: str | None = None
        self._endpoint = os.environ.get(
            "VBIO_ESM3_URL", "http://172.17.3.200:9333")

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._endpoint, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())

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

        result = self._post({
            "mode": "propose",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": pep_len,
            "n_samples": max(n * 3, 48),
            "n_keep": n,
            "temperature": 0.9 + 0.1 * min(self._generation, 3),
        })
        if not result.get("ok"):
            raise RuntimeError(f"ESM3 propose failed: {result.get('error')}")

        proposals = [
            (item["sequence"], [], [], "denovo")
            for item in result.get("sequences", [])
            if item.get("sequence") and len(item["sequence"]) >= 4
        ]
        self._generation += 1
        self._log(f"[esm3] gen {self._generation}: {len(proposals)} proposals "
                  f"(len={pep_len})")
        return proposals

    def pseudo_perplexity(self, sequence: str) -> float:
        result = self._post({
            "mode": "perplexity",
            "receptor_sequence": self.receptor_sequence,
            "peptide_sequence": sequence,
        })
        return float(result.get("perplexity", 10.0)) if result.get("ok") else 10.0

    def learn(self, elite_rows: list[dict], all_rows: list[dict]):
        rollouts = [
            {"sequence": r["sequence"],
             "reward": float(r.get("composite_score") or r.get("ipsae_dom") or 0)}
            for r in all_rows
            if r.get("sequence") and isinstance(
                r.get("composite_score") or r.get("ipsae_dom"), (int, float))
        ]
        if len(rollouts) < 2:
            return
        adapter = f"/tmp/esm3_adapter_gen{self._generation}"
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

    def close(self):
        pass

    def __del__(self):
        self.close()
