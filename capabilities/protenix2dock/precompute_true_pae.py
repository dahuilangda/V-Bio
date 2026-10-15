#!/usr/bin/env python3
"""Precompute TRUE pose-conditioned expected-PAE targets for affinity training.

Why: the feature cache stores only ligand row/col slices of the trunk pair
tensor, so training-time `expected_pae` had to be a z-probe (output linear
applied to raw z, skipping the 4 confidence blocks) — a different
distribution from production PAE (documented defect in train_affinity).
This script replays the frozen trunk for every STRUCTURED row of the index
CSV, runs the real confidence head on the crystal pose, and stores the
expected-PAE matrix as fp16 alongside the ligand-token index.

Output: <out>/<name>.npz with expected_pae [N, N] fp16 (Å), lig_tokens
[int64], n_token. Unstructured (BDB) rows are skipped — the pose-conditioned
PAE is undefined without coordinates; the aux-PAE loss masks those rows.

Usage (inside the protenix runtime container):
  python precompute_true_pae.py --index_csv ... --out ... \
      [--shard 0 --n_shards 1] [--limit N] [--max_seq_len 440]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

# make trainer internals + capability imports importable
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))            # train_affinity.py lives here
sys.path.insert(0, "/workspace/vbio/vendor/protenix-source")
sys.path.insert(0, "/workspace/vbio/capabilities/protenix2dock")

import torch  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index_csv", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--n_shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="rows per shard (0=all)")
    ap.add_argument("--max_seq_len", type=int, default=440)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--checkpoint_dir", default="/workspace/model")
    ap.add_argument("--model_name", default="protenix-v2")
    args = ap.parse_args()

    import train_affinity as T
    from runner.inference import InferenceRunner
    from core.runner import build_configs
    from protenix.data.inference.infer_dataloader import InferenceDataset
    from protenix.model.sample_confidence import get_bin_centers

    device = torch.device("cuda")
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- rows ---------------------------------------------------------------
    import csv
    with open(args.index_csv, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)]
    # structured rows only (pose-conditioned PAE needs coordinates)
    rows = [r for r in rows if r.get("protein_path") and r.get("ligand_path")]
    # token cap mirrors the training override
    nt_map_path = Path("/data/affinity_training/mix_feats/n_token_map.json")
    if nt_map_path.exists():
        true_tokens = json.loads(nt_map_path.read_text())
        rows = [r for r in rows
                if int(true_tokens.get(r.get("name", ""), 10**9)) <= args.max_seq_len]
    rows = [r for i, r in enumerate(rows) if i % args.n_shards == args.shard]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[precompute] shard {args.shard}/{args.n_shards}: {len(rows)} structured rows",
          flush=True)

    # --- trunk (same init as train_affinity.train) --------------------------
    work_dir = out_dir / f"_trunk_shard{args.shard}"
    work_dir.mkdir(parents=True, exist_ok=True)
    job = [{"name": "train", "sequences": [
        {"proteinChain": {"sequence": "MKVLAAALLASWQVQGTQASWQ", "count": 1}},
        {"ligand": {"ligand": "CCD_GOL", "count": 1}},
    ], "modelSeeds": [args.seed]}]
    (work_dir / "trunk_input.json").write_text(json.dumps(job))
    configs = build_configs(
        input_json_path=work_dir / "trunk_input.json",
        output_dir=work_dir / "trunk_out",
        model_name=args.model_name,
        checkpoint_dir=Path(args.checkpoint_dir),
        seeds=[42], n_step=1, n_sample=1, sigma_max=160.0, guidance_enable=False,
    )
    runner = InferenceRunner(configs)
    dataset = InferenceDataset(copy.deepcopy(configs))
    trunk = T._FrozenTrunk(runner.model, dataset)
    model = runner.model

    pcfg = model.configs.loss.pae
    centers = get_bin_centers(float(pcfg.min_bin), float(pcfg.max_bin),
                              int(pcfg.no_bins)).to(device)

    # tiny namespace for _sample_job (msa off)
    class _A:  # noqa: D401 - minimal arg surface
        msa_prob = 0
        msa_cache_dir = None
        msa_server_url = ""
        seed = args.seed
    sa = _A()

    done = failed = 0
    t0 = time.perf_counter()
    for i, row in enumerate(rows):
        name = row.get("name", "")
        dest = out_dir / f"{name}.npz"
        if dest.exists():
            done += 1
            continue
        try:
            job_s, chains, ligand_ref, structured = T._sample_job(row, sa, work_dir,
                                                                  use_msa=False)
            feats, s_inputs, s, z, _ed, _hpl = trunk.representations(job_s, device)
            coords = T._crystal_coords(job_s, chains, ligand_ref).to(device)
            with torch.no_grad():
                _pl, pae_logits, _pde, _r = model.run_confidence_head(
                    input_feature_dict=feats,
                    s_inputs=s_inputs,
                    s_trunk=s.float(),
                    z_trunk=z.float(),
                    pair_mask=None,
                    x_pred_coords=coords.unsqueeze(0).float(),
                    triangle_multiplicative=getattr(
                        model.configs, "triangle_multiplicative", "torch"),
                    triangle_attention=getattr(
                        model.configs, "triangle_attention", "torch"),
                    inplace_safe=True,
                    chunk_size=None,
                )
                # [N_tok, N_tok] expected PAE (Å) from the ipTM-best sample
                probs = torch.softmax(pae_logits[0].float(), dim=-1)
                exp_pae = (probs * centers).sum(-1)
            is_lig = feats["is_ligand"].to(torch.bool)
            lig_tokens = torch.unique(
                feats["atom_to_token_idx"].to(device).long()[is_lig.to(device)])
            import numpy as np
            np.savez_compressed(
                dest,
                expected_pae=exp_pae.cpu().numpy().astype(np.float16),
                lig_tokens=lig_tokens.cpu().numpy().astype(np.int64),
                n_token=int(exp_pae.shape[0]),
            )
            done += 1
            del pae_logits, probs, exp_pae, z, s
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 10:
                print(f"[skip] {name}: {exc}", flush=True)
        if (i + 1) % 25 == 0:
            rate = (i + 1) / (time.perf_counter() - t0)
            print(f"[precompute] {i+1}/{len(rows)} done={done} failed={failed} "
                  f"rate={rate:.2f}/s eta={(len(rows)-i-1)/max(rate,1e-9)/3600:.1f}h",
                  flush=True)
    print(f"[precompute] FINISHED done={done} failed={failed} "
          f"wall={(time.perf_counter()-t0)/3600:.2f}h", flush=True)


if __name__ == "__main__":
    main()
