"""ESM3-based proposal engine for the peptide design workflow.

Drop-in replacement for BackendProposer (PepMLM): same interface, but
sequence proposals come from the ESM3 3B masked language model with
optional GRPO fine-tuning between generations.

Architecture: ESM3 runs in a SUBPROCESS using the Boltz2Score venv's
Python (which has esm>=3.4 + torch+CUDA); the parent process (CPU worker)
communicates via JSON files. This keeps the 3B model off the CPU worker's
memory and gives it a dedicated GPU slot.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]  # V-Bio root

# The Boltz2Score venv has esm 3.4.1 + torch 2.11 + CUDA
_ESM3_PYTHON = "/data/Boltz2Score/.venv/bin/python"
_ESM3_INFERENCE_SCRIPT = str(
    Path(__file__).parent / "_esm3_inference_worker.py")

# Residue encoding shared with the ESM3 loop
_AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class ESM3Proposer:
    """Same interface as BackendProposer but backed by ESM3.

    The ESM3 model (3B params, bf16) runs in a GPU subprocess; this class
    handles file-based communication and format conversion. The adapter
    directory persists across generations within one design task, so GRPO
    updates accumulate.
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
        """Generate n candidate sequences via ESM3.

        Returns [(base_sequence, modifications, cys_anchors, proposal_group)]
        matching BackendProposer's contract.
        """
        # Determine peptide length for this generation
        if self.peptide_length:
            pep_len = self.peptide_length
        elif elite_rows:
            # adaptive: sample around elite lengths
            import random
            elite_lens = [len(r.get("sequence", "")) for r in elite_rows if r.get("sequence")]
            if elite_lens:
                pep_len = random.choice(elite_lens)
            else:
                pep_len = (self.len_range[0] + self.len_range[1]) // 2
        else:
            import random
            pep_len = random.randint(self.len_range[0], self.len_range[1])

        # Build the inference request
        req = {
            "mode": "propose",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": pep_len,
            "n_samples": max(n * 3, 48),  # oversample for diversity filter
            "n_keep": n,
            "temperature": 0.9 + 0.1 * min(self._generation, 3),  # anneal
            "seed": self.seed + self._generation,
            "adapter_dir": self.adapter_dir,
            "device": self.device,
        }

        result = self._run_inference(req)
        if result is None:
            self._log("[esm3-proposer] inference failed, falling back to random")
            return self._random_fallback(natural_pool, n, pep_len)

        sequences = result.get("sequences", [])
        proposals = []
        for i, item in enumerate(sequences):
            seq = item.get("sequence", "")
            if not seq or len(seq) < 4:
                continue
            group = item.get("group", f"gen{self._generation}_denovo")
            proposals.append((seq, [], [], group))

        self._generation += 1
        self._log(f"[esm3-proposer] gen{self._generation}: {len(proposals)} proposals "
                   f"(len={pep_len}, best_logprob={result.get('best_logprob', '?')})")
        return proposals

    def pseudo_perplexity(self, sequence: str) -> float:
        """ESM3 mean negative log-likelihood (lower = more confident)."""
        req = {
            "mode": "perplexity",
            "receptor_sequence": self.receptor_sequence,
            "peptide_sequence": sequence,
            "adapter_dir": self.adapter_dir,
            "device": self.device,
        }
        result = self._run_inference(req)
        if result is None:
            return 10.0  # fallback: moderate penalty
        return float(result.get("perplexity", 10.0))

    def learn(self, elite_rows: list[dict], all_rows: list[dict]):
        """GRPO update on the ESM3 adapter from evaluation results."""
        # Convert production rows to rollouts
        rollouts = []
        for row in all_rows:
            seq = row.get("sequence", "")
            score = row.get("composite_score") or row.get("ipsae_dom") or 0.0
            if seq and isinstance(score, (int, float)):
                rollouts.append({"sequence": seq, "reward": float(score)})

        if len(rollouts) < 2:
            self._log("[esm3-proposer] insufficient rollouts for GRPO, skipping")
            return

        # Assign groups (de novo vs refill)
        groups = {}
        for row in all_rows:
            g = row.get("proposal_group") or f"gen{self._generation}"
            groups.setdefault(g, []).append(row.get("sequence", ""))

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
            self._log(f"[esm3-proposer] GRPO update done → {self.adapter_dir}")
        else:
            self._log("[esm3-proposer] GRPO update skipped (insufficient groups)")

    def _run_inference(self, request: dict) -> dict | None:
        """Run ESM3 inference in a subprocess with GPU access."""
        req_file = self.work_dir / "request.json"
        resp_file = self.work_dir / "response.json"
        resp_file.unlink(missing_ok=True)

        req_file.write_text(json.dumps(request))

        env = dict(os.environ)
        env["PYTHONPATH"] = str(_ROOT / "capabilities" / "peptide_lm")
        gpu_idx = request.get("device", "cuda:0")
        if "cuda:" in gpu_idx:
            env["CUDA_VISIBLE_DEVICES"] = gpu_idx.split(":")[1]

        try:
            proc = subprocess.run(
                [_ESM3_PYTHON, _ESM3_INFERENCE_SCRIPT, str(req_file), str(resp_file)],
                capture_output=True, text=True, timeout=600, env=env,
            )
            if proc.returncode != 0:
                self._log(f"[esm3-proposer] subprocess rc={proc.returncode}: "
                          f"{proc.stderr[-300:]}")
                return None
            if resp_file.exists():
                return json.loads(resp_file.read_text())
        except (subprocess.TimeoutExpired, Exception) as exc:
            self._log(f"[esm3-proposer] inference error: {exc}")
        return None

    def _random_fallback(self, pool, n, pep_len):
        """Diversity fallback when ESM3 is unavailable."""
        import random
        rng = random.Random(self.seed + self._generation)
        one_letter = [_AA3_TO_1.get(c, "A") for c in pool if c in _AA3_TO_1]
        if not one_letter:
            one_letter = list("ACDEFGHIKLMNPQRSTVWY")
        out = []
        for i in range(n):
            seq = "".join(rng.choice(one_letter) for _ in range(pep_len))
            out.append((seq, [], [], f"fallback_{i}"))
        return out
