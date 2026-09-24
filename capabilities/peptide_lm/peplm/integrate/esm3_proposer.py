"""ESM3 proposal engine for the peptide design workflow.

Generates receptor-conditioned candidate sequences via a 3B masked
language model with GRPO fine-tuning between generations. The model
runs in a GPU subprocess (Boltz2Score venv: esm>=3.4 + torch+CUDA);
this module handles file-based communication and format conversion.
The adapter persists across generations within one design task so GRPO
updates accumulate.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_ESM3_PYTHON = "/data/Boltz2Score/.venv/bin/python"
_WORKER = str(Path(__file__).parent / "_esm3_inference_worker.py")


class ESM3Proposer:
    """Receptor-conditioned sequence proposals from ESM3 + GRPO.

    Interface: propose / pseudo_perplexity / learn (same contract as
    the production design workflow's proposer protocol).
    """

    def __init__(
        self,
        receptor_sequence: str,
        peptide_length: int | None = None,
        len_range: tuple[int, int] = (10, 18),
        cyclic: bool = False,
        device: str = "cuda:0",
        seed: int = 42,
        adapter_dir: str | None = None,
        work_dir: str = "/tmp/esm3_proposer",
        log=print,
    ):
        self.receptor_sequence = receptor_sequence
        self.peptide_length = peptide_length
        self.len_range = len_range
        self.cyclic = cyclic
        self.device = device
        self.seed = seed
        self.adapter_dir = adapter_dir
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._log = log
        self._generation = 0

    def propose(
        self,
        natural_pool: list[str],
        unnatural_pool: list[dict],
        elite_rows: list[dict],
        n: int,
        plddt_hint: float | None = None,
    ) -> list[tuple[str, list[dict], list[int], str]]:
        """Generate n candidate sequences conditioned on the receptor."""
        if self.peptide_length:
            pep_len = self.peptide_length
        elif elite_rows:
            elite_lens = [len(r.get("sequence", "")) for r in elite_rows if r.get("sequence")]
            pep_len = random.choice(elite_lens) if elite_lens else sum(self.len_range) // 2
        else:
            pep_len = random.randint(self.len_range[0], self.len_range[1])

        req = {
            "mode": "propose",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": pep_len,
            "n_samples": max(n * 3, 48),
            "n_keep": n,
            "temperature": 0.9 + 0.1 * min(self._generation, 3),
            "seed": self.seed + self._generation,
            "adapter_dir": self.adapter_dir,
            "device": self.device,
        }
        result = self._run_inference(req)
        if result is None:
            raise RuntimeError(
                "ESM3 inference failed — check GPU availability and the "
                "Boltz2Score venv (/data/Boltz2Score/.venv, needs esm>=3.4)")

        proposals = [
            (item["sequence"], [], [], item.get("group", "denovo"))
            for item in result.get("sequences", [])
            if item.get("sequence") and len(item["sequence"]) >= 4
        ]

        self._generation += 1
        self._log(f"[esm3] gen {self._generation}: {len(proposals)} proposals "
                  f"(len={pep_len})")
        return proposals

    def pseudo_perplexity(self, sequence: str) -> float:
        """Mean negative log-likelihood under the current policy."""
        req = {
            "mode": "perplexity",
            "receptor_sequence": self.receptor_sequence,
            "peptide_sequence": sequence,
            "adapter_dir": self.adapter_dir,
            "device": self.device,
        }
        result = self._run_inference(req)
        return float(result.get("perplexity", 10.0)) if result else 10.0

    def learn(self, elite_rows: list[dict], all_rows: list[dict]):
        """GRPO update from oracle evaluation results."""
        rollouts = [
            {"sequence": row.get("sequence", ""),
             "reward": float(row.get("composite_score") or row.get("ipsae_dom") or 0)}
            for row in all_rows
            if row.get("sequence") and isinstance(
                row.get("composite_score") or row.get("ipsae_dom"), (int, float))
        ]
        if len(rollouts) < 2:
            self._log("[esm3] insufficient rollouts for GRPO, skipping")
            return

        req = {
            "mode": "learn",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": self.peptide_length or 14,
            "rollouts": rollouts,
            "adapter_dir": self.adapter_dir,
            "new_adapter_dir": str(self.work_dir / f"adapter_gen{self._generation + 1}"),
            "device": self.device,
        }
        result = self._run_inference(req)
        if result and result.get("adapter_dir"):
            self.adapter_dir = result["adapter_dir"]
            self._log(f"[esm3] GRPO update → {self.adapter_dir}")

    def _run_inference(self, request: dict) -> dict | None:
        """Run the ESM3 worker subprocess and return its JSON response."""
        req_file = self.work_dir / "request.json"
        resp_file = self.work_dir / "response.json"
        resp_file.unlink(missing_ok=True)
        req_file.write_text(json.dumps(request))

        env = dict(os.environ)
        env["PYTHONPATH"] = str(_ROOT / "capabilities" / "peptide_lm")
        gpu = request.get("device", "cuda:0")
        if "cuda:" in gpu:
            env["CUDA_VISIBLE_DEVICES"] = gpu.split(":")[1]
            request["device"] = "cuda:0"  # visible device is always 0
            req_file.write_text(json.dumps(request))

        try:
            proc = subprocess.run(
                [_ESM3_PYTHON, _WORKER, str(req_file), str(resp_file)],
                capture_output=True, text=True, timeout=600, env=env,
            )
            if proc.returncode != 0:
                self._log(f"[esm3] worker rc={proc.returncode}: {proc.stderr[-200:]}")
                return None
            if resp_file.exists():
                return json.loads(resp_file.read_text())
        except Exception as exc:
            self._log(f"[esm3] worker error: {exc}")
        return None
