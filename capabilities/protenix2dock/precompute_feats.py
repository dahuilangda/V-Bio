#!/usr/bin/env python3
"""Featurize index rows into the mix_feats cache format (reconstructed).

The original precompute_feats.py was lost in a git checkout; this rebuild
mirrors the exact npz schema the trainer's _cached_or_repr reads:
  s_inputs [N,449], s [N,384], z_lig_rows [n_lig,N,256], z_lig_cols
  [N,n_lig,256], lig_tokens [n_lig], n_token, coords [0,3] (empty for
  structure-free rows), expected_dist [N,N], atom_to_token_idx, is_ligand,
  h_pl — written as BOTH {name}_msa0.npz and {name}_msa1.npz (the trainer's
  cache filter requires both; msa-prob-0 training only reads _msa0).
Also appends to n_token_map.json in the out dir.

Sharded: --shard i --n_shards N. Skips rows whose msa0 file already exists.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, "/workspace/vbio/vendor/protenix-source")
sys.path.insert(0, "/workspace/vbio/capabilities/protenix2dock")

import numpy as np  # noqa: E402
import torch  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index_csv", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--n_shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max_seq_len", type=int, default=440)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--checkpoint_dir", default="/workspace/model")
    ap.add_argument("--model_name", default="protenix-v2")
    args = ap.parse_args()

    import train_affinity as T
    from runner.inference import InferenceRunner
    from core.runner import build_configs
    from protenix.data.inference.infer_dataloader import InferenceDataset

    device = torch.device("cuda")
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(args.index_csv, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)]
    rows = [r for i, r in enumerate(rows) if i % args.n_shards == args.shard]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[feats] shard {args.shard}/{args.n_shards}: {len(rows)} rows", flush=True)

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

    class _A:  # minimal arg surface for _sample_job (msa off)
        msa_prob = 0
        msa_cache_dir = None
        msa_server_url = ""
        seed = args.seed
    sa = _A()

    nt_map_path = out_dir / "n_token_map.json"
    nt_map = json.loads(nt_map_path.read_text()) if nt_map_path.exists() else {}

    done = failed = skipped = 0
    t0 = time.perf_counter()
    for i, row in enumerate(rows):
        name = row.get("name", "")
        dest = out_dir / f"{name}_msa0.npz"
        if dest.exists():
            skipped += 1
            continue
        try:
            job_s, chains, ligand_ref, structured = T._sample_job(row, sa, work_dir,
                                                                  use_msa=False)
            feats, s_inputs, s, z, expected_dist, h_pl = trunk.representations(job_s, device)
            is_lig = feats["is_ligand"].to(torch.bool)
            lig_tokens = torch.unique(
                feats["atom_to_token_idx"].to(device).long()[is_lig.to(device)])
            zc = z.float()
            zr = zc[lig_tokens].cpu().numpy()          # [n_lig, N, 256]
            zcl = zc[:, lig_tokens].cpu().numpy()      # [N, n_lig, 256]
            payload = dict(
                s_inputs=s_inputs.float().cpu().numpy().astype(np.float16),
                s=s.float().cpu().numpy().astype(np.float16),
                z_lig_rows=zr.astype(np.float16),
                z_lig_cols=zcl.astype(np.float16),
                lig_tokens=lig_tokens.cpu().numpy().astype(np.int64),
                n_token=int(z.shape[0]),
                coords=np.zeros((0, 3), dtype=np.float32),
                expected_dist=expected_dist.float().cpu().numpy().astype(np.float16),
                atom_to_token_idx=feats["atom_to_token_idx"].cpu().numpy().astype(np.int64),
                is_ligand=is_lig.cpu().numpy(),
                h_pl=float(h_pl) if h_pl is not None else float("nan"),
            )
            np.savez_compressed(dest, **payload)
            np.savez_compressed(out_dir / f"{name}_msa1.npz", **payload)
            nt_map[name] = int(z.shape[0])
            done += 1
            del z, zc, zr, zcl, expected_dist, s_inputs, s
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 10:
                print(f"[skip] {name}: {exc}", flush=True)
        if (i + 1) % 50 == 0:
            if done % 50 == 0:  # persist the map periodically
                nt_map_path.write_text(json.dumps(nt_map))
            rate = (i + 1) / (time.perf_counter() - t0)
            print(f"[feats] {i+1}/{len(rows)} done={done} skip={skipped} "
                  f"failed={failed} rate={rate:.2f}/s "
                  f"eta={(len(rows)-i-1)/max(rate,1e-9)/3600:.1f}h", flush=True)
    nt_map_path.write_text(json.dumps(nt_map))
    print(f"[feats] FINISHED done={done} skip={skipped} failed={failed} "
          f"wall={(time.perf_counter()-t0)/3600:.2f}h", flush=True)


if __name__ == "__main__":
    main()
