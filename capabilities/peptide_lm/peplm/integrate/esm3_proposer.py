"""ESM3 proposal engine for the peptide design workflow.

Spawns a persistent GPU service process (ESM3 3B loaded once) and
communicates via stdin/stdout JSON lines. Adapter state persists across
generations so GRPO updates accumulate within a design task.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_PYTHON = "/data/Boltz2Score/.venv/bin/python"
_SERVICE = str(Path(__file__).parent / "_esm3_service.py")


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
        **_unused,
    ):
        self.receptor_sequence = receptor_sequence
        self.peptide_length = peptide_length
        self.len_range = len_range
        self.cyclic = cyclic
        self.seed = seed
        self._log = log
        self._generation = 0
        self._req_counter = 0
        self._adapter_dir: str | None = None

        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._start_service(device)

    def _start_service(self, device: str):
        gpu = device.split(":")[-1] if "cuda:" in device else "0"
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = gpu
        env["PYTHONPATH"] = str(_ROOT / "capabilities" / "peptide_lm")
        self._proc = subprocess.Popen(
            [_PYTHON, _SERVICE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        ready_line = self._proc.stdout.readline()
        ready = json.loads(ready_line) if ready_line.strip() else {}
        if not ready.get("ready"):
            stderr = self._proc.stderr.read()[-500:] if self._proc.poll() else ""
            raise RuntimeError(f"ESM3 service failed to start: {stderr}")
        self._log(f"[esm3] service ready (GPU {gpu})")

    def _request(self, payload: dict) -> dict | None:
        if self._proc is None or self._proc.poll() is not None:
            return None
        self._req_counter += 1
        payload["id"] = f"req-{self._req_counter}"
        if self._adapter_dir:
            payload.setdefault("adapter_dir", self._adapter_dir)

        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line.strip():
                    return None
                return json.loads(line)
            except (BrokenPipeError, OSError):
                return None

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

        result = self._request({
            "mode": "propose",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": pep_len,
            "n_samples": max(n * 3, 48),
            "n_keep": n,
            "temperature": 0.9 + 0.1 * min(self._generation, 3),
        })
        if result is None or not result.get("ok"):
            raise RuntimeError(f"ESM3 propose failed: {result}")

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
        result = self._request({
            "mode": "perplexity",
            "receptor_sequence": self.receptor_sequence,
            "peptide_sequence": sequence,
        })
        return float(result.get("perplexity", 10.0)) if result and result.get("ok") else 10.0

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

        adapter_path = Path(f"/tmp/esm3_adapter_gen{self._generation}")
        result = self._request({
            "mode": "learn",
            "receptor_sequence": self.receptor_sequence,
            "peptide_length": self.peptide_length or 14,
            "rollouts": rollouts,
            "new_adapter_dir": str(adapter_path),
        })
        if result and result.get("ok") and result.get("adapter_dir"):
            self._adapter_dir = result["adapter_dir"]
            self._log(f"[esm3] GRPO update → gen {self._generation}")

    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()

    def __del__(self):
        self.close()
