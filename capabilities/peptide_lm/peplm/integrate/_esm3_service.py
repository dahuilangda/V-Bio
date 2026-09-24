#!/usr/bin/env python
"""ESM3 persistent inference service.

Loads the 3B model once, then serves propose / perplexity / learn requests
over stdin/stdout JSON lines. Parent processes communicate via a simple
request-response protocol — no socket overhead, no repeated model loading.

Protocol (one JSON object per line):
  → {"id": "req-1", "mode": "propose", ...}
  ← {"id": "req-1", "ok": true, ...}  |  {"id": "req-1", "ok": false, "error": "..."}
  EOF on stdin shuts down cleanly.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PEPLM_ROOT))

import torch


def load_policy(device: str, adapter_dir: str | None):
    from peplm.models.esm3_policy import ESM3Policy

    model_path = PEPLM_ROOT / "models" / "external" / "esm3_sm_open_v1.pth"
    policy = ESM3Policy(
        checkpoint=model_path,
        device=device,
        dtype=torch.bfloat16,
    )
    if adapter_dir and Path(adapter_dir).is_dir():
        try:
            policy.load_adapter(Path(adapter_dir))
        except Exception:
            pass  # stale adapter shapes — continue cold
    return policy


def handle_propose(policy, req):
    receptor = req["receptor_sequence"]
    pep_len = int(req.get("peptide_length", 14))
    n_samples = int(req.get("n_samples", 48))
    n_keep = int(req.get("n_keep", 16))
    temperature = float(req.get("temperature", 0.9))

    with torch.no_grad():
        trajs = policy.sample_batch(
            receptor=receptor,
            pep_len=pep_len,
            n=n_samples,
            keep=n_keep,
            temperature=temperature,
            num_steps=8,
            strategy="entropy",
        )

    sequences = []
    for traj in trajs:
        tokens = traj.peptide_tokens
        if not isinstance(tokens, list) or len(tokens) != pep_len:
            continue
        seq = policy.decode_tokens(tokens)
        if not seq or len(seq) != pep_len:
            continue
        freq = Counter(seq)
        h = -sum((c / pep_len) * math.log2(c / pep_len) for c in freq.values())
        if h < 1.5:
            continue
        sequences.append({
            "sequence": seq,
            "logprob": traj.mean_logprob,
            "group": req.get("group", "denovo"),
        })

    sequences.sort(key=lambda s: -s.get("logprob", 0))
    return {
        "sequences": sequences[:n_keep],
        "best_logprob": sequences[0]["logprob"] if sequences else None,
    }


def handle_perplexity(policy, req):
    score = policy.score_sequence(
        req.get("receptor_sequence", ""),
        req.get("peptide_sequence", ""))
    return {"perplexity": float(-score) if score is not None else 10.0}


def handle_learn(policy, req):
    from peplm.generate.masked_grpo import MaskedGRPOUpdater, Rollout

    rollouts_data = req.get("rollouts", [])
    if len(rollouts_data) < 2:
        return {"skipped": "insufficient rollouts"}

    pep_len = int(req.get("peptide_length", 14))
    groups: dict[str, list] = {}
    for rd in rollouts_data:
        groups.setdefault(rd.get("group", "default"), []).append(rd)

    valid = {g: rows for g, rows in groups.items() if len(rows) >= 2}
    if not valid:
        return {"skipped": "no group with >= 2 members"}

    rollouts = []
    for g, rows in valid.items():
        rewards = [r["reward"] for r in rows]
        mean_r = sum(rewards) / len(rewards)
        spread = max(max(rewards) - mean_r, mean_r - min(rewards), 1e-6)
        for r in rows:
            tokens = policy.encode_peptide(r["sequence"], pep_len)
            if tokens is None:
                continue
            rollouts.append(Rollout(
                peptide_tokens=tokens,
                advantage=(r["reward"] - mean_r) / spread,
                group=g,
            ))

    if len(rollouts) < 2:
        return {"skipped": "tokenization failed"}

    updater = MaskedGRPOUpdater(policy, lr=1e-5, kl_beta=0.04)
    stats = updater.update(rollouts)

    adapter_dir = req.get("new_adapter_dir")
    if adapter_dir:
        Path(adapter_dir).mkdir(parents=True, exist_ok=True)
        policy.save_adapter(Path(adapter_dir))

    return {
        "adapter_dir": adapter_dir,
        "stats": {k: float(v) if isinstance(v, (int, float)) else v
                  for k, v in stats.items()},
    }


def main():
    device = "cuda:0"
    policy = load_policy(device, None)
    print(json.dumps({"ok": True, "ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id", "")
            mode = req.get("mode", "")

            # adapter hot-swap between requests
            adapter = req.get("adapter_dir")
            current_adapter = getattr(policy, "_loaded_adapter", None)
            if adapter and adapter != current_adapter and Path(adapter).is_dir():
                try:
                    policy.load_adapter(Path(adapter))
                    policy._loaded_adapter = adapter
                except Exception:
                    pass

            if mode == "propose":
                resp = handle_propose(policy, req)
            elif mode == "perplexity":
                resp = handle_perplexity(policy, req)
            elif mode == "learn":
                resp = handle_learn(policy, req)
            else:
                resp = {"error": f"unknown mode: {mode}"}

            resp["id"] = req_id
            resp.setdefault("ok", True)
        except Exception as exc:
            resp = {"id": req.get("id", ""), "ok": False, "error": str(exc)}

        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
