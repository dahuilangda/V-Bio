#!/usr/bin/env python
"""ESM3 inference worker — runs in the Boltz2Score venv (esm 3.4 + torch + CUDA).

Reads a JSON request, runs ESM3 inference (propose / perplexity / GRPO learn),
writes a JSON response. This script is called as a subprocess by ESM3Proposer.

Usage: python _esm3_inference_worker.py <request.json> <response.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure peplm is importable
PEPLM_ROOT = Path(__file__).resolve().parents[2]  # → peptide_lm root
sys.path.insert(0, str(PEPLM_ROOT))


def main():
    req_path, resp_path = sys.argv[1], sys.argv[2]
    req = json.loads(Path(req_path).read_text())
    mode = req.get("mode", "propose")

    import torch
    device = req.get("device", "cuda:0")

    from peplm.models.esm3_policy import ESM3Policy
    from peplm.models.esm3_sft import load_pairs

    # Model path
    model_path = PEPLM_ROOT / "models" / "external" / "esm3_sm_open_v1.pth"
    if not model_path.exists():
        _write_error(resp_path, f"ESM3 model not found: {model_path}")
        return

    policy = ESM3Policy(
        checkpoint=model_path,
        device=device,
        dtype=torch.bfloat16,
    )

    # Load adapter if available
    adapter_dir = req.get("adapter_dir")
    if adapter_dir and Path(adapter_dir).exists():
        try:
            policy.load_adapter(Path(adapter_dir))
            print(f"[worker] loaded adapter: {adapter_dir}")
        except Exception as exc:
            print(f"[worker] adapter load failed (continuing cold): {exc}")

    receptor = req.get("receptor_sequence", "")

    if mode == "propose":
        _do_propose(policy, req, resp_path, receptor, device)

    elif mode == "perplexity":
        _do_perplexity(policy, req, resp_path, receptor)

    elif mode == "learn":
        _do_learn(policy, req, resp_path, receptor, device)

    else:
        _write_error(resp_path, f"unknown mode: {mode}")


def _do_propose(policy, req, resp_path, receptor, device):
    """Generate candidate sequences."""
    import torch
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
            num_steps=8,  # MaskGIT iterative unmasking
            strategy="entropy",
        )

    # Convert trajectories to sequence strings
    from peplm.residues import ONE_OF as THREE2ONE
    # The policy returns token IDs — need to decode
    # For now, use the trajectory's peptide_tokens attribute
    sequences = []
    for traj in trajs:
        # Decode tokens to amino acid letters
        # ESM3 uses standard amino acid token IDs
        tokens = traj.peptide_tokens
        if isinstance(tokens, list) and len(tokens) == pep_len:
            # Use the tokenizer to decode
            seq = policy.decode_tokens(tokens)
            if seq and len(seq) == pep_len:
                # Entropy filter (diversity gate)
                from collections import Counter
                import math
                freq = Counter(seq)
                h = -sum((c / pep_len) * math.log2(c / pep_len) for c in freq.values())
                if h >= 1.5:  # ESM3 cold-start diversity floor (tuned 2026-09-23)
                    sequences.append({
                        "sequence": seq,
                        "logprob": traj.mean_logprob,
                        "group": f"gen_denovo",
                    })

    # Sort by logprob (higher = more confident)
    sequences.sort(key=lambda s: -s.get("logprob", 0))

    result = {
        "sequences": sequences[:n_keep],
        "best_logprob": sequences[0]["logprob"] if sequences else None,
        "n_generated": len(trajs),
        "n_eligible": len(sequences),
    }
    Path(resp_path).write_text(json.dumps(result))


def _do_perplexity(policy, req, resp_path, receptor):
    """Compute mean negative log-likelihood of a peptide."""
    peptide = req.get("peptide_sequence", "")
    if not peptide:
        _write_error(resp_path, "empty peptide")
        return

    import torch
    import torch.nn.functional as F

    with torch.no_grad():
        # Encode context and peptide
        # Use the policy's scoring method
        score = policy.score_sequence(receptor, peptide)
        if score is not None:
            ppl = float(-score)  # negative logprob
        else:
            ppl = 10.0

    Path(resp_path).write_text(json.dumps({"perplexity": ppl}))


def _do_learn(policy, req, resp_path, receptor, device):
    """GRPO update from evaluation rollouts."""
    import torch
    from peplm.generate.masked_grpo import MaskedGRPOUpdater, Rollout

    rollouts_data = req.get("rollouts", [])
    pep_len = int(req.get("peptide_length", 14))

    if len(rollouts_data) < 2:
        Path(resp_path).write_text(json.dumps({"skipped": "insufficient rollouts"}))
        return

    # Group by proposal_group to compute within-group advantages
    groups = {}
    for rd in rollouts_data:
        g = rd.get("group", "default")
        groups.setdefault(g, []).append(rd)

    # Need at least one group with >=2 members
    valid_groups = {g: rows for g, rows in groups.items() if len(rows) >= 2}
    if not valid_groups:
        Path(resp_path).write_text(json.dumps({"skipped": "no valid GRPO groups"}))
        return

    # Build rollouts
    rollouts = []
    for g, rows in valid_groups.items():
        rewards = [r["reward"] for r in rows]
        mean_r = sum(rewards) / len(rewards)
        std_r = max(abs(max(rewards) - mean_r), abs(min(rewards) - mean_r), 1e-6)
        for r in rows:
            advantage = (r["reward"] - mean_r) / std_r
            # Encode as tokens
            tokens = policy.encode_peptide(r["sequence"], pep_len)
            if tokens is not None:
                rollouts.append(Rollout(
                    peptide_tokens=tokens,
                    advantage=advantage,
                    group=g,
                ))

    if len(rollouts) < 2:
        Path(resp_path).write_text(json.dumps({"skipped": "tokenization failed"}))
        return

    # Run GRPO update
    updater = MaskedGRPOUpdater(policy, lr=1e-5, kl_beta=0.04)
    stats = updater.update(rollouts)

    # Save updated adapter
    new_adapter_dir = req.get("new_adapter_dir", "/tmp/esm3_adapter_new")
    Path(new_adapter_dir).mkdir(parents=True, exist_ok=True)
    policy.save_adapter(Path(new_adapter_dir))

    result = {
        "adapter_dir": new_adapter_dir,
        "grpo_stats": {k: float(v) if isinstance(v, (int, float)) else v
                       for k, v in stats.items()},
        "n_rollouts": len(rollouts),
    }
    Path(resp_path).write_text(json.dumps(result))


def _write_error(resp_path, message):
    Path(resp_path).write_text(json.dumps({"error": message}))


if __name__ == "__main__":
    main()
